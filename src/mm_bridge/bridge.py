"""Bridge orchestrator — dispatches Mattermost ↔ agent-harness events to handlers."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    attribution,
    commands,
    directives,
    held_posts,
    name_sync,
    purpose,
    resume_header,
    watchdog_notices,
)
from .backend_errors import (
    exception_detail,
    format_backend_error,
    run_failure_detail,
)
from .agent_harness_client import (
    AgentHarnessClient,
    HarnessForkUnsupported,
    HarnessInterruptUnsupported,
    HarnessResumeUnsupported,
    HarnessRunNotFound,
)
from .config import Anchor, ChannelMapping, Config
from .mm_client import MattermostClient
from .typing_indicator import TypingIndicator

logger = logging.getLogger(__name__)

# Fallback turn for attachment-only engagement in a dormant channel.
INVITE_PLACEHOLDER = (
    "Hello! I've just been added to a Mattermost channel. "
    "I'll wait for the user to start the conversation."
)

MM_POST_MAX_LEN = 16000

# Markers wrapping a coalesced flush of posts that arrived while the session
# was working. Bracketed like ``_format_catch_up_block`` so the agent reads
# one consistent "this is bridge-injected context, not a live user turn"
# convention. A SINGLE held post is rendered bare (no markers): the header
# would be noise when there is nothing to disambiguate, but the per-post
# ``HH:MM username:`` stamp is always kept — staleness is the payload.
HELD_BLOCK_HEADER = (
    "[{n} posts arrived while you were working — the newest governs]"
)
HELD_BLOCK_FOOTER = "[End of held posts]"

# Reaction placed on a post the bridge is holding, and the one that replaces
# it on delivery. Mattermost emoji names: ⏳ and ✅.
HELD_REACTION = "hourglass_flowing_sand"
DELIVERED_REACTION = "white_check_mark"

# Outcome of `Bridge._deliver_to_session`. The flush path needs more than a
# bool: a TRANSIENT failure means "keep the posts held and retry on the next
# trigger", while UNSUPPORTED is permanent — that session can never accept a
# run, so retrying would strand them forever.
DELIVERY_OK = "ok"
DELIVERY_FAILED = "failed"
DELIVERY_UNSUPPORTED = "unsupported"
MM_DISPLAY_NAME_MAX = 64
# Event types that unconditionally mean "the session is actively working" and
# should (re)start the typing indicator. ``session.updated`` is NOT here: it is
# status-driven (see ``_session_updated_is_activity``) because the harness reuses
# it for both running- and idle-flips.
HARNESS_ACTIVITY_EVENTS = {
    "message",
    "message.delta",
    "tool.call",
    "tool.result",
    "run.started",
    "permission.denied",
}
HARNESS_RUN_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.interrupted"}

# Run statuses (agent-harness ``RunStatus``) that mean the run is still in
# flight. The Run row is the authoritative "is the coding agent running?"
# signal: unlike ``session.status`` (freshness-based, flips to "idle" on
# rollout-file silence MID-RUN during long tool calls), run status only
# changes on real lifecycle transitions.
HARNESS_LIVE_RUN_STATUSES = {"queued", "running"}

# Session statuses (agent-harness ``SessionStatus``) that mean the session is
# QUIET, not actively producing output. The harness freshness fix emits a
# ``session.updated`` carrying one of these specifically to signal the session
# went idle (see agent-harness ``observer._maybe_publish_status_flip``); a
# bare event-type check would mis-read that "went quiet" flip as activity and
# keep the typing indicator stuck ON. Only ``status == "running"`` on a
# ``session.updated`` is treated as activity; missing/unknown status falls
# back to the SAFE non-activity choice (genuine output always ALSO emits
# ``message`` / ``message.delta`` / ``tool.*`` events, which keep typing alive
# on their own), and those QUIET flips additionally STOP typing.
HARNESS_QUIET_SESSION_STATUSES = {"idle", "waiting_for_input", "archived"}

# Watchdog events — emitted by the harness `RunProcess` watchdogs. NOT in
# HARNESS_RUN_TERMINAL_EVENTS or HARNESS_ACTIVITY_EVENTS: they're supplemental
# signals that PRECEDE (or, for `run.idle_warning`, may precede) a normal
# terminal event (`run.completed`/`run.failed`/`run.interrupted`), which still
# does the typing-stop / run-id-pop cleanup.
#
# - `run.terminated_after_end_turn` (PR #10): rendered SILENT.
# - `run.timed_out_idle` (PR #10): the idle-kill notice.
# - `run.idle_warning` / `run.max_runtime_warning` / `run.exceeded_max_runtime`
#   (v0.1.1, idle-watchdog spec): the deferred-kill, cap-approaching, and
#   cap-kill notices respectively.
HARNESS_WATCHDOG_EVENTS = {
    "run.terminated_after_end_turn",
    "run.timed_out_idle",
    "run.idle_warning",
    "run.max_runtime_warning",
    "run.exceeded_max_runtime",
}

# Watchdog events that post a channel notice, mapped to the formatter that
# turns the event payload into the operator-facing Markdown. Each notice is
# posted at most once per run (see ``Bridge._on_harness_watchdog_event``).
# ``run.terminated_after_end_turn`` is deliberately absent — it's silent.
HARNESS_WATCHDOG_NOTICE_FORMATTERS = {
    "run.timed_out_idle": watchdog_notices.idle_timeout_notice,
    "run.idle_warning": watchdog_notices.idle_warning_notice,
    "run.max_runtime_warning": watchdog_notices.max_runtime_warning_notice,
    "run.exceeded_max_runtime": watchdog_notices.exceeded_max_runtime_notice,
}

# How often to flush the SSE cursor (`mapping.last_event_seq`) to disk.
# Each event triggers an in-memory update; the disk write is throttled to
# avoid burning IO on busy streams. A crash loses at most this many seconds
# of cursor progress — the bridge replays from the last flushed seq.
EVENT_SEQ_FLUSH_INTERVAL_SECONDS = 2.0

# Hard cap on channel-create retries per session. Some session ids cannot
# produce a valid MM channel name (collisions, invalid slug). Without a
# cap, every subsequent ``session.updated`` event re-tries creation in a
# tight loop. Adding to ``_known_sessions`` after the cap stops retrying.
MAX_CHANNEL_CREATE_ATTEMPTS = 3

# Session-id prefixes that should never get an MM channel. These are
# claude-code subagent transcripts (``agent-<hex>.jsonl``), surfaced by the
# harness observer alongside user-facing sessions. They're internal to the
# parent run and would just spam channels.
SUPPRESSED_SESSION_PREFIXES = ("claude_agent-",)

# Channel-join welcome — posted when the bot is added to a channel (auto-join
# OR manual /invite). Edit the template to change what new users see first.
# The `{backends}` and `{context}` placeholders are filled in by
# ``Bridge._format_channel_join_welcome`` so the message reflects the actually
# configured per-backend default models and (when known) the channel's
# effective config.
CHANNEL_JOIN_WELCOME_PROP = "from_bridge"
CHANNEL_JOIN_WELCOME_PROP_VALUE = "welcome"

CHANNEL_JOIN_WELCOME_TEMPLATE = (
    ":wave: Hi, I'm **@{bot}** — an AI coding assistant. "
    "Tag `@{bot}` to talk to me (or `.autorespond` so every message "
    "reaches me).{context}\n"
    "\n"
    "Configure anytime with dot-commands: `.model <name>` · "
    "`.backend <name>` · `.autorespond`. Available backends: {backends}. "
    "The **Channel Purpose** sets the defaults for new sessions and "
    "`mm-bridge spawn`.\n"
    "\n"
    "Start with `.help` for the full command list; "
    "`.stop` to interrupt. Also `@{bot} catch up {catch_up_n}` · "
    "`@{bot} leave`. Catch-up is automatic on the first message. "
    "More: [README](https://github.com/tijszwinkels/mattermost-agent-bridge#readme)."
)

def _compile_bot_command_res(
    bot: str,
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """Compile the catch-up/leave/stop regexes for bot handle ``bot``.

    ``bot`` is the runtime bot username (no leading ``@``); it's ``re.escape``-d
    so an exotic handle can't inject regex metacharacters. Falls back to
    ``claude`` when empty (pre-login) so the patterns are always usable.
    Matching stays case-insensitive — identical to the old ``@claude`` literals
    for a bot actually named ``claude``.
    """
    handle = re.escape(bot or "claude")
    catch_up = re.compile(
        rf"^@{handle}\s+catch\s+up(?:\s+(\d+))?\s*$", re.IGNORECASE,
    )
    leave = re.compile(
        rf"^@{handle}\s+leave\b(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL,
    )
    stop = re.compile(rf"^(?P<mention>@{handle}\s+)?stop\s*$", re.IGNORECASE)
    return catch_up, leave, stop


@dataclass
class WarmingUpChannel:
    """A channel with a session create/link HTTP round trip in progress."""

    channel_id: str
    queued_posts: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class ToolUseRun:
    """A single per-turn tool-use placeholder post, edited in place as
    the assistant fires tool calls. Lines accumulate one per tool switch;
    consecutive calls of the same tool bump the last line's counter.
    """
    post_id: str
    lines: list[list] = field(default_factory=list)  # [[tool_name, count], ...]


def _format_tool_run(run: ToolUseRun) -> str:
    parts: list[str] = []
    for tool, count in run.lines:
        suffix = f" (x{count})" if count > 1 else ""
        parts.append(f"_Using tool: {tool}{suffix}_")
    return "\n".join(parts)


def _bump_or_append(run: ToolUseRun, tool: str) -> None:
    if run.lines and run.lines[-1][0] == tool:
        run.lines[-1][1] += 1
    else:
        run.lines.append([tool, 1])


def _extract_text_from_blocks(blocks: list[dict]) -> str:
    """Flatten text + error blocks (not tool_use). Used for the real
    assistant response; tool_use blocks are handled separately via the
    ToolUseRun coalescing path in `_on_harness_message`.
    """
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_result":
            content = block.get("content", "")
            if block.get("is_error"):
                parts.append(f"**Tool error:** {str(content)[:500]}")
    return "\n".join(p for p in parts if p)


def _truncate_for_mm(text: str, max_len: int = MM_POST_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 50] + "\n\n_(truncated)_"


def _is_suppressed_session(session_id: str) -> bool:
    """Return True for ids the bridge should never attach a channel to.

    Currently: claude-code subagent transcripts (``claude_agent-<hex>``),
    which are internal to a parent run.
    """
    return any(session_id.startswith(p) for p in SUPPRESSED_SESSION_PREFIXES)


# `.sessions` list sizing (client-side — the harness endpoint has no limit).
_SESSIONS_DEFAULT_N = 15
_SESSIONS_MAX_N = 50


def _parse_count_arg(arg: str | None, *, default: int, maximum: int) -> int:
    """Parse an optional integer command argument, clamped to [1, maximum].

    Non-integer or absent args fall back to ``default``.
    """
    if not arg:
        return default
    try:
        n = int(arg.strip())
    except ValueError:
        return default
    return max(1, min(n, maximum))


def _session_to_channel_name(session_id: str) -> str:
    """Derive a stable, collision-resistant MM channel slug.

    Backend-prefixed ids (``claude_<uuid>``, ``codex_<uuid>``, ``ses_<uuid>``)
    have most of their entropy *after* the underscore. Naively taking
    ``session_id[:12]`` truncates that entropy, causing same-prefix ids to
    collide on the same slug. Strip the known backend prefix first so the
    12-char window samples the unique part.
    """
    BACKEND_PREFIXES = ("claude_agent-", "claude_", "codex_", "ses_")
    tail = session_id
    for prefix in BACKEND_PREFIXES:
        if session_id.startswith(prefix):
            tail = session_id[len(prefix):]
            break
    short = tail[:12].replace("-", "").lower()
    return f"s-{short}"


def _normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    return str(path).rstrip("/")


def _is_mm_system_post(post: dict) -> bool:
    return bool(post.get("type"))


def _format_first_message_preamble(
    channel_name: str, human_usernames: list[str],
) -> str:
    """Build the one-line `[...]` preamble for the first forwarded user message.

    The preamble tells the model it's running inside a Mattermost channel,
    who it's talking to, and how to keep their attention. Shape:

        [Running inside Mattermost channel "<name>". <who>. @-mention to keep their attention.]

    `<who>` varies:
      - 1 human  → `You're talking to @<u>`
      - ≥2       → ``Multiple users in this channel (`u1`, `u2`, ...) — messages are prefixed with `username:` ``
      - 0        → omitted entirely
    """
    pieces = [f'Running inside Mattermost channel "{channel_name}".']
    if len(human_usernames) == 1:
        pieces.append(f"You're talking to @{human_usernames[0]}.")
    elif len(human_usernames) > 1:
        user_list = ", ".join(f"`{u}`" for u in human_usernames)
        pieces.append(
            f"Multiple users in this channel ({user_list}) "
            "— messages are prefixed with `username:`."
        )
    pieces.append("@-mention to keep their attention.")
    return "[" + " ".join(pieces) + "]"


MM_INBOX_DIRNAME = ".mattermost-inbox"


def _safe_inbox_filename(name: str, fallback: str) -> str:
    """Sanitize a MM-supplied filename so it can't escape the inbox dir."""
    base = Path(name or "").name.strip().lstrip(".")
    base = base.replace("\0", "").replace("/", "_").replace("\\", "_")
    return base or fallback


def _unique_inbox_path(dest_dir: Path, filename: str) -> Path:
    """Return dest_dir/filename, with -N suffix on conflict."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(1, 10_000):
        cand = dest_dir / f"{stem}-{n}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"too many filename conflicts in {dest_dir}")


def resolve_attachment_path(
    raw_path: str,
    project_path: str | None,
    allowed_roots: list[str],
) -> Path | None:
    """Resolve a path from an <openFile/> directive against safety roots.
    Returns None if outside the allowed roots or if resolution fails.
    """
    try:
        # ``expanduser`` first so a ``~/…`` path resolves against $HOME
        # instead of being treated as a cwd-relative dir literally named
        # ``~`` (which never matches a configured root).
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            if not project_path:
                return None
            candidate = Path(project_path) / candidate
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return None

    root_candidates: list[Path] = []
    if project_path:
        try:
            root_candidates.append(Path(project_path).resolve(strict=False))
        except (OSError, ValueError):
            pass
    for r in allowed_roots:
        try:
            root_candidates.append(Path(r).expanduser().resolve(strict=False))
        except (OSError, ValueError):
            continue

    if not root_candidates:
        return resolved  # no sandbox configured → trust the caller

    for root in root_candidates:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


def describe_allowed_roots(
    project_path: str | None, allowed_roots: list[str],
) -> str:
    """Backtick-quoted, comma-joined list of the roots an attachment may live
    under — the session cwd first (always implicitly allowed), then the
    operator-configured roots. Used to make a rejected-attach warning
    self-explaining so the agent knows where to put the file instead of
    guessing (``/tmp`` etc.). Paths are expanded + resolved so a ``~`` root
    shows as a real directory. Returns ``""`` when nothing is configured.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for raw in ([project_path] if project_path else []) + list(allowed_roots):
        try:
            resolved = str(Path(raw).expanduser().resolve(strict=False))
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        labels.append(f"`{resolved}`")
    return ", ".join(labels)


