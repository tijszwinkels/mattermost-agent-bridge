"""Command-line entry points for `mm-bridge`.

Subcommands:
  - `serve`                 → run the bridge daemon
  - `invite <username>`     → invite a MM user to this session's channel
  - `channel`               → print this session's MM channel_id (debug)
  - `spawn <prompt>`        → start a sub-session in a new sibling channel
  - `inbox`                 → print posts held for this session, not yet delivered

A bare `mm-bridge` prints help and exits with status 1 — this is intentional
so that a typo like `mm-bridge` (meaning to ask a question inside a channel)
doesn't accidentally spin up a second daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import json

from . import sidecar, spawn as spawn_mod
from .agent_harness_client import AgentHarnessClient
from .bridge import Bridge, resolve_attachment_path
from .codex_session import find_active_codex_rollout_uuid, iter_session_ids_by_cwd
from .config import Anchor, ChannelMapping, Config
from .held_posts import HeldPostStore
from .channel_ref import ChannelRef, ChannelRefError, parse_channel_ref
from .mm_client import MattermostClient

logger = logging.getLogger(__name__)


class NotInMattermostChannel(RuntimeError):
    """Raised when the current Claude session has no MM-bridge sidecar."""


class ChannelResolutionError(RuntimeError):
    """``--channel``/``--thread`` was well-formed but can't be resolved:
    unknown slug, unknown permalink post, or a URL for a different host.
    Reported as a usage error (exit 2), not a Mattermost failure (3)."""


class StdinError(RuntimeError):
    """Raised when a ``-`` free-text argument can't be read from stdin.

    Two cases: stdin is an interactive terminal (reading would hang
    forever), or the piped body is empty when the caller required content.
    """


# ─────────────────────── Helpers (unit-tested) ────────────────────────────


def _read_stdin_arg(
    value: str,
    *,
    label: str,
    allow_empty: bool = False,
    stdin=None,
) -> str:
    """Resolve a free-text positional that may be ``-`` (read from stdin).

    The shared backing for the ``-`` convention across subcommands (``post``
    message, ``spawn`` prompt), so the semantics stay identical everywhere:

    * A value other than ``-`` is returned verbatim — stdin is never touched.
    * ``-`` reads all of stdin and strips a trailing newline run
      (``rstrip("\\n")``), matching ``post``'s long-standing behaviour.

    Guards against two foot-guns, surfaced as :class:`StdinError` (the caller
    maps it to a clear message + exit code rather than hanging or dispatching
    an empty brief):

    * stdin is an interactive TTY → would block on ``read()`` forever.
    * the piped body is empty/whitespace and *allow_empty* is False.

    *label* names the argument in error messages (``"message"``, ``"prompt"``).
    *stdin* is injectable for tests; it defaults to :data:`sys.stdin`.
    """
    if value != "-":
        return value
    stream = stdin if stdin is not None else sys.stdin
    if stream.isatty():
        raise StdinError(
            f"{label} was '-' but stdin is a terminal — pipe the {label} in "
            f"(e.g. `... - <<'EOF'`) or pass it as an argument.",
        )
    body = stream.read().rstrip("\n")
    if not allow_empty and not body.strip():
        raise StdinError(f"{label} was '-' but stdin was empty.")
    return body


def _resolve_anchor_from_session(
    sidecar_dir: Path | str, session_id: str,
) -> Anchor:
    """Return the :class:`Anchor` for `session_id` by reading its sidecar.

    Channel-level sidecars yield ``Anchor(channel_id)``; thread-fork
    sidecars (two-line files) yield ``Anchor(channel_id, root_id)``.
    Raises :class:`NotInMattermostChannel` when the sidecar is missing
    or unreadable.
    """
    result = sidecar.read(Path(sidecar_dir), session_id)
    if result is None:
        path = Path(sidecar_dir) / session_id
        raise NotInMattermostChannel(
            f"not running inside a Mattermost channel (no sidecar at {path})",
        )
    channel_id, root_id = result
    return Anchor(channel_id, root_id)


def _invite_to_channel(mm, channel_id: str, username: str) -> None:
    """Resolve @username → user_id and invite them to `channel_id`.

    `mm` must expose `get_user_by_username(name) -> {id}` and
    `invite_user(channel_id, user_id)`. Raises on failure.
    """
    clean = username.lstrip("@").strip()
    if not clean:
        raise ValueError("username must not be empty")
    user = mm.get_user_by_username(clean)
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        raise RuntimeError(f"could not resolve user_id for @{clean}")
    mm.invite_user(channel_id, user_id)


def _current_session_id(sidecar_dir: Path | str | None = None) -> str:
    """Resolve the current session id, trying multiple sources in order.

    Resolver chain:
      1. ``MM_BRIDGE_SESSION_ID`` env var WITH a matching sidecar — the
         explicit pin from ``mm-bridge spawn``. When set and resolvable,
         this wins outright: it's the authoritative "this shell belongs
         to that bridge session" signal, and it overrides a leaked
         ``CLAUDE_SESSION_ID`` from the parent shell. The sidecar gate
         is what makes the override safe — an orphan env value with no
         sidecar on disk falls through to the next step.
      2. ``CLAUDE_SESSION_ID`` env var — set by the Claude Code
         SessionStart hook (see ``~/.claude/hooks/export-session-id.sh``).
         The legacy claude path: when ``MM_BRIDGE_SESSION_ID`` is unset
         (or has no sidecar), this is the source of truth. No sidecar
         gate here for backward compatibility — claude sessions
         routinely run before the daemon has written a sidecar and the
         caller surfaces the env value verbatim.
      3. ``MM_BRIDGE_SESSION_ID`` env var WITHOUT a matching sidecar —
         backend-agnostic fallback. Returned verbatim when no other
         source resolves but the env var carries something. Preserves
         the original "trust the env" behaviour for any consumer that
         pre-dates the sidecar-gating contract.
      4. Live-codex parent (``/proc`` tie-breaker). When env vars miss,
         walk the parent-pid chain looking for a live ``codex`` ancestor
         and read the rollout filename from its open fds. This is the
         in-turn case — first-turn tool shells, where the env wasn't
         pinned yet — and it's the case where multiple codex sessions
         share a cwd. Linux-only; macOS / no-``/proc`` falls through.
      5. Cwd-matched codex rollout files. Mtime-ordered walk of rollouts
         under ``~/.codex/sessions``; adopts the first candidate whose
         sidecar reads back. Catches the cases (4) misses: shells that
         outlive their codex parent (background tasks, stale shells)
         and macOS hosts where ``/proc`` doesn't exist.

    Why this order: an authoritative pin from ``mm-bridge spawn`` (step
    1) trumps everything — that's how we break the RC1 leak where a
    parent's ``CLAUDE_SESSION_ID`` would otherwise mis-resolve to the
    parent's channel inside the child. Steps (2)/(3) preserve the
    legacy semantics for non-spawn paths. Steps (4)/(5) only fire when
    the env vars don't resolve at all.

    The fallback steps require *sidecar_dir* — callers without a config
    handy get only the env-var paths.
    """
    mm_sid = os.environ.get("MM_BRIDGE_SESSION_ID", "").strip()
    claude_sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()

    # 1. Explicit pin: MM_BRIDGE_SESSION_ID + sidecar present.
    if mm_sid and sidecar_dir is not None:
        if sidecar.read(Path(sidecar_dir), mm_sid) is not None:
            return mm_sid

    # 2. Legacy claude path: CLAUDE_SESSION_ID surfaced verbatim.
    if claude_sid:
        return claude_sid

    # 3. MM_BRIDGE_SESSION_ID without a sidecar (or no sidecar_dir).
    if mm_sid:
        return mm_sid

    if sidecar_dir is not None:
        sdir = Path(sidecar_dir)
        # 4. PPid tie-breaker — live codex ancestor wins outright.
        active_uuid = find_active_codex_rollout_uuid()
        if active_uuid is not None and sidecar.read(sdir, active_uuid) is not None:
            return active_uuid
        # 5. Mtime walk — newest cwd-matched rollout with a sidecar.
        for candidate in iter_session_ids_by_cwd(os.getcwd()):
            if sidecar.read(sdir, candidate) is not None:
                return candidate
    raise NotInMattermostChannel(
        "could not determine current session id — checked "
        "MM_BRIDGE_SESSION_ID, CLAUDE_SESSION_ID, the codex parent's "
        "open rollout file, and cwd-matched codex rollout files. This "
        "command only works inside a Claude Code or codex session "
        "linked to a Mattermost channel.",
    )


def _make_mm_client(cfg: Config) -> MattermostClient:
    return MattermostClient(
        url=cfg.mm_url,
        port=cfg.mm_port,
        scheme=cfg.mm_scheme,
        token=cfg.mm_bot_token,
        team_name=cfg.mm_team,
    )


def _require_bot_token(cfg: Config) -> None:
    if not cfg.mm_bot_token:
        print(
            "Error: MM_BOT_TOKEN environment variable is required.",
            file=sys.stderr,
        )
        sys.exit(1)


def _snapshot_sidecar_names(sidecar_dir: Path | str) -> set[str]:
    """Current set of sidecar filenames (session_ids), or empty if missing."""
    d = Path(sidecar_dir)
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if p.is_file()}


def _wait_for_new_sidecar(
    sidecar_dir: Path | str,
    before: set[str],
    *,
    timeout: float = 30.0,
    interval: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """Poll `sidecar_dir` for a file whose name wasn't in `before`.

    Returns ``(session_id, channel_id)``. When multiple new sidecars appear
    concurrently (rare: two spawns racing), the one with the newest mtime
    is chosen — a best-effort tiebreaker. Raises ``TimeoutError`` if no new
    sidecar appears within ``timeout`` seconds.
    """
    deadline = clock() + timeout
    while True:
        current = _snapshot_sidecar_names(sidecar_dir)
        new = current - before
        if new:
            if len(new) == 1:
                sid = next(iter(new))
            else:
                sid = max(
                    new,
                    key=lambda n: (Path(sidecar_dir) / n).stat().st_mtime,
                )
            result = sidecar.read(Path(sidecar_dir), sid)
            if result is None:
                raise RuntimeError(f"new sidecar {sid} is empty or unreadable")
            channel_id, _root_id = result
            return sid, channel_id
        if clock() >= deadline:
            raise TimeoutError(
                f"no new sidecar appeared in {sidecar_dir} within {timeout}s "
                "— is the mm-bridge daemon running?",
            )
        sleep(interval)


# ─────────────────────── Subcommand handlers ──────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.load()
    _require_bot_token(cfg)
    bridge = Bridge(cfg)

    async def _run() -> None:
        try:
            await bridge.start()
        except KeyboardInterrupt:
            pass
        finally:
            await bridge.stop()

    asyncio.run(_run())
    return 0


def cmd_channel(args: argparse.Namespace) -> int:
    cfg = Config.load()
    try:
        session_id = _current_session_id(cfg.sidecar_dir)
        anchor = _resolve_anchor_from_session(cfg.sidecar_dir, session_id)
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(anchor.channel_id)
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    try:
        session_id = _current_session_id(cfg.sidecar_dir)
        channel_id = _resolve_anchor_from_session(
            cfg.sidecar_dir, session_id,
        ).channel_id
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        _invite_to_channel(mm, channel_id, args.username)
    except Exception as exc:
        print(
            f"Error: could not invite @{args.username.lstrip('@')} "
            f"to {channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3

    print(f"Invited @{args.username.lstrip('@')} to this channel.")
    return 0


async def _harness_create_session(
    harness_url: str,
    message: str,
    cwd: str,
    backend: str | None,
    model: str | None,
    title: str | None = None,
) -> dict:
    """Async wrapper: one-shot harness client for the spawn CLI path."""
    harness = AgentHarnessClient(harness_url)
    try:
        session = await harness.create_session(
            backend=backend or "claude",
            model=model,
            cwd=cwd,
            title=title,
        )
        session_id = session.get("id")
        if not session_id:
            raise RuntimeError("agent-harness create_session response missing id")
        run = await harness.create_run(session_id, message)
        return {
            "status": "started",
            "session_id": session_id,
            "run_id": run.get("run_id") or run.get("id"),
        }
    finally:
        await harness.close()


_PURPOSE_BADGE_CAP = 40


def _sanitise_badge(text: str) -> str:
    """Collapse tabs and newlines to single spaces for tab-separated output."""
    return " ".join(text.split())


def _format_channels_text(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        display = row["display_name"] or row["name"]
        badges: list[str] = []
        if row.get("session_id"):
            badges.append("[session]")
        purpose = _sanitise_badge(row.get("purpose") or "")
        if purpose:
            if len(purpose) > _PURPOSE_BADGE_CAP:
                purpose = purpose[: _PURPOSE_BADGE_CAP - 1] + "…"
            badges.append(f"[purpose: {purpose}]")
        lines.append("\t".join([row["id"], display, " ".join(badges)]))
    return "\n".join(lines)


def cmd_channels(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    mm = _make_mm_client(cfg)
    try:
        mm.login()
        channels = mm.list_bot_channels()
    except Exception as exc:
        print(f"Error: could not list channels: {exc}", file=sys.stderr)
        return 3

    channels = [c for c in channels if c.get("type") != "D"]

    if args.title:
        kw = args.title.lower()
        channels = [
            c for c in channels
            if kw in (
                (c.get("display_name") or "") + " " + (c.get("name") or "")
            ).lower()
        ]

    channels.sort(
        key=lambda c: (c.get("last_post_at") or 0, c.get("create_at") or 0),
        reverse=True,
    )

    try:
        mapping = ChannelMapping.load(
            cfg.state_file,
            sidecar_dir=cfg.sidecar_dir,
            reconcile_sidecars=False,
        )
        channel_to_session = {
            a.channel_id: sid
            for a, sid in mapping.anchor_to_session.items()
            if a.root_id is None
        }
    except Exception:
        logger.warning("Failed to load channel mapping", exc_info=True)
        channel_to_session = {}

    if args.n and args.n > 0:
        channels = channels[: args.n]

    rows = [
        {
            "id": c["id"],
            "name": c.get("name") or "",
            "display_name": c.get("display_name") or "",
            "last_post_at": c.get("last_post_at") or 0,
            "create_at": c.get("create_at") or 0,
            "purpose": c.get("purpose") or "",
            "header": c.get("header") or "",
            "session_id": channel_to_session.get(c["id"]),
        }
        for c in channels
    ]

    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        text = _format_channels_text(rows)
        if text:
            print(text)
    return 0


_MAX_POST_ATTACHMENTS = 10


def _configured_host(cfg: Config) -> str:
    """Hostname of the configured Mattermost server (MM_URL), lowercased.

    MM_URL is usually a bare host (``localhost``/``mm.example``); tolerate a
    scheme-prefixed value as well.
    """
    raw = (cfg.mm_url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https") and parts.hostname:
        return parts.hostname.lower()
    return urlsplit(f"//{raw}").hostname or raw.lower()


def _assert_same_host(ref: ChannelRef, cfg: Config) -> None:
    """Refuse to resolve a URL that points at a different Mattermost server:
    the token + team + channel live on cfg.mm_url, so a foreign-host URL
    can never resolve (and would just 404 with a misleading error)."""
    if ref.host is None:
        return  # bare id/slug — no host to compare
    expected = _configured_host(cfg)
    if ref.host != expected:
        raise ChannelResolutionError(
            f"channel URL host {ref.host!r} does not match the configured "
            f"Mattermost host {expected!r} (MM_URL={cfg.mm_url!r}); refusing "
            f"to resolve it."
        )


def _validate_channel_ref(cfg: Config, value: str | None) -> ChannelRef | None:
    """Pure (pre-login) validation of a ``--channel`` value.

    Parses it and rejects foreign-host URLs *before any network use*:
    the token and team live on cfg.mm_url, so a URL for another host can
    never resolve. Returns the parsed ref (or None for no value).
    """
    if not value:
        return None
    try:
        ref = parse_channel_ref(value)
    except ChannelRefError as exc:
        raise ChannelResolutionError(
            f"invalid --channel value: {exc}"
        ) from exc
    _assert_same_host(ref, cfg)
    return ref


def _validate_thread_ref(cfg: Config, value: str | None) -> ChannelRef | None:
    """Pure (pre-login) validation of a ``--thread`` value.

    Rejects before any network use: a channel URL (wrong kind of thing for
    a thread) and permalink URLs for a foreign host. Post ids and bare
    slugs are legal syntax either way and the server will reject genuinely
    bad ids at post time.

    Returns the parsed ref so the post-login resolver doesn't parse again
    (``None`` when there is no value, or when it is an opaque post id).
    """
    if not value:
        return None
    try:
        ref = parse_channel_ref(value)
    except ChannelRefError:
        return None  # not a URL → opaque post id, fine
    if ref.kind == "slug" and ref.host:
        # It parsed as a channel URL (…/<team>/channels/<slug>) — never a
        # thread reference.
        raise ChannelResolutionError(
            "--thread must be a root post id or a permalink "
            "(https://host/<team>/pl/<post_id>), not a channel URL "
            f"({value!r})."
        )
    if ref.kind == "permalink":
        _assert_same_host(ref, cfg)
    return ref


def _resolve_explicit_anchor(ref: ChannelRef, cfg: Config, mm: MattermostClient | None) -> Anchor:
    """Resolve a parsed ``--channel`` reference to an :class:`Anchor`.

    * ``id``        → used verbatim; the only kind resolvable without a
      client (no lookup needed).
    * ``slug``      → ``mm.get_channel_by_name(team, slug)`` against the
      team from the URL, or ``cfg.mm_team`` for a bare slug.
    * ``permalink`` → ``mm.get_post(post_id)``; the post's ``channel_id``
      becomes the channel and its thread root (``root_id``, or the post
      itself if it is a root) becomes the default root.

    Falling back to the raw reference string (slug, URL…) as a channel id
    would silently re-introduce the URL-as-id defect: every failure mode
    here raises instead.
    """
    if ref.kind == "id":
        return Anchor(ref.value, None)
    if mm is None:
        raise ChannelResolutionError(
            f"resolving a {ref.kind!r} --channel value ({ref.value!r}) "
            f"needs a logged-in Mattermost client; a 26-char channel id "
            f"works without one."
        )
    if ref.kind == "slug":
        team = ref.team or cfg.mm_team
        if not team:
            raise ChannelResolutionError(
                f"no team to resolve channel {ref.slug!r} in: pass a full "
                f"channel URL (https://host/<team>/channels/<slug>) or set "
                f"MM_TEAM."
            )
        try:
            channel = mm.get_channel_by_name(team, ref.slug)
        except Exception as exc:
            raise ChannelResolutionError(
                f"could not resolve channel {ref.slug!r} in team {team!r}: "
                f"{exc}. It may be private or archived, or the bot may not "
                f"be a member of it."
            ) from exc
        channel_id = (channel or {}).get("id")
        if not channel_id:
            raise ChannelResolutionError(
                f"server returned no channel id when resolving "
                f"{ref.slug!r} in team {team!r}."
            )
        return Anchor(channel_id, None)
    # permalink
    try:
        post = mm.get_post(ref.post_id)
    except Exception as exc:
        raise ChannelResolutionError(
            f"could not resolve post {ref.post_id!r} from permalink: {exc}."
        ) from exc
    channel_id = (post or {}).get("channel_id")
    if not channel_id:
        raise ChannelResolutionError(
            f"permalink post {ref.post_id!r} has no channel_id."
        )
    root_id = (post or {}).get("root_id") or (post or {}).get("id") or ref.post_id
    return Anchor(channel_id, root_id)


def _resolve_post_anchor(
    cfg: Config,
    channel_ref: ChannelRef | None,
    mm: MattermostClient | None,
) -> Anchor:
    """Channel + default root_id for a post/read.

    ``--channel`` may be a channel id, a bare slug, a channel URL, or a
    permalink URL (the last two need a logged-in client). Without
    ``--channel``, fall back to the current session's sidecar (which may
    carry a root_id for thread-forked sessions).
    """
    if channel_ref is not None:
        return _resolve_explicit_anchor(channel_ref, cfg, mm)
    return _resolve_anchor_from_session(
        cfg.sidecar_dir, _current_session_id(cfg.sidecar_dir),
    )


def _resolve_thread_root(
    cfg: Config,
    thread: str | None,
    thread_ref: ChannelRef | None,
    mm: MattermostClient | None,
) -> str | None:
    """Resolve an explicit ``--thread`` value to a root post id.

    A permalink URL resolves to the post's thread root (``root_id``, or the
    post itself if it is a root) via ``mm.get_post``. A channel URL is a
    usage error (a channel is not a thread). Every other value is passed
    through untouched as a post id: it is either a real id, or something
    the server will validate for us.
    """
    if not thread:
        return None
    if thread_ref is None or thread_ref.kind != "permalink":
        # Opaque post id (or a bare slug the server will reject). Channel
        # URLs were already refused pre-login by `_validate_thread_ref`.
        return thread
    anchor = _resolve_explicit_anchor(thread_ref, cfg, mm)
    return anchor.root_id or thread_ref.post_id


def _resolve_self_identity(cfg: Config) -> tuple[str, str] | None:
    """Return ``(harness_session_id, channel_id)`` of the current bridge session.

    Used to stamp ``props.from_bridge_cli_channel`` AND
    ``props.from_bridge_cli_session`` on CLI-authored posts. The bridge's
    incoming-post predicate compares ``from_bridge_cli_session`` against the
    session its anchor mapping stores, so the stamped id MUST be the canonical
    HARNESS id (``ses_<hex>``) — NOT the dashed ``CLAUDE_SESSION_ID`` UUID a
    claude sub-session resolves itself by. The dashed UUID is only a symlink
    alias, so stamping it verbatim made the bridge compare across namespaces
    and the self-post suppression never fired (the 2026-07-10 live-smoke miss).
    ``sidecar.canonical_id`` follows the alias to the ``ses_`` id.

    Returns ``None`` (caller omits the marker → bridge forwards, conservative)
    when the CLI is outside any bridge session, or the id can't be resolved to
    a harness id.
    """
    try:
        sid = _current_session_id(cfg.sidecar_dir)
        channel_id = _resolve_anchor_from_session(cfg.sidecar_dir, sid).channel_id
    except NotInMattermostChannel:
        return None
    harness_sid = sidecar.canonical_id(Path(cfg.sidecar_dir), sid)
    if harness_sid is None:
        return None
    return harness_sid, channel_id


def _resolve_effective_root(
    anchor: Anchor, thread: str | None, no_thread: bool,
) -> str | None:
    if no_thread:
        return None
    if thread:
        return thread
    return anchor.root_id


def _validate_attachments(
    paths: list[str], allowed_roots: list[str], max_bytes: int,
) -> tuple[list[Path] | None, int, str]:
    """Resolve + stat every --file. Returns (paths|None, exit_code, err_msg).

    A non-None paths list means all checks passed. On failure, ``paths``
    is None and the caller should exit with ``exit_code``.
    """
    if len(paths) > _MAX_POST_ATTACHMENTS:
        return (
            None, 2,
            f"Error: at most {_MAX_POST_ATTACHMENTS} --file attachments "
            f"allowed per post (got {len(paths)}).",
        )
    resolved: list[Path] = []
    for raw in paths:
        raw_path = Path(raw)
        candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        path = resolve_attachment_path(
            str(candidate), project_path=None, allowed_roots=allowed_roots,
        )
        if path is None:
            return (
                None, 2,
                f"Error: --file {raw!r} is outside allowed_attachment_roots.",
            )
        if not path.is_file():
            return (
                None, 3,
                f"Error: --file {raw!r} is not a readable file ({path}).",
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            return None, 3, f"Error: could not stat --file {raw!r}: {exc}"
        if size > max_bytes:
            return (
                None, 3,
                f"Error: --file {raw!r} is {size} bytes, exceeds server "
                f"max of {max_bytes} bytes.",
            )
        resolved.append(path)
    return resolved, 0, ""


def cmd_post(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    # ``allow_empty=True``: post keeps its own empty-body check below, which
    # also honours the empty-body-with-``--file`` case the helper can't see.
    try:
        body = _read_stdin_arg(args.message, label="message", allow_empty=True)
    except StdinError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not body.strip() and not args.file:
        print("Error: message body is empty.", file=sys.stderr)
        return 2

    # Validate --channel / --thread BEFORE any network use: malformed values
    # and URL refs pointing at a different Mattermost host than MM_URL exit
    # 2 without even logging in.
    try:
        channel_ref = _validate_channel_ref(cfg, args.channel)
        thread_ref = _validate_thread_ref(cfg, args.thread)
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    # Resolve --channel (id / bare slug / channel URL / permalink URL) and
    # --thread (post id / permalink URL) now that we are logged in: both
    # need the API for the slug/permalink forms. Usage problems here exit 2.
    try:
        anchor = _resolve_post_anchor(cfg, channel_ref, mm)
    except NotInMattermostChannel as exc:
        print(
            f"Error: not running inside a Mattermost channel and "
            f"no --channel given ({exc}).",
            file=sys.stderr,
        )
        return 2
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        thread_root = _resolve_thread_root(cfg, args.thread, thread_ref, mm)
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    root_id = _resolve_effective_root(anchor, thread_root, args.no_thread)

    try:
        max_bytes = mm.get_max_file_size()
    except Exception:
        logger.debug("get_max_file_size failed, using 50MB fallback", exc_info=True)
        max_bytes = 50 * 1024 * 1024

    resolved, err_code, err_msg = _validate_attachments(
        args.file, cfg.allowed_attachment_roots, max_bytes,
    )
    if resolved is None:
        print(err_msg, file=sys.stderr)
        return err_code

    file_ids: list[str] = []
    for path in resolved:
        try:
            file_ids.append(mm.upload_file(anchor.channel_id, path))
        except Exception as exc:
            print(
                f"Error: upload failed for {path}: {exc}", file=sys.stderr,
            )
            return 3

    # Marker only matters when the CLI is running inside a bridge session: the
    # daemon's per-process own-post tracker can't suppress the WS echo for posts
    # that the CLI authored, so we tag those posts with the SENDER's channel and
    # session ids AND the caller's INTENT (``from_bridge_cli_target``):
    #   * "self"     — the default post-into-my-own-channel path (no
    #                  ``--channel``/``--thread``). The daemon drops it iff it
    #                  loops back to its own author session — that's a status
    #                  update the session must not read as a user turn.
    #   * "explicit" — ``--channel``/``--thread`` was given. The daemon ALWAYS
    #                  forwards these, so agentcom can never be silently dropped
    #                  (even if the session resolver is poisoned). The one
    #                  accepted gap: an explicit ``--channel <your own channel>``
    #                  / ``--thread <your own root>`` forwards.
    # ``cross-post-mirror`` (below) is the only sender-side artifact the
    # recipient suppresses on channel-equality. From a non-session shell there
    # is no echo concern, so emit no marker at all.
    identity = _resolve_self_identity(cfg)
    post_props: dict | None = None
    if identity is not None:
        self_session_id, self_channel_id = identity
        target_kind = "explicit" if (args.channel or args.thread) else "self"
        post_props = {
            "from_bridge_cli": "post",
            "from_bridge_cli_target": target_kind,
            "from_bridge_cli_channel": self_channel_id,
            "from_bridge_cli_session": self_session_id,
        }
    try:
        post = mm.post(
            anchor.channel_id, body,
            file_ids=file_ids or None,
            root_id=root_id,
            props=post_props,
        )
    except Exception as exc:
        print(f"Error: post failed: {exc}", file=sys.stderr)
        return 3

    if args.channel:
        _maybe_mirror_cross_channel_post(
            mm, cfg, anchor.channel_id, body, file_ids,
        )

    print(post["id"])
    return 0


def _maybe_mirror_cross_channel_post(
    mm, cfg: Config, target_channel_id: str,
    body: str, file_ids: list[str],
) -> None:
    """Post a transcript-visibility mirror into the sender's own channel.

    Only fires when the caller is inside a bridge-backed session AND the
    explicit ``--channel`` differs from the session's own channel id. The
    mirror is informational only — files are NOT re-uploaded — and carries
    ``props.from_bridge_cli`` so the daemon dispatcher skips re-injecting
    it into the sender's session as a user turn.
    """
    try:
        sid = _current_session_id(cfg.sidecar_dir)
        self_anchor = _resolve_anchor_from_session(cfg.sidecar_dir, sid)
    except NotInMattermostChannel:
        return
    if self_anchor.channel_id == target_channel_id:
        return

    try:
        slug = mm.get_channel(target_channel_id).get("name") or target_channel_id
    except Exception:
        logger.debug(
            "get_channel(%s) failed; using bare id in mirror footer",
            target_channel_id, exc_info=True,
        )
        slug = target_channel_id

    parts = [f"→ also sent to ~{slug}~"]
    if file_ids:
        parts.append(f"with {len(file_ids)} attachment(s)")
    footer = "_" + " ".join(parts) + "_"
    mirror_body = f"{body}\n\n{footer}" if body else footer

    try:
        mm.post(
            self_anchor.channel_id, mirror_body,
            root_id=self_anchor.root_id,
            # The mirror lands in the sender's own channel by design —
            # it exists only for transcript visibility in the sender's
            # session view. Recording the same channel id makes the
            # daemon's predicate fire and drop it, instead of looping
            # it back as a user turn. The session id rides along for
            # telemetry symmetry with the explicit-agentcom marker.
            props={
                "from_bridge_cli": "cross-post-mirror",
                "from_bridge_cli_channel": self_anchor.channel_id,
                "from_bridge_cli_session": sid,
            },
        )
    except Exception:
        logger.warning(
            "cross-channel mirror post failed (target=%s, self=%s)",
            target_channel_id, self_anchor.channel_id, exc_info=True,
        )


_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_DURATION_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def _parse_since(raw: str, now_ms: int) -> int:
    """Return a ms-epoch for a ``--since`` argument.

    Accepted forms:

    * ``<int><m|h|d>`` — relative duration, subtracted from ``now_ms``.
    * All-digits → parsed as an ms-epoch verbatim.
    * ISO-8601 timestamp (``Z`` or offset suffix accepted).
    """
    match = _DURATION_RE.match(raw)
    if match:
        amount = int(match.group(1))
        unit_ms = _DURATION_UNIT_MS[match.group(2)]
        return now_ms - amount * unit_ms
    if raw.isdigit():
        return int(raw)
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            "--since must be an ISO-8601 timestamp, an ms-epoch, or a "
            "duration like '30m', '2h', '1d'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class _UserCache:
    """Per-invocation username cache; unknown users degrade gracefully."""

    def __init__(self, mm) -> None:
        self._mm = mm
        self._cache: dict[str, str] = {}

    def username(self, user_id: str) -> str:
        if user_id in self._cache:
            return self._cache[user_id]
        try:
            user = self._mm.get_user(user_id)
            name = user.get("username") or f"user:{user_id[:8]}"
        except Exception:
            name = f"user:{user_id[:8]}"
        self._cache[user_id] = name
        return name


def _human_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n // 1000} KB"
    return f"{n} B"


def _render_post_text(
    post: dict, username: str, file_infos: list[dict],
) -> str:
    ts_ms = int(post.get("create_at") or 0)
    ts = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    lines = [f"[{ts}] {username}"]
    body = (post.get("message") or "").rstrip()
    if body:
        lines.append(body)
    for info in file_infos:
        name = info.get("name") or info.get("id") or "file"
        size = info.get("size") or 0
        lines.append(f"📎 {name} ({_human_size(size)})")
    return "\n".join(lines)


def _project_post_json(
    post: dict, username: str, is_bot: bool, file_infos: list[dict],
) -> dict:
    return {
        "id": post.get("id"),
        "create_at": post.get("create_at"),
        "user_id": post.get("user_id"),
        "username": username,
        "is_bot": is_bot,
        "root_id": post.get("root_id") or "",
        "message": post.get("message") or "",
        "files": [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "size": f.get("size"),
                "mime_type": f.get("mime_type"),
            }
            for f in file_infos
        ],
    }


def _collect_file_infos(mm, post: dict) -> list[dict]:
    infos: list[dict] = []
    for fid in post.get("file_ids") or []:
        try:
            infos.append(mm.get_file_info(fid))
        except Exception:
            logger.debug("get_file_info failed for %s", fid, exc_info=True)
            infos.append({"id": fid, "name": fid, "size": 0})
    return infos


def cmd_read(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    since_ms: int | None = None
    if args.since:
        try:
            since_ms = _parse_since(args.since, int(time.time() * 1000))
        except ValueError as exc:
            print(f"Error: --since: {exc}", file=sys.stderr)
            return 2

    if args.n == 0:
        limit = cfg.catch_up_max_n
        uncapped = True
    else:
        limit = min(max(args.n, 1), cfg.catch_up_max_n)
        uncapped = False

    # Validate --channel / --thread BEFORE any network use: malformed values
    # and URL refs pointing at a different Mattermost host than MM_URL exit
    # 2 without even logging in.
    try:
        channel_ref = _validate_channel_ref(cfg, args.channel)
        thread_ref = _validate_thread_ref(cfg, args.thread)
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    # Resolve --channel (id / bare slug / channel URL / permalink URL) and
    # --thread (post id / permalink URL) now that we are logged in: both
    # need the API for the slug/permalink forms. Usage problems exit 2.
    try:
        anchor = _resolve_post_anchor(cfg, channel_ref, mm)
    except NotInMattermostChannel as exc:
        print(
            f"Error: not running inside a Mattermost channel and "
            f"no --channel given ({exc}).",
            file=sys.stderr,
        )
        return 2
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        thread_root = _resolve_thread_root(cfg, args.thread, thread_ref, mm)
    except ChannelResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    root_id = _resolve_effective_root(anchor, thread_root, args.no_thread)

    try:
        if root_id:
            posts = mm.get_thread_posts(root_id)
        elif since_ms is not None:
            posts = mm.get_posts_since(anchor.channel_id, since_ms)
        else:
            posts = mm.get_posts(anchor.channel_id, limit)
    except Exception as exc:
        print(f"Error: could not fetch posts: {exc}", file=sys.stderr)
        return 3

    if since_ms is not None:
        posts = [p for p in posts if (p.get("create_at") or 0) >= since_ms]

    posts = [p for p in posts if not (p.get("type") or "")]
    if args.no_bot:
        bot_id = getattr(mm, "bot_user_id", "")
        posts = [p for p in posts if p.get("user_id") != bot_id]

    posts.sort(key=lambda p: p.get("create_at") or 0)

    if not uncapped and len(posts) > limit:
        posts = posts[-limit:]

    users = _UserCache(mm)
    bot_id = getattr(mm, "bot_user_id", "")

    if args.format == "text":
        blocks: list[str] = []
        for p in posts:
            uname = users.username(p.get("user_id") or "")
            infos = _collect_file_infos(mm, p)
            blocks.append(_render_post_text(p, uname, infos))
        if blocks:
            print("\n\n".join(blocks))
        return 0

    projected = []
    for p in posts:
        uname = users.username(p.get("user_id") or "")
        infos = _collect_file_infos(mm, p)
        projected.append(
            _project_post_json(p, uname, p.get("user_id") == bot_id, infos),
        )

    if args.format == "jsonl":
        for obj in projected:
            print(json.dumps(obj))
    else:
        print(json.dumps(projected, indent=2))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    """Print the posts held for this session but not yet delivered to it.

    The agent-facing half of hold-and-coalesce: a session that is about to
    freeze a conclusion can ask whether anything arrived while it was
    working, instead of finding out after the fact.

    Read-only and best-effort **by contract**. The daemon owns the holds
    file and may append between our read and our print, so the output is a
    snapshot rather than a lock. Its writes are atomic (temp file +
    ``os.replace``), so we can never read a half-written file — the worst
    case is a snapshot that is one post stale. Every failure mode (missing
    file, corrupt JSON, unknown schema) renders as ``(empty)``: an agent
    checking its inbox must never be the thing that breaks.

    With no ``--channel`` this touches Mattermost not at all — the username
    is stored in each envelope at hold time precisely so this path needs no
    bot token.
    """
    cfg = Config.load()

    if args.channel:
        try:
            ref = _validate_channel_ref(cfg, args.channel)
        except ChannelResolutionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if ref is not None and ref.kind != "id":
            # A slug or URL has to be resolved server-side, which is the one
            # case where this command needs credentials.
            _require_bot_token(cfg)
            mm = _make_mm_client(cfg)
            mm.login()
            try:
                anchor = _resolve_explicit_anchor(ref, cfg, mm)
            except ChannelResolutionError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
        else:
            anchor = Anchor(args.channel)
    else:
        try:
            session_id = _current_session_id(cfg.sidecar_dir)
            anchor = _resolve_anchor_from_session(cfg.sidecar_dir, session_id)
        except NotInMattermostChannel as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    store = HeldPostStore(cfg.resolved_held_posts_file(), cap=1)
    store.load()
    held = store.peek(anchor)

    if args.json:
        print(json.dumps([h.to_json() for h in held], indent=2))
        return 0

    if not held:
        print("(empty)")
        return 0

    where = anchor.channel_id + (f" (thread {anchor.root_id})" if anchor.root_id else "")
    print(f"{len(held)} post(s) held for {where}, not yet delivered:")
    for h in held:
        print(f"  {h.render()}")
    return 0


def cmd_spawn(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _require_bot_token(cfg)

    # Resolve ``-`` → stdin FIRST: an empty/TTY brief must fail before any
    # MM login or harness call, so we never dispatch a sub-session with a
    # bogus prompt (the 2026-07-14 literal ``"-"`` foot-gun).
    try:
        prompt = _read_stdin_arg(args.prompt, label="prompt")
    except StdinError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        parent_session_id = _current_session_id(cfg.sidecar_dir)
        parent_anchor = _resolve_anchor_from_session(
            cfg.sidecar_dir, parent_session_id,
        )
    except NotInMattermostChannel as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    parent_channel_id = parent_anchor.channel_id

    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:
        print(f"Error: could not log into Mattermost: {exc}", file=sys.stderr)
        return 3

    try:
        parent_channel = mm.get_channel(parent_channel_id)
    except Exception as exc:
        print(
            f"Error: could not fetch parent channel {parent_channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3
    parent_name = parent_channel.get("name") or parent_channel_id

    cwd = args.cwd or os.getcwd()
    backend = args.backend or cfg.default_backend
    # Explicit --model wins; otherwise the per-backend default so
    # ``--backend codex`` doesn't inherit claude's ``opus`` (that
    # combination makes ``codex exec`` exit 1 immediately).
    model = args.model or cfg.default_model_for(backend)

    before = _snapshot_sidecar_names(cfg.sidecar_dir)

    try:
        resp = asyncio.run(
            _harness_create_session(
                cfg.agent_harness_url,
                prompt,
                cwd,
                backend,
                model,
                title=args.title,
            ),
        )
    except Exception as exc:
        print(f"Error: agent-harness create_session failed: {exc}", file=sys.stderr)
        return 3
    status = resp.get("status")
    if status != "started":
        print(
            f"Error: agent-harness returned unexpected status {status!r}",
            file=sys.stderr,
        )
        return 3

    try:
        session_id, new_channel_id = _wait_for_new_sidecar(
            cfg.sidecar_dir, before,
        )
    except TimeoutError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3

    # Compute the env-overlay the spawned child SHOULD inherit. The
    # contract (see ``spawn.build_spawn_child_env``) is to pin
    # ``MM_BRIDGE_SESSION_ID`` to the new session id and unset the
    # parent's ``CLAUDE_SESSION_ID`` — that's how RC1 is closed at the
    # child end. Today the agent-harness ``create_session`` API does
    # not accept an env override (``extra="forbid"`` on its request
    # model), so this is logged at debug for operator visibility and
    # forms the hook point for when the harness adds env-pinning
    # support. Until then, the bridge's resolver-order fix
    # (``MM_BRIDGE_SESSION_ID`` with sidecar beats ``CLAUDE_SESSION_ID``)
    # provides defence in depth for any backend that DOES inherit the
    # right ``MM_BRIDGE_SESSION_ID`` via other means.
    child_env_overlay = spawn_mod.build_spawn_child_env(
        os.environ, new_session_id=session_id, backend=backend,
    )
    logger.debug(
        "spawn: child env overlay for session %s: %s",
        session_id, child_env_overlay,
    )

    try:
        new_channel = mm.get_channel(new_channel_id)
    except Exception as exc:
        print(
            f"Error: could not fetch new channel {new_channel_id}: {exc}",
            file=sys.stderr,
        )
        return 3
    new_channel_name = new_channel.get("name") or new_channel_id

    display_name = spawn_mod.derive_display_name(
        args.title, new_channel.get("display_name", "") or new_channel_name,
    )
    if args.title:
        try:
            mm.rename_channel(new_channel_id, display_name)
        except Exception:
            logger.warning(
                "Failed to set display_name on %s", new_channel_id, exc_info=True,
            )

    thread_permalink: str | None = None
    if parent_anchor.is_thread:
        base_url = cfg.mm_public_url or spawn_mod.build_mm_base_url(
            cfg.mm_scheme, cfg.mm_url, cfg.mm_port,
        )
        thread_permalink = spawn_mod.format_post_permalink(
            base_url, cfg.mm_team, parent_anchor.root_id or "",
        )
    try:
        mm.set_channel_header(
            new_channel_id,
            spawn_mod.format_parent_header(
                parent_name, thread_permalink=thread_permalink,
            ),
        )
    except Exception:
        logger.warning(
            "Failed to set parent header on %s", new_channel_id, exc_info=True,
        )

    if not args.no_forward_prompt:
        try:
            mm.post(
                new_channel_id,
                spawn_mod.format_spawn_kickoff(parent_name, prompt),
                # VD already received `prompt` via `create_session`
                # and delivers it as the new session's first user turn.
                # The kickoff post is a visual record for the channel,
                # not a duplicate user input — without this marker the
                # daemon would forward it into the new session as a
                # second copy of the prompt, queued behind the agent's
                # in-flight first turn. The recorded channel is the new
                # channel itself so the daemon's channel-scoped
                # predicate drops it on the new session.
                props={
                    "from_bridge_cli": "spawn-kickoff",
                    "from_bridge_cli_channel": new_channel_id,
                },
            )
        except Exception:
            logger.warning(
                "Failed to post kickoff message to new channel",
                exc_info=True,
            )

    if args.invite:
        try:
            _invite_to_channel(mm, new_channel_id, args.invite)
        except Exception as exc:
            print(
                f"Warning: could not invite @{args.invite.lstrip('@')}: {exc}",
                file=sys.stderr,
            )

    if not args.no_forward_prompt:
        try:
            mm.post(
                parent_channel_id,
                spawn_mod.format_spawn_announcement(
                    display_name, new_channel_name, prompt,
                ),
                root_id=parent_anchor.root_id,
                # The daemon's per-process own-post tracker only sees
                # IDs created by *its own* MattermostClient, so a CLI-
                # authored post would otherwise be forwarded to the
                # parent session as a user turn. The recorded channel
                # is the parent channel — the dispatcher's predicate
                # drops it on the parent session via own-channel echo.
                props={
                    "from_bridge_cli": "spawn-announcement",
                    "from_bridge_cli_channel": parent_channel_id,
                },
            )
        except Exception:
            logger.warning(
                "Failed to post spawn announcement to parent channel",
                exc_info=True,
            )

    print(
        f"Spawned session {session_id[:12]} in ~{new_channel_name}~ "
        f"({new_channel_id})",
    )
    return 0


# ─────────────────────────── doctor ───────────────────────────────────────
#
# `mm-bridge doctor` is the runbook's verification primitive: one ✓/✗ line per
# check, nonzero exit if any fail. It diagnoses only — it never mutates config,
# creates directories, or otherwise "fixes" anything, so it's safe to run at
# every install checkpoint.


@dataclass
class _CheckResult:
    """One diagnostic line: a labelled ✓/✗ with a human-readable detail."""

    label: str
    ok: bool
    detail: str


def _doctor_check_config(cfg: Config) -> _CheckResult:
    """Required config keys are present (token, MM url/team, harness url)."""
    required = {
        "MM_BOT_TOKEN": cfg.mm_bot_token,
        "mm_url": cfg.mm_url,
        "mm_team": cfg.mm_team,
        "agent_harness_url": cfg.agent_harness_url,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return _CheckResult(
            "config", False,
            f"missing required keys: {', '.join(missing)} "
            f"(config file: {cfg.config_file})",
        )
    return _CheckResult(
        "config", True, f"loaded {cfg.config_file}; required keys present",
    )


def _doctor_check_mattermost(cfg: Config) -> _CheckResult:
    """Mattermost reachable and MM_BOT_TOKEN valid; resolves the bot username."""
    if not cfg.mm_bot_token:
        return _CheckResult(
            "mattermost", False, "MM_BOT_TOKEN not set — cannot authenticate",
        )
    mm = _make_mm_client(cfg)
    try:
        mm.login()
    except Exception as exc:  # noqa: BLE001 — surface any auth/connection error
        return _CheckResult(
            "mattermost", False,
            f"login failed at {cfg.mm_scheme}://{cfg.mm_url}:{cfg.mm_port} "
            f"({exc})",
        )
    return _CheckResult(
        "mattermost", True,
        f"authenticated as @{mm.bot_username} (team {cfg.mm_team})",
    )


async def _harness_health(url: str) -> None:
    """Hit the harness ``/v1/health`` endpoint, closing the client afterwards."""
    client = AgentHarnessClient(url)
    try:
        await client.health()
    finally:
        await client.close()


def _doctor_check_harness(cfg: Config) -> _CheckResult:
    """agent-harness base URL reachable (``/v1/health``)."""
    try:
        asyncio.run(_harness_health(cfg.agent_harness_url))
    except Exception as exc:  # noqa: BLE001 — any transport error means "down"
        return _CheckResult(
            "agent-harness", False,
            f"unreachable at {cfg.agent_harness_url} ({exc})",
        )
    return _CheckResult(
        "agent-harness", True, f"reachable at {cfg.agent_harness_url}",
    )


def _doctor_check_sidecar(cfg: Config) -> _CheckResult:
    """Sidecar directory is writable (or its parent is, so it can be created).

    Diagnose only: never creates the directory. Walks up to the nearest
    existing ancestor and tests write+traverse permission there.
    """
    path = Path(os.path.expanduser(cfg.sidecar_dir))
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return _CheckResult(
            "sidecar-dir", False,
            f"{path} not writable (no write permission on {probe})",
        )
    state = "exists" if path.exists() else "will be created on first session"
    return _CheckResult("sidecar-dir", True, f"{path} writable ({state})")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the local install: config, Mattermost, harness, sidecar dir.

    Prints one ✓/✗ line per check and returns nonzero if any check fails.
    Diagnose only — never mutates config or creates directories.
    """
    print("mm-bridge doctor — checking the local install\n")
    try:
        cfg = Config.load()
    except Exception as exc:  # noqa: BLE001 — a broken config file is a finding
        print(f"✗ config: failed to load ({exc})")
        sys.stdout.flush()  # keep stdout/stderr ordered when output is captured
        print("\n1 check failed.", file=sys.stderr)
        return 1

    checks = [
        _doctor_check_config(cfg),
        _doctor_check_mattermost(cfg),
        _doctor_check_harness(cfg),
        _doctor_check_sidecar(cfg),
    ]
    for check in checks:
        print(f"{'✓' if check.ok else '✗'} {check.label}: {check.detail}")

    failed = [c for c in checks if not c.ok]
    if failed:
        sys.stdout.flush()  # keep stdout/stderr ordered when output is captured
        print(
            f"\n{len(failed)} of {len(checks)} checks failed.", file=sys.stderr,
        )
        return 1
    print(f"\nAll {len(checks)} checks passed.")
    return 0


