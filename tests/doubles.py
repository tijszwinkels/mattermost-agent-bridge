"""Shared test doubles for the bridge — importable across test modules.

Holds the fake Mattermost / agent-harness clients (moved out of
``test_bridge.py`` so any test module can reuse them) plus
``EventEchoingMattermostClient``, an active double that mirrors the live
Mattermost server's WS echo: every Channel Purpose write it receives is
queued as a ``channel_updated`` event and, when drained, delivered back into
the bridge's ``_on_mm_channel_updated`` handler — the same round-trip the
production WS loop performs. This lets restart / purpose-notice tests
exercise self-write dedup end-to-end instead of hand-injecting events.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mm_bridge.agent_harness_client import HarnessForkUnsupported
from mm_bridge.bridge import Bridge
from mm_bridge.config import Config


@dataclass
class _Post:
    channel_id: str
    message: str
    file_ids: list[str] | None = None
    root_id: str | None = None
    props: dict | None = None


@dataclass
class FakeMattermostClient:
    bot_user_id: str = "bot-user"
    bot_username: str = "claude"
    team_id: str = "team-1"
    channels: dict = field(default_factory=dict)
    posted: list[_Post] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[tuple[str, bool]] = field(default_factory=list)  # (post_id, permanent)
    renames: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    invited: list[tuple[str, str]] = field(default_factory=list)
    typing: list[tuple[str, str | None]] = field(default_factory=list)
    uploaded: list[tuple[str, str]] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    purposes: list[tuple[str, str]] = field(default_factory=list)
    users: dict = field(default_factory=dict)
    posts_by_channel: dict = field(default_factory=dict)
    posts_by_id: dict = field(default_factory=dict)
    files_by_id: dict = field(default_factory=dict)
    download_failures: set = field(default_factory=set)
    # Auto-join support:
    bot_channel_ids: set = field(default_factory=set)
    public_channels: list = field(default_factory=list)
    joined: list = field(default_factory=list)
    # First-message preamble support:
    channel_members: dict = field(default_factory=dict)
    # Permanent-delete failure simulation:
    permanent_delete_disabled: bool = False
    # Channel-create failure simulation (used by retry-semantics tests):
    fail_create_channel: bool = False
    # File ids passed to `download_file`, in call order. Lets a test assert
    # WHEN a download happened — held posts must download at FLUSH time, not
    # at hold time, so a superseded post never costs a transfer.
    downloaded: list = field(default_factory=list)
    # post_id → set of emoji names, mirroring the MM reactions API. Held
    # posts get an hourglass, delivered ones a check mark.
    reactions: dict = field(default_factory=dict)
    # Times `list_bot_channels` was called — `.fleet` must fan out over ONE
    # listing, never one call per row.
    list_bot_channels_calls: int = 0
    _post_counter: int = 0
    # Mirror of MattermostClient's own-post tracking — populated
    # whenever the fake creates or edits a post.
    own_post_ids: set = field(default_factory=set)

    def _next_post_id(self) -> str:
        self._post_counter += 1
        return f"p{self._post_counter}"

    def login(self) -> None:
        pass

    async def listen_websocket(self, handlers) -> None:
        # Not driven in unit tests.
        return

    def is_own_post(self, post_id: str) -> bool:
        return bool(post_id) and post_id in self.own_post_ids

    def post_message(self, channel_id: str, message: str) -> dict:
        pid = self._next_post_id()
        self.posted.append(_Post(channel_id, message))
        self.own_post_ids.add(pid)
        return {"id": pid}

    def post(self, channel_id: str, message: str, *, file_ids=None, root_id=None, props=None):
        pid = self._next_post_id()
        self.posted.append(_Post(channel_id, message, file_ids, root_id, props))
        self.own_post_ids.add(pid)
        return {"id": pid}

    def update_post(self, post_id: str, message: str) -> dict:
        self.edits.append((post_id, message))
        self.own_post_ids.add(post_id)
        return {"id": post_id}

    def delete_post(self, post_id: str, *, permanent: bool = False) -> None:
        if permanent and self.permanent_delete_disabled:
            raise RuntimeError(
                "Cannot delete post, ServiceSettings.EnableAPIPostDeletion is not enabled."
            )
        self.deletes.append((post_id, permanent))

    def create_channel(self, name: str, display_name: str, purpose: str = "") -> dict:
        if self.fail_create_channel:
            raise RuntimeError("simulated MM create_channel failure")
        cid = f"c-{name}"
        self.channels[cid] = {
            "id": cid, "name": name, "display_name": display_name, "purpose": purpose,
        }
        return {"id": cid}

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self.renames.append((channel_id, display_name))

    def set_channel_header(self, channel_id: str, header: str) -> None:
        self.headers.append((channel_id, header))
        self.channels.setdefault(channel_id, {"id": channel_id})["header"] = header

    def set_channel_purpose(self, channel_id: str, purpose: str) -> None:
        self.purposes.append((channel_id, purpose))
        self.channels.setdefault(channel_id, {"id": channel_id})["purpose"] = purpose

    def get_channel(self, channel_id: str) -> dict:
        return self.channels.get(channel_id, {"id": channel_id, "purpose": ""})

    def remove_self_from_channel(self, channel_id: str) -> None:
        self.removed.append(channel_id)

    def invite_user(self, channel_id: str, user_id: str) -> dict:
        self.invited.append((channel_id, user_id))
        return {"channel_id": channel_id, "user_id": user_id}

    def get_user(self, user_id: str) -> dict:
        return self.users.get(user_id, {"id": user_id, "username": f"u-{user_id[:4]}"})

    def publish_user_typing(self, channel_id: str, parent_id=None) -> None:
        self.typing.append((channel_id, parent_id))

    def upload_file(self, channel_id: str, path) -> str:
        fid = f"f-{len(self.uploaded)}"
        self.uploaded.append((channel_id, str(path)))
        return fid

    def download_file(self, file_id: str) -> bytes:
        self.downloaded.append(file_id)
        if file_id in self.download_failures:
            raise RuntimeError(f"simulated download failure for {file_id}")
        return self.files_by_id.get(file_id, b"")

    def get_file_info(self, file_id: str) -> dict:
        return self.files_by_id.get(file_id) or {"id": file_id, "name": file_id}

    def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.setdefault(post_id, set()).add(emoji_name)

    def remove_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.setdefault(post_id, set()).discard(emoji_name)

    def get_max_file_size(self) -> int:
        return 50 * 1024 * 1024

    def get_posts(self, channel_id: str, limit: int) -> list[dict]:
        return self.posts_by_channel.get(channel_id, [])[:limit]

    def get_post(self, post_id: str) -> dict:
        return self.posts_by_id.get(post_id, {"message": ""})

    def get_bot_channel_ids(self) -> set:
        return set(self.bot_channel_ids)

    def list_bot_channels(self) -> list:
        """Full channel records for the bot's memberships.

        The fleet view reads `header` + `display_name` from exactly this
        call, so the fake serves whole records out of `channels` (which
        `create_channel` / `set_channel_header` already populate).
        """
        self.list_bot_channels_calls += 1
        return [dict(c) for c in self.channels.values()]

    def list_public_team_channels(self) -> list:
        return list(self.public_channels)

    def join_channel(self, channel_id: str) -> None:
        self.joined.append(channel_id)
        self.bot_channel_ids.add(channel_id)

    def join_all_public_team_channels(self) -> list:
        newly = [
            ch["id"] for ch in self.public_channels
            if ch["id"] not in self.bot_channel_ids
        ]
        for cid in newly:
            self.join_channel(cid)
        return newly

    def get_channel_members(self, channel_id: str) -> list:
        return list(self.channel_members.get(channel_id, []))


@dataclass
class FakeAgentHarnessClient:
    created: list[dict] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    forks: list[tuple[str, str]] = field(default_factory=list)
    titles: list[tuple[str, str | None]] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
    next_session_id: str = "session-next"
    session_create_count: int = 0
    fork_response: dict | None = None
    sessions_meta: list[dict] = field(default_factory=list)
    # Run rows served by get_run / list_session_runs, mirroring the harness
    # GET runs endpoints. ``run_probe_error`` simulates a dead harness.
    runs_meta: dict = field(default_factory=dict)  # (session_id, run_id) → row
    session_runs_meta: dict = field(default_factory=dict)  # session_id → [rows]
    run_probe_error: Exception | None = None
    models_by_backend: dict = field(default_factory=lambda: {
        "claude": ["opus", "sonnet"],
        "codex": ["gpt-5.4"],
        "pi": ["pi-v1"],
        "opencode": [],
    })

    async def close(self) -> None:
        pass

    async def health(self) -> dict:
        return {"ok": True}

    async def create_session(
        self,
        *,
        backend,
        model=None,
        cwd,
        title=None,
    ) -> dict:
        self.created.append({
            "cwd": cwd, "backend": backend, "model": model, "title": title,
        })
        self.session_create_count += 1
        session_id = self.next_session_id
        if self.session_create_count > 1:
            session_id = f"{self.next_session_id}-{self.session_create_count}"
        return {
            "id": session_id,
            "backend": backend,
            "model": model,
            "project": {"path": cwd, "name": Path(cwd).name},
            "title": title,
            "origin": "harness",
        }

    async def create_run(self, session_id, message) -> dict:
        self.sent.append((session_id, message))
        return {"session_id": session_id, "run_id": f"run-{len(self.sent)}"}

    async def send_message(self, session_id, message) -> dict:
        return await self.create_run(session_id, message)

    async def fork_session(self, session_id, *, message=None, title=None) -> dict:
        message = message or ""
        self.forks.append((session_id, message))
        if self.fork_response is not None:
            if self.fork_response.get("status") == "fork_unavailable":
                raise HarnessForkUnsupported(self.fork_response.get("reason", "unsupported"))
            return self.fork_response
        return {
            "session": {"id": f"fork-{len(self.forks)}"},
            "run": {"id": f"fork-run-{len(self.forks)}"},
        }

    async def list_sessions(self) -> list[dict]:
        return self.sessions_meta

    async def get_session(self, session_id):
        for s in self.sessions_meta:
            if s.get("id") == session_id:
                if "project" not in s and s.get("projectPath"):
                    return {**s, "project": {"path": s.get("projectPath")}}
                return s
        return None

    async def get_session_meta(self, session_id):
        return await self.get_session(session_id) or {}

    async def get_run(self, session_id, run_id):
        if self.run_probe_error is not None:
            raise self.run_probe_error
        return self.runs_meta.get((session_id, run_id))

    async def list_session_runs(self, session_id):
        if self.run_probe_error is not None:
            raise self.run_probe_error
        return self.session_runs_meta.get(session_id, [])

    async def set_session_title(self, session_id, title) -> None:
        self.titles.append((session_id, title))

    async def interrupt_run(self, session_id, run_id) -> dict:
        self.interrupted.append((session_id, run_id))
        return {"status": "interrupted", "session_id": session_id, "run_id": run_id}

    async def interrupt_session(self, session_id) -> dict:
        await self.interrupt_run(session_id, "legacy-run")
        return {"status": "interrupted", "session_id": session_id}

    async def probe_current_sequence(self, **_kwargs) -> int:
        # High default keeps warm-restart tests free of a no-op reset.
        # Reset-detection tests override this attribute on the instance.
        return 10**9

    async def list_backend_models(self, backend) -> list[str]:
        return self.models_by_backend.get(backend, [])

    async def list_models(self, backend, *, force_refresh: bool = False) -> list[str]:
        return await self.list_backend_models(backend)

    async def stream_events(self, on_event) -> None:
        return


class EventEchoingMattermostClient(FakeMattermostClient):
    """FakeMattermostClient that echoes Channel Purpose writes back as WS
    ``channel_updated`` events (delivered explicitly via
    :meth:`deliver_ws_events`, keeping tests single-threaded and
    deterministic). Mirrors what the real Mattermost server does after the
    bridge PATCHes a channel's purpose.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        # (channel_id, purpose) writes not yet delivered as WS events.
        self._pending_purpose_events: list[tuple[str, str]] = []

    def set_channel_purpose(self, channel_id: str, purpose: str) -> None:
        super().set_channel_purpose(channel_id, purpose)
        self._pending_purpose_events.append((channel_id, purpose))

    async def deliver_ws_events(self, bridge: Bridge) -> None:
        """Flush every queued purpose write into the bridge as a
        ``channel_updated`` event, in write order — as the WS loop would."""
        events, self._pending_purpose_events = self._pending_purpose_events, []
        for cid, pur in events:
            ch = self.channels.get(cid, {})
            await bridge._on_mm_channel_updated({
                "id": cid,
                "display_name": ch.get("display_name", ""),
                "purpose": pur,
            })


def make_bridge(
    tmp_dir: str,
    *,
    echoing: bool = True,
    empty_catalog: bool = True,
    **config_overrides,
) -> Bridge:
    """Build a Bridge wired to the test doubles.

    ``echoing`` selects :class:`EventEchoingMattermostClient` (purpose writes
    echo back as WS events) vs a plain :class:`FakeMattermostClient`.
    ``empty_catalog`` mirrors the live claude-code backend (no enumerated
    models). Extra kwargs pass through to :class:`Config`.
    """
    cfg = Config(
        mm_bot_token="t",
        default_cwd="/tmp/proj",
        state_file=f"{tmp_dir}/state.json",
        sidecar_dir=f"{tmp_dir}/sidecar",
        default_backend="claude",
        **config_overrides,
    )
    b = Bridge(cfg)
    b.mm = EventEchoingMattermostClient() if echoing else FakeMattermostClient()
    b.harness = FakeAgentHarnessClient()
    if empty_catalog:
        b.harness.models_by_backend = {
            "claude": [], "codex": [], "pi": [], "opencode": [],
        }
    b.vd = b.harness
    from mm_bridge.typing_indicator import TypingIndicator
    b.typing = TypingIndicator(b.mm, refresh_s=0.01)
    return b
