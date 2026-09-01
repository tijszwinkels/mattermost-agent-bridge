"""Anchor-keyed buffers of Mattermost posts held while a run is in flight.

The bridge used to treat *arrival* as *submission*: every post that cleared
the mention gate became its own eagerly-created harness run, which the
harness then FIFO'd and fired as a full turn however stale it had become.
This module is the buffer that breaks that equation — posts wait here while
their session is busy and are delivered as ONE coalesced run when it frees up
(see ``Bridge._flush_held``).

Two properties earn this its own module:

* **Durability.** Today a post to a busy session sits in the harness queue,
  which survives a bridge restart. Holding it purely in memory would be a
  regression, so the buffer is mirrored to a JSON file, written atomically
  (temp + ``os.replace``) so a concurrent ``mm-bridge inbox`` can never read
  a torn file.
* **Purity.** The store owns only the buffer and the file. Rendering the
  flush body, downloading attachments and calling ``create_run`` need the
  Mattermost / harness clients and stay in the Bridge, so everything here is
  synchronous and unit-testable on its own.

Deliberately a SIBLING file rather than a section of the v5 state file:
``ChannelMapping.save()`` rewrites the whole state file, and the SSE cursor
calls it on a 2-second throttle for the life of every busy stream. Parking up
to ``cap`` post dicts per anchor in there would re-serialize the entire
backlog every 2 seconds for the daemon's lifetime, and would couple transient
queue state to the durable channel↔session topology.

Spec: specs/20260901-hold-and-coalesce/design.md §1 and §5.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Anchor

logger = logging.getLogger(__name__)

# On-disk schema version for the holds file. Bumped only on a breaking
# change; an unknown version loads as empty (see ``HeldPostStore.load``)
# rather than crashing the daemon on boot.
HELD_POSTS_SCHEMA_VERSION = 1

# Rendered when a post carries no usable ``create_at``. Chosen over silently
# omitting the stamp: the timestamp is the payload of a held post (it is how
# the agent knows how stale the message is), so its absence should be
# visible rather than invisible.
UNKNOWN_TIMESTAMP = "??:??"


@dataclass(frozen=True)
class HeldPost:
    """One Mattermost post held while its session's run was in flight.

    Carries the FULL post dict, not just an id. Re-fetching by id at flush
    time would cost a Mattermost round trip per post and would *fail outright
    on a deleted post*, losing the user's message — the exact durability
    regression this feature exists to avoid. ``username`` is resolved once,
    at hold time, so ``mm-bridge inbox`` can render the buffer with no
    Mattermost access and no bot token.
    """

    post: dict
    username: str
    held_at_ms: int

    @property
    def post_id(self) -> str:
        return str(self.post.get("id") or "")

    @property
    def user_id(self) -> str:
        return str(self.post.get("user_id") or "")

    @property
    def message(self) -> str:
        return (self.post.get("message") or "").strip()

    def timestamp(self) -> str:
        """``HH:MM`` in the daemon's LOCAL timezone.

        Local, not UTC: the operator reads these lines next to Mattermost's
        own rendering of the same posts, which is local, so a UTC stamp would
        read as simply wrong.
        """
        if not self.held_at_ms:
            return UNKNOWN_TIMESTAMP
        try:
            return datetime.fromtimestamp(self.held_at_ms / 1000).strftime("%H:%M")
        except (OverflowError, OSError, ValueError):
            logger.debug("Bad held_at_ms %r on post %s",
                         self.held_at_ms, self.post_id)
            return UNKNOWN_TIMESTAMP

    def render(self, notes: list[str] | None = None) -> str:
        """``HH:MM username: body`` — the line a flush contributes per post.

        Attribution is unconditional here, unlike the live forward path's
        ``PosterTracker`` logic: a held post's whole point is that it is
        *late*, and a bare body would hide both who said it and when.

        ``notes`` carries the ``[User attached file: …]`` lines produced when
        the flush downloads this post's attachments. Returns ``""`` when
        there is nothing to say at all (a post that was only a bot mention),
        so callers can skip it rather than emit a stamped empty line.
        """
        parts = [*(notes or [])]
        if self.message:
            parts.append(self.message)
        if not parts:
            return ""
        return f"{self.timestamp()} {self.username}: " + "\n".join(parts)

    def to_json(self) -> dict:
        return {
            "held_at_ms": self.held_at_ms,
            "username": self.username,
            "post": self.post,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "HeldPost | None":
        """Rebuild from a persisted envelope, or ``None`` if unusable.

        A post with no id can't be deduped, discarded or reacted to, so it is
        dropped rather than half-supported.
        """
        if not isinstance(raw, dict):
            return None
        post = raw.get("post")
        if not isinstance(post, dict) or not post.get("id"):
            return None
        return cls(
            post=post,
            username=str(raw.get("username") or ""),
            held_at_ms=int(raw.get("held_at_ms") or post.get("create_at") or 0),
        )


class HeldPostStore:
    """Per-anchor hold buffers, mirrored to ``path``.

    Keyed by :class:`Anchor` — the same ``(channel_id, root_id)`` key
    ``Bridge._silent_drops`` uses — so a thread fork holds independently of
    its parent channel. Every mutator persists synchronously: the hold
    decision must stay atomic with respect to the event loop (see design
    §4.2), so nothing here may be async.
    """

    def __init__(self, path: str | Path, *, cap: int) -> None:
        self._path = Path(path)
        self._cap = int(cap)
        self._by_anchor: dict[Anchor, list[HeldPost]] = {}
        self._session_by_anchor: dict[Anchor, str] = {}

    # ----- buffer -----

    def add(
        self, anchor: Anchor, held: HeldPost, *, session_id: str = "",
    ) -> bool:
        """Buffer ``held`` under ``anchor``.

        Returns ``False`` when the anchor is at capacity — the caller then
        falls back to an eager submit and logs loudly (R6); overflow is never
        a silent drop. A non-positive cap disables holding entirely, which is
        how an operator turns the buffer off without touching the
        ``coalesce_posts`` kill switch.

        A post id already in the buffer returns ``True`` without appending:
        the caller's contract is "True means you don't have to submit this",
        and a duplicate is already going to be delivered. The warming-queue
        replay path (``_flush_queued``) re-dispatches post dicts, so this is
        load-bearing, not merely defensive.
        """
        if self._cap <= 0:
            return False
        queue = self._by_anchor.get(anchor)
        if queue is None:
            queue = []
            self._by_anchor[anchor] = queue
        if any(h.post_id == held.post_id for h in queue):
            return True
        if len(queue) >= self._cap:
            if not queue:
                self._by_anchor.pop(anchor, None)
            return False
        queue.append(held)
        if session_id:
            self._session_by_anchor[anchor] = session_id
        self._save()
        return True

    def peek(self, anchor: Anchor) -> list[HeldPost]:
        """Snapshot of ``anchor``'s buffer, oldest first.

        A *peek*, not a pop: the flush only discards posts once
        ``create_run`` has actually succeeded, so that a failed delivery
        leaves the buffer intact for the next retry (design §2.8).
        """
        return list(self._by_anchor.get(anchor, ()))

    def discard(self, anchor: Anchor, ids: set[str]) -> None:
        """Remove exactly the posts in ``ids``.

        By id rather than "drop the anchor" because posts can arrive during
        a flush's awaits; those must survive to be delivered by the flush's
        next iteration instead of being swept away with the batch that was
        actually sent.
        """
        if not ids:
            return
        queue = self._by_anchor.get(anchor)
        if queue is None:
            return
        kept = [h for h in queue if h.post_id not in ids]
        if len(kept) == len(queue):
            return
        if kept:
            self._by_anchor[anchor] = kept
        else:
            self._forget(anchor)
        self._save()

    def clear(self, anchor: Anchor) -> list[HeldPost]:
        """Drop ``anchor``'s buffer, returning what was dropped.

        The return value is what lets ``.queue clear`` name the authors and
        timestamps it discarded — an explicit drop is still never a silent
        one.
        """
        dropped = self._by_anchor.get(anchor, [])
        if not dropped:
            return []
        self._forget(anchor)
        self._save()
        return dropped

    def forget_anchor(self, anchor: Anchor) -> None:
        """Drop one anchor's buffer without reporting it (teardown paths)."""
        if anchor in self._by_anchor or anchor in self._session_by_anchor:
            self._forget(anchor)
            self._save()

    def forget_channel(self, channel_id: str) -> None:
        """Drop every buffer for ``channel_id``, threads included.

        Mirrors ``Bridge._forget_channel_silent_drops`` so a torn-down
        channel can't leak a previous session's backlog into the next
        session mapped there.
        """
        stale = [a for a in self._all_anchors() if a.channel_id == channel_id]
        if not stale:
            return
        for anchor in stale:
            self._forget(anchor)
        self._save()

    def anchors(self) -> list[Anchor]:
        """Anchors that currently hold at least one post."""
        return [a for a, q in self._by_anchor.items() if q]

    def session_id(self, anchor: Anchor) -> str:
        """Session recorded when the anchor's posts were held.

        Diagnostics and the restart probe only — a flush always delivers to
        the anchor's CURRENT session, since a `.model` restart or an external
        session replacement may have swapped it underneath.
        """
        return self._session_by_anchor.get(anchor, "")

    def __len__(self) -> int:
        return sum(len(q) for q in self._by_anchor.values())

    # ----- persistence -----

    def load(self) -> None:
        """Replace in-memory state with the file's contents.

        Every failure mode — missing file, unreadable file, invalid JSON,
        unknown schema version, malformed entry — degrades to "that much is
        empty" and logs. A corrupt holds file must never stop the daemon from
        booting; the worst case is the loss this feature was built to avoid,
        and crashing would guarantee it rather than bound it.
        """
        self._by_anchor = {}
        self._session_by_anchor = {}
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            logger.warning(
                "Unreadable held-posts file %s — starting with an empty "
                "buffer", self._path, exc_info=True,
            )
            return
        if not isinstance(data, dict):
            logger.warning("Held-posts file %s is not an object", self._path)
            return
        version = data.get("version")
        if version != HELD_POSTS_SCHEMA_VERSION:
            logger.warning(
                "Held-posts file %s has schema version %r (expected %d) — "
                "ignoring it", self._path, version, HELD_POSTS_SCHEMA_VERSION,
            )
            return
        for entry in data.get("anchors") or []:
            if not isinstance(entry, dict):
                continue
            channel_id = entry.get("channel_id")
            if not channel_id:
                logger.warning("Skipping held-posts entry with no channel_id")
                continue
            anchor = Anchor(str(channel_id), entry.get("root_id") or None)
            posts = [
                h for h in (
                    HeldPost.from_json(raw) for raw in entry.get("posts") or []
                ) if h is not None
            ]
            if not posts:
                continue
            self._by_anchor[anchor] = posts
            if entry.get("session_id"):
                self._session_by_anchor[anchor] = str(entry["session_id"])
        if self._by_anchor:
            logger.info(
                "Rehydrated %d held post(s) across %d anchor(s) from %s",
                len(self), len(self._by_anchor), self._path,
            )

    # ----- internals -----

    def _all_anchors(self) -> set[Anchor]:
        return set(self._by_anchor) | set(self._session_by_anchor)

    def _forget(self, anchor: Anchor) -> None:
        self._by_anchor.pop(anchor, None)
        self._session_by_anchor.pop(anchor, None)

    def _save(self) -> None:
        """Atomically mirror the buffer to disk.

        Temp file in the same directory + ``os.replace``: the rename is
        atomic on POSIX for a same-filesystem move, so ``mm-bridge inbox``
        reads either the old file or the new one and never a half-written
        one. A write failure is logged, not raised — losing durability is bad,
        but taking the daemon's whole post-forwarding path down with it is
        worse.
        """
        payload = {
            "version": HELD_POSTS_SCHEMA_VERSION,
            "anchors": [
                {
                    "channel_id": anchor.channel_id,
                    "root_id": anchor.root_id,
                    "session_id": self._session_by_anchor.get(anchor, ""),
                    "posts": [h.to_json() for h in queue],
                }
                for anchor, queue in self._by_anchor.items()
                if queue
            ],
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2))
            os.replace(tmp, self._path)
        except OSError:
            logger.warning(
                "Failed to persist held posts to %s — the buffer is still "
                "live in memory but will not survive a restart",
                self._path, exc_info=True,
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