# ─────────────────────── argparse dispatch ────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mm-bridge",
        description="Mattermost ↔ agent-harness bridge.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_serve = sub.add_parser("serve", help="Run the bridge daemon.")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose the local install (config, Mattermost, harness, "
             "sidecar dir).",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_invite = sub.add_parser(
        "invite",
        help="Invite a Mattermost user to this session's channel.",
    )
    p_invite.add_argument(
        "username", help="Mattermost username (with or without leading @)",
    )
    p_invite.set_defaults(func=cmd_invite)

    p_channel = sub.add_parser(
        "channel",
        help="Print the Mattermost channel_id for the current session.",
    )
    p_channel.set_defaults(func=cmd_channel)

    p_channels = sub.add_parser(
        "channels",
        help="List Mattermost channels the bot can see.",
    )
    p_channels.add_argument(
        "--title",
        help="Case-insensitive substring filter on display_name or name.",
    )
    p_channels.add_argument(
        "-n", type=int, default=20,
        help="Max rows to display (0 = unlimited). Default 20.",
    )
    p_channels.add_argument(
        "--format", choices=["text", "json"], default="text",
    )
    p_channels.set_defaults(func=cmd_channels)

    p_post = sub.add_parser(
        "post", help="Post a message to a Mattermost channel.",
    )
    p_post.add_argument(
        "--channel",
        help=(
            "Channel id, bare slug, channel URL "
            "(…/<team>/channels/<slug>), or permalink URL (…/<team>/pl/<post>) — "
            "the last also posts into that post's thread. Defaults to the "
            "current session's channel."
        ),
    )
    thread_group = p_post.add_mutually_exclusive_group()
    thread_group.add_argument(
        "--thread", metavar="ROOT_POST_ID",
        help=(
            "Post as a reply inside this thread. A root post id or a "
            "permalink URL (…/<team>/pl/<post>) whose post's thread root is used."
        ),
    )
    thread_group.add_argument(
        "--no-thread", action="store_true",
        help="Post at channel level even if the session is thread-forked.",
    )
    p_post.add_argument(
        "--file", action="append", default=[], metavar="PATH",
        help="Attachment path (repeatable, max 10).",
    )
    p_post.add_argument(
        "message", help="Message body, or '-' to read from stdin.",
    )
    p_post.set_defaults(func=cmd_post)

    p_read = sub.add_parser(
        "read", help="Print recent posts from a Mattermost channel.",
    )
    p_read.add_argument(
        "--channel",
        help=(
            "Channel id, bare slug (resolved against the configured team), "
            "channel URL (…/<team>/channels/<slug>), or permalink URL "
            "(…/<team>/pl/<post>) — the last also reads that post's thread "
            "unless --no-thread is given. Defaults to the current session's channel."
        ),
    )
    read_thread_group = p_read.add_mutually_exclusive_group()
    read_thread_group.add_argument(
        "--thread", metavar="ROOT_POST_ID",
        help=(
            "Read only posts inside this thread. A root post id or a "
            "permalink URL (…/<team>/pl/<post>) whose post's thread root is used."
        ),
    )
    read_thread_group.add_argument(
        "--no-thread", action="store_true",
        help="Read channel-level even if the session is thread-forked.",
    )
    p_read.add_argument(
        "-n", type=int, default=50,
        help="Max posts (0 = unlimited within --since). Default 50, cap 500.",
    )
    p_read.add_argument(
        "--since",
        help="ISO-8601 timestamp, ms-epoch, or duration like '1h', '2d'.",
    )
    p_read.add_argument(
        "--format", choices=["text", "json", "jsonl"], default="text",
    )
    p_read.add_argument(
        "--no-bot", action="store_true", help="Exclude bot posts.",
    )
    p_read.set_defaults(func=cmd_read)

    p_inbox = sub.add_parser(
        "inbox",
        help="Print posts held for this session but not yet delivered.",
        description=(
            "Posts that arrived while this session's agent was working are "
            "held by the bridge and delivered as one turn when the run ends. "
            "This prints what is currently waiting. Best-effort: the daemon "
            "may append between the read and the print, so treat the output "
            "as a snapshot."
        ),
    )
    p_inbox.add_argument(
        "--channel",
        help=(
            "Channel id, bare slug, channel URL or permalink URL. Defaults "
            "to the current session's own anchor (channel or thread fork), "
            "which needs no bot token."
        ),
    )
    p_inbox.add_argument(
        "--json", action="store_true",
        help="Emit the raw held-post envelopes as JSON.",
    )
    p_inbox.set_defaults(func=cmd_inbox)

    p_spawn = sub.add_parser(
        "spawn",
        help="Start an agent-harness sub-session in a new sibling MM channel.",
    )
    p_spawn.add_argument(
        "prompt",
        help=(
            "Initial prompt for the sub-session, or '-' to read it from "
            "stdin (e.g. a heredoc: `mm-bridge spawn - <<'EOF'`)."
        ),
    )
    p_spawn.add_argument(
        "--cwd",
        help="Working directory for the sub-session (default: current dir).",
    )
    p_spawn.add_argument(
        "--backend",
        help="Backend name (e.g. 'claude', 'codex'). Default: config default.",
    )
    p_spawn.add_argument(
        "--model",
        help=(
            "Model for the sub-session (e.g. 'claude-fable-5'). "
            "Default: per-backend config default."
        ),
    )
    p_spawn.add_argument(
        "--title",
        help="Display name for the new channel (default: daemon-derived).",
    )
    p_spawn.add_argument(
        "--invite",
        metavar="USERNAME",
        help="Invite a Mattermost user to the new channel.",
    )
    p_spawn.add_argument(
        "--no-forward-prompt",
        action="store_true",
        help="Don't post the prompt to either the new or parent channel.",
    )
    p_spawn.set_defaults(func=cmd_spawn)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        sys.exit(1)
    rc = args.func(args) or 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