class Bridge:
    """Mediates Mattermost ↔ agent-harness traffic."""

    def __init__(self, config: Config):
        self.config = config
        self.mapping = ChannelMapping.load(config.state_file, config.sidecar_dir)
        self.mm = MattermostClient(
            url=config.mm_url,
            port=config.mm_port,
            scheme=config.mm_scheme,
            token=config.mm_bot_token,
            team_name=config.mm_team,
        )
        self.harness = AgentHarnessClient(config.agent_harness_url)
        self.posters = attribution.PosterTracker()
        self.name_sync = name_sync.NameSync(
            window_seconds=config.name_sync_window_seconds
        )
        self.typing: TypingIndicator | None = None  # created after login
        self.purpose_by_channel: dict[str, purpose.PurposeConfig] = {}
        self.warming_up_sessions: dict[str, WarmingUpChannel] = {}
        self.dead_threads: set[tuple[str, str]] = set()
        self.last_channel_state: dict[str, dict] = {}
        self.last_activity_ts: dict[str, float] = {}
        self.current_run_id_by_session: dict[str, str] = {}
        # Sessions with a run currently in flight, from SSE ``run.started``
        # → terminal events. Origin-agnostic (also tracks runs the bridge
        # didn't submit, unlike ``current_run_id_by_session``). Value is the
        # run_id, or None when the event omitted it. Read by the typing
        # watchdog (reconcile instead of stop) and the quiet-flip handler
        # (ignore freshness idle-flips mid-run).
        self.active_run_by_session: dict[str, str | None] = {}
        # Watchdog notice events (idle/runtime warnings + kills) already posted
        # for the session's CURRENT run, so a harness re-emit doesn't double
        # post. Keyed by session_id → set of event types; at most one run is
        # active per session, and the set is cleared on the run's terminal
        # event (see ``_on_harness_run_lifecycle``), so it bounds naturally and
        # a later run in the same session can warn afresh.
        self._posted_watchdog_notices: dict[str, set[str]] = {}
        self._known_sessions: set[str] = set()
        # Sessions the harness reports as ``origin: external`` — i.e. NOT
        # launched by the harness itself, so it has no stdin / IPC channel
        # to inject new user turns into. The bridge forwards messages for
        # such sessions to a fresh harness-origin replacement instead;
        # see ``_replace_external_session``. Populated at bootstrap and
        # pruned as sessions are replaced.
        self._external_sessions: set[str] = set()
        # How many times we tried (and failed) to create an MM channel for a
        # given session_id. After ``MAX_CHANNEL_CREATE_ATTEMPTS`` the session
        # is added to ``_known_sessions`` to stop retrying.
        self._channel_create_attempts: dict[str, int] = {}
        # Monotonic timestamp of the last cursor flush to disk. Throttled by
        # ``EVENT_SEQ_FLUSH_INTERVAL_SECONDS`` so a busy stream doesn't beat
        # up the filesystem. The latest seq is kept in
        # ``self.mapping.last_event_seq``; only the flush is throttled.
        self._last_seq_flush_ts: float = 0.0
        self._pending_seq: int | None = None
        # SSE start cursor, resolved in ``start()`` from the persisted
        # ``mapping.last_event_seq`` or a cold-start probe of the harness.
        self._event_cursor: int | None = None
        # Legacy/deferred first-forward state used by adopted session paths and
        # the forwarding helper. New channel membership uses `_dormant_channels`
        # and creates no session until the first conversational message.
        self._awaiting_first_forward: set[str] = set()
        self._pending_initial_catch_up: dict[str, str] = {}
        # Channel purposes we just wrote ourselves, so the subsequent
        # `channel_updated` event doesn't trigger a spurious change notice.
        # A SET per channel, not a single value: a `.model`/`.backend` restart
        # writes the Purpose twice (resume block during restart, then config),
        # and a single slot would forget the first write and let it notify.
        self._self_written_purpose: dict[str, set[str]] = {}
        # Channels we joined ourselves (auto-join). The resulting
        # `user_added` event must not trigger a new VD session — presence
        # should be silent until a user engages.
        self._self_joined_channels: set[str] = set()
        # Channels where the bot is a member but no agent session exists yet.
        # Manual invites and auto-joins intentionally converge on this state:
        # safe config commands work immediately, while the first conversational
        # message creates the one and only harness session.
        self._dormant_channels: set[str] = set()
        self._max_file_size: int | None = None
        # Per-session coalesced tool-use placeholder posts. Created on the
        # first tool_use block of a turn, edited on subsequent ones; the
        # state is dropped (but the post is left intact) on turn end so
        # the next turn starts a fresh placeholder. The post remains as a
        # compact summary of that turn's tool usage.
        self.tool_use_runs: dict[str, ToolUseRun] = {}
        # Posts silently dropped in mention-only mode, keyed by
        # (channel_id, thread_root). Drained on the next forwarded
        # message in the same anchor and prepended as a catch-up block
        # so the session sees the preceding conversation it missed.
        self._silent_drops: dict[tuple[str, str | None], deque[dict]] = {}
        # user_id of the most recent MM post forwarded into each session.
        # Read by `_mention_triggerer_on_done` to @-mention that user when
        # the run ends. Cleared on use, so a single completion event only
        # pings once, and on session teardown (see `*_forget_session`).
        self._session_triggerer: dict[str, str] = {}
        # Posts held because the anchor's session already had a run in
        # flight. Sibling of `_silent_drops` — same (channel_id, thread_root)
        # key — but these WOULD have been forwarded: they are deferred, not
        # dropped, and are delivered as ONE coalesced run when the live run
        # reaches a terminal state. Persisted, because today an un-coalesced
        # post lands in the harness FIFO and survives a bridge restart;
        # holding it purely in memory would be a durability regression.
        self._held = held_posts.HeldPostStore(
            config.resolved_held_posts_file(), cap=config.coalesce_max_held,
        )
        # Anchors with a flush in progress. Posts arriving during a flush's
        # awaits must be held rather than eagerly submitted, or their run
        # would overtake the flush's own `create_run` and reorder the
        # conversation.
        self._flushing: set[Anchor] = set()
        # Anchors whose session is being swapped — unlinked, and not yet
        # relinked to the replacement. `mapping.get_session` reads None across
        # that window, which the sweep would take as "session gone" and use to
        # abandon the backlog. See `_anchor_relink_window`.
        self._relinking: set[Anchor] = set()
        # Monotonic timestamp of the last harness liveness probe per session,
        # for the held-anchor sweep. Same rate-limit discipline as the typing
        # watchdog (one probe per silence window), so a long-held anchor can't
        # turn into a per-tick GET storm. Popped on flush and on teardown.
        self._held_probe_ts: dict[str, float] = {}
        # Recently-sent backend message bodies, keyed by session_id. Used to
        # de-duplicate the role=user echo the harness broadcasts when the bridge
        # itself shipped the message (MM forwards, catch-up blocks,
        # firstMessage on invite/fork claims). Capped per session.
        self._recent_harness_sends: dict[str, deque[tuple[float, str]]] = {}

    # ----- lifecycle -----

    async def start(self) -> None:
        self.mm.login()
        self.typing = TypingIndicator(self.mm, self.config.typing_refresh_seconds)

        try:
            health = await self.harness.health()
            logger.info("agent-harness health: %s", health)
        except Exception:
            try:
                await self.harness.list_sessions()
                logger.info("agent-harness session list probe succeeded")
            except Exception:
                logger.exception("agent-harness health check failed — continuing anyway")

        await self._bootstrap_known_sessions()
        await self._bootstrap_dormant_channels()
        self._event_cursor = await self._bootstrap_event_cursor()

        logger.info(
            "Connected — Mattermost (team=%s, bot=%s) + agent-harness (%s)",
            self.config.mm_team, self.mm.bot_username, self.config.agent_harness_url,
        )

        try:
            await self._reconcile_resume_purposes()
        except Exception:
            logger.warning(
                "resume-purpose: startup reconcile failed", exc_info=True,
            )

        # Reload posts that were held when the daemon last stopped, and
        # deliver the ones whose run has since finished. Runs after the
        # bootstrap so the anchor→session mapping is populated, and before
        # the listeners so a restart can't interleave a new post with the
        # backlog it belongs behind.
        try:
            await self._rehydrate_held()
        except Exception:
            logger.exception("Failed to rehydrate held posts at startup")

        await asyncio.gather(
            self._run_mm_listener(),
            self._run_harness_listener(),
            self._run_typing_watchdog(),
            self._run_auto_join_reconciler(),
        )

    async def stop(self) -> None:
        # Flush any throttled SSE cursor before tearing down — a clean
        # stop inside the 2s persist window would otherwise discard the
        # latest seq, and the next boot would replay up to ~2s of events.
        if self._pending_seq is not None:
            try:
                self.mapping.set_event_seq(self._pending_seq)
            except Exception:
                logger.exception("Failed to flush pending event seq on shutdown")
            self._pending_seq = None
        if self.typing:
            await self.typing.shutdown()
        await self.harness.close()

    async def _bootstrap_known_sessions(self) -> None:
        try:
            sessions = await self.harness.list_sessions()
        except Exception:
            logger.warning(
                "Bootstrap GET /v1/sessions failed — falling back to mapping",
                exc_info=True,
            )
            sessions = []
        self._known_sessions = set(self.mapping.session_to_anchor.keys())
        for session in sessions:
            session_id = session.get("id")
            if not session_id:
                continue
            is_external = session.get("origin") == "external"
            if is_external and self.mapping.get_anchor(session_id):
                # Inbound MM posts to this mapping can't be delivered:
                # the harness has no stdin for an externally-launched
                # session. ``_on_mm_posted`` will swap in a fresh
                # harness-origin session on the next user post.
                self._external_sessions.add(session_id)
            if self.mapping.get_anchor(session_id):
                self._known_sessions.add(session_id)
                continue
            if _is_suppressed_session(session_id):
                self._known_sessions.add(session_id)
                continue
            if session_id in self.mapping.adopted_session_ids:
                # Previously replaced by ``_replace_external_session``.
                # The harness still lists it (sessions aren't deleted),
                # but a fresh harness session has taken over its channel.
                # Don't auto-spawn a recovery channel.
                self._known_sessions.add(session_id)
                continue
            if not is_external:
                # Harness-origin sessions in the repo at bootstrap are
                # either pre-existing spawn-CLI / bridge-created records
                # whose channel was wired in a previous run, or test
                # leftovers from prior ``agent-harness`` runs. Either way,
                # we must NOT auto-create a channel for them on startup —
                # but we MUST mark them ``known`` so the SSE bootstrap-
                # replay of their ``session.updated`` event doesn't trip
                # the live-create path in ``_on_harness_session_seen``.
                # Without this guard the cursor-reset recovery flow would
                # re-spawn an MM channel for every leftover test session
                # in the harness DB (the 2026-05-12 ghost-channel burst).
                self._known_sessions.add(session_id)
                continue
            if not self.config.mirror_external_sessions:
                # Operator opted out of mirroring CLI/terminal sessions
                # into channels (shared-server deployment). Mark known so
                # the SSE bootstrap replay can't trip the live-create
                # path for the same session.
                logger.info(
                    "Not mirroring external session %s "
                    "(mirror_external_sessions=false)",
                    session_id[:24],
                )
                self._known_sessions.add(session_id)
                continue
            if await self._create_channel_for_session(session):
                self._known_sessions.add(session_id)

    async def _bootstrap_dormant_channels(self) -> None:
        """Recover joined-but-unmapped channels after a daemon restart."""
        try:
            channel_ids = await asyncio.to_thread(self.mm.get_bot_channel_ids)
        except Exception:
            logger.warning(
                "Failed to enumerate bot channel memberships for dormant-state bootstrap",
                exc_info=True,
            )
            return
        self._dormant_channels = {
            channel_id for channel_id in channel_ids
            if not self.mapping.get_session(Anchor(channel_id))
        }
        if self._dormant_channels:
            logger.info(
                "Recovered %d dormant channel membership(s)",
                len(self._dormant_channels),
            )

    async def _bootstrap_event_cursor(self) -> int | None:
        """Return the SSE cursor to resume from on this boot.

        Warm restart: use the persisted ``last_event_seq`` from state.json,
        but verify the harness's current max sequence first. The harness
        event bus is in-memory — if the harness was restarted, its sequence
        rolls back to 0 and the persisted cursor becomes a "future" value
        that would mask every event the new harness emits. Detected reset
        ⇒ reset cursor to 0 and replay whatever's currently in the new
        harness's bus.

        Cold start (no persisted seq): probe the harness for its current
        max sequence so the bridge only sees *new* events. Without this,
        agent-harness replays its full history on connect, which the bridge
        used to mirror back into Mattermost as a 1000+ post flood.
        """
        persisted = self.mapping.last_event_seq
        try:
            harness_max = await self.harness.probe_current_sequence()
        except Exception:
            if persisted is not None:
                logger.warning(
                    "Cursor probe failed — falling back to persisted seq=%d "
                    "(may skip recent events if harness was restarted)",
                    persisted,
                )
                return persisted
            logger.exception(
                "Cold-start cursor probe failed — starting from current end "
                "(SSE will resync once new events arrive)",
            )
            return None

        if persisted is None:
            self.mapping.set_event_seq(harness_max)
            logger.info(
                "Cold-start SSE cursor probed: starting from seq=%d", harness_max,
            )
            return harness_max

        if persisted > harness_max:
            logger.warning(
                "Detected harness sequence reset (persisted=%d > harness_max=%d). "
                "Resetting cursor to 0 to replay current in-memory events; "
                "already-mapped sessions are filtered by anchor lookup but a "
                "few duplicate posts are possible during recovery.",
                persisted,
                harness_max,
            )
            self.mapping.reset_event_seq(0)
            return 0

        logger.info(
            "Resuming SSE stream from persisted seq=%d (harness_max=%d)",
            persisted,
            harness_max,
        )
        return persisted

    async def _persist_event_seq(self, sequence: int) -> None:
        """Throttled write of the SSE cursor. Always updates in-memory."""
        if not isinstance(sequence, int) or sequence <= 0:
            return
        self._pending_seq = sequence
        now = time.monotonic()
        if now - self._last_seq_flush_ts < EVENT_SEQ_FLUSH_INTERVAL_SECONDS:
            return
        self._last_seq_flush_ts = now
        self.mapping.set_event_seq(sequence)
        self._pending_seq = None

    def _track_run_response(self, session_id: str, run: dict | None) -> None:
        if not run:
            return
        run_id = run.get("run_id") or run.get("id")
        if run_id:
            self.current_run_id_by_session[session_id] = run_id

    # ----- listener loops -----

    async def _run_mm_listener(self) -> None:
        logger.info("Starting Mattermost WebSocket listener...")
        await self.mm.listen_websocket({
            "posted": self._on_mm_posted,
            "user_added": self._on_mm_user_added,
            "user_removed": self._on_mm_user_removed,
            "channel_updated": self._on_mm_channel_updated,
            "channel_created": self._on_mm_channel_created,
        })

    async def _run_auto_join_reconciler(self) -> None:
        """Periodically join all public team channels the bot is missing.

        No-op when `auto_join_public_channels` is disabled. Runs forever;
        each iteration's failures are logged but don't stop the loop.
        """
        if not self.config.auto_join_public_channels:
            return
        while True:
            try:
                await self._reconcile_auto_join_once()
            except Exception:
                logger.exception("auto-join reconciler iteration failed")
            await asyncio.sleep(self.config.auto_join_reconcile_seconds)

    async def _reconcile_auto_join_once(self) -> None:
        """One reconciler sweep — join each missing public channel, one by one.

        The mark-then-join order matters: the MM server dispatches
        `user_added` over WS the moment the HTTP join lands, and the event
        arrives on the main asyncio loop. If we marked after the join, the
        event could race in and spawn a VD session before the bridge knows
        the join was self-initiated. Marking first closes that window.
        """
        bot_ids = await asyncio.to_thread(self.mm.get_bot_channel_ids)
        public = await asyncio.to_thread(self.mm.list_public_team_channels)
        joined = 0
        for ch in public:
            cid = ch["id"]
            if cid in bot_ids:
                continue
            self._self_joined_channels.add(cid)
            try:
                await asyncio.to_thread(self.mm.join_channel, cid)
                joined += 1
                logger.info(
                    "Auto-joined channel: %s (%s)",
                    ch.get("display_name") or cid, cid,
                )
            except Exception:
                self._self_joined_channels.discard(cid)
                logger.warning(
                    "Failed to auto-join %s (%s)", ch.get("display_name") or cid, cid,
                    exc_info=True,
                )
        if joined:
            logger.info("Auto-join reconciler joined %d channel(s)", joined)

    async def _run_harness_listener(self) -> None:
        logger.info("Starting agent-harness SSE listener...")
        await self.harness.stream_events(
            self._on_harness_event,
            after_sequence=getattr(self, "_event_cursor", None),
            on_progress=self._persist_event_seq,
            on_reset=self._on_harness_sequence_reset,
        )

    async def _on_harness_sequence_reset(self) -> None:
        """Called when the SSE client detects the harness restarted mid-session
        (its in-memory sequence rolled back below our cursor). Reset the
        persisted cursor too so a subsequent bridge restart picks the same
        recovery path on bootstrap."""
        logger.warning(
            "Harness restart detected mid-session — resetting persisted "
            "event cursor to 0",
        )
        self.mapping.reset_event_seq(0)

    async def _run_typing_watchdog(self) -> None:
        """Stop or reconcile typing loops whose event stream went silent."""
        while True:
            await asyncio.sleep(self.config.typing_refresh_seconds)
            await self._typing_watchdog_tick()
            # Second phase, same task: reconcile anchors holding posts. The
            # typing tick can't cover them — see `_sweep_held_anchors`.
            try:
                await self._sweep_held_anchors()
            except Exception:
                logger.exception("Held-anchor sweep iteration failed")

    async def _typing_watchdog_tick(self) -> None:
        """One watchdog pass over the typing sessions silent for longer than
        ``typing_stop_after_silence_seconds``.

        Silence alone is NOT proof the agent stopped: during a long tool
        call or an async subagent wait the harness emits no message/tool
        events for minutes while the run is very much alive. So a silent
        session WITH a tracked active run is reconciled against the harness
        Run row instead of blindly stopped; sessions without one (external/
        observer — no run lifecycle events exist for them) keep the old
        silence-stop behavior.
        """
        if not self.typing:
            return
        timeout = self.config.typing_stop_after_silence_seconds
        now = time.monotonic()
        for session_id in list(self.typing.running_sessions()):
            last = self.last_activity_ts.get(session_id)
            if last is not None and now - last <= timeout:
                continue
            if session_id not in self.active_run_by_session:
                logger.debug(
                    "No agent-harness activity for %s in %.0fs, stopping typing",
                    session_id[:8], timeout,
                )
                await self.typing.stop(session_id)
                self.last_activity_ts.pop(session_id, None)
                continue
            if await self._active_run_is_alive(session_id):
                # Run confirmed queued/running. Count the probe as activity
                # so the next reconcile only happens after another full
                # silence window (natural rate limit on harness GETs).
                self.last_activity_ts[session_id] = time.monotonic()
                continue
            # Terminal, unknown (404), or unreachable harness →
            # missed-terminal-event recovery. Conservative on errors so a
            # dead harness can't leave typing stuck ON.
            logger.info(
                "Tracked run for %s not alive after %.0fs silence, "
                "stopping typing",
                session_id[:8], timeout,
            )
            await self.typing.stop(session_id)
            self.last_activity_ts.pop(session_id, None)
            self.active_run_by_session.pop(session_id, None)
            # The run died without a terminal event reaching us, so nothing
            # else will flush this session's held posts.
            await self._maybe_flush_held_for_session(session_id)

    async def _active_run_is_alive(self, session_id: str) -> bool:
        """Ask the harness whether the session's tracked run is still in
        flight (status queued/running).

        ``session.status`` is deliberately NOT consulted: it is freshness-
        based and flips to "idle" on rollout-file silence mid-run. When the
        tracked run_id is missing (event omitted it) fall back to the
        session's runs list — ANY live row counts. Every failure mode
        (404, HTTP error, dead harness) returns False.
        """
        run_id = self.active_run_by_session.get(session_id)
        try:
            if run_id:
                run = await self.harness.get_run(session_id, run_id)
                runs = [run] if run else []
            else:
                runs = await self.harness.list_session_runs(session_id)
        except Exception as exc:
            logger.info(
                "Typing reconcile probe failed for %s: %s",
                session_id[:8], exc,
            )
            return False
        return any(
            (run or {}).get("status") in HARNESS_LIVE_RUN_STATUSES
            for run in runs
        )

    # ─────────────────────── Mattermost WS handlers ───────────────────────

    async def _on_mm_posted(self, post: dict) -> None:
        if _is_mm_system_post(post):
            return
        # Bridge CLI subcommands stamp `props.from_bridge_cli` on posts
        # they author themselves. Marker taxonomy and dispatch:
        #
        #   * ``"post"`` — an explicit ``mm-bridge post`` call. Keyed on the
        #     stamped INTENT (``from_bridge_cli_target``), not just the session
        #     id: "self" = the default post-into-my-own-channel path; "explicit"
        #     = ``--channel``/``--thread`` was given. We drop ONLY a "self" post
        #     whose ``from_bridge_cli_session`` also equals the session the
        #     target anchor maps to (belt and braces) — that's a status update
        #     looping back to its author. Everything else forwards: "explicit"
        #     always (that's agentcom — a sender in channel X telling channel Y
        #     "hi"), a "self" post landing on a different session (anomalous),
        #     and posts with no target tag (old CLI, forwards-compat).
        #     Keying on intent — not session id alone — is deliberate: a
        #     poisoned resolver (the RC1 incident) that leaks a parent session
        #     id can then NEVER cause an explicit agentcom post to be silently
        #     dropped, because agentcom always carries ``--channel`` → "explicit".
        #     The one accepted gap: an explicit ``--channel <your own
        #     channel>`` / ``--thread <your own root>`` is "explicit" and
        #     forwards (a deliberate override, harmless). The
        #     daemon's OWN outbound posts are separately dedup'd upstream by
        #     post id (`mm_client.py`'s `is_own_post`).
        #   * ``"cross-post-mirror"`` — the informational mirror that
        #     ``mm-bridge post --channel <other>`` lands in the
        #     SENDER's own channel for transcript visibility. Drop
        #     iff the recorded origin channel matches where the post
        #     landed (own-channel mirror); otherwise (shouldn't happen
        #     in practice) treat as agentcom and forward.
        #   * ``"spawn-announcement"`` / ``"spawn-kickoff"`` — bridge
        #     artifacts that the sender's or new channel's linked
        #     session must not consume as a user turn. Keep the
        #     channel-equality drop for these.
        #   * Any other / future marker with no channel field — drop
        #     conservatively (marker authors all live in this
        #     codebase, so an absent channel field is a forwards-compat
        #     hedge for a marker we don't recognise yet).
        props = post.get("props") or {}
        if isinstance(props, dict) and props.get("from_bridge_cli"):
            marker = props.get("from_bridge_cli")
            origin_channel = props.get("from_bridge_cli_channel")
            if marker == "post":
                # Drop only a default (target=self) post that loops back to its
                # own author session; explicit / mismatched / untagged → forward.
                origin_session = props.get("from_bridge_cli_session")
                if (
                    props.get("from_bridge_cli_target") == "self"
                    and origin_session
                    and self._cli_post_targets_own_session(post, origin_session)
                ):
                    logger.debug(
                        "Dropping self-post loop-back: session %s posted into "
                        "its own channel %s via `mm-bridge post`",
                        origin_session[:8], post["channel_id"],
                    )
                    return
            elif origin_channel is None or origin_channel == post["channel_id"]:
                # Mirror / spawn-artifact landing in the recorded
                # channel — drop so the linked session doesn't read it
                # back as a user turn.
                return
        message = (post.get("message") or "").strip()
        if not message and not post.get("file_ids"):
            return
        channel_id = post["channel_id"]
        root_id = post.get("root_id") or None

        # Thread reply?
        if root_id:
            await self._handle_thread_post(channel_id, root_id, post, message)
            return

        session_id = self.mapping.get_session(Anchor(channel_id))

        # A first conversational message may be creating the session right
        # now. Preserve the full post so commands, attribution and attachments
        # are re-routed normally once the mapping is ready.
        warming = self.warming_up_sessions.get(channel_id)
        if not session_id and warming:
            warming.queued_posts.append(dict(post))
            return

        # Dot-commands (`.stop`, `.help`, ...) are handled by the bridge
        # itself. Dispatched here — after the channel→session lookup but
        # before every forward path — so they bypass the mention-only gate
        # and are never delivered to the agent. Global commands work even
        # without a mapped session; session-scoped ones reply "no session".
        parsed = commands.parse(message, mentions=self._command_mentions())
        if parsed is not None:
            if self._commands_allowed_here(
                channel_id, session_id, message, parsed,
            ):
                await self._dispatch_command(
                    channel_id, session_id, post, parsed, thread_root=None,
                )
                return
            if channel_id in self._dormant_channels:
                # A bare sensitive/unknown dot-word in a dormant channel is
                # neither a command nor conversation. Swallow it so
                # autorespond can never turn `.backend`-shaped text into the
                # first LLM turn, while avoiding global-session data leaks.
                return

        if not session_id:
            if channel_id not in self._dormant_channels:
                return

            # Natural-language control phrases advertised in the join welcome
            # must not accidentally create a session just to say there is no
            # session. They behave identically for invite and auto-join.
            if self._catch_up_re.match(message):
                self._post_cmd_reply(
                    channel_id,
                    ":information_source: Catch-up is included automatically "
                    "with your first message.",
                    None,
                )
                return
            if m := self._leave_cmd_re.match(message):
                try:
                    await asyncio.to_thread(self.mm.remove_self_from_channel, channel_id)
                except Exception:
                    logger.warning("Failed to leave dormant channel %s", channel_id, exc_info=True)
                    self._post_cmd_reply(
                        channel_id, ":warning: Failed to leave the channel.", None,
                    )
                    return
                self._dormant_channels.discard(channel_id)
                self.purpose_by_channel.pop(channel_id, None)
                return
            if self._stop_cmd_re.match(message):
                self._post_cmd_reply(
                    channel_id, ":information_source: No session in this channel.", None,
                )
                return

            await self._maybe_start_engagement_session(
                channel_id, post, message,
            )
            return

        # Channels still mapped to a pre-cutover external session can't
        # receive injected user turns (harness has no stdin for them).
        # Replace with a fresh harness-origin session and let the new
        # session pick up the user's current message as its first turn.
        if session_id in self._external_sessions:
            await self._replace_external_session(
                channel_id, session_id, post, message,
            )
            return

        # A legacy deferred first message carries the MM-context preamble and
        # catch-up. `_forward_user_post`
        # consumes the slot only after mention/empty-message gates, restoring
        # it if delivery fails. Content is never parsed as config — dot-
        # commands and Channel Purpose are the only configuration paths.
        is_first_user_message = channel_id in self._awaiting_first_forward

        # (The bare `autorespond` / `noautorespond` message-content toggle was
        # removed — message content is never config. `.autorespond` is the
        # supported path; such words now forward to the agent verbatim.)

        # Command: @claude catch up
        if m := self._catch_up_re.match(message):
            await self._run_catch_up(channel_id, session_id, None, m)
            return
        # Command: @claude leave
        if m := self._leave_cmd_re.match(message):
            await self._run_leave_command(
                channel_id, session_id, thread_root=None, reason=(m.group(1) or "").strip(),
            )
            return
        # Command: @claude stop (bare `stop` is honored only in autorespond mode)
        if sm := self._stop_cmd_re.match(message):
            cfg = self.purpose_by_channel.get(channel_id)
            if sm.group("mention") or not (cfg and cfg.mention_only):
                await self._run_stop_command(channel_id, session_id, thread_root=None)
                return

        await self._forward_user_post(
            channel_id, session_id, post, message, thread_root=None,
            first_message=is_first_user_message,
        )

    def _cli_post_targets_own_session(self, post: dict, origin_session: str) -> bool:
        """True when a ``mm-bridge post`` (``from_bridge_cli="post"``) landed
        in the anchor its OWN originating session maps to.

        The recipient is resolved exactly as the forward path would pick it:
        a threaded post targets ``Anchor(channel, root_id)``, a channel post
        targets ``Anchor(channel)``. When that recipient equals the stamped
        origin session, the post is a self-status-update looping back and must
        not be re-injected as a user turn. Cross-session (agentcom) posts have
        a different recipient and return False (→ forwarded).
        """
        root_id = post.get("root_id") or None
        anchor = (
            Anchor(post["channel_id"], root_id) if root_id
            else Anchor(post["channel_id"])
        )
        return self.mapping.get_session(anchor) == origin_session

    async def _on_mm_user_added(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        if self.mapping.get_session(Anchor(channel_id)):
            self._self_joined_channels.discard(channel_id)
            logger.info("Bot already mapped for channel %s — skipping", channel_id)
            return
        if channel_id in self.warming_up_sessions:
            return
        joined_by_bridge = channel_id in self._self_joined_channels
        self._self_joined_channels.discard(channel_id)
        self._dormant_channels.add(channel_id)
        logger.info(
            "%s channel %s — dormant until first engagement",
            "Auto-joined" if joined_by_bridge else "Invited to",
            channel_id,
        )
        await self._post_channel_join_welcome(channel_id)

    async def _maybe_start_engagement_session(
        self,
        channel_id: str,
        post: dict,
        message: str,
    ) -> None:
        """Start a session on first engagement in any dormant channel.

        "Engagement" depends on the channel's effective config:
          - mention-only  → require an `@claude` / `@<bot>` mention
          - autorespond   → any non-empty message qualifies

        The engagement message itself becomes the session's first VD message
        (stripped of bot mention) and is forwarded verbatim — never parsed as
        config (dot-commands / Channel Purpose are the only config paths).
        """
        # Reserve before the first await. Two near-simultaneous first messages
        # must never race into two session creations; later posts are queued by
        # `_on_mm_posted` and replayed after the mapping exists.
        if channel_id in self.warming_up_sessions:
            self.warming_up_sessions[channel_id].queued_posts.append(dict(post))
            return
        self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)

        try:
            cfg = await self._load_channel_config(channel_id, force=True)
        except Exception:
            logger.exception("Failed to fetch channel config for %s", channel_id)
            queued = self.warming_up_sessions.pop(channel_id, None)
            if queued and queued.queued_posts:
                await self._flush_queued(channel_id, queued.queued_posts)
            return

        mentioned = self._message_mentions_bot(message)
        if cfg.mention_only and not mentioned:
            queued = self.warming_up_sessions.pop(channel_id, None)
            if queued and queued.queued_posts:
                await self._flush_queued(channel_id, queued.queued_posts)
            return  # silent — not engaging

        cleaned = self._strip_bot_mention(message)
        if not cleaned and not post.get("file_ids"):
            queued = self.warming_up_sessions.pop(channel_id, None)
            if queued and queued.queued_posts:
                await self._flush_queued(channel_id, queued.queued_posts)
            return

        # The first engagement message starts the session and is forwarded
        # verbatim — never parsed as config (dot-commands / Channel Purpose
        # are the only config paths). This mirrors the invite path.
        initial = cleaned or INVITE_PLACEHOLDER
        preamble = await self._compute_first_message_preamble(channel_id)
        if preamble:
            initial = f"{preamble}\n\n{initial}"

        await self._start_invited_session(
            channel_id,
            initial_message=initial,
            awaits_first_message=False,
            post_welcome=False,
            exclude_post_id=post.get("id"),
            post=post,
        )

    async def _on_mm_channel_created(self, channel_id: str) -> None:
        """New channel appeared — join it if auto-join is enabled.

        Note: Mattermost scopes `channel_created` WS events to the creating
        user's session only, so this path typically fires only for channels
        the bot creates itself (e.g. `mm-bridge spawn`). Human-created public
        channels are picked up by the reconciler.
        """
        if not self.config.auto_join_public_channels:
            return
        self._self_joined_channels.add(channel_id)
        try:
            await asyncio.to_thread(self.mm.join_channel, channel_id)
            logger.info("Auto-joined newly-created channel %s", channel_id)
        except Exception:
            self._self_joined_channels.discard(channel_id)
            logger.warning(
                "Failed to auto-join newly-created channel %s", channel_id,
                exc_info=True,
            )

    async def _on_mm_user_removed(self, channel_id: str, user_id: str) -> None:
        if user_id != self.mm.bot_user_id:
            return
        session_id = self.mapping.unlink(Anchor(channel_id))
        self.purpose_by_channel.pop(channel_id, None)
        self.warming_up_sessions.pop(channel_id, None)
        self._dormant_channels.discard(channel_id)
        # Drain per-channel config state so a removal mid-restart (before the
        # self-write's `channel_updated` event arrives) doesn't leak an entry.
        self._self_written_purpose.pop(channel_id, None)
        self._awaiting_first_forward.discard(channel_id)
        self._pending_initial_catch_up.pop(channel_id, None)
        self._forget_channel_silent_drops(channel_id)
        self._held.forget_channel(channel_id)
        if session_id:
            self._held_probe_ts.pop(session_id, None)
            self._end_tool_use_run(session_id)
            self.posters.forget(session_id)
            self._session_triggerer.pop(session_id, None)
            self._recent_harness_sends.pop(session_id, None)
            if self.typing:
                await self.typing.stop(session_id)
        logger.info("Bot removed from channel %s (session %s unlinked)",
                    channel_id, (session_id or "")[:8])

    async def _on_mm_channel_updated(self, channel: dict) -> None:
        channel_id = channel.get("id")
        if not channel_id:
            return
        prev = self.last_channel_state.get(channel_id, {})
        new_display = channel.get("display_name", "") or ""
        new_purpose = channel.get("purpose", "") or ""
        self.last_channel_state[channel_id] = {
            "display_name": new_display, "purpose": new_purpose,
        }

        # Purpose change (only emit notice if actually changed AND we didn't
        # write it ourselves). A single self-write may fan out into more than
        # one `channel_updated` event, so match against the pending SET and
        # drain the matched entry rather than a lone slot.
        purpose_changed = bool(prev and prev.get("purpose") != new_purpose)
        if purpose_changed:
            pending = self._self_written_purpose.get(channel_id)
            if pending is not None and new_purpose in pending:
                pending.discard(new_purpose)
                if not pending:
                    self._self_written_purpose.pop(channel_id, None)
                return

            if channel_id in self._dormant_channels:
                try:
                    await self._load_channel_config(channel_id, force=True)
                except Exception:
                    logger.warning(
                        "Failed to refresh dormant config after Purpose change in %s",
                        channel_id,
                        exc_info=True,
                    )
                return

        session_id = self.mapping.get_session(Anchor(channel_id))
        if not session_id:
            return

        if purpose_changed:
            try:
                self.mm.post_message(
                    channel_id,
                    "_Channel Purpose changed — this takes effect only for new "
                    "sessions. Start a new channel (or thread) to use these "
                    "settings._",
                )
            except Exception:
                logger.warning("Failed to post purpose-change notice", exc_info=True)

    # ─────────────────────── Invite → new session ──────────────────────────

    async def _start_invited_session(
        self,
        channel_id: str,
        *,
        initial_message: str | None = None,
        awaits_first_message: bool = True,
        post_welcome: bool = True,
        exclude_post_id: str | None = None,
        post: dict | None = None,
    ) -> None:
        # Reserve the channel-id slot first so any MM post that lands while
        # we're still fetching channel metadata / model lists / catch-up
        # transcript is queued instead of dropped. The entry is cleared on
        # the success path (after the run starts) and on every error
        # branch below.
        if channel_id not in self.warming_up_sessions:
            self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)

        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            self.warming_up_sessions.pop(channel_id, None)
            logger.exception("Failed to fetch channel %s on invite", channel_id)
            return
        purpose_text = ch.get("purpose", "") or ""
        display_name = (ch.get("display_name") or "").strip() or None

        # purpose.parse takes a sync callable; preload model lists so the
        # callable can just dict-lookup.
        models_by_backend: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models_by_backend[b] = await self.harness.list_backend_models(b)
            except Exception:
                models_by_backend[b] = []

        cfg = purpose.parse(
            purpose_text,
            self.config.default_backend,
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )

        effective_cwd = self._resolve_purpose_cwd(cfg)

        for w in cfg.warnings:
            try:
                self.mm.post_message(channel_id, f":warning: {w}")
            except Exception:
                logger.debug("posting purpose warning failed", exc_info=True)

        # The deferred branch remains for compatibility with callers that need
        # a mapped-but-quiet session. Dormant engagement and replacement flows
        # have a real initial message and send it with catch-up prepended.
        effective_initial: str | None = None
        self._pending_initial_catch_up.pop(channel_id, None)
        if awaits_first_message:
            catch_up = self._initial_catch_up_block(
                channel_id, exclude_post_id=exclude_post_id,
            )
            if catch_up:
                self._pending_initial_catch_up[channel_id] = catch_up
        else:
            assert initial_message is not None
            effective_initial = self._prepend_catch_up(
                channel_id, initial_message, exclude_post_id=exclude_post_id,
            )
            # Download attachments on the session-CREATING message so the
            # opening turn includes them. The forward path
            # (``_forward_user_post``) only runs for posts to an EXISTING
            # session, so without this a file attached to the very first
            # message in a channel — or to the message that adopts an
            # unreachable external session — is silently dropped. The files
            # land in ``effective_cwd`` (the cwd this session is created with),
            # matching where ``_forward_user_post`` writes for live sessions.
            if post and post.get("file_ids"):
                if effective_cwd:
                    notes = await self._save_mm_attachments(post, effective_cwd)
                else:
                    notes = ["[MM attachment skipped: no project path for session]"]
                if notes:
                    notes_block = "\n".join(notes)
                    effective_initial = (
                        f"{notes_block}\n\n{effective_initial}"
                        if effective_initial else notes_block
                    )

        # ``warming_up_sessions[channel_id]`` was set at function entry so
        # any race-arriving MM post is queued; reuse that entry here.
        self.purpose_by_channel[channel_id] = cfg
        session_id: str | None = None
        queued: WarmingUpChannel | None = None

        try:
            session = await self.harness.create_session(
                backend=cfg.backend,
                model=self._resolve_session_model(cfg),
                cwd=effective_cwd,
                title=display_name,
            )
            session_id = session.get("id")
            if not session_id:
                raise RuntimeError("agent-harness create_session response missing id")
            self.mapping.link(Anchor(channel_id), session_id)
            self._dormant_channels.discard(channel_id)
            self._known_sessions.add(session_id)
            if awaits_first_message:
                self._awaiting_first_forward.add(channel_id)
            else:
                assert effective_initial is not None
                run = await self.harness.create_run(session_id, effective_initial)
                self._track_run_response(session_id, run)
                self._record_harness_send(session_id, effective_initial)
            await self._update_resume_purpose(
                channel_id, session_id, cfg.backend, effective_cwd,
            )
        except Exception as exc:
            logger.exception("Failed to create agent-harness session for channel %s", channel_id)
            self.purpose_by_channel.pop(channel_id, None)
            self._awaiting_first_forward.discard(channel_id)
            self._pending_initial_catch_up.pop(channel_id, None)
            try:
                self.mm.post_message(
                    channel_id,
                    format_backend_error(
                        "start a session", cfg.backend, exception_detail(exc),
                    ),
                )
            except Exception:
                logger.debug("Failed to post session-start error", exc_info=True)
            return
        finally:
            queued = self.warming_up_sessions.pop(channel_id, None)

        if post_welcome:
            welcome = self._format_welcome(cfg, effective_cwd)
            try:
                self.mm.post_message(channel_id, welcome)
            except Exception:
                logger.warning("Failed to post welcome message", exc_info=True)

        if session_id and queued and queued.queued_posts:
            await self._flush_queued(channel_id, queued.queued_posts)

    def _resolve_cwd(self, cfg: purpose.PurposeConfig) -> tuple[str, bool]:
        """Return ``(effective_cwd, rejected)`` for ``cfg``.

        ``effective_cwd`` is a Channel Purpose ``cwd=`` validated against
        ``allowed_attachment_roots`` (with no roots configured we trust the
        caller, same as ``resolve_attachment_path``), else
        ``config.default_cwd``. ``rejected`` is True only when a *supplied*
        ``cwd=`` fell outside the allowed roots and was replaced by the
        default. Pure — no warnings, no logging, no mutation of ``cfg`` — so
        the warning-emitting create path and read-only ``.status`` can share
        one resolution and never disagree.
        """
        if not cfg.cwd:
            return self.config.default_cwd, False
        resolved = resolve_attachment_path(
            cfg.cwd, project_path=None,
            allowed_roots=self.config.allowed_attachment_roots,
        )
        if resolved is None:
            return self.config.default_cwd, True
        return str(resolved), False

    def _resolve_purpose_cwd(self, cfg: purpose.PurposeConfig) -> str:
        """Effective cwd for the session-create paths.

        Delegates resolution to :meth:`_resolve_cwd` and additionally records
        a channel-visible warning (and a log line) when a supplied ``cwd=``
        override is rejected, so the channel learns the override didn't take.
        """
        cwd, rejected = self._resolve_cwd(cfg)
        if rejected:
            cfg.warnings.append(
                f"Channel Purpose `cwd={cfg.cwd}` is not inside any allowed_attachment_root "
                f"— using default `{self.config.default_cwd}`."
            )
            logger.warning(
                "Rejected Channel Purpose cwd=%r (not in allowed_attachment_roots)", cfg.cwd,
            )
        elif cfg.cwd:
            logger.info("Channel Purpose cwd override → %s", cwd)
        return cwd

    # ─────────────────── Session reconfig + runtime toggle ─────────────────

    def _merge_configs(
        self,
        current: purpose.PurposeConfig | None,
        new: purpose.PurposeConfig,
    ) -> purpose.PurposeConfig:
        """Layer `new` on top of `current`. Any field explicitly set by the
        new parse wins; omitted fields fall back to current.

        Exception: when the backend changes, the carried model is dropped.
        Models are backend-specific (``sonnet`` is claude-only, ``gpt-5.5``
        is codex-only) — letting one leak across a backend swap crashes
        session-create on the new backend. With ``model=None`` the
        per-backend default in ``Config.default_models`` kicks in at
        ``_resolve_session_model`` time.
        """
        if current is None:
            return new
        backend_changed = new.backend != current.backend
        if new.model is not None:
            model = new.model
        elif backend_changed:
            model = None
        else:
            model = current.model
        return purpose.PurposeConfig(
            backend=new.backend,
            model=model,
            mention_only=new.mention_only,
            cwd=new.cwd if new.cwd is not None else current.cwd,
            warnings=[],
        )

    async def _replace_external_session(
        self,
        channel_id: str,
        old_session_id: str,
        post: dict,
        message: str,
    ) -> None:
        """Adopt a channel currently mapped to an ``origin: external``
        harness session.

        Externally-launched sessions (e.g. a terminal ``claude``/``codex`` the
        harness only *observes*, or one created directly against the harness
        API) appear in ``GET /v1/sessions`` but the harness has no stdin / IPC
        channel into them — ``POST /v1/sessions/<id>/runs``
        returns 200 but the message goes nowhere. The user's current MM
        post would silently vanish.

        Recovery: drop the stale mapping, post a brief adoption notice in
        the channel, and start a fresh harness-origin session whose first
        turn is the user's actual message. Channel scrollback stays
        visible as context for the human; the new session gets the bridge's
        existing catch-up preamble via ``_start_invited_session``.
        """
        logger.info(
            "Adopting channel %s — replacing external session %s with a fresh harness session",
            channel_id, old_session_id[:12],
        )
        # Unlink the old mapping up front so concurrent MM posts hit the
        # ``warming_up_sessions`` queue rather than re-entering this path
        # against the same dead session. Per-session bookkeeping for the
        # old id is safe to drop now: even if replacement fails, the old
        # session is by definition unreachable from MM.
        # Everything from here to the relink runs with the anchor
        # session-less. Protect its held posts across that window BEFORE
        # the first await, or the sweep abandons the backlog mid-adoption.
        async with self._anchor_relink_window(Anchor(channel_id)):
            self.mapping.unlink(Anchor(channel_id))
            self._end_tool_use_run(old_session_id)
            self.posters.forget(old_session_id)
            self._forget_channel_silent_drops(channel_id)
            self._session_triggerer.pop(old_session_id, None)
            self._recent_harness_sends.pop(old_session_id, None)
            self._external_sessions.discard(old_session_id)
            if self.typing:
                await self.typing.stop(old_session_id)
            try:
                self.mm.post_message(
                    channel_id,
                    ":arrows_counterclockwise: Previous session is no longer reachable from "
                    "Mattermost. Starting a fresh session for this channel.",
                )
            except Exception:
                logger.debug("Failed to post adoption notice", exc_info=True)
            await self._start_invited_session(
                channel_id,
                initial_message=message,
                awaits_first_message=False,
                post_welcome=False,
                exclude_post_id=post.get("id"),
                post=post,
            )

            # Commit the "old session is gone" state ONLY if the replacement
            # actually produced a fresh mapping. ``_start_invited_session``
            # logs+returns on harness/MM failure without raising and without
            # linking; if we'd ``mark_adopted``'d the old id eagerly, the
            # next bootstrap would skip auto-recovery for the still-mapped
            # channel and the operator would have to surgically edit
            # ``state.json`` to recover.
            new_session_id = self.mapping.get_session(Anchor(channel_id))
            if new_session_id and new_session_id != old_session_id:
                self.mapping.mark_adopted(old_session_id)
                # Stop the bootstrap recovery path from re-spawning a fresh
                # channel for this session id on the next restart.
                self._known_sessions.add(old_session_id)
            else:
                # Replacement failed — restore the old mapping so the next MM
                # post re-enters this path and retries. ``_start_invited_session``
                # has already posted a user-visible warning on its own error path.
                self.mapping.link(Anchor(channel_id), old_session_id)
                self._external_sessions.add(old_session_id)
                logger.warning(
                    "Replacement of external session %s failed — restored old "
                    "mapping for retry on next MM post",
                    old_session_id[:12],
                )

        # The anchor is linked again — to the replacement, or restored to the
        # old session. Either way it has an owner now, so deliver anything
        # that was held across the swap.
        if self.mapping.get_session(Anchor(channel_id)):
            await self._flush_held(Anchor(channel_id))

    async def _restart_session_with_config(
        self,
        channel_id: str,
        old_session_id: str,
        cfg: purpose.PurposeConfig,
    ) -> str | None:
        """Tear down the current session for `channel_id` and start a new one.

        We keep the MM channel and mapping slot; only the harness session is
        replaced. The old session is abandoned (no explicit backend delete).
        The new session starts QUIET — no greeting run — because the only
        callers (`.model` / `.backend` / `.cwd`) post their own confirmation;
        the next user message is the new session's first turn.

        Returns the new session id on success, or ``None`` if the restart
        failed — in which case the channel's prior (still-live) session
        mapping and config are restored so it isn't left session-less, and a
        ``:warning:`` is posted. Callers must not claim success on ``None``.
        """
        previous_cfg = self.purpose_by_channel.get(channel_id)
        # Same unlink -> await -> relink window as the adoption path: the
        # anchor has no session across `typing.stop` and `create_session`,
        # and `create_session` routinely outlives one sweep interval.
        async with self._anchor_relink_window(Anchor(channel_id)):
            self.mapping.unlink(Anchor(channel_id))
            self._end_tool_use_run(old_session_id)
            self.posters.forget(old_session_id)
            self._forget_channel_silent_drops(channel_id)
            self._session_triggerer.pop(old_session_id, None)
            self._recent_harness_sends.pop(old_session_id, None)
            if self.typing:
                await self.typing.stop(old_session_id)
            effective_cwd = self._resolve_purpose_cwd(cfg)

            self.warming_up_sessions[channel_id] = WarmingUpChannel(channel_id)
            self.purpose_by_channel[channel_id] = cfg
            session_id: str | None = None

            try:
                session = await self.harness.create_session(
                    backend=cfg.backend,
                    model=self._resolve_session_model(cfg),
                    cwd=effective_cwd,
                )
                session_id = session.get("id")
                if not session_id:
                    raise RuntimeError("agent-harness create_session response missing id")
                self.mapping.link(Anchor(channel_id), session_id)
                self._known_sessions.add(session_id)
                # No greeting/warming run: a restart is triggered by `.model` /
                # `.backend`, which post their own confirmation. Firing the
                # INVITE_PLACEHOLDER prompt here would burn a run and post a
                # spurious "Hi! I'm set up…" greeting. The next real user message
                # becomes the new session's first run.
                await self._update_resume_purpose(
                    channel_id, session_id, cfg.backend, effective_cwd,
                )
            except Exception as exc:
                logger.exception("Failed to restart agent-harness session for %s", channel_id)
                # Restore the prior mapping/config so the channel keeps talking to
                # its old (still-live) session instead of being orphaned — a lost
                # session would silently drop every subsequent message.
                self.mapping.link(Anchor(channel_id), old_session_id)
                if previous_cfg is not None:
                    self.purpose_by_channel[channel_id] = previous_cfg
                else:
                    self.purpose_by_channel.pop(channel_id, None)
                try:
                    self.mm.post_message(
                        channel_id,
                        format_backend_error(
                            "restart the session", cfg.backend, exception_detail(exc),
                        ),
                    )
                except Exception:
                    logger.debug("Failed to post session-restart error", exc_info=True)
                session_id = None
            finally:
                queued = self.warming_up_sessions.pop(channel_id, None)
        # Held posts predate the restart and are still buffered under this
        # anchor. Now that it points at the replacement (or, on failure, back
        # at the old session), deliver them there rather than waiting up to a
        # sweep interval. Flushed BEFORE the warming queue because they are
        # OLDER than anything that arrived during the restart — arrival order
        # is what the coalesced turn promises.
        if self.mapping.get_session(Anchor(channel_id)):
            await self._flush_held(Anchor(channel_id))
        # On success, posts target the replacement. On failure, the old
        # mapping above was restored, so re-dispatch there instead of dropping
        # anything that arrived during the failed restart.
        if (
            self.mapping.get_session(Anchor(channel_id))
            and queued
            and queued.queued_posts
        ):
            await self._flush_queued(channel_id, queued.queued_posts)
        return session_id

    def _backend_for_channel(self, channel_id: str) -> str | None:
        """Backend name configured for ``channel_id``, or ``None`` if unknown.

        Read from the cached Channel Purpose config. Used to name the backend
        in surfaced error messages; ``None`` (external/observer channels with
        no cached config) degrades the template to a generic "the backend".
        """
        cfg = self.purpose_by_channel.get(channel_id)
        return cfg.backend if cfg else None

    def _resolve_session_model(self, cfg: purpose.PurposeConfig) -> str | None:
        """Pick the model to send to ``harness.create_session``.

        Operators who set a model token in Channel Purpose win. Otherwise we
        fall back to the per-backend default (``opus`` for claude,
        ``gpt-5.5`` for codex; ``None`` for backends without an explicit
        default — letting the harness pick). This is the only place that
        knows the backend → model mapping; the parser layer always sees
        ``default_model=None`` so a claude default never leaks into a codex
        session.
        """
        if cfg.model:
            return cfg.model
        return self.config.default_model_for(cfg.backend)

    async def _models_for_known_backends(self) -> dict[str, list[str]]:
        models: dict[str, list[str]] = {}
        for b in purpose.KNOWN_BACKENDS:
            try:
                models[b] = await self.harness.list_backend_models(b)
            except Exception:
                models[b] = []
        return models

    async def _load_channel_config(
        self, channel_id: str, *, force: bool = False,
    ) -> purpose.PurposeConfig:
        """Return and cache the effective Channel Purpose configuration."""
        cached = self.purpose_by_channel.get(channel_id)
        if cached is not None and not force:
            return cached
        ch = await asyncio.to_thread(self.mm.get_channel, channel_id)
        self.last_channel_state.setdefault(channel_id, {
            "display_name": ch.get("display_name", "") or "",
            "purpose": ch.get("purpose", "") or "",
        })
        models_by_backend = await self._models_for_known_backends()
        cfg = purpose.parse(
            ch.get("purpose", "") or "",
            self.config.default_backend,
            default_model=None,
            available_models_for=lambda b: models_by_backend.get(b, []),
            default_autorespond=self.config.default_autorespond,
        )
        self.purpose_by_channel[channel_id] = cfg
        return cfg

    async def _config_for_update(
        self, channel_id: str, command: str,
    ) -> purpose.PurposeConfig | None:
        """Channel config for a dot-command that rewrites and persists it.

        A cold ``purpose_by_channel`` is the ORDINARY state for a mapped
        channel after a daemon restart: ``_bootstrap_known_sessions`` doesn't
        populate it, and the dot-command preload only covers dormant channels.
        So `.model` / `.backend` / `.cwd` / `.autorespond` must read the
        Channel Purpose rather than rebuild the config from defaults — that
        would silently rewrite every setting the command isn't about (the
        channel's directory, its autorespond flag, its backend and model)
        and then persist the result, while the confirmation names only the
        one thing the user asked to change.

        Returns ``None`` when the Purpose can't be read; callers refuse via
        :meth:`_refuse_update_unknown_config`.
        """
        try:
            return await self._load_channel_config(channel_id)
        except Exception:
            logger.exception(
                "`%s` could not load the channel config for %s", command, channel_id,
            )
            return None

    def _refuse_update_unknown_config(
        self, channel_id: str, thread_root: str | None, keeping: str,
    ) -> None:
        """Refuse a config change we couldn't read the current config for.

        Writing invented values would hand the channel a configuration the
        user never chose — and ``_persist_purpose`` needs the same channel
        read that just failed, so it would persist with an empty resume
        section, wiping that block too.
        """
        self._post_cmd_reply(
            channel_id,
            ":warning: Could not read this channel's configuration — the "
            f"settings I'd have to carry over are stored there. {keeping}.",
            thread_root,
        )

    def _live_backend(
        self, meta: dict, cfg: purpose.PurposeConfig | None,
    ) -> str:
        """The backend this session is actually running on.

        Harness meta wins over the Channel Purpose. Channels created *around*
        an existing session (`mm-bridge spawn`, `.invite <id>`, bootstrap
        discovery) get a Purpose of "agent-harness session <id>" — no config
        tokens — which loads fine and parses to plain defaults, so trusting it
        would recreate a codex session on claude and persist the flip. The
        Purpose still owns what meta doesn't carry: cwd and the autorespond
        flag.
        """
        return purpose.canonical_backend(
            meta.get("backend") or (cfg.backend if cfg else None)
        ) or self.config.default_backend

    async def _run_runtime_toggle(
        self, channel_id: str, token: str, current: purpose.PurposeConfig,
    ) -> None:
        """Flip the channel's mention_only flag from an autorespond token.

        The sole caller is ``_cmd_autorespond`` (the `.autorespond` command),
        which passes the literal ``autorespond`` / ``noautorespond`` token and
        the channel's current config (see ``_config_for_update`` — everything
        but the flag is carried through to the persisted Purpose untouched).
        """
        turn_on_autorespond = token.strip().lower() in purpose.AUTORESPOND_ALIASES
        updated = purpose.PurposeConfig(
            backend=current.backend,
            model=current.model,
            mention_only=not turn_on_autorespond,
            cwd=current.cwd,
            warnings=[],
        )
        self.purpose_by_channel[channel_id] = updated
        self._persist_purpose(channel_id, updated)
        note = (
            ":loud_sound: _Autorespond on — I'll reply to every message._"
            if turn_on_autorespond
            else ":mute: _Mention-only — @mention me to talk._"
        )
        try:
            self.mm.post_message(channel_id, note)
        except Exception:
            logger.debug("failed posting runtime-toggle confirmation", exc_info=True)

    def _persist_purpose(
        self, channel_id: str, cfg: purpose.PurposeConfig,
    ) -> None:
        """Write the canonical form of `cfg` back to the MM channel's Purpose.

        Preserves any trailing resume block below the section separator —
        that block is owned by ``_update_resume_purpose`` and lives
        independently of the config tokens. Without the preserve step,
        any operator-triggered config change (autorespond toggle, model
        swap) would silently strip the resume block until the next daemon
        restart.

        Marks the resulting `channel_updated` event as self-triggered so
        the bridge doesn't post a "purpose changed" notice to itself.
        """
        config_section = purpose.to_purpose_string(
            cfg, default_autorespond=self.config.default_autorespond,
        )
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            resume_section = ""
        else:
            _, resume_section = purpose.split_config_section(
                ch.get("purpose") or "",
            )
        serialized = purpose.join_sections(config_section, resume_section)
        self._note_self_wrote_purpose(channel_id, serialized)
        try:
            self.mm.set_channel_purpose(channel_id, serialized)
        except Exception:
            logger.warning(
                "Failed to persist Channel Purpose for %s", channel_id, exc_info=True,
            )

    def _note_self_wrote_purpose(self, channel_id: str, purpose_text: str) -> None:
        self._self_written_purpose.setdefault(channel_id, set()).add(purpose_text)

    def _format_welcome(self, cfg: purpose.PurposeConfig, cwd: str) -> str:
        parts = [
            f"*Session started — backend: `{cfg.backend}`",
            f"model: `{cfg.model or 'default'}`",
            f"cwd: `{cwd}`*",
        ]
        head = ", ".join(parts)
        hint = (
            "Hi! Reply to catch me up, or just start asking. "
            f"Use `@{self.mm.bot_username} catch up "
            f"{self.config.catch_up_default_n}` to include "
            f"the last {self.config.catch_up_default_n} messages."
        )
        if cfg.mention_only:
            hint = f"_mention-only mode — @mention me to talk._\n{hint}"
        config_hint = (
            "_Reconfigure anytime with `.model <name>` · `.backend <name>` · "
            "`.cwd <path>` · `.autorespond`; `.help` for the full list._"
        )
        return f"{head}\n\n{hint}\n\n{config_hint}"

    def _format_channel_join_welcome(
        self, cfg: purpose.PurposeConfig | None,
    ) -> str:
        """Render the channel-join welcome.

        Backend list comes from ``Config.default_models`` keys — only
        backends the operator has actually configured a default model for
        appear in the welcome (so unimplemented entries in
        ``KNOWN_BACKENDS`` don't get advertised to users). When ``cfg``
        is given (Purpose was parsed), append a one-line "this channel"
        summary so users see what they landed on.
        """
        configured = [
            (b, m) for b, m in sorted(self.config.default_models.items()) if m
        ]
        backends = (
            ", ".join(f"`{b}` (default `{m}`)" for b, m in configured)
            if configured else "none configured"
        )

        context = ""
        if cfg is not None:
            model = (
                cfg.model
                or self.config.default_models.get(cfg.backend)
                or "default"
            )
            flag = "mention-only" if cfg.mention_only else "autorespond"
            context = (
                f" _This channel: `{cfg.backend}` / `{model}` / {flag}._"
            )

        return CHANNEL_JOIN_WELCOME_TEMPLATE.format(
            bot=self.mm.bot_username,
            backends=backends,
            catch_up_n=self.config.catch_up_default_n,
            context=context,
        )

    async def _post_channel_join_welcome(self, channel_id: str) -> None:
        """Post the channel-join welcome. Best-effort, never raises.

        Idempotent at the call site: a re-add posts the welcome again on
        purpose — a re-invite implies fresh contact. The post is tagged
        with ``props.{CHANNEL_JOIN_WELCOME_PROP}=welcome`` so operators
        can filter / dedupe historically.
        """
        cfg: purpose.PurposeConfig | None = None
        try:
            cfg = await self._load_channel_config(channel_id, force=True)
        except Exception:
            logger.debug(
                "failed to derive cfg for join welcome on %s",
                channel_id, exc_info=True,
            )

        body = self._format_channel_join_welcome(cfg)
        try:
            self.mm.post(
                channel_id, body,
                props={CHANNEL_JOIN_WELCOME_PROP: CHANNEL_JOIN_WELCOME_PROP_VALUE},
            )
        except Exception:
            logger.warning(
                "failed to post channel-join welcome on %s",
                channel_id, exc_info=True,
            )

    # ─────────────────────── Forwarding user posts ─────────────────────────

    async def _forward_user_post(
        self,
        channel_id: str,
        session_id: str,
        post: dict,
        message: str,
        thread_root: str | None,
        first_message: bool = False,
    ) -> None:
        cfg = self.purpose_by_channel.get(channel_id)
        bot_mention = f"@{self.mm.bot_username}"

        if cfg and cfg.mention_only and bot_mention not in message:
            # Silently dropped for now; remember it so the next forwarded
            # message in this anchor can prepend it as catch-up context.
            self._enqueue_silent_drop(channel_id, thread_root, post)
            return

        # Hold rather than submit while this anchor's session is busy.
        #
        # Placed exactly here on purpose: AFTER the mention gate, because a
        # mention-only drop is a *drop* and must still reach `_silent_drops`
        # for the catch-up replay; and BEFORE the attachment download and the
        # mention strip, because R3 puts the download at flush time — no point
        # transferring a file for a post a later one supersedes.
        anchor = Anchor(channel_id, thread_root)
        if self._should_hold(anchor, session_id):
            if await self._hold_post(anchor, session_id, post, message):
                return
            # Overflow (R6): the buffer is full, so fall through to the
            # pre-coalescing path — an eager submit into the harness FIFO.
            # Loud, never silent; `_hold_post` has already logged it.

        # Strip the @-mention so the agent doesn't see a stray handle.
        cleaned = self._strip_bot_mention(message)

        # Download any inbound MM attachments into the session's cwd.
        attachment_notes: list[str] = []
        if post.get("file_ids"):
            try:
                cwd = await self._project_path_for(session_id)
            except Exception:
                logger.exception("project-path lookup failed for %s", session_id[:8])
                cwd = None
            if cwd:
                attachment_notes = await self._save_mm_attachments(post, cwd)
            else:
                attachment_notes = ["[MM attachment skipped: no project path for session]"]

        if not cleaned and not attachment_notes:
            return

        user_id = post.get("user_id", "")
        attribute = self.posters.note_post(session_id, user_id)
        if user_id:
            self._session_triggerer[session_id] = user_id
        if attribute and cleaned:
            username = self._resolve_username(user_id)
            cleaned = self.posters.format(cleaned, username, True)

        body = cleaned
        if attachment_notes:
            notes_block = "\n".join(attachment_notes)
            body = f"{notes_block}\n\n{body}" if body else notes_block

        await self._deliver_to_session(
            channel_id, session_id, body,
            thread_root=thread_root,
            first_message=first_message,
            exclude_post_id=post.get("id"),
            requeue_on_failure=[post],
        )

    async def _deliver_to_session(
        self,
        channel_id: str,
        session_id: str,
        body: str,
        *,
        thread_root: str | None,
        first_message: bool,
        exclude_post_id: str | None,
        requeue_on_failure: list[dict],
    ) -> str:
        """Prepend the pending context blocks to `body` and create the run.

        Shared tail of the two ways a message reaches the agent: a live
        forward (`_forward_user_post`) and a coalesced flush of held posts
        (`_deliver_held`). Everything the agent expects around a turn —
        the silent-drop catch-up block, the first-message preamble and its
        rollback on failure — lives here so both paths behave identically.

        Returns one of ``DELIVERY_OK`` / ``DELIVERY_FAILED`` /
        ``DELIVERY_UNSUPPORTED``. The distinction matters to the flush path:
        a transient failure means "keep the posts held and retry", while
        ``DELIVERY_UNSUPPORTED`` is permanent (that session can never accept
        a run) and retrying it forever would strand them.

        `requeue_on_failure` is the list of posts to preserve as silent drops
        if delivery fails — the live path passes the post it was forwarding,
        the flush path passes nothing because its posts are already durably
        buffered and requeueing would duplicate them.
        """
        silent_block, silent_key = await self._peek_silent_drops_as_block(
            channel_id, thread_root, session_id, exclude_post_id=exclude_post_id,
        )
        if silent_block:
            body = f"{silent_block}\n\n{body}" if body else silent_block

        first_catch_up: str | None = None
        if first_message:
            first_catch_up = self._pending_initial_catch_up.pop(channel_id, None)
            if first_catch_up:
                body = f"{first_catch_up}\n\n{body}" if body else first_catch_up
            preamble = await self._compute_first_message_preamble(channel_id)
            if preamble:
                body = f"{preamble}\n\n{body}" if body else preamble

            # Reserve the first-forward slot only after the message has passed
            # the mention/empty gates. Restore it if delivery fails.
            self._awaiting_first_forward.discard(channel_id)

        logger.info("MM → agent-harness [%s]: %s", session_id[:8], body[:80])
        self._record_harness_send(session_id, body)
        try:
            run = await self.harness.create_run(session_id, body)
            self._track_run_response(session_id, run)
        except HarnessResumeUnsupported:
            self._restore_first_message_state(
                channel_id, first_message, first_catch_up,
            )
            logger.warning("agent-harness resume unsupported for %s", session_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Can't resume this external session from Mattermost.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            for pending in requeue_on_failure:
                self._enqueue_silent_drop(channel_id, thread_root, pending)
            return DELIVERY_UNSUPPORTED
        except Exception as exc:
            self._restore_first_message_state(
                channel_id, first_message, first_catch_up,
            )
            logger.exception("Failed to create run for harness session %s", session_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    format_backend_error(
                        "run your message",
                        self._backend_for_channel(channel_id),
                        exception_detail(exc),
                    ),
                    root_id=thread_root,
                )
            except Exception:
                logger.debug("Failed to post run-delivery error", exc_info=True)
            # Preserve the previously-queued drops AND enqueue the
            # current post too: the user's actual request would
            # otherwise vanish with the failed send. The next mention
            # replays everything that wasn't delivered.
            for pending in requeue_on_failure:
                self._enqueue_silent_drop(channel_id, thread_root, pending)
            return DELIVERY_FAILED
        else:
            self._clear_silent_drops(silent_key)
            return DELIVERY_OK

    def _restore_first_message_state(
        self,
        channel_id: str,
        first_message: bool,
        first_catch_up: str | None,
    ) -> None:
        """Put the first-forward slot back after a failed delivery, so the
        next attempt still carries the MM-context preamble and catch-up."""
        if not first_message:
            return
        self._awaiting_first_forward.add(channel_id)
        if first_catch_up:
            self._pending_initial_catch_up[channel_id] = first_catch_up

    # ─────────────────────── Hold & coalesce ──────────────────────────────

    def _should_hold(self, anchor: Anchor, session_id: str) -> bool:
        """True when this post must wait rather than start its own run.

        Every condition is synchronous so the caller can decide and enqueue
        with no `await` in between. That atomicity is half of the race
        closure: the terminal-event handler pops `active_run_by_session`
        before *its* first await, so "checked live, but the flush had already
        run" cannot interleave. The other half is the post-enqueue re-check in
        `_hold_post` — atomicity says nothing about a tracker that is simply
        stale because a lifecycle event never arrived.
        """
        if not self.config.coalesce_posts:
            return False
        if anchor in self._flushing:
            # A flush is mid-loop for this anchor. Submitting now would race
            # a run ahead of the flush's own `create_run` and reorder the
            # conversation; the flush picks this post up on its next pass.
            return True
        return self._session_has_active_run(session_id)

    async def _hold_post(
        self,
        anchor: Anchor,
        session_id: str,
        post: dict,
        message: str,
    ) -> bool:
        """Buffer `post` for a later coalesced flush.

        Returns True when held, False on overflow — the caller then falls
        back to an eager submit (R6). The mention is stripped here rather
        than at flush time so `.queue` and `mm-bridge inbox` show exactly the
        text the agent will eventually receive.
        """
        stored = dict(post)
        stored["message"] = self._strip_bot_mention(message)
        held = held_posts.HeldPost(
            post=stored,
            username=self._resolve_username(post.get("user_id", "")),
            held_at_ms=int(post.get("create_at") or 0),
        )
        # --- atomic span: no await between `_should_hold` and this append ---
        accepted = self._held.add(anchor, held, session_id=session_id)
        # --- end atomic span ---
        if not accepted:
            logger.warning(
                "Hold buffer for %s is full (%d) — submitting post %s eagerly "
                "instead; it will run behind the live turn in the harness "
                "queue", anchor.channel_id, self.config.coalesce_max_held,
                held.post_id[:8],
            )
            return False

        logger.info(
            "Holding post %s for %s — %d post(s) now waiting on the live run",
            held.post_id[:8], session_id[:8], len(self._held.peek(anchor)),
        )
        await self._react_best_effort(held.post_id, HELD_REACTION, add=True)

        if anchor in self._flushing:
            # Held only because a flush is already looping; it will collect
            # this post itself. Probing the harness here would be wasted.
            return True

        # The tracker said "live". Ask the harness whether that is still
        # true: a lost terminal event, a harness restart or an SSE gap all
        # leave the tracker asserting a run that is already gone, and then
        # nothing would ever trigger a flush. One GET, paid only on the
        # (rare) hold path. `_active_run_is_alive` returns False on every
        # failure mode, so an unreachable harness flushes rather than
        # strands — and a flush that then fails keeps the posts held.
        self._held_probe_ts[session_id] = time.monotonic()
        if not await self._active_run_is_alive(session_id):
            logger.info(
                "Held a post for %s but its run is no longer alive — "
                "flushing immediately", session_id[:8],
            )
            await self._flush_held(anchor)
        return True

    @asynccontextmanager
    async def _anchor_relink_window(self, anchor: Anchor):
        """Protect `anchor`'s held posts while its session is being swapped.

        `_restart_session_with_config` (`.model` / `.backend` / `.cwd`) and
        `_replace_external_session` both unlink the anchor, then await —
        `typing.stop`, then `create_session` — before linking the
        replacement. Across that window `mapping.get_session(anchor)` is
        None, and `_sweep_held_anchors` runs every `typing_refresh_seconds`
        (3s by default). Without this marker the sweep reads the transient
        None as "the session went away" and abandons the whole backlog —
        loudly, so not a silent drop, but a drop. Session creation routinely
        takes longer than one sweep interval, and `.model` while a run is in
        flight is an ordinary operator move.

        Entered BEFORE the unlink so no await after it can be unprotected.
        Protecting slightly early is harmless: the anchor still points at its
        old session, and the only effect is that a flush waits for the swap
        to finish — which is what we want anyway, since the replacement is
        the conversation's new owner.
        """
        self._relinking.add(anchor)
        try:
            yield
        finally:
            self._relinking.discard(anchor)

    def _held_is_protected(self, anchor: Anchor) -> bool:
        """True while `anchor`'s held posts must not be flushed or abandoned."""
        return anchor in self._relinking

    async def _maybe_flush_held_for_session(self, session_id: str) -> None:
        """Flush the anchor bound to `session_id`, if it holds anything."""
        anchor = self.mapping.get_anchor(session_id)
        if anchor is None or not self._held.peek(anchor):
            return
        await self._flush_held(anchor)

    async def _flush_held(self, anchor: Anchor) -> None:
        """Deliver everything held for `anchor` as ONE coalesced run.

        Loops rather than delivering once: posts that arrive during the
        awaits of a delivery are held (see `_should_hold`) and must not be
        left behind, since the terminal event that would have flushed them
        has already been consumed.
        """
        if anchor in self._flushing:
            return
        self._flushing.add(anchor)
        try:
            while True:
                held = self._held.peek(anchor)
                if not held:
                    return
                if self._held_is_protected(anchor):
                    # A session swap is in progress for this anchor, so
                    # `get_session` is transiently None and the branch below
                    # would read that as "session gone" and abandon the
                    # backlog. Defer instead — the restart flushes to the
                    # replacement once it relinks.
                    #
                    # Checked INSIDE the loop, not just on entry: a swap can
                    # open while an earlier iteration is suspended in
                    # `_deliver_held` (a `create_run` plus 2N reaction round
                    # trips is seconds for a deep batch), and by then the
                    # entry check is long past. Checking here covers entry
                    # too — the first iteration runs immediately — so this is
                    # the only guard needed, and every caller inherits it:
                    # the terminal event, the watchdog, the post-enqueue
                    # probe and the sweep alike.
                    logger.debug(
                        "Deferring flush for %s — session swap in progress",
                        anchor.channel_id,
                    )
                    return
                session_id = self.mapping.get_session(anchor)
                if not session_id:
                    await self._abandon_held(anchor, held)
                    return
                if not await self._deliver_held(anchor, session_id, held):
                    return
        finally:
            self._flushing.discard(anchor)

    async def _deliver_held(
        self, anchor: Anchor, session_id: str, held: list[held_posts.HeldPost],
    ) -> bool:
        """Render and deliver one batch. True = keep looping.

        The batch is only discarded once `create_run` has actually
        succeeded, and then BY POST ID — not by dropping the anchor — so
        that (a) a failed delivery leaves the buffer intact for the next
        trigger to retry, and (b) posts that arrived during this delivery's
        awaits survive to be sent by the next iteration.
        """
        channel_id = anchor.channel_id
        cwd: str | None = None
        if any(h.post.get("file_ids") for h in held):
            try:
                cwd = await self._project_path_for(session_id)
            except Exception:
                logger.exception(
                    "project-path lookup failed while flushing held posts "
                    "for %s", session_id[:8],
                )
                cwd = None

        lines: list[str] = []
        delivered_ids: set[str] = set()
        last_user_id = ""
        for h in held:
            delivered_ids.add(h.post_id)
            notes: list[str] = []
            if h.post.get("file_ids"):
                # Downloaded HERE, not at hold time: a post that a later one
                # supersedes should never have cost a file transfer.
                notes = (
                    await self._save_mm_attachments(h.post, cwd) if cwd
                    else ["[MM attachment skipped: no project path for session]"]
                )
            # Keep the poster tracker accurate even though held posts are
            # attributed unconditionally — the next LIVE forward still asks
            # it whether to prefix a username.
            self.posters.note_post(session_id, h.user_id)
            line = h.render(notes)
            if not line:
                continue
            lines.append(line)
            if h.user_id:
                last_user_id = h.user_id

        if not lines:
            # Nothing renderable (e.g. a bare "@claude"). Drop them rather
            # than sending an empty turn, and loop in case more arrived.
            self._held.discard(anchor, delivered_ids)
            return True

        # The newest post governs, so its author is who's waiting on the
        # answer and who `_mention_triggerer_on_done` should ping.
        if last_user_id:
            self._session_triggerer[session_id] = last_user_id

        if len(lines) == 1:
            body = lines[0]
        else:
            body = "\n".join([
                HELD_BLOCK_HEADER.format(n=len(lines)),
                *lines,
                HELD_BLOCK_FOOTER,
            ])

        status = await self._deliver_to_session(
            channel_id, session_id, body,
            thread_root=anchor.root_id,
            first_message=channel_id in self._awaiting_first_forward,
            exclude_post_id=None,
            requeue_on_failure=[],
        )

        if status == DELIVERY_OK:
            self._held.discard(anchor, delivered_ids)
            self._held_probe_ts.pop(session_id, None)
            for h in held:
                await self._react_best_effort(h.post_id, HELD_REACTION, add=False)
                await self._react_best_effort(
                    h.post_id, DELIVERED_REACTION, add=True,
                )
            return True

        if status == DELIVERY_UNSUPPORTED:
            # Permanent: this session can never accept a run, so retrying
            # would strand the posts and re-post the warning on every sweep.
            # Hand them to the silent-drop queue, where the next successful
            # forward replays them verbatim as catch-up — exactly what the
            # single-post path does today, applied to the whole batch.
            #
            # KNOWN DURABILITY DOWNGRADE: `_silent_drops` is in-memory, so a
            # restart between here and that next forward loses them. Accepted
            # because it matches today's contract for this exact failure, and
            # because keep-held would either nag every sweep interval forever
            # or need a new frozen-hold lifecycle. See spec requirements §3.10.
            self._held.discard(anchor, delivered_ids)
            if self.config.initial_catch_up_n > 0:
                logger.warning(
                    "Session %s can't accept runs — moving %d held post(s) to "
                    "the catch-up queue", session_id[:8], len(held),
                )
                for h in held:
                    self._enqueue_silent_drop(channel_id, anchor.root_id, h.post)
                    await self._react_best_effort(
                        h.post_id, HELD_REACTION, add=False,
                    )
            else:
                # `initial_catch_up_n <= 0` disables the silent-drop queue,
                # so there is nowhere to park them. Name them instead — a
                # disabled catch-up must not turn a permanent delivery
                # failure into a silent loss.
                await self._announce_dropped_held(
                    anchor, held,
                    f"the session ({session_id[:8]}) can no longer accept "
                    "messages",
                )
            return False

        # Transient failure: keep them held. The next terminal event, probe
        # or sweep retries, and the sweep is guaranteed to come around.
        logger.warning(
            "Flush of %d held post(s) for %s failed — keeping them buffered "
            "for the next retry", len(held), session_id[:8],
        )
        return False

    async def _abandon_held(
        self, anchor: Anchor, held: list[held_posts.HeldPost],
    ) -> None:
        """Give up on an anchor whose session is gone — loudly, by name.

        There is nobody left to deliver to and retrying forever would be a
        silent strand, so the posts are dropped and the buffer cleared.
        """
        self._held.clear(anchor)
        await self._announce_dropped_held(
            anchor, held, "this channel's session went away",
        )

    async def _announce_dropped_held(
        self, anchor: Anchor, held: list[held_posts.HeldPost], reason: str,
    ) -> None:
        """Report held posts that will never be delivered — by author and
        timestamp, in the log AND in the channel.

        The one rule the hold buffer must never break is "no silent drop", so
        every path that gives up on a batch comes through here. Callers own
        clearing the buffer; this only reports.
        """
        summary = ", ".join(
            f"{h.timestamp()} {h.username}" for h in held
        ) or "(none)"
        logger.warning(
            "Dropping %d held post(s) for %s%s — %s: %s",
            len(held), anchor.channel_id,
            f":{anchor.root_id[:8]}" if anchor.root_id else "",
            reason, summary,
        )
        try:
            self.mm.post(
                anchor.channel_id,
                f":warning: {len(held)} held message(s) could not be "
                f"delivered because {reason}: {summary}.",
                root_id=anchor.root_id,
            )
        except Exception:
            logger.debug("Failed to post dropped-holds notice", exc_info=True)
        # Through `_react_best_effort` like every other reaction: these are
        # blocking HTTP calls, and clearing a 50-deep anchor inline would
        # stall the event loop for the whole batch.
        for h in held:
            await self._react_best_effort(h.post_id, HELD_REACTION, add=False)

    async def _announce_held_before_leaving(
        self, anchor: Anchor, reason: str,
    ) -> None:
        """Name an anchor's held posts on the way out, if it has any.

        Teardown forgets held posts silently, which is right for
        `_on_mm_user_removed` (nobody left to tell) but wrong for a
        deliberate `.leave`: the operator chose to drop a backlog and should
        see whose messages went with it. No-op on an empty buffer, so an
        ordinary leave stays quiet.
        """
        held = self._held.peek(anchor)
        if held:
            await self._announce_dropped_held(anchor, held, reason)

    async def _sweep_held_anchors(self) -> None:
        """Reconcile held anchors whose run lifecycle events went missing,
        and retry flushes that failed.

        Deliberately independent of the typing watchdog's sweep list. A
        session whose `run.started` was lost never enters
        `active_run_by_session`, so `_typing_watchdog_tick` takes its
        silence-stop branch, cancels the typing loop, and the session leaves
        `running_sessions()` *while its run is still alive* — after which a
        typing-scoped sweep would never look at it again. For the same reason
        there is no `if not self.typing` guard here: held posts must
        reconcile even with no typing indicator at all.

        Probe discipline mirrors the typing watchdog: at most one harness GET
        per session per `typing_stop_after_silence_seconds`, and only while a
        buffer is non-empty, so an idle daemon costs nothing.
        """
        if not len(self._held):
            return
        timeout = self.config.typing_stop_after_silence_seconds
        now = time.monotonic()
        for anchor in self._held.anchors():
            if anchor in self._flushing:
                continue  # a flush is already looping over this anchor
            session_id = self.mapping.get_session(anchor)
            if not session_id:
                await self._flush_held(anchor)  # → `_abandon_held`
                continue
            last = self._held_probe_ts.get(session_id)
            if last is not None and now - last <= timeout:
                continue
            self._held_probe_ts[session_id] = now
            if not await self._active_run_is_alive(session_id):
                logger.info(
                    "Sweep found no live run for %s with held posts — "
                    "flushing", session_id[:8],
                )
                await self._flush_held(anchor)

    async def _rehydrate_held(self) -> None:
        """Reload held posts from disk at startup and act on each anchor.

        Today an un-coalesced post to a busy session sits in the harness
        FIFO and survives a bridge restart, so holding it purely in memory
        would be a durability regression. Reloading restores parity; probing
        each anchor turns the restart into a *delivery* rather than a wait.
        """
        self._held.load()
        for anchor in self._held.anchors():
            session_id = self.mapping.get_session(anchor)
            if not session_id:
                await self._abandon_held(anchor, self._held.peek(anchor))
                continue
            if await self._active_run_is_alive(session_id):
                logger.info(
                    "Session %s still running after restart — keeping %d "
                    "held post(s) buffered", session_id[:8],
                    len(self._held.peek(anchor)),
                )
                continue
            await self._flush_held(anchor)

    async def _react_best_effort(
        self, post_id: str, emoji_name: str, *, add: bool,
    ) -> None:
        """Add/remove a reaction, swallowing every failure.

        Held-post visibility is a convenience; a Mattermost hiccup here must
        never affect whether a message reaches the agent.
        """
        if not post_id:
            return
        call = self.mm.add_reaction if add else self.mm.remove_reaction
        try:
            await asyncio.to_thread(call, post_id, emoji_name)
        except Exception:
            logger.debug(
                "Reaction %s %s on %s failed",
                "add" if add else "remove", emoji_name, post_id, exc_info=True,
            )

    def _resolve_username(self, user_id: str) -> str:
        try:
            u = self.mm.get_user(user_id)
            return u.get("username") or user_id[:8]
        except Exception:
            return user_id[:8]

    async def _compute_first_message_preamble(self, channel_id: str) -> str:
        """Build the MM-context preamble for the first forwarded user message.

        Returns an empty string if the MM lookups fail — we'd rather ship the
        user's message without a preamble than swallow it entirely.
        """
        try:
            ch = await asyncio.to_thread(self.mm.get_channel, channel_id)
        except Exception:
            logger.warning(
                "Failed to fetch channel %s for first-message preamble",
                channel_id, exc_info=True,
            )
            return ""
        channel_name = (
            ch.get("display_name") or ch.get("name") or channel_id
        ).strip() or channel_id

        try:
            members = await asyncio.to_thread(
                self.mm.get_channel_members, channel_id,
            )
        except Exception:
            logger.warning(
                "Failed to fetch members for %s (first-message preamble)",
                channel_id, exc_info=True,
            )
            members = []

        human_usernames: list[str] = []
        for m in members or []:
            uid = m.get("user_id")
            if not uid or uid == self.mm.bot_user_id:
                continue
            try:
                u = await asyncio.to_thread(self.mm.get_user, uid)
            except Exception:
                logger.debug(
                    "Failed to resolve user %s for preamble", uid, exc_info=True,
                )
                continue
            if u.get("is_bot"):
                continue
            uname = u.get("username")
            if uname:
                human_usernames.append(uname)

        return _format_first_message_preamble(channel_name, human_usernames)

    # ─────────────────────── Thread forks ──────────────────────────────────

    async def _handle_thread_post(
        self,
        channel_id: str,
        root_id: str,
        post: dict,
        message: str,
    ) -> None:
        if (channel_id, root_id) in self.dead_threads:
            return

        thread_session = self.mapping.get_session(Anchor(channel_id, root_id))

        # Dot-commands inside a thread — same interception as the channel
        # path. Dispatched before the leave/fork logic so they never trigger
        # a fork or get forwarded; session-scoped ones resolve the thread
        # session (may be None → "no session" reply).
        parsed = commands.parse(message, mentions=self._command_mentions())
        if parsed is not None:
            # A dormant channel has no session (so no thread fork either): apply
            # the same privacy gate the channel path uses, so a bare global
            # command (`.sessions`/`.running`/`.invite`) replied to the welcome
            # in a thread can't leak operator-wide state without an @mention.
            # Mapped channels keep dispatching thread commands unconditionally.
            if channel_id in self._dormant_channels and not self._commands_allowed_here(
                channel_id, thread_session, message, parsed,
            ):
                return
            await self._dispatch_command(
                channel_id, thread_session, post, parsed, thread_root=root_id,
            )
            return

        # Leave command inside a thread only removes the thread mapping.
        if m := self._leave_cmd_re.match(message):
            await self._run_leave_command(
                channel_id, session_id=None, thread_root=root_id,
                reason=(m.group(1) or "").strip(),
            )
            return

        if thread_session:
            if cm := self._catch_up_re.match(message):
                await self._run_catch_up(channel_id, thread_session, root_id, cm)
                return
            if sm := self._stop_cmd_re.match(message):
                cfg = self.purpose_by_channel.get(channel_id)
                if sm.group("mention") or not (cfg and cfg.mention_only):
                    await self._run_stop_command(
                        channel_id, thread_session, thread_root=root_id,
                    )
                    return
            await self._forward_user_post(
                channel_id, thread_session, post, message, thread_root=root_id,
            )
            return

        # No mapping yet — try to fork.
        parent_session = self.mapping.get_session(Anchor(channel_id))
        if not parent_session:
            return  # thread in an unmapped channel

        parent_meta = await self.harness.get_session(parent_session) or {}
        cwd = (parent_meta.get("project") or {}).get("path") or self.config.default_cwd

        # Download attachments into the parent's cwd (the fork inherits it).
        attachment_notes: list[str] = []
        if post.get("file_ids") and cwd:
            attachment_notes = await self._save_mm_attachments(post, cwd)

        message_for_llm = message
        if attachment_notes:
            notes_block = "\n".join(attachment_notes)
            message_for_llm = f"{notes_block}\n\n{message}" if message else notes_block

        fork_message = self._wrap_thread_fork_message(channel_id, root_id, message_for_llm)

        try:
            resp = await self.harness.fork_session(parent_session, message=fork_message)
        except HarnessForkUnsupported as exc:
            self._mark_dead_thread(
                channel_id, root_id,
                f"Couldn't fork ({exc or 'unsupported'}).",
            )
            return
        except Exception:
            logger.exception("fork_session error for session %s", parent_session[:8])
            self._mark_dead_thread(channel_id, root_id,
                                   "Couldn't fork this conversation.")
            return

        session = resp.get("session") or {}
        session_id = session.get("id")
        if not session_id:
            logger.warning("fork_session response missing session id: %r", resp)
            self._mark_dead_thread(channel_id, root_id, "Couldn't fork this conversation.")
            return

        self.mapping.link(Anchor(channel_id, root_id), session_id)
        self._known_sessions.add(session_id)
        self._record_harness_send(session_id, fork_message)
        self._track_run_response(session_id, resp.get("run"))
        logger.info("Thread fork linked for %s:%s from parent %s → %s",
                    channel_id, root_id[:8], parent_session[:8], session_id[:8])
        try:
            self.mm.post(
                channel_id,
                ":information_source: _Forked conversation. The full history of the "
                "parent session up to its current state is included — not only up to "
                "the message you replied on._",
                root_id=root_id,
            )
        except Exception:
            logger.debug("Failed to post fork disclaimer", exc_info=True)

    def _wrap_thread_fork_message(
        self, channel_id: str, root_id: str, message: str,
    ) -> str:
        """Prefix the fork's initial message with thread context for the LLM."""
        quoted_root = ""
        try:
            root_post = self.mm.get_post(root_id)
            root_text = (root_post.get("message") or "").strip()
            if root_text:
                quoted_root = "\n".join(f"> {line}" for line in root_text.splitlines())
        except Exception:
            logger.debug("Failed to fetch thread-root post %s", root_id, exc_info=True)

        header = (
            "[Mattermost thread context] You are continuing the parent "
            "conversation in a Mattermost thread. The user replied to this "
            "message:\n\n"
            f"{quoted_root or '> (original message could not be retrieved)'}\n\n"
            "Their reply follows:\n\n"
        )
        return header + message

    def _mark_dead_thread(
        self, channel_id: str, root_id: str, reason_text: str,
    ) -> None:
        self.dead_threads.add((channel_id, root_id))
        try:
            self.mm.post(
                channel_id,
                f":warning: {reason_text} Reply in the main channel instead.",
                root_id=root_id,
            )
        except Exception:
            logger.warning("Failed to post dead-thread notice", exc_info=True)

    # ─────────────────────── Catch-up & leave ──────────────────────────────

    def _enqueue_silent_drop(
        self,
        channel_id: str,
        thread_root: str | None,
        post: dict,
    ) -> None:
        """Record a mention-only drop so the next forwarded message in
        this anchor can replay it as catch-up. Capped at
        ``config.initial_catch_up_n``; ``<= 0`` disables entirely."""
        cap = self.config.initial_catch_up_n
        if cap <= 0:
            return
        key = (channel_id, thread_root)
        q = self._silent_drops.get(key)
        if q is None or q.maxlen != cap:
            # Rebuild on cap change so runtime config tweaks apply.
            old = list(q) if q is not None else []
            q = deque(old[-cap:], maxlen=cap)
            self._silent_drops[key] = q
        q.append(post)

    async def _peek_silent_drops_as_block(
        self,
        channel_id: str,
        thread_root: str | None,
        session_id: str,
        *,
        exclude_post_id: str | None = None,
    ) -> tuple[str, tuple[str, str | None] | None]:
        """Render pending silent drops for ``(channel_id, thread_root)``
        as a catch-up block without mutating the queue. Downloads any
        attachments on queued posts into the session cwd so the replay
        carries the file the user uploaded before mentioning the bot.
        Returns ``(block_text, clear_key)``; callers should only pass
        ``clear_key`` to :meth:`_clear_silent_drops` after the forwarded
        message has been successfully delivered to agent-harness, so a transient
        send failure doesn't discard queued conversation.
        """
        key = (channel_id, thread_root)
        dropped = self._silent_drops.get(key)
        if not dropped:
            return "", None
        cwd: str | None = None
        if any(p.get("file_ids") for p in dropped):
            try:
                cwd = await self._project_path_for(session_id)
            except Exception:
                logger.exception(
                    "project-path lookup failed while replaying drops for %s",
                    session_id[:8],
                )
                cwd = None
        lines: list[str] = []
        for p in dropped:
            if exclude_post_id and p.get("id") == exclude_post_id:
                continue
            if self.mm.is_own_post(p.get("id", "")):
                continue
            if _is_mm_system_post(p):
                continue
            msg = (p.get("message") or "").strip()
            note_lines: list[str] = []
            if p.get("file_ids"):
                if cwd:
                    note_lines = await self._save_mm_attachments(p, cwd)
                else:
                    note_lines = ["[MM attachment skipped: no project path for session]"]
            if not msg and not note_lines:
                continue
            username = self._resolve_username(p.get("user_id", ""))
            rendered = "\n".join([*note_lines, msg] if msg else note_lines)
            lines.append(f"{username}: {rendered}")
        if not lines:
            return "", None
        return self._format_catch_up_block(lines), key

    def _clear_silent_drops(
        self, key: tuple[str, str | None] | None,
    ) -> None:
        if key is not None:
            self._silent_drops.pop(key, None)

    def _drain_silent_drops_matching(
        self,
        channel_id: str,
        thread_root: str | None,
        ids: set[str],
    ) -> None:
        """Remove only queue entries whose post id is in ``ids``.

        Used after an explicit ``@claude catch up`` so that a partial
        catch-up doesn't throw away queued messages it never surfaced.
        """
        if not ids:
            return
        key = (channel_id, thread_root)
        q = self._silent_drops.get(key)
        if q is None:
            return
        kept = [p for p in q if p.get("id", "") not in ids]
        if not kept:
            self._silent_drops.pop(key, None)
            return
        if len(kept) == len(q):
            return
        # Preserve the deque's original maxlen so the cap survives.
        self._silent_drops[key] = deque(kept, maxlen=q.maxlen)

    def _forget_channel_silent_drops(self, channel_id: str) -> None:
        """Drop every queued silent-drop entry for this channel (root
        and any threads). Called on teardown paths so drops from a
        previous session don't leak into the next one mapped here."""
        for key in [k for k in self._silent_drops if k[0] == channel_id]:
            self._silent_drops.pop(key, None)

    def _forget_thread_silent_drops(
        self, channel_id: str, thread_root: str,
    ) -> None:
        self._silent_drops.pop((channel_id, thread_root), None)

    def _collect_catch_up_lines(
        self,
        channel_id: str,
        n: int,
        *,
        exclude_post_id: str | None = None,
    ) -> tuple[list[str], set[str]]:
        """Fetch up to `n` user messages from a channel, oldest-first, as
        `username: message` lines. Returns ``(lines, included_post_ids)``
        — the id set lets callers scope queue cleanup to only the posts
        that were actually surfaced. Skips bot posts, system posts, and
        any `@claude catch up` commands themselves.

        The `user_id == bot_user_id` filter is intentionally blanket: on
        a restart `is_own_post()` is empty-set, so using it here would
        let prior replies of this same bridge leak into catch-up context
        and confuse the model with its own earlier output. Cross-bot
        posts (sibling sessions, etc.) are already handled live by the
        dispatcher in `mm_client._dispatch_event`; history replay only
        needs to surface user-authored messages.
        """
        try:
            posts = self.mm.get_posts(channel_id, max(n + 2, 1))
        except Exception:
            logger.exception("get_posts failed for catch-up")
            return [], set()

        lines: list[str] = []
        included_ids: set[str] = set()
        for p in posts:
            if exclude_post_id and p.get("id") == exclude_post_id:
                continue
            if p.get("user_id") == self.mm.bot_user_id:
                continue
            if _is_mm_system_post(p):
                continue
            if self._catch_up_re.match((p.get("message") or "").strip()):
                continue
            username = self._resolve_username(p.get("user_id", ""))
            lines.append(f"{username}: {p.get('message', '')}")
            pid = p.get("id")
            if pid:
                included_ids.add(pid)
            if len(lines) >= n:
                break
        return lines, included_ids

    @staticmethod
    def _format_catch_up_block(lines: list[str]) -> str:
        return (
            f"[Catch-up context — last {len(lines)} messages from this channel, oldest first]\n"
            + "\n".join(lines) + "\n[End of catch-up]"
        )

    def _prepend_catch_up(
        self,
        channel_id: str,
        initial_message: str,
        *,
        exclude_post_id: str | None = None,
    ) -> str:
        """Prepend the channel's recent history to `initial_message` when the
        `initial_catch_up_n` config is enabled and there's actual history to
        quote. Returns `initial_message` unchanged when the block is empty.
        """
        block = self._initial_catch_up_block(
            channel_id, exclude_post_id=exclude_post_id,
        )
        if not block:
            return initial_message
        return f"{block}\n\n{initial_message}"

    def _initial_catch_up_block(
        self,
        channel_id: str,
        *,
        exclude_post_id: str | None = None,
    ) -> str:
        """Return invite/engagement history as a block, or an empty string."""
        n = self.config.initial_catch_up_n
        if n <= 0:
            return ""
        lines, _ = self._collect_catch_up_lines(
            channel_id, n, exclude_post_id=exclude_post_id,
        )
        return self._format_catch_up_block(lines) if lines else ""

    async def _run_catch_up(
        self,
        channel_id: str,
        session_id: str,
        thread_root: str | None,
        match: re.Match,
    ) -> None:
        n_arg = match.group(1)
        n = int(n_arg) if n_arg else self.config.catch_up_default_n
        clamped = False
        if n > self.config.catch_up_max_n:
            n = self.config.catch_up_max_n
            clamped = True

        lines, included_ids = self._collect_catch_up_lines(channel_id, n)
        block = self._format_catch_up_block(lines)

        self._record_harness_send(session_id, block)
        try:
            run = await self.harness.create_run(session_id, block)
            self._track_run_response(session_id, run)
        except Exception as exc:
            logger.exception("Failed to send catch-up block")
            try:
                self.mm.post(
                    channel_id,
                    format_backend_error(
                        "send the catch-up context",
                        self._backend_for_channel(channel_id),
                        exception_detail(exc),
                    ),
                    root_id=thread_root,
                )
            except Exception:
                logger.debug("Failed to post catch-up error", exc_info=True)
            return

        # A successful explicit catch-up in the main channel supersedes the
        # invite-time history snapshot. Keep `_awaiting_first_forward` so the
        # first conversational turn still receives its MM-context preamble,
        # but don't inject the same history a second time.
        if thread_root is None:
            self._pending_initial_catch_up.pop(channel_id, None)

        # Explicit catch-up already surfaced these messages from MM
        # history; drop only the queue entries that were actually
        # replayed. A smaller catch-up (e.g. `catch up 1`) or one
        # outside the queue's window must leave the rest intact so the
        # next mention still sees the missed conversation.
        self._drain_silent_drops_matching(
            channel_id, thread_root, included_ids,
        )

        note = (
            f":arrows_counterclockwise: Sent the last {len(lines)} messages as context."
        )
        if clamped:
            note += f" (Clamped to {self.config.catch_up_max_n}.)"
        try:
            self.mm.post(channel_id, note, root_id=thread_root)
        except Exception:
            pass

    async def _run_leave_command(
        self,
        channel_id: str,
        session_id: str | None,
        thread_root: str | None,
        reason: str,
    ) -> None:
        if thread_root:
            removed = self.mapping.unlink(Anchor(channel_id, thread_root))
            self.dead_threads.add((channel_id, thread_root))
            self._forget_thread_silent_drops(channel_id, thread_root)
            await self._announce_held_before_leaving(
                Anchor(channel_id, thread_root), "the bot left this thread",
            )
            self._held.forget_anchor(Anchor(channel_id, thread_root))
            if removed:
                self._end_tool_use_run(removed)
                self.posters.forget(removed)
                self._session_triggerer.pop(removed, None)
                self._recent_harness_sends.pop(removed, None)
                if self.typing:
                    await self.typing.stop(removed)
            farewell = (
                f"Leaving this thread: {reason}" if reason
                else "Leaving this thread — reply in the main channel to continue."
            )
            try:
                self.mm.post(channel_id, farewell, root_id=thread_root)
            except Exception:
                pass
            return

        if not session_id:
            return  # nothing to leave from

        farewell = (
            f"Leaving: {reason}" if reason
            else "Leaving — invite me back any time for a fresh session."
        )
        await self._leave_channel(channel_id, session_id, farewell)

    # ─────────────────────── Dot-command dispatch ─────────────────────────

    def _bot_command_res(
        self,
    ) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
        """The catch-up/leave/stop regexes, compiled for the live bot handle.

        Compiled lazily from ``self.mm.bot_username`` (stable after login) and
        cached; recompiled only if the resolved handle ever changes. Keying off
        the runtime handle is what makes the bot name configurable — no more
        hardcoded ``@claude``.
        """
        bot = self.mm.bot_username or "claude"
        cache = getattr(self, "_bot_command_res_cache", None)
        if cache is None or cache[0] != bot:
            cache = (bot, _compile_bot_command_res(bot))
            self._bot_command_res_cache = cache
        return cache[1]

    @property
    def _catch_up_re(self) -> re.Pattern[str]:
        return self._bot_command_res()[0]

    @property
    def _leave_cmd_re(self) -> re.Pattern[str]:
        return self._bot_command_res()[1]

    @property
    def _stop_cmd_re(self) -> re.Pattern[str]:
        return self._bot_command_res()[2]

    def _command_mentions(self) -> tuple[str, ...]:
        """Bot handles whose leading ``@mention`` ``commands.parse`` strips."""
        return (self.mm.bot_username,)

    def _message_mentions_bot(self, message: str) -> bool:
        """True if ``message`` @-mentions this bot (matches engagement logic).

        Case-insensitive on the runtime handle — for a bot named ``claude``
        this matches ``@claude``/``@Claude`` exactly as before.
        """
        return f"@{self.mm.bot_username}".lower() in message.lower()

    def _strip_bot_mention(self, message: str) -> str:
        """Remove this bot's ``@handle`` from a forwarded message.

        Case-sensitive, matching the historical strip: the agent shouldn't see
        a stray ``@<bot>`` handle. Keyed off the runtime bot username — for a
        bot named ``claude`` this is identical to the old
        ``.replace("@claude", "")``.
        """
        return message.replace(f"@{self.mm.bot_username}", "").strip()

    def _commands_allowed_here(
        self,
        channel_id: str,
        session_id: str | None,
        message: str,
        parsed: commands.ParsedCommand,
    ) -> bool:
        """Whether a dot-command should be honored (intercepted) here.

        Single-source-of-truth gate driven by :class:`commands.CommandSpec`
        metadata — there is no hand-maintained per-command allowlist, so a new
        command can't accidentally become (or suppress) an LLM first turn.

        * Active sessions (``session_id`` set) retain the full command set.
        * Dormant channels intercept every *known* dot-command so it's handled
          by the bridge and never forwarded — the only exception being bare
          ``global_scope`` (operator-wide) commands, which require an explicit
          ``@mention`` so a bare dot-word in a shared room can't leak
          cross-channel state. ``False`` here means the main handler swallows
          the message (dropping it, never an LLM turn).
        * An explicit ``@mention`` deliberately addresses the bot, so it always
          intercepts — including an unknown dot-word, which then gets the
          "unknown command" hint instead of silence.
        * A *bare* unknown dot-word (``spec is None``) stays swallowed in a
          dormant channel — casual ``.foo`` chatter in an auto-joined room
          shouldn't draw a bot reply. Active channels always answer with a hint.
        """
        if session_id:
            return True
        if channel_id not in self._dormant_channels:
            return not self.config.auto_join_public_channels
        if self._message_mentions_bot(message):
            return True  # explicit address — always intercept and answer
        spec = parsed.spec
        if spec is None:
            return False  # bare unknown dot-word: drop silently while dormant
        if spec.global_scope:
            return False  # bare operator-wide command needs an explicit @mention
        return True

    def _post_cmd_reply(
        self, channel_id: str, text: str, thread_root: str | None,
    ) -> None:
        """Post a dot-command reply, swallowing MM errors (never fatal)."""
        try:
            self.mm.post(channel_id, text, root_id=thread_root)
        except Exception:
            logger.debug("Failed posting dot-command reply", exc_info=True)

    async def _dispatch_command(
        self,
        channel_id: str,
        session_id: str | None,
        post: dict,
        parsed: commands.ParsedCommand,
        *,
        thread_root: str | None,
    ) -> None:
        """Execute an intercepted dot-command. Never forwards to the agent.

        Unknown dot-words and session-scoped commands in a session-less
        channel get a helpful reply instead of silence.
        """
        spec = parsed.spec
        if spec is None:
            self._post_cmd_reply(
                channel_id,
                f":grey_question: Unknown command `.{parsed.name}` — try `.help`.",
                thread_root,
            )
            return
        if not session_id and channel_id in self._dormant_channels:
            # Commands with a pre-session answer read the channel's effective
            # config, so ensure it's loaded (and cached) first.
            if spec.name in {
                "backend", "model", "cwd", "models", "autorespond", "status",
            }:
                try:
                    await self._load_channel_config(channel_id)
                except Exception:
                    logger.exception(
                        "Failed to load dormant config for command in %s", channel_id,
                    )
                    self._post_cmd_reply(
                        channel_id,
                        ":warning: Could not read this channel's configuration.",
                        thread_root,
                    )
                    return
            if spec.name == "backend":
                await self._cmd_dormant_backend(
                    channel_id, parsed.arg, thread_root,
                )
                return
            if spec.name == "model":
                await self._cmd_dormant_model(
                    channel_id, parsed.arg, thread_root,
                )
                return
            if spec.name == "cwd":
                await self._cmd_dormant_cwd(
                    channel_id, parsed.arg, thread_root,
                )
                return
            if spec.name == "status":
                await self._cmd_dormant_status(channel_id, thread_root)
                return
        if spec.session_scoped and not session_id:
            self._post_cmd_reply(
                channel_id,
                ":information_source: No session in this channel.",
                thread_root,
            )
            return

        if spec.name == "help":
            await self._cmd_help(channel_id, thread_root)
        elif spec.name == "stop":
            await self._run_stop_command(
                channel_id, session_id, thread_root=thread_root,
            )
        elif spec.name == "autorespond":
            await self._cmd_autorespond(channel_id, parsed.arg, thread_root)
        elif spec.name == "status":
            await self._cmd_status(channel_id, session_id, thread_root)
        elif spec.name == "model":
            await self._cmd_model(channel_id, session_id, parsed.arg, thread_root)
        elif spec.name == "backend":
            await self._cmd_backend(channel_id, session_id, parsed.arg, thread_root)
        elif spec.name == "cwd":
            await self._cmd_cwd(channel_id, session_id, parsed.arg, thread_root)
        elif spec.name == "models":
            await self._cmd_models(channel_id, session_id, thread_root)
        elif spec.name == "running":
            await self._cmd_running(channel_id, thread_root)
        elif spec.name == "sessions":
            await self._cmd_sessions(channel_id, parsed.arg, thread_root)
        elif spec.name == "queue":
            await self._cmd_queue(
                channel_id, session_id, parsed.arg, thread_root,
            )
        elif spec.name == "invite":
            await self._cmd_invite(channel_id, post, parsed.arg, thread_root)
        else:  # registered but not wired — defensive, shouldn't happen
            self._post_cmd_reply(
                channel_id,
                f":warning: `.{spec.name}` isn't available yet.",
                thread_root,
            )

    async def _cmd_help(self, channel_id: str, thread_root: str | None) -> None:
        body = commands.help_text()
        if channel_id in self._dormant_channels:
            body += "\n\n" + commands.dormant_help_note()
        self._post_cmd_reply(channel_id, body, thread_root)

    async def _cmd_autorespond(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """`.autorespond [on|off]` — bare toggles relative to current state.

        Reuses ``_run_runtime_toggle`` by translating the intent into the
        literal token it already understands (autorespond / noautorespond).

        Nothing restarts here, but the flag is *persisted* alongside the rest
        of the config — so a cold cache has to be filled from the Channel
        Purpose first, or the write wipes the channel's backend, model and
        `cwd=` tokens (invisibly, until the next session starts on them).
        """
        cfg = await self._config_for_update(channel_id, ".autorespond")
        if cfg is None:
            self._refuse_update_unknown_config(
                channel_id, thread_root, "Leaving the autorespond flag as it is",
            )
            return
        arg_lc = (arg or "").strip().lower()
        if arg_lc == "on":
            turn_on = True
        elif arg_lc == "off":
            turn_on = False
        elif arg_lc == "":
            # Bare toggles relative to the channel's CURRENT state, which
            # lives in the Purpose — not in ``config.default_autorespond``.
            turn_on = cfg.mention_only
        else:
            self._post_cmd_reply(
                channel_id,
                ":grey_question: Usage: `.autorespond [on|off]`",
                thread_root,
            )
            return
        token = (
            purpose.AUTORESPOND_TOKEN if turn_on else purpose.NOAUTORESPOND_TOKEN
        )
        await self._run_runtime_toggle(channel_id, token, cfg)

    async def _cmd_status(
        self, channel_id: str, session_id: str, thread_root: str | None,
    ) -> None:
        """`.status` — session, model, autorespond flag and run state."""
        cfg = self.purpose_by_channel.get(channel_id)
        try:
            meta = await self.harness.get_session(session_id)
        except Exception:
            logger.debug("`.status` harness get_session failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        meta = meta or {}
        backend = meta.get("backend") or (cfg.backend if cfg else "?")
        model = meta.get("model") or (cfg.model if cfg else None) or "default"
        cwd = (meta.get("project") or {}).get("path") or "?"
        autorespond = "mention-only" if (cfg and cfg.mention_only) else "on"

        run_id = self.current_run_id_by_session.get(session_id)
        if run_id:
            run_state = f"running (`{run_id}`)"
        elif session_id in self.active_run_by_session:
            run_state = "running"
        else:
            run_state = "idle"

        lines = [
            f"**Status** — session `{session_id[:12]}`",
            f"• backend: `{backend}`  ·  model: `{model}`",
            f"• cwd: `{cwd}`",
            f"• autorespond: `{autorespond}`",
            f"• run: {run_state}",
        ]
        hstatus = meta.get("status")
        if hstatus:
            lines.append(f"• harness session: `{hstatus}`")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    async def _cmd_dormant_status(
        self, channel_id: str, thread_root: str | None,
    ) -> None:
        """`.status` before a session exists.

        Reports the effective config the FIRST message will start with —
        backend / model / cwd / autorespond — so operators can verify a
        dormant channel without spending an LLM turn. The values resolve
        exactly as ``_maybe_start_engagement_session`` will at create time:
        Channel Purpose (cached in ``purpose_by_channel``) with the config
        defaults filling any gap. Layout mirrors :meth:`_cmd_status`.
        """
        cfg = self.purpose_by_channel.get(channel_id)
        backend = cfg.backend if cfg else self.config.default_backend
        model = (
            (cfg.model if cfg else None)
            or self.config.default_model_for(backend)
            or "default"
        )
        # Resolve cwd exactly as the create path does (validating any Purpose
        # `cwd=` against allowed roots) so `.status` never advertises a cwd the
        # session won't actually get.
        cwd = self._resolve_cwd(cfg)[0] if cfg else self.config.default_cwd
        mention_only = (
            cfg.mention_only if cfg else not self.config.default_autorespond
        )
        autorespond = "mention-only" if mention_only else "on"
        lines = [
            "**Status** — no session yet (starts on your first message).",
            f"• backend: `{backend}`  ·  model: `{model}`",
            f"• cwd: `{cwd}`",
            f"• autorespond: `{autorespond}`",
        ]
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    def _session_has_active_run(self, session_id: str) -> bool:
        """True if a run is in flight for ``session_id`` (bridge-tracked)."""
        return (
            session_id in self.active_run_by_session
            or bool(self.current_run_id_by_session.get(session_id))
        )

    def _config_switch_blocked_in_thread(
        self, channel_id: str, thread_root: str | None, usage: str,
    ) -> bool:
        """Refuse a `.model`/`.backend`/`.cwd` SWITCH inside a thread fork.

        ``_restart_session_with_config`` only unlinks/relinks
        ``Anchor(channel_id)``, so a switch inside a thread would replace the
        CHANNEL's session while the thread keeps its own — a silent mismatch
        that also posts a false "session restarted" confirmation. Bare
        (read-only) `.model`/`.backend` are unaffected. Returns True (and posts
        the refusal) when in a thread, so callers ``return`` early.
        """
        if not thread_root:
            return False
        self._post_cmd_reply(
            channel_id,
            f":warning: Thread forks keep their own config — run `{usage}` in "
            "the channel, not in a thread.",
            thread_root,
        )
        return True

    async def _cmd_dormant_model(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """Read or persist a model without creating a harness session."""
        cfg = self.purpose_by_channel[channel_id]
        if not arg:
            model = (
                cfg.model
                or self.config.default_model_for(cfg.backend)
                or "default"
            )
            self._post_cmd_reply(
                channel_id,
                f":robot_face: Model for the next session: `{model}` "
                f"(backend `{cfg.backend}`). Switch with `.model <name>`; "
                "see options with `.models`.",
                thread_root,
            )
            return

        updated = purpose.PurposeConfig(
            backend=cfg.backend,
            model=arg,
            mention_only=cfg.mention_only,
            cwd=cfg.cwd,
            warnings=[],
        )
        self.purpose_by_channel[channel_id] = updated
        self._persist_purpose(channel_id, updated)
        self._post_cmd_reply(
            channel_id,
            f":gear: Model set to `{arg}` — it will apply when the session starts. "
            "If the backend rejects it, the error will appear then.",
            thread_root,
        )

    async def _cmd_dormant_backend(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """Read or persist a backend without creating a harness session."""
        cfg = self.purpose_by_channel[channel_id]
        known_list = ", ".join(
            f"`{backend}`" for backend in sorted(purpose.KNOWN_BACKENDS)
        )
        if not arg:
            self._post_cmd_reply(
                channel_id,
                f":electric_plug: Backend for the next session: `{cfg.backend}`. "
                f"Switch with `.backend <name>`. Known: {known_list}.",
                thread_root,
            )
            return

        requested = purpose.canonical_backend(arg.strip())
        if requested not in purpose.KNOWN_BACKENDS:
            self._post_cmd_reply(
                channel_id,
                f":warning: Unknown backend `{arg.strip()}`. Known: {known_list}.",
                thread_root,
            )
            return
        if requested == cfg.backend:
            self._post_cmd_reply(
                channel_id,
                f":information_source: Already configured for backend `{requested}`.",
                thread_root,
            )
            return

        target = purpose.PurposeConfig(
            backend=requested,
            model=None,
            mention_only=cfg.mention_only,
            cwd=cfg.cwd,
            warnings=[],
        )
        updated = self._merge_configs(cfg, target)
        self.purpose_by_channel[channel_id] = updated
        self._persist_purpose(channel_id, updated)
        self._post_cmd_reply(
            channel_id,
            f":gear: Backend set to `{requested}` — it will apply when the "
            f"session starts (model reset to the `{requested}` default).",
            thread_root,
        )

    async def _cmd_model(
        self,
        channel_id: str,
        session_id: str,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.model [<name>]` — show the current model, or switch it.

        Names are free text (no catalog validation): a bad one fails loudly
        with the backend error when the restarted session's first run runs.
        Switching recreates the session (the harness has no model-mutate
        endpoint), so it's refused while a run is active.
        """
        cfg = await self._config_for_update(channel_id, ".model")
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            logger.debug("`.model` harness get_session failed", exc_info=True)
            meta = {}
        backend = self._live_backend(meta, cfg)

        # Bare `.model` → report the current model + hint `.models`.
        if not arg:
            model = (
                meta.get("model")
                or (cfg.model if cfg else None)
                or self.config.default_model_for(backend)
                or "default"
            )
            self._post_cmd_reply(
                channel_id,
                f":robot_face: Current model: `{model}` (backend `{backend}`). "
                f"Switch with `.model <name>`; see options with `.models`.",
                thread_root,
            )
            return

        # A switch inside a thread would restart the wrong (channel) session.
        if self._config_switch_blocked_in_thread(channel_id, thread_root, ".model <name>"):
            return

        # Switching mid-run would orphan the active run — refuse.
        if self._session_has_active_run(session_id):
            self._post_cmd_reply(
                channel_id,
                ":warning: A run is active — `.stop` it first, then `.model <name>`.",
                thread_root,
            )
            return

        if cfg is None:
            self._refuse_update_unknown_config(
                channel_id, thread_root, "Leaving the session on its current model",
            )
            return

        # Only the model moves: the backend (the LIVE one — see
        # ``_live_backend``), the working directory and the autorespond flag
        # carry over untouched.
        new_cfg = purpose.PurposeConfig(
            backend=backend,
            model=arg,
            mention_only=cfg.mention_only,
            cwd=cfg.cwd,
            warnings=[],
        )
        new_session = await self._restart_session_with_config(
            channel_id, session_id, new_cfg,
        )
        if not new_session:
            # The restart failed; `_restart_session_with_config` already posted
            # a warning and restored the prior session. Don't claim success or
            # persist the unreachable model.
            return
        self._persist_purpose(channel_id, new_cfg)
        self._post_cmd_reply(
            channel_id,
            f":gear: Model set to `{arg}` — session restarted. "
            "If the backend rejects it, the error will appear above.",
            thread_root,
        )

    async def _cmd_backend(
        self,
        channel_id: str,
        session_id: str,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.backend [<name>]` — show the current backend, or switch it.

        Unlike models, backends ARE enumerable, so a name is validated against
        ``purpose.KNOWN_BACKENDS`` (aliases like ``claude-code`` canonicalise
        first) and a typo is rejected inline. Switching recreates the session
        (same as `.model`), so it's refused while a run is active. The carried
        model is dropped on the swap — models are backend-specific, so the new
        backend's per-backend default applies (via ``_merge_configs``).
        """
        cfg = await self._config_for_update(channel_id, ".backend")
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            logger.debug("`.backend` harness get_session failed", exc_info=True)
            meta = {}
        current_backend = self._live_backend(meta, cfg)

        known_list = ", ".join(f"`{b}`" for b in sorted(purpose.KNOWN_BACKENDS))

        # Bare `.backend` → report the current backend + the known set.
        if not arg:
            self._post_cmd_reply(
                channel_id,
                f":electric_plug: Current backend: `{current_backend}`. "
                f"Switch with `.backend <name>`. Known: {known_list}.",
                thread_root,
            )
            return

        # A switch inside a thread would restart the wrong (channel) session.
        if self._config_switch_blocked_in_thread(channel_id, thread_root, ".backend <name>"):
            return

        requested = purpose.canonical_backend(arg.strip())
        if requested not in purpose.KNOWN_BACKENDS:
            self._post_cmd_reply(
                channel_id,
                f":warning: Unknown backend `{arg.strip()}`. Known: {known_list}.",
                thread_root,
            )
            return

        if requested == current_backend:
            self._post_cmd_reply(
                channel_id,
                f":information_source: Already on backend `{requested}`.",
                thread_root,
            )
            return

        # Switching mid-run would orphan the active run — refuse.
        if self._session_has_active_run(session_id):
            self._post_cmd_reply(
                channel_id,
                ":warning: A run is active — `.stop` it first, then `.backend <name>`.",
                thread_root,
            )
            return

        if cfg is None:
            self._refuse_update_unknown_config(
                channel_id, thread_root, "Leaving the session on its current backend",
            )
            return

        # Layer the new backend over the current config via ``_merge_configs``
        # so the carried model is dropped (a claude model can't run on codex).
        # The working directory and the autorespond flag survive the swap.
        target = purpose.PurposeConfig(
            backend=requested,
            model=None,
            mention_only=cfg.mention_only,
            cwd=cfg.cwd,
            warnings=[],
        )
        # Merge against the LIVE backend, not the Purpose's: in a token-less
        # channel the Purpose parses to the default backend, which would make
        # ``_merge_configs`` see no change and carry the old model (or, with
        # the live harness's empty model catalog, the Purpose's own text) onto
        # the new backend.
        new_cfg = self._merge_configs(replace(cfg, backend=current_backend), target)
        new_session = await self._restart_session_with_config(
            channel_id, session_id, new_cfg,
        )
        if not new_session:
            # The restart failed; `_restart_session_with_config` already posted
            # a warning and restored the prior session. Don't claim success.
            return
        self._persist_purpose(channel_id, new_cfg)
        self._post_cmd_reply(
            channel_id,
            f":gear: Backend set to `{requested}` — session restarted "
            f"(model reset to the `{requested}` default). "
            "If the backend rejects it, the error will appear above.",
            thread_root,
        )

    async def _cmd_queue(
        self,
        channel_id: str,
        session_id: str | None,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.queue [clear]` — inspect or drop the posts held for this anchor.

        `.stop` deliberately leaves the buffer alone (interrupting a run and
        discarding the backlog are different intents), so this is the
        explicit escape hatch. A clear names what it dropped: an explicit
        drop is still never a silent one.
        """
        anchor = Anchor(channel_id, thread_root)
        held = self._held.peek(anchor)
        wants_clear = (arg or "").strip().lower()
        if wants_clear and wants_clear != "clear":
            self._post_cmd_reply(
                channel_id,
                ":grey_question: Usage: `.queue` or `.queue clear`.",
                thread_root,
            )
            return

        if not held:
            self._post_cmd_reply(
                channel_id,
                ":inbox_tray: Nothing held — the queue is empty.",
                thread_root,
            )
            return

        listing = "\n".join(f"• `{h.summary()}`" for h in held)
        if not wants_clear:
            self._post_cmd_reply(
                channel_id,
                f":hourglass_flowing_sand: **{len(held)} post(s) held** — "
                f"they'll be delivered as one turn when the run "
                f"finishes:\n{listing}",
                thread_root,
            )
            return

        dropped = self._held.clear(anchor)
        for h in dropped:
            await self._react_best_effort(h.post_id, HELD_REACTION, add=False)
        logger.info(
            "`.queue clear` dropped %d held post(s) in %s",
            len(dropped), channel_id,
        )
        self._post_cmd_reply(
            channel_id,
            f":wastebasket: Dropped {len(dropped)} held post(s); they will "
            f"NOT be delivered:\n{listing}",
            thread_root,
        )

    def _validate_cwd(self, raw: str) -> tuple[str | None, str | None]:
        """Resolve a user-supplied `.cwd` value → ``(absolute_path, problem)``.

        Exactly one of the two is non-None. Deliberately stricter than the
        Channel Purpose parser: an unusable ``cwd=`` token there degrades
        silently to ``config.default_cwd``, whereas the command can name the
        exact failure — so we refuse rather than confirm a directory the
        session would never get.

        ``~`` IS expanded here (``purpose._parse_cwd_token`` is strict and
        doesn't), which is why callers persist the returned absolute path
        rather than the typed one.
        """
        try:
            candidate = Path(raw).expanduser()
        except (OSError, ValueError, RuntimeError):
            return None, f"`{raw}` is not a usable path."
        if not candidate.is_absolute():
            return None, (
                f"`{raw}` is not an absolute path — give a full path, "
                "e.g. `/srv/repos/thing`."
            )
        try:
            exists, is_dir = candidate.exists(), candidate.is_dir()
        except OSError:
            exists, is_dir = False, False
        if not exists:
            return None, f"`{candidate}` does not exist."
        if not is_dir:
            return None, f"`{candidate}` is not a directory."
        resolved = resolve_attachment_path(
            str(candidate), project_path=None,
            allowed_roots=self.config.allowed_attachment_roots,
        )
        if resolved is None:
            roots = describe_allowed_roots(
                None, self.config.allowed_attachment_roots,
            )
            return None, (
                f"`{candidate}` is outside the allowed roots — sessions can "
                f"only run under {roots}."
            )
        # The Channel Purpose is a comma-separated config list, so a cwd
        # containing a comma can't round-trip: `purpose.parse` would split it
        # into a truncated path plus a stray token. Checked on the RESOLVED
        # path — the value actually persisted — so a symlink pointing at a
        # comma-bearing directory is caught too.
        if "," in str(resolved):
            return None, (
                f"`{resolved}` contains a comma. The Channel Purpose stores "
                "the working directory in a comma-separated list, so such a "
                "path can't be saved — move or symlink it somewhere without "
                "a comma."
            )
        return str(resolved), None

    async def _cmd_cwd(
        self,
        channel_id: str,
        session_id: str,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.cwd [<path>]` — show the session's working directory, or move it.

        Bare is a pure read, so it works inside a thread fork. A set has to
        recreate the session (the harness has no cwd-mutate endpoint), which
        puts it under the same rules as `.model` / `.backend`: refused inside
        a thread fork, and refused while a run is in flight.
        """
        cfg = await self._config_for_update(channel_id, ".cwd")
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            logger.debug("`.cwd` harness get_session failed", exc_info=True)
            meta = {}
        current = (meta.get("project") or {}).get("path") or (
            self._resolve_cwd(cfg)[0] if cfg else self.config.default_cwd
        )

        # Bare `.cwd` → report where this session is actually running.
        if not arg:
            self._post_cmd_reply(
                channel_id,
                f":file_folder: Current directory: `{current}`. "
                "Change with `.cwd <path>` (restarts the session).",
                thread_root,
            )
            return

        # A switch inside a thread would restart the wrong (channel) session.
        if self._config_switch_blocked_in_thread(channel_id, thread_root, ".cwd <path>"):
            return

        target, problem = self._validate_cwd(arg)
        if problem:
            self._post_cmd_reply(channel_id, f":warning: {problem}", thread_root)
            return
        if target == current:
            self._post_cmd_reply(
                channel_id,
                f":information_source: Already running in `{target}`.",
                thread_root,
            )
            return

        # Switching mid-run would orphan the active run — refuse.
        if self._session_has_active_run(session_id):
            self._post_cmd_reply(
                channel_id,
                ":warning: A run is active — `.stop` it first, then `.cwd <path>`.",
                thread_root,
            )
            return

        if cfg is None:
            self._refuse_update_unknown_config(
                channel_id, thread_root, "Leaving the session where it is",
            )
            return

        # Only the directory moves: the backend (the LIVE one — see
        # ``_live_backend``), model and the autorespond flag carry over
        # untouched (unlike `.backend`, where the model can't survive the
        # swap).
        new_cfg = purpose.PurposeConfig(
            backend=self._live_backend(meta, cfg),
            model=cfg.model,
            mention_only=cfg.mention_only,
            cwd=target,
            warnings=[],
        )
        new_session = await self._restart_session_with_config(
            channel_id, session_id, new_cfg,
        )
        if not new_session:
            # The restart failed; `_restart_session_with_config` already posted
            # a warning and restored the prior session. Don't claim success or
            # persist a directory the channel isn't actually in.
            return
        self._persist_purpose(channel_id, new_cfg)
        self._post_cmd_reply(
            channel_id,
            f":file_folder: Working directory set to `{target}` — "
            "session restarted there.",
            thread_root,
        )

    async def _cmd_dormant_cwd(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """Read or persist a working directory without creating a session."""
        cfg = self.purpose_by_channel[channel_id]
        current = self._resolve_cwd(cfg)[0]
        if not arg:
            self._post_cmd_reply(
                channel_id,
                f":file_folder: Directory for the next session: `{current}`. "
                "Change with `.cwd <path>`.",
                thread_root,
            )
            return

        target, problem = self._validate_cwd(arg)
        if problem:
            self._post_cmd_reply(channel_id, f":warning: {problem}", thread_root)
            return
        if target == current:
            self._post_cmd_reply(
                channel_id,
                f":information_source: Already configured for `{target}`.",
                thread_root,
            )
            return

        updated = purpose.PurposeConfig(
            backend=cfg.backend,
            model=cfg.model,
            mention_only=cfg.mention_only,
            cwd=target,
            warnings=[],
        )
        self.purpose_by_channel[channel_id] = updated
        self._persist_purpose(channel_id, updated)
        self._post_cmd_reply(
            channel_id,
            f":file_folder: Working directory set to `{target}` — "
            "it will apply when the session starts.",
            thread_root,
        )

    async def _cmd_models(
        self, channel_id: str, session_id: str | None, thread_root: str | None,
    ) -> None:
        """`.models` — list this channel's backend's models, marking the
        current one. Merges the operator `[models]` catalog with the harness
        catalog (empty today). Works without a session (uses default backend).
        """
        cfg = self.purpose_by_channel.get(channel_id)
        backend = cfg.backend if cfg else None
        current_model = cfg.model if cfg else None
        if session_id:
            try:
                meta = await self.harness.get_session(session_id) or {}
            except Exception:
                logger.debug("`.models` harness get_session failed", exc_info=True)
                meta = {}
            backend = meta.get("backend") or backend
            current_model = meta.get("model") or current_model
        backend = purpose.canonical_backend(backend) or self.config.default_backend

        configured = self.config.configured_models_for(backend)
        try:
            catalog = await self.harness.list_backend_models(backend)
        except Exception:
            logger.debug("`.models` list_backend_models failed", exc_info=True)
            catalog = []
        # Dedup preserving order: configured first, then harness extras.
        models = list(dict.fromkeys([*configured, *(catalog or [])]))

        if not models:
            self._post_cmd_reply(
                channel_id,
                f":information_source: No models listed for `{backend}`. "
                "You can still switch to any name with `.model <name>` — "
                "a bad one fails loudly.",
                thread_root,
            )
            return

        lines = [f"**Models for `{backend}`:**"]
        for m in models:
            mark = (
                "  ← current"
                if current_model and m.lower() == current_model.lower()
                else ""
            )
            lines.append(f"• `{m}`{mark}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    async def _cmd_running(
        self, channel_id: str, thread_root: str | None,
    ) -> None:
        """`.running` — sessions with a run in flight right now.

        Uses the bridge's in-memory ``active_run_by_session`` (populated from
        SSE ``run.started`` → terminal, origin-agnostic — so it covers runs
        the bridge didn't submit too).
        """
        active = [
            sid for sid in self.active_run_by_session
            if not _is_suppressed_session(sid)
        ]
        if not active:
            self._post_cmd_reply(
                channel_id, ":zzz: No runs are active right now.", thread_root,
            )
            return
        lines = ["**Running now:**"]
        for sid in active:
            try:
                meta = await self.harness.get_session(sid) or {}
            except Exception:
                meta = {}
            label = self._session_label(meta, sid)
            backend = meta.get("backend") or "?"
            lines.append(f"• {label} · `{backend}`{self._channel_suffix(sid)}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    def _session_label(self, meta: dict, session_id: str) -> str:
        """A human label for a session row: title/project name + short id."""
        title = meta.get("title") or (meta.get("project") or {}).get("name")
        if title:
            return f"{title} (`{session_id[:8]}`)"
        return f"`{session_id[:12]}`"

    def _channel_name_for_session(self, session_id: str) -> str | None:
        """Display name of the MM channel mapped to ``session_id``, or None."""
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return None
        try:
            ch = self.mm.get_channel(anchor.channel_id)
        except Exception:
            ch = None
        ch = ch or {}
        return ch.get("display_name") or ch.get("name") or anchor.channel_id

    def _channel_suffix(self, session_id: str) -> str:
        """`" → ~channel~"` when mapped, else a not-on-Mattermost note."""
        name = self._channel_name_for_session(session_id)
        return f" → ~{name}~" if name else " · not on Mattermost"

    async def _cmd_sessions(
        self, channel_id: str, arg: str | None, thread_root: str | None,
    ) -> None:
        """`.sessions [N]` — the N most recent sessions across the harness,
        including terminal TUI sessions (``origin:"external"`` rows the
        transcript observer discovers). Sorted client-side by ``updated_at``.
        """
        n = _parse_count_arg(
            arg, default=_SESSIONS_DEFAULT_N, maximum=_SESSIONS_MAX_N,
        )
        try:
            sessions = await self.harness.list_sessions()
        except Exception:
            logger.debug("`.sessions` list_sessions failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        rows = [
            s for s in (sessions or [])
            if not _is_suppressed_session(s.get("id") or "")
        ]
        # The endpoint returns everything in insertion order with no limit —
        # sort newest-first (ISO8601 strings sort lexically) and truncate.
        rows.sort(
            key=lambda s: s.get("updated_at") or s.get("created_at") or "",
            reverse=True,
        )
        rows = rows[:n]
        if not rows:
            self._post_cmd_reply(
                channel_id, ":information_source: No sessions found.", thread_root,
            )
            return
        lines = [f"**Recent sessions** (top {len(rows)}):"]
        for s in rows:
            sid = s.get("id") or "?"
            label = self._session_label(s, sid)
            backend = s.get("backend") or "?"
            name = self._channel_name_for_session(sid)
            hint = (
                f" → ~{name}~" if name
                else f" · not on Mattermost — `.invite {sid}`"
            )
            lines.append(f"• {label} · `{backend}`{hint}")
        self._post_cmd_reply(channel_id, "\n".join(lines), thread_root)

    async def _cmd_invite(
        self,
        channel_id: str,
        post: dict,
        arg: str | None,
        thread_root: str | None,
    ) -> None:
        """`.invite <session-id>` — get the requester into the session's MM
        channel, creating it for unmapped/external sessions first.
        """
        session_id = (arg or "").strip()
        if not session_id:
            self._post_cmd_reply(
                channel_id, ":grey_question: Usage: `.invite <session-id>`",
                thread_root,
            )
            return
        user_id = post.get("user_id") or ""

        # Already mapped → just add the requester to its channel.
        anchor = self.mapping.get_anchor(session_id)
        if anchor:
            await self._invite_requester(
                anchor.channel_id, user_id, session_id, channel_id, thread_root,
            )
            return

        # Unmapped — look the session up so we can create a channel for it.
        try:
            meta = await self.harness.get_session(session_id)
        except Exception:
            logger.debug("`.invite` get_session failed", exc_info=True)
            self._post_cmd_reply(
                channel_id, ":warning: harness unreachable.", thread_root,
            )
            return
        if not meta:
            self._post_cmd_reply(
                channel_id,
                f":warning: No session `{session_id}` — check `.sessions`.",
                thread_root,
            )
            return

        # External pi sessions aren't resumable (harness 409), so a channel
        # would be a dead end — reject up front.
        backend = purpose.canonical_backend(meta.get("backend"))
        if meta.get("origin") == "external" and backend == "pi":
            self._post_cmd_reply(
                channel_id,
                f":warning: Session `{session_id[:12]}` is an external `pi` "
                "session — it can't be resumed from Mattermost.",
                thread_root,
            )
            return

        new_channel_id = await self._create_channel_for_session(meta)
        if not new_channel_id:
            self._post_cmd_reply(
                channel_id,
                ":warning: Couldn't create a channel for that session.",
                thread_root,
            )
            return

        # Resuming a still-open TUI session forks it — the MM channel becomes
        # a parallel branch, not a remote control of the live terminal.
        if meta.get("origin") == "external":
            try:
                self.mm.post(
                    new_channel_id,
                    ":information_source: _Heads-up: posting here resumes this "
                    "session via `--resume`. If it's still open in a terminal, "
                    "this channel becomes a **fork** of the conversation, not a "
                    "remote control of the live TUI._",
                )
            except Exception:
                logger.debug("failed posting resume-fork warning", exc_info=True)

        await self._invite_requester(
            new_channel_id, user_id, session_id, channel_id, thread_root,
        )

    async def _invite_requester(
        self,
        target_channel_id: str,
        user_id: str,
        session_id: str,
        reply_channel_id: str,
        thread_root: str | None,
    ) -> None:
        """Add ``user_id`` to ``target_channel_id`` and confirm in the channel
        the command was issued from."""
        if not user_id:
            self._post_cmd_reply(
                reply_channel_id,
                ":warning: Couldn't tell who to invite.",
                thread_root,
            )
            return
        try:
            self.mm.invite_user(target_channel_id, user_id)
        except Exception:
            logger.exception(
                "Failed to invite %s to channel %s", user_id, target_channel_id,
            )
            self._post_cmd_reply(
                reply_channel_id,
                ":warning: Failed to invite you to the channel.",
                thread_root,
            )
            return
        name = self._channel_name_for_session(session_id) or target_channel_id
        self._post_cmd_reply(
            reply_channel_id,
            f":white_check_mark: Invited you to ~{name}~ "
            f"for session `{session_id[:12]}`.",
            thread_root,
        )

    async def _resolve_active_run_id(self, session_id: str) -> str | None:
        """Authoritative fallback for `.stop`: ask the harness which run is
        live for ``session_id``.

        Both in-memory trackers can miss a genuinely-running run — most
        importantly after a bridge restart mid-run, where the in-memory state
        is lost but the harness process keeps the run alive. The harness is
        the source of truth, so consult it before concluding there is nothing
        to stop.

        Session-scoped by construction: ``list_session_runs`` only returns runs
        belonging to ``session_id``, so this can never reach into another
        session. Guarded to a *single* ``running`` **harness-origin** run — an
        external (TUI-resumed) run isn't ours to kill (interrupt would 409),
        and more than one running run is unexpected, so we don't guess.
        """
        try:
            runs = await self.harness.list_session_runs(session_id)
        except Exception:
            logger.debug(
                "stop: list_session_runs failed for %s", session_id[:8],
                exc_info=True,
            )
            return None
        # Deliberately `running`-only, NOT the codebase-wide liveness set
        # HARNESS_LIVE_RUN_STATUSES ({"queued","running"}): `.stop` targets the
        # run that is actually executing. A queued run has no process to
        # interrupt, and counting it would make the "exactly 1" guard below
        # misfire during a normal running+queued overlap (→ refuse to stop the
        # real run). The origin filter drops external/TUI-resumed runs, which
        # aren't ours to kill (interrupt would 409); harness Run rows carry
        # ``origin`` (agent-harness models.Run), so this is a live guard.
        running = [
            r for r in runs
            if (r.get("status") or "").lower() == "running"
            and (r.get("origin") or "harness") == "harness"
        ]
        if len(running) != 1:
            if len(running) > 1:
                logger.warning(
                    "stop: %d running runs for session %s — not guessing which "
                    "to interrupt", len(running), session_id[:8],
                )
            return None
        return running[0].get("id") or running[0].get("run_id")

    async def _run_stop_command(
        self,
        channel_id: str,
        session_id: str,
        thread_root: str | None,
    ) -> None:
        # Resolve the in-flight run from three sources, most-convenient first.
        # Only ``current_run_id_by_session`` holds runs THIS daemon submitted
        # via ``create_run``; a spawned session's run (created by the
        # ``mm-bridge spawn`` CLI process) or any autorespond kickoff is seen
        # only through the ``run.started`` SSE lifecycle → ``active_run_by_session``.
        # Reading the first alone made `.stop` answer "Nothing to stop" while a
        # live codex run was working. The harness query is the authoritative
        # last resort (survives a restart that wiped both dicts) and is only
        # hit — via ``or`` short-circuit — when both trackers miss.
        run_id = (
            self.current_run_id_by_session.get(session_id)
            or self.active_run_by_session.get(session_id)
            or await self._resolve_active_run_id(session_id)
        )
        if not run_id:
            try:
                self.mm.post(
                    channel_id,
                    ":octagonal_sign: Nothing to stop.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        try:
            await self.harness.interrupt_run(session_id, run_id)
        except HarnessInterruptUnsupported:
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Can't interrupt this run — external session, not owned by the harness.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        except HarnessRunNotFound:
            pass
        except Exception:
            logger.exception("Failed to interrupt harness run %s/%s", session_id[:8], run_id[:8])
            try:
                self.mm.post(
                    channel_id,
                    ":warning: Couldn't interrupt the run.",
                    root_id=thread_root,
                )
            except Exception:
                pass
            return
        self._end_tool_use_run(session_id)
        if self.typing:
            await self.typing.stop(session_id)
        logger.info("MM → agent-harness [%s]: stop (interrupt)", session_id[:8])
        try:
            self.mm.post(channel_id, ":octagonal_sign: Stopped.", root_id=thread_root)
        except Exception:
            pass

    async def _leave_channel(
        self,
        channel_id: str,
        session_id: str,
        farewell: str | None,
    ) -> None:
        if farewell:
            try:
                self.mm.post_message(channel_id, farewell)
            except Exception:
                pass
        # Leaving discards this channel's held posts. Name them first —
        # "an explicit drop is still never a silent one" — and first is
        # literal: after `remove_self_from_channel` we can't post here.
        await self._announce_held_before_leaving(
            Anchor(channel_id), "the bot left this channel",
        )
        try:
            self.mm.remove_self_from_channel(channel_id)
        except Exception:
            logger.warning("Failed to remove self from channel %s", channel_id, exc_info=True)
            try:
                self.mm.post_message(
                    channel_id, ":warning: Failed to leave the channel.",
                )
            except Exception:
                pass
            return
        self.mapping.unlink(Anchor(channel_id))
        self.purpose_by_channel.pop(channel_id, None)
        self._awaiting_first_forward.discard(channel_id)
        self._pending_initial_catch_up.pop(channel_id, None)
        self._forget_channel_silent_drops(channel_id)
        self._held.forget_channel(channel_id)
        self._held_probe_ts.pop(session_id, None)
        self._end_tool_use_run(session_id)
        self.posters.forget(session_id)
        self._session_triggerer.pop(session_id, None)
        self._recent_harness_sends.pop(session_id, None)
        self._posted_watchdog_notices.pop(session_id, None)
        if self.typing:
            await self.typing.stop(session_id)

    # ─────────────────────── agent-harness SSE handlers ───────────────────

    async def _on_harness_event(self, event_type: str, data: dict) -> None:
        # The harness SSE Event envelope (see agent_harness/models.py: Event)
        # carries ``session_id`` and ``run_id`` at the TOP level, alongside the
        # event-specific ``data`` payload. Flatten them into the dict handed
        # to handlers so a single ``data.get("session_id")`` works regardless
        # of whether the field came from the envelope or the inner payload.
        inner = dict(data.get("data") or {})
        for key in ("session_id", "run_id"):
            if inner.get(key) is None and data.get(key) is not None:
                inner[key] = data[key]

        session_id = (
            inner.get("session_id")
            or (inner.get("session") or {}).get("id")
        )
        if session_id and event_type in HARNESS_ACTIVITY_EVENTS:
            self.last_activity_ts[session_id] = time.monotonic()
            await self._start_typing_for_activity(session_id)
        elif session_id and event_type == "session.updated":
            # The harness reuses ``session.updated`` for both running- and
            # idle-flips. Read the status payload to decide: a running-flip is
            # activity (keep typing); a quiet-flip is the explicit "went quiet"
            # signal and must STOP typing — for external/observer sessions
            # there is no run-terminal event to do that cleanup, and repeated
            # freshness ticks would otherwise keep the silence watchdog from
            # ever firing, leaving typing stuck ON.
            if self._session_updated_is_activity(inner):
                self.last_activity_ts[session_id] = time.monotonic()
                await self._start_typing_for_activity(session_id)
            else:
                await self._stop_typing_for_idle(session_id)

        if event_type == "session.updated":
            await self._on_harness_session_seen(inner)
        elif event_type == "message":
            await self._on_harness_message(inner)
        elif event_type in {
            "message.delta",
            "tool.call",
            "tool.result",
            "permission.denied",
            "ping",
        }:
            return
        elif event_type in HARNESS_RUN_TERMINAL_EVENTS or event_type == "run.started":
            await self._on_harness_run_lifecycle(event_type, inner)
        elif event_type in HARNESS_WATCHDOG_EVENTS:
            await self._on_harness_watchdog_event(event_type, inner)
        else:
            logger.debug("Unhandled agent-harness event %s", event_type)

    @staticmethod
    def _session_updated_is_activity(inner: dict) -> bool:
        """Decide whether a ``session.updated`` payload represents the session
        actively working (→ typing) or having gone quiet (→ no typing / stop).

        The canonical status location is ``data.session.status`` (the harness
        dumps the full ``Session`` row there); ``data.status`` is accepted as a
        fallback. Only ``status == "running"`` counts as activity. Anything in
        ``HARNESS_QUIET_SESSION_STATUSES`` — or a missing/unknown status — is
        treated as NON-activity (the SAFE default): real output always also
        emits ``message`` / ``message.delta`` / ``tool.*`` events that keep
        typing alive independently, so a status-less freshness tick must not.
        """
        status = (inner.get("session") or {}).get("status") or inner.get("status")
        return status == "running"

    async def _stop_typing_for_idle(self, session_id: str) -> None:
        """A quiet ``session.updated`` flip is the explicit "went quiet"
        signal: drop the activity timestamp and stop the typing loop so the
        indicator clears immediately rather than waiting on the silence
        watchdog (which the prior freshness ticks would have kept resetting).

        EXCEPT while a run is in flight: the harness observer flips
        ``session.status`` to "idle" on pure rollout-file silence, which
        fires MID-RUN during long quiet tool calls. Run lifecycle events
        are authoritative — the terminal event (or the watchdog's Run-row
        reconcile, for a missed one) does the cleanup then. Sessions
        without a tracked run (external/observer) keep the immediate stop.
        """
        if session_id in self.active_run_by_session:
            logger.debug(
                "quiet status flip ignored: run %s still active — rollout "
                "freshness noise during long tool calls",
                self.active_run_by_session.get(session_id),
            )
            return
        self.last_activity_ts.pop(session_id, None)
        if self.typing:
            await self.typing.stop(session_id)

    async def _start_typing_for_activity(self, session_id: str) -> None:
        if not self.typing:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return
        await self.typing.start(session_id, anchor.channel_id, anchor.root_id)

    async def _on_harness_session_seen(self, data: dict) -> None:
        session = data.get("session") or data
        session_id = session.get("id") or data.get("session_id") or ""
        if not session_id:
            return
        if self.mapping.get_anchor(session_id):
            self._known_sessions.add(session_id)
            return  # already mapped
        if session_id in self._known_sessions:
            return
        # Suppress claude-code subagent transcripts — internal to a parent
        # run, would just churn channels.
        if _is_suppressed_session(session_id):
            self._known_sessions.add(session_id)
            return
        # Operator opted out of mirroring CLI/terminal sessions into
        # channels (shared-server deployment: those channels are public
        # and auto-joining bots answer into them). Harness-origin
        # sessions (``mm-bridge spawn``, future IPC clients) must still
        # fall through to the create below.
        if (
            session.get("origin") == "external"
            and not self.config.mirror_external_sessions
        ):
            logger.info(
                "Not mirroring external session %s "
                "(mirror_external_sessions=false)",
                session_id[:24],
            )
            self._known_sessions.add(session_id)
            return
        # Both external and harness-origin sessions surface as channels
        # here. External sessions are CLI processes the operator started
        # outside the harness. Harness-origin sessions reach this branch
        # when something outside the daemon created them (``mm-bridge
        # spawn``, integration tests against a live harness, or a future
        # IPC client) — the bridge daemon's own creations register the
        # mapping synchronously via ``mapping.link`` and hit the early
        # ``get_anchor`` exit above. Pre-existing test ghosts are filtered
        # by ``_bootstrap_known_sessions`` marking them ``known``, so the
        # bootstrap SSE replay of their stale ``session.updated`` events
        # is skipped before reaching this line.
        # Only mark known after the channel actually exists; otherwise a
        # transient MM error means we'd silently skip future
        # ``session.updated`` events for this session and never spawn its
        # channel. ``_create_channel_for_session`` returns None on failure
        # and logs at error level. Bound retries so a permanently-failing
        # slug (collision, invalid name) doesn't loop forever on every SSE
        # event for the session.
        if await self._create_channel_for_session(session):
            self._known_sessions.add(session_id)
            self._channel_create_attempts.pop(session_id, None)
            return
        attempts = self._channel_create_attempts.get(session_id, 0) + 1
        self._channel_create_attempts[session_id] = attempts
        if attempts >= MAX_CHANNEL_CREATE_ATTEMPTS:
            logger.error(
                "Giving up on channel creation for session %s after %d attempts",
                session_id[:24], attempts,
            )
            self._known_sessions.add(session_id)
            self._channel_create_attempts.pop(session_id, None)

    async def _update_resume_purpose(
        self,
        channel_id: str,
        session_id: str,
        backend: str | None,
        cwd: str | None,
    ) -> None:
        """Best-effort: refresh the Resume block in the channel Purpose.

        Reads current Purpose, swaps the trailing resume section for a
        fresh one (preserving the config section above the separator),
        and writes back. Skipped (no MM call) when the backend has no
        resume command, the channel fetch fails, or the merge produces
        no change. All errors are logged and swallowed — Purpose writes
        must never break the calling claim/reconcile path.

        The backend argument accepts purpose tokens, canon names, and
        raw SSE display strings; normalisation happens inside
        :func:`resume_header.format_resume_block`.
        """
        if not session_id:
            return
        # Unsupported backend → fall through with block=None so any
        # *stale* resume block from a previous (supported) binding is
        # stripped from Purpose. The merge layer is a no-op when
        # Purpose has no resume section, so this path is safe even for
        # channels that never had one.
        canonical = resume_header.normalize_backend(backend)
        if canonical is None:
            block: str | None = None
        else:
            block = resume_header.format_resume_block(
                canonical, session_id, cwd,
                dangerous=self.config.dangerous_permissions,
            )
            if block is None:
                return  # defensive — covered by the canonical guard above
        try:
            channel = self.mm.get_channel(channel_id)
        except Exception:
            logger.warning(
                "resume-purpose: could not fetch channel %s", channel_id,
                exc_info=True,
            )
            return
        current = channel.get("purpose") or ""
        merged = resume_header.merge_into_purpose(current, block)
        if merged == current:
            return
        # The MM channel_updated event we receive after our own write would
        # otherwise be mistaken for an operator edit; tag it as self-written
        # so the bridge skips its "purpose changed" notice post.
        self._note_self_wrote_purpose(channel_id, merged)
        try:
            self.mm.set_channel_purpose(channel_id, merged)
        except Exception:
            logger.warning(
                "resume-purpose: set_channel_purpose failed for %s", channel_id,
                exc_info=True,
            )

    async def _reconcile_resume_purposes(self) -> None:
        """Refresh the Resume block on every channel-level mapping.

        Runs once at startup. Pulls each session's metadata from agent-harness
        so the resume command points at the right backend AND cwd even
        after a daemon restart (when ``purpose_by_channel`` is empty and
        the persisted MM Purpose may not name a backend either). Falls
        back to the MM Purpose for backend resolution when the harness doesn't
        know the session. Thread-fork anchors are skipped — a fork
        session lives inside a thread; writing its resume command into
        the parent channel's Purpose would clobber the channel session's
        own block.
        """
        for anchor, session_id in list(self.mapping.anchor_to_session.items()):
            if anchor.is_thread:
                continue
            backend, cwd = await self._resume_meta_for(anchor.channel_id, session_id)
            try:
                await self._update_resume_purpose(
                    anchor.channel_id, session_id, backend, cwd,
                )
            except Exception:
                logger.warning(
                    "resume-purpose: reconcile failed for %s", anchor.channel_id,
                    exc_info=True,
                )

    async def _resume_meta_for(
        self, channel_id: str, session_id: str,
    ) -> tuple[str | None, str | None]:
        """Resolve (backend, cwd) for a reconcile-time resume write.

        Prefers agent-harness session metadata (the source of truth for cwd).
        Falls back to the MM-side backend resolution when the harness doesn't know
        the session (stale mapping, harness restart since the mapping was
        persisted). Cwd is left None in the fallback path because we don't
        have a trustworthy source — the resume command stays runnable but
        omits the `cd` prefix.
        """
        backend: str | None = None
        cwd: str | None = None
        try:
            meta = await self.harness.get_session(session_id) or {}
        except Exception:
            meta = {}
            logger.debug(
                "resume-purpose: agent-harness meta lookup failed for %s",
                session_id[:8], exc_info=True,
            )
        if meta:
            backend = meta.get("backend") or meta.get("backendName") or None
            cwd = (meta.get("project") or {}).get("path") or meta.get("cwd") or None
        if not backend:
            backend = self._backend_for_channel(channel_id)
        return backend, cwd

    def _backend_for_channel(self, channel_id: str) -> str | None:
        """Backend used to resume a session in `channel_id`.

        Resolution order:

        1. The cached ``PurposeConfig`` (populated when the bridge
           handled an invite/fork/spawn in this daemon's lifetime).
        2. Re-parse the channel's persisted Mattermost Purpose. The
           cache is empty after a daemon restart, and falling straight
           to ``default_backend`` here would write the wrong Resume
           command for codex/pi channels whose Purpose was set during
           a previous run.
        3. ``config.default_backend`` as a last resort (channels with
           no Purpose at all).
        """
        cached = self.purpose_by_channel.get(channel_id)
        if cached and cached.backend:
            return cached.backend
        try:
            ch = self.mm.get_channel(channel_id)
        except Exception:
            return self.config.default_backend or None
        raw = (ch.get("purpose") or "").strip()
        if not raw:
            return self.config.default_backend or None
        parsed = purpose.parse(
            raw,
            default_backend=self.config.default_backend,
            default_model=None,
            available_models_for=lambda _b: [],
            default_autorespond=self.config.default_autorespond,
        )
        return parsed.backend or self.config.default_backend or None

    async def _flush_queued(
        self,
        channel_id: str,
        queued_posts: list[dict],
    ) -> None:
        """Re-dispatch posts that arrived while a session was being mapped."""
        for post in queued_posts:
            try:
                await self._on_mm_posted(post)
            except Exception:
                logger.exception("Failed to re-dispatch queued post")
                try:
                    self.mm.post_message(
                        channel_id,
                        ":warning: Failed to deliver a queued post to the new session.",
                    )
                except Exception:
                    pass
                break

    async def _create_channel_for_session(self, data: dict) -> str | None:
        session_id = data.get("id") or data.get("session_id") or ""
        if not session_id:
            return None
        channel_name = _session_to_channel_name(session_id)
        project = data.get("project") or {}
        display_name = (
            data.get("title")
            or project.get("name")
            or session_id[:12]
        )
        display_name = str(display_name)[:MM_DISPLAY_NAME_MAX]
        try:
            ch = self.mm.create_channel(
                name=channel_name,
                display_name=display_name,
                purpose=f"agent-harness session {session_id}",
            )
            channel_id = ch["id"]
            self.mapping.link(Anchor(channel_id), session_id)
            logger.info(
                "Created channel %s (%s) for agent-harness session %s",
                display_name, channel_name, session_id[:12],
            )
        except Exception:
            logger.exception("Failed to create channel for session %s", session_id[:12])
            return None
        # External-origin sessions have no harness stdin — a later MM post
        # must trigger ``_replace_external_session`` instead of a silent
        # ``create_run``. Tag the mapping so ``_on_mm_posted`` takes the
        # replacement path on the next inbound message.
        if data.get("origin") == "external":
            self._external_sessions.add(session_id)
        await self._update_resume_purpose(
            channel_id, session_id,
            data.get("backend"), project.get("path"),
        )
        return channel_id

    # ----- agent-harness message → Mattermost post -----

    async def _on_harness_message(self, data: dict) -> None:
        session_id = data.get("session_id", "")
        msg = data.get("message", {}) or {}
        role = msg.get("role")
        if role == "user":
            await self._maybe_mirror_user_message(session_id, msg)
            return
        if role != "assistant":
            return

        anchor = self.mapping.get_anchor(session_id)
        channel_id = anchor.channel_id if anchor else ""
        thread_root = anchor.root_id if anchor else None
        if not channel_id:
            return

        # Walk blocks in order. tool_use blocks coalesce into a per-session
        # placeholder (unless `show_tool_use` is off, in which case they're
        # silently dropped); text/error blocks end the run and post normally.
        for block in msg.get("blocks", []):
            btype = block.get("type")
            if btype == "tool_use":
                if not self.config.show_tool_use:
                    continue
                tool = block.get("name") or block.get("tool_name") or "unknown"
                self._upsert_tool_use(session_id, channel_id, thread_root, tool)
            elif btype == "tool_result" and block.get("is_error"):
                self._end_tool_use_run(session_id)
                err_text = f"**Tool error:** {str(block.get('content',''))[:500]}"
                try:
                    self.mm.post(
                        channel_id, _truncate_for_mm(err_text), root_id=thread_root,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post tool error for session %s", session_id[:8],
                    )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                await self._handle_assistant_text_block(
                    session_id, channel_id, thread_root, text,
                )

    # ----- direct user-message mirroring -----

    _RECENT_HARNESS_SEND_MAX = 32

    def _record_harness_send(self, session_id: str, body: str) -> None:
        """Remember that we just shipped ``body`` to ``session_id`` so the
        SSE echo can be suppressed by ``_consume_dedup_match``."""
        if not session_id or not body:
            return
        q = self._recent_harness_sends.setdefault(
            session_id, deque(maxlen=self._RECENT_HARNESS_SEND_MAX),
        )
        q.append((time.monotonic(), body))

    def _consume_dedup_match(self, session_id: str, body: str) -> bool:
        """Return True (and pop the matching entry) if ``body`` matches a
        recent backend send for ``session_id`` within the configured window."""
        q = self._recent_harness_sends.get(session_id)
        if not q:
            return False
        window = self.config.direct_user_message_dedup_window_seconds
        now = time.monotonic()
        while q and now - q[0][0] > window:
            q.popleft()
        for i, (_ts, recorded) in enumerate(q):
            if recorded == body:
                del q[i]
                return True
        return False

    async def _maybe_mirror_user_message(self, session_id: str, msg: dict) -> None:
        """Post role=user events into the bound MM channel when they
        represent direct input to the agent (typed in the local CLI/UI),
        not bridge-originated sends or tool results."""
        if not self.config.mirror_direct_user_messages:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return  # not bound — nothing to mirror to

        text_parts: list[str] = []
        for block in msg.get("blocks", []) or []:
            if block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            return  # tool_result or empty turn — skip

        if self._consume_dedup_match(session_id, text):
            return  # echo of a body we just sent

        body = f"_via coding agent:_ {text}"
        try:
            self.mm.post(
                anchor.channel_id, _truncate_for_mm(body), root_id=anchor.root_id,
            )
        except Exception:
            logger.exception(
                "Failed to mirror direct user message for session %s",
                session_id[:8],
            )

    async def _handle_assistant_text_block(
        self,
        session_id: str,
        channel_id: str,
        thread_root: str | None,
        text: str,
    ) -> None:
        """Real assistant response — clear the tool-use placeholder, then
        run the existing directives / attachments / post pipeline.
        """
        self._end_tool_use_run(session_id)

        cleaned, dirs = directives.extract(text)

        # <leaveChannel/> takes precedence.
        leave = next((d for d in dirs if d.kind == "leave_channel"), None)
        if leave:
            reason = leave.attrs.get("reason", "").strip()
            body = cleaned.strip()
            if not body and reason:
                body = f"_Leaving: {reason}_"
            elif not body:
                body = "_Leaving._"
            if thread_root:
                # Only remove the thread mapping, not the channel.
                try:
                    self.mm.post(channel_id, _truncate_for_mm(body), root_id=thread_root)
                except Exception:
                    pass
                self.mapping.unlink(Anchor(channel_id, thread_root))
                self._forget_thread_silent_drops(channel_id, thread_root)
                self._held.forget_anchor(Anchor(channel_id, thread_root))
                self.posters.forget(session_id)
                self._session_triggerer.pop(session_id, None)
                self._recent_harness_sends.pop(session_id, None)
                if self.typing:
                    await self.typing.stop(session_id)
            else:
                await self._leave_channel(channel_id, session_id, _truncate_for_mm(body))
            return

        # <openFile/> directives → attachments
        file_ids: list[str] = []
        warnings: list[str] = []
        line_hints: list[str] = []
        open_files = [d for d in dirs if d.kind == "open_file"]
        if open_files:
            project_path = await self._project_path_for(session_id)
            max_size = self._get_max_file_size()
            for d in open_files:
                raw_path = d.attrs.get("path", "")
                if not raw_path:
                    continue
                resolved = resolve_attachment_path(
                    raw_path, project_path, self.config.allowed_attachment_roots,
                )
                if not resolved:
                    roots_desc = describe_allowed_roots(
                        project_path, self.config.allowed_attachment_roots,
                    )
                    hint = (
                        f" Copy it into one of these and re-attach: {roots_desc}."
                        if roots_desc else ""
                    )
                    warnings.append(
                        f"_Could not attach `{raw_path}`: outside the allowed "
                        f"roots.{hint}_"
                    )
                    continue
                if not resolved.exists():
                    warnings.append(f"_Could not attach `{raw_path}`: file not found._")
                    continue
                try:
                    size = resolved.stat().st_size
                except OSError:
                    warnings.append(f"_Could not attach `{raw_path}`: permission denied._")
                    continue
                if size > max_size:
                    warnings.append(
                        f"_Could not attach `{raw_path}`: file too large ({size} bytes)._"
                    )
                    continue
                try:
                    fid = self.mm.upload_file(channel_id, resolved)
                    file_ids.append(fid)
                    if line := d.attrs.get("line"):
                        line_hints.append(f"`{raw_path}` (jump to line {line})")
                except Exception:
                    logger.exception("Upload failed for %s", raw_path)
                    warnings.append(f"_Could not attach `{raw_path}`: upload failed._")

        body = cleaned.strip()
        if line_hints:
            body = (body + "\n\n" + "\n".join(line_hints)).strip()
        if warnings:
            body = (body + "\n\n" + "\n".join(warnings)).strip()

        if not body and not file_ids:
            return

        try:
            self.mm.post(
                channel_id, _truncate_for_mm(body),
                file_ids=file_ids or None, root_id=thread_root,
            )
        except Exception:
            logger.exception("Failed to post to MM channel for session %s",
                             session_id[:8])

    # ----- Coalesced tool-use placeholder -----

    def _upsert_tool_use(
        self,
        session_id: str,
        channel_id: str,
        thread_root: str | None,
        tool: str,
    ) -> None:
        """Create or edit the per-turn tool-use placeholder post for
        `session_id`. Repeats of the last-used tool bump a counter; a
        different tool appends a new line.
        """
        run = self.tool_use_runs.get(session_id)
        if run is None:
            body = f"_Using tool: {tool}_"
            try:
                post = self.mm.post(channel_id, body, root_id=thread_root)
            except Exception:
                logger.exception(
                    "Failed to post tool-use placeholder for %s", session_id[:8],
                )
                return
            post_id = post.get("id")
            if not post_id:
                return
            self.tool_use_runs[session_id] = ToolUseRun(
                post_id=post_id, lines=[[tool, 1]],
            )
            return

        _bump_or_append(run, tool)
        try:
            self.mm.update_post(run.post_id, _format_tool_run(run))
        except Exception:
            logger.exception(
                "Failed to edit tool-use placeholder for %s", session_id[:8],
            )

    def _end_tool_use_run(self, session_id: str) -> None:
        """Drop per-session state so the next turn starts a fresh
        placeholder. The existing placeholder post is left in the channel
        as a compact, permanent record of the tools used this turn —
        intentionally not deleted (avoids tombstones, no system_admin
        permission needed).
        """
        self.tool_use_runs.pop(session_id, None)

    async def _project_path_for(self, session_id: str) -> str | None:
        meta = await self.harness.get_session(session_id) or {}
        return (meta.get("project") or {}).get("path") or None

    async def _save_mm_attachments(self, post: dict, cwd: str) -> list[str]:
        """Download MM file attachments into <cwd>/.mattermost-inbox/.

        Returns human-readable notes to prepend to the forwarded message.
        Each successful download yields `[User attached file: <abs-path>]`;
        failures yield a skipped-with-reason note so the LLM still sees them.
        """
        file_ids = post.get("file_ids") or []
        if not file_ids:
            return []

        try:
            inbox = Path(cwd).expanduser().resolve(strict=False) / MM_INBOX_DIRNAME
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("Failed to prepare MM inbox in %s", cwd)
            return [f"[MM attachment skipped: cannot write to inbox in {cwd}]"]

        file_meta = {
            f.get("id"): f
            for f in ((post.get("metadata") or {}).get("files") or [])
            if isinstance(f, dict) and f.get("id")
        }

        notes: list[str] = []
        max_size = self._get_max_file_size()
        for fid in file_ids:
            meta = file_meta.get(fid) or {}
            name = _safe_inbox_filename(meta.get("name", ""), f"file-{fid[:8]}")
            size = int(meta.get("size") or 0)
            if size and size > max_size:
                notes.append(
                    f"[MM attachment skipped: `{name}` too large ({size} bytes)]"
                )
                continue
            try:
                data = await asyncio.to_thread(self.mm.download_file, fid)
            except Exception:
                logger.exception("Download failed for MM file %s", fid)
                notes.append(f"[MM attachment skipped: `{name}` download failed]")
                continue
            try:
                dest = _unique_inbox_path(inbox, name)
                dest.write_bytes(data)
            except OSError:
                logger.exception("Write failed for MM file %s", fid)
                notes.append(f"[MM attachment skipped: `{name}` write failed]")
                continue
            logger.info("Saved MM attachment %s → %s", fid[:8], dest)
            notes.append(f"[User attached file: {dest}]")
        return notes

    def _get_max_file_size(self) -> int:
        if self._max_file_size is None:
            self._max_file_size = self.mm.get_max_file_size()
        return self._max_file_size

    # ----- agent-harness run lifecycle → typing indicator -----

    async def _on_harness_run_lifecycle(self, event_type: str, data: dict) -> None:
        session_id = data.get("session_id", "")
        if not session_id:
            return

        if event_type == "run.started":
            # Track the in-flight run origin-agnostically (also for runs the
            # bridge didn't submit) so the silence watchdog and quiet-flip
            # handler can tell "run still active" from "gone quiet". The
            # run_id may legitimately be absent from the payload (None).
            self.active_run_by_session[session_id] = (
                data.get("run_id") or data.get("id")
            )
            await self._start_typing_for_activity(session_id)
            return

        run_id = data.get("run_id") or data.get("id")
        current = self.current_run_id_by_session.get(session_id)
        if run_id and current == run_id:
            self.current_run_id_by_session.pop(session_id, None)
        self.active_run_by_session.pop(session_id, None)
        self.last_activity_ts.pop(session_id, None)
        # The run is over — clear its watchdog-notice dedup so the next run in
        # this session can warn afresh (and the map stays bounded).
        self._posted_watchdog_notices.pop(session_id, None)
        self._end_tool_use_run(session_id)
        if self.typing:
            await self.typing.stop(session_id)
        # ``run.failed`` means the backend run died without producing a reply
        # (CLI not found / boot error / non-zero exit). Surface it — the
        # channel would otherwise just see typing stop with no explanation.
        # Watchdog kills (idle-timeout / end-turn) publish ``run.interrupted``,
        # NOT ``run.failed``, so this never double-posts with those warnings.
        if event_type == "run.failed":
            await self._surface_run_failure(session_id, data)
        self._mention_triggerer_on_done(session_id)
        # The session is free again — deliver anything that arrived while it
        # was working, as ONE coalesced turn. Placed AFTER the completion
        # ping so that ping still belongs to the run that just ended.
        await self._maybe_flush_held_for_session(session_id)

    async def _surface_run_failure(self, session_id: str, data: dict) -> None:
        """Post a channel-facing warning for a ``run.failed`` SSE event.

        No-op when the session has no mapped anchor (external/observer runs
        with no channel to post into). The full payload is already logged by
        the SSE dispatch layer; this shapes the human-visible line.
        """
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            logger.warning(
                "run.failed for unmapped session %s (no channel to notify): %r",
                session_id[:8], data,
            )
            return
        detail = run_failure_detail(data)
        logger.error(
            "Harness run failed: session=%s detail=%s", session_id[:8], detail,
        )
        try:
            self.mm.post(
                anchor.channel_id,
                format_backend_error(
                    "run your message",
                    self._backend_for_channel(anchor.channel_id),
                    detail,
                ),
                root_id=anchor.root_id,
            )
        except Exception:
            logger.warning(
                "Failed to post run-failure notice for session %s",
                session_id[:8], exc_info=True,
            )

    async def _on_harness_watchdog_event(self, event_type: str, data: dict) -> None:
        """Handle the harness `RunProcess` watchdog events.

        All are SUPPLEMENTAL — a normal terminal event still follows a kill
        (or the run keeps going after a deferral). This handler does NOT
        touch typing indicators or run-id state; the terminal-event handler
        does.

        - ``run.terminated_after_end_turn``: silent (INFO log only). The LLM
          already finished its turn; operator doesn't need noise.
        - ``run.timed_out_idle`` (idle kill), ``run.idle_warning`` (deferred
          kill), ``run.max_runtime_warning`` (cap approaching), and
          ``run.exceeded_max_runtime`` (cap kill): post a channel notice
          shaped from the event payload. Each posts AT MOST ONCE per run —
          if the harness re-emits, the dedup set swallows the repeat.
        """
        session_id = data.get("session_id", "")
        sid_short = session_id[:8] if session_id else "?"

        if event_type == "run.terminated_after_end_turn":
            logger.info(
                "Harness watchdog terminated run after end_turn "
                "(session=%s hard_kill=%s returncode=%s grace_seconds=%s)",
                sid_short,
                data.get("hard_kill"),
                data.get("returncode"),
                data.get("grace_seconds"),
            )
            return

        formatter = HARNESS_WATCHDOG_NOTICE_FORMATTERS.get(event_type)
        if formatter is None:
            logger.debug("Unhandled watchdog event %s", event_type)
            return

        logger.warning(
            "Harness watchdog event %s (session=%s payload=%s)",
            event_type, sid_short, data,
        )
        if not session_id:
            return
        if event_type in self._posted_watchdog_notices.get(session_id, ()):
            logger.debug(
                "Watchdog notice %s already posted for session %s; skipping",
                event_type, sid_short,
            )
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            logger.debug(
                "No anchor for watchdog event %s (session %s); skipping post",
                event_type, sid_short,
            )
            return
        try:
            self.mm.post(
                anchor.channel_id,
                formatter(data),
                root_id=anchor.root_id,
            )
        except Exception:
            logger.warning(
                "Failed to post watchdog notice %s to channel %s (session %s)",
                event_type, anchor.channel_id, sid_short, exc_info=True,
            )
            return
        self._posted_watchdog_notices.setdefault(session_id, set()).add(event_type)

    def _mention_triggerer_on_done(self, session_id: str) -> None:
        """Post ``@<username>`` in the session's channel/thread so the user
        whose MM message triggered this run gets a push notification. No-op
        when the feature is disabled, no triggerer was tracked, or the
        anchor can't be resolved. The triggerer is consumed on use so a
        single run completion only pings once."""
        if not self.config.mention_user_when_done:
            return
        user_id = self._session_triggerer.pop(session_id, None)
        if not user_id:
            return
        anchor = self.mapping.get_anchor(session_id)
        if not anchor:
            return
        username = self._resolve_username(user_id)
        try:
            self.mm.post(
                anchor.channel_id,
                f"@{username}",
                root_id=anchor.root_id,
            )
        except Exception:
            logger.warning(
                "Failed to @-mention %s on session %s completion",
                username, session_id[:8], exc_info=True,
            )
