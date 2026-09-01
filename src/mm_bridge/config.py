"""Bridge configuration (TOML + env) and channel/thread mapping state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import json
import logging
import os
import sys
import tomllib

from . import sidecar
from .session_state import SessionState

logger = logging.getLogger(__name__)


def _expand(path: str) -> str:
    return str(Path(os.path.expandvars(path)).expanduser())


def _config_file_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "mm-bridge" / "config.toml"


@dataclass
class Config:
    # Mattermost
    mm_url: str = "localhost"
    mm_port: int = 8065
    mm_scheme: str = "http"
    mm_bot_token: str = ""
    mm_team: str = "workspace"

    # User-facing MM base URL (e.g. a Tailscale hostname) used for
    # permalinks embedded in channel headers / messages. Empty = fall back
    # to the daemon's own ``mm_scheme://mm_url:mm_port``.
    mm_public_url: str = ""

    # agent-harness
    agent_harness_url: str = "http://localhost:8877"

    # Session defaults
    default_backend: str = "claude"
    # Per-backend default model. ``opus`` is the right default for Claude
    # Code; codex needs ``gpt-5.5`` (a backend-agnostic ``opus`` here caused
    # codex sessions to die immediately — ``codex exec --model opus`` exits
    # 1). Backends not in this dict get no model hint — the harness picks
    # its own backend default. Keys match ``KNOWN_BACKENDS`` in
    # ``mm_bridge.purpose``.
    default_models: dict[str, str] = field(default_factory=lambda: {
        "claude": "opus",
        "codex": "gpt-5.5",
    })
    # Operator-maintained per-backend model catalog surfaced by the `.models`
    # command. The harness ``GET /v1/backends/{b}/models`` returns ``[]`` for
    # every backend today, so this static list (a ``[models]`` TOML table) is
    # the v1 source; ``.models`` merges it with whatever the harness
    # eventually enumerates. Keys match ``KNOWN_BACKENDS``. Empty by default.
    models: dict[str, list[str]] = field(default_factory=dict)
    default_cwd: str = str(Path.home())
    default_autorespond: bool = False

    # Surface tool-use notifications in channel as a coalesced placeholder
    # post. On by default — gives visibility into what Claude is doing.
    # Disable to keep channels to only real assistant replies.
    show_tool_use: bool = True

    # Mirror user turns that arrive via the coding agent's own UI/CLI
    # (rather than via Mattermost) back into the channel, so MM watchers
    # see the conversation in full. Tool results (role=user, tool_result
    # blocks) are never mirrored. Bridge-originated sends are de-duped
    # against a recent-send window so MM-routed turns don't echo back.
    mirror_direct_user_messages: bool = True
    # Window inside which an outbound send is treated as the source of a
    # subsequent role=user echo. Claude Code startup can be slow, so the
    # default is generous.
    direct_user_message_dedup_window_seconds: float = 30.0

    # Auto-create MM channels for "external" sessions — CLI/terminal agent
    # sessions the harness observer discovers outside the bridge
    # (``origin == "external"``). On a private single-tenant server this
    # mirrors all local agent activity into MM. On a shared server it
    # publishes terminal-session titles as PUBLIC channels, where
    # auto-joining bots start answering against the mirrored content —
    # set false there. Mattermost-initiated flows (invite, ``mm-bridge
    # spawn``) are harness-origin and are never affected by this knob.
    mirror_external_sessions: bool = True

    # When a run finishes, post a standalone ``@<username>`` in the same
    # channel/thread to notify the user whose message triggered it.
    # No-op if the run had no tracked triggerer (e.g. autorespond loops
    # or queued messages flushed at session start).
    mention_user_when_done: bool = True

    # Auto-join all public channels (silent presence — sessions start on
    # first engagement, not on join).
    auto_join_public_channels: bool = False
    auto_join_reconcile_seconds: float = 5.0

    # Hold posts that arrive while the anchor's session already has a run in
    # flight, and deliver them as ONE annotated run when it finishes. OFF is
    # exactly the pre-coalescing behavior: every post becomes its own eager
    # run and the harness FIFO decides the order — including the order of
    # posts that went stale while they waited.
    coalesce_posts: bool = True
    # Per-anchor cap on held posts. On overflow the overflowing post falls
    # back to an eager `create_run` and the bridge logs loudly; a held post is
    # never silently dropped. `<= 0` disables holding without touching the
    # kill switch above.
    coalesce_max_held: int = 50

    # State + config file paths
    state_file: str = str(Path.home() / ".config/mm-bridge/state.json")
    config_file: str = str(_config_file_path())
    sidecar_dir: str = str(Path.home() / ".mm-bridge/sessions")
    # Where held posts are persisted across daemon restarts. Empty = derived
    # from `state_file`'s directory in `load()`, so a relocated state file
    # carries its holds file with it. Deliberately NOT a section of the state
    # file: that one is rewritten on a 2-second throttle for the SSE cursor,
    # and the holds buffer has no business riding along on that cadence.
    held_posts_file: str = ""

    # Attachment safety
    allowed_attachment_roots: list[str] = field(default_factory=list)

    # Catch-up command
    catch_up_default_n: int = 50
    catch_up_max_n: int = 500
    # Auto-inject the last N channel messages on the first agent turn
    # (0 disables). Applies to both invite and engagement flows.
    initial_catch_up_n: int = 50

    # Typing indicator
    typing_refresh_seconds: float = 3.0
    # ----- awaiting-nag (F2) -----
    # The bridge is the only always-awake party, and a post into a channel IS
    # a turn — so an `awaiting` state that goes stale can be rung. OFF leaves
    # the state directive and the fleet view fully working; only the doorbell
    # stops (spec §5.8).
    nag_enabled: bool = True
    awaiting_nag_after_seconds: float = 1800.0
    # Who to @-mention when a wait escalates. Empty = fall back to the most
    # recent non-bot poster in the awaiting session's channel.
    operator_username: str = ""
    typing_stop_after_silence_seconds: float = 15.0

    # Claim window for matching session_added → pending MM invite
    pending_session_merge_window_seconds: float = 30.0

    # Name-sync debounce window
    name_sync_window_seconds: float = 10.0

    # Mirrors the coding-agent elevated-permissions mode. When True,
    # the resume command written into each bridged channel's Purpose
    # includes the matching elevated-permission flag
    # (`--dangerously-skip-permissions` for claude,
    # `--dangerously-bypass-approvals-and-sandbox` for codex), so resuming
    # a session locally lands in the same permission state the daemon is
    # running in. The backend does not expose its own `_skip_permissions`
    # state via a public API, so this is operator-owned. Defaults to True
    # because the typical operator runs the bridge with elevated local
    # coding-agent sessions; set ``MM_BRIDGE_DANGEROUS_PERMISSIONS=0``
    # (or the TOML key to false) to
    # opt out for constrained deployments.
    dangerous_permissions: bool = True

    @classmethod
    def load(cls) -> "Config":
        """Precedence: class defaults < TOML file < env vars. Fatal on bad TOML."""
        cfg = cls()

        config_path_env = os.environ.get("MM_BRIDGE_CONFIG")
        if config_path_env:
            cfg.config_file = _expand(config_path_env)

        toml_path = Path(cfg.config_file)
        if toml_path.exists():
            try:
                with toml_path.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                logger.error("Invalid TOML in %s: %s", toml_path, exc)
                sys.exit(1)
            cfg._apply_toml(data)
            logger.info("Loaded config from %s", toml_path)
        else:
            logger.info("No config file at %s — using defaults + env", toml_path)

        cfg._apply_env()
        cfg.default_cwd = _expand(cfg.default_cwd)
        cfg.state_file = _expand(cfg.state_file)
        cfg.sidecar_dir = _expand(cfg.sidecar_dir)
        # Derived AFTER state_file is expanded so the holds file lands beside
        # whichever state file the operator actually configured.
        cfg.held_posts_file = cfg.resolved_held_posts_file()
        cfg.allowed_attachment_roots = [_expand(p) for p in cfg.allowed_attachment_roots]
        return cfg

    def resolved_held_posts_file(self) -> str:
        """Absolute path for the held-posts file.

        Falls back to a sibling of ``state_file`` when unset, so a relocated
        state file carries its holds file with it. ``load()`` bakes the
        result back into the field, but callers that construct a ``Config``
        directly (tests, embedders) never run ``load()`` — so the Bridge asks
        for the resolved value rather than trusting the raw field.
        """
        if self.held_posts_file:
            return _expand(self.held_posts_file)
        return str(Path(_expand(self.state_file)).parent / "held_posts.json")

    def default_model_for(self, backend: str | None) -> str | None:
        """Return the configured default model for ``backend``, or ``None``.

        ``None`` means "let the harness pick" — we deliberately do not
        guess a model for an unconfigured backend, since a wrong guess is
        what crashed codex sessions when this used to be a single
        backend-agnostic scalar.
        """
        if not backend:
            return None
        return self.default_models.get(backend.lower())

    def configured_models_for(self, backend: str | None) -> list[str]:
        """Return the operator-configured model list for ``backend``.

        Empty when no ``[models]`` table entry exists — callers merge this
        with the (currently empty) harness catalog. Case-insensitive.
        """
        if not backend:
            return []
        return list(self.models.get(backend.lower(), []))

    # ----- internals -----

    def _apply_mm_url(self, raw: str) -> None:
        """Accept either a bare hostname or a full URL for MM_URL.

        A full URL (``http://host[:port]`` / ``https://host[:port]``) is
        split into scheme/host/port so callers can keep a single canonical
        value in their .env files. Explicit ``MM_PORT`` / ``MM_SCHEME``
        still override (applied after this by the caller).
        """
        if not (raw.startswith("http://") or raw.startswith("https://")):
            self.mm_url = raw
            return
        parsed = urlparse(raw)
        self.mm_scheme = parsed.scheme
        self.mm_url = parsed.hostname or raw
        if parsed.port is not None:
            self.mm_port = parsed.port
        else:
            self.mm_port = 443 if parsed.scheme == "https" else 80

    def _apply_toml(self, data: dict) -> None:
        for key in (
            "default_backend",
            "default_cwd",
            "default_autorespond",
            "show_tool_use",
            "mirror_direct_user_messages",
            "direct_user_message_dedup_window_seconds",
            "mirror_external_sessions",
            "mention_user_when_done",
            "auto_join_public_channels",
            "auto_join_reconcile_seconds",
            "state_file",
            "sidecar_dir",
            "allowed_attachment_roots",
            "catch_up_default_n",
            "catch_up_max_n",
            "initial_catch_up_n",
            "typing_refresh_seconds",
            "typing_stop_after_silence_seconds",
            "pending_session_merge_window_seconds",
            "name_sync_window_seconds",
            "dangerous_permissions",
            "coalesce_posts",
            "coalesce_max_held",
            "held_posts_file",
            "nag_enabled",
            "awaiting_nag_after_seconds",
            "operator_username",
        ):
            if key in data:
                setattr(self, key, data[key])

        mm = data.get("mattermost", {}) or {}
        if "url" in mm:
            self.mm_url = mm["url"]
        if "port" in mm:
            self.mm_port = int(mm["port"])
        if "scheme" in mm:
            self.mm_scheme = mm["scheme"]
        if "team" in mm:
            self.mm_team = mm["team"]
        if "public_url" in mm:
            self.mm_public_url = mm["public_url"]

        ah = data.get("agent_harness", {}) or {}
        if "url" in ah:
            self.agent_harness_url = ah["url"]

        # Legacy scalar first — applied to claude only. Warn so operators
        # migrate. The per-backend table below is applied last so an
        # operator who has both keys gets the explicit table (their
        # migration in progress), not the deprecated scalar.
        legacy_model = data.get("default_model")
        if legacy_model:
            logger.warning(
                "TOML key `default_model` is deprecated — applying %r to claude. "
                "Use a `[default_models]` table (e.g. `claude = \"opus\"`, "
                "`codex = \"gpt-5.5\"`) instead.",
                legacy_model,
            )
            self.default_models["claude"] = str(legacy_model)
        # New per-backend table — wins over the legacy scalar. Merge into
        # the built-ins rather than replacing so a partial table
        # (e.g. only ``claude = "sonnet"``) keeps codex=gpt-5.5 alive —
        # without that, codex sessions silently revert to no-model-hint.
        dm_table = data.get("default_models")
        if isinstance(dm_table, dict):
            self.default_models.update({
                str(k).lower(): str(v) for k, v in dm_table.items() if v
            })

        # Per-backend model catalog for `.models`. Replaces (not merges) —
        # the operator owns the full list per backend.
        models_table = data.get("models")
        if isinstance(models_table, dict):
            self.models = {
                str(k).lower(): [str(v) for v in vals]
                for k, vals in models_table.items()
                if isinstance(vals, (list, tuple))
            }

    def _apply_env(self) -> None:
        env = os.environ
        if "MM_URL" in env:
            self._apply_mm_url(env["MM_URL"])
        if "MM_PORT" in env:
            self.mm_port = int(env["MM_PORT"])
        if "MM_SCHEME" in env:
            self.mm_scheme = env["MM_SCHEME"]
        if "MM_BOT_TOKEN" in env:
            self.mm_bot_token = env["MM_BOT_TOKEN"]
        if "MM_TEAM" in env:
            self.mm_team = env["MM_TEAM"]
        if "MM_PUBLIC_URL" in env:
            self.mm_public_url = env["MM_PUBLIC_URL"]
        if "AH_URL" in env:
            self.agent_harness_url = env["AH_URL"]
        if "MM_BRIDGE_DEFAULT_CWD" in env:
            self.default_cwd = env["MM_BRIDGE_DEFAULT_CWD"]
        if "MM_BRIDGE_DEFAULT_BACKEND" in env:
            self.default_backend = env["MM_BRIDGE_DEFAULT_BACKEND"]
        # Legacy scalar first — applied to claude only. Per-backend env
        # vars below override, so an operator who's set both gets the
        # per-backend opt-in (their migration in progress) rather than the
        # deprecated scalar silently winning.
        if "MM_BRIDGE_DEFAULT_MODEL" in env:
            legacy = env["MM_BRIDGE_DEFAULT_MODEL"]
            logger.warning(
                "MM_BRIDGE_DEFAULT_MODEL is deprecated — applying %r to claude. "
                "Use MM_BRIDGE_DEFAULT_MODEL_CLAUDE / MM_BRIDGE_DEFAULT_MODEL_CODEX "
                "etc. for per-backend defaults.",
                legacy,
            )
            if legacy:
                self.default_models["claude"] = legacy
            else:
                self.default_models.pop("claude", None)
        # Per-backend env vars: ``MM_BRIDGE_DEFAULT_MODEL_<BACKEND>``.
        # Empty string removes the default for that backend (so the harness
        # picks). Anything else overrides the built-in.
        for key, value in env.items():
            if not key.startswith("MM_BRIDGE_DEFAULT_MODEL_"):
                continue
            backend = key[len("MM_BRIDGE_DEFAULT_MODEL_"):].lower()
            if not backend:
                continue
            if value:
                self.default_models[backend] = value
            else:
                self.default_models.pop(backend, None)
        if "MM_BRIDGE_DEFAULT_AUTORESPOND" in env:
            self.default_autorespond = env["MM_BRIDGE_DEFAULT_AUTORESPOND"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_SHOW_TOOL_USE" in env:
            self.show_tool_use = env["MM_SHOW_TOOL_USE"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_MIRROR_DIRECT_USER_MESSAGES" in env:
            self.mirror_direct_user_messages = (
                env["MM_MIRROR_DIRECT_USER_MESSAGES"].lower()
                in ("1", "true", "yes", "on")
            )
        if "MM_MIRROR_EXTERNAL_SESSIONS" in env:
            self.mirror_external_sessions = (
                env["MM_MIRROR_EXTERNAL_SESSIONS"].lower()
                in ("1", "true", "yes", "on")
            )
        if "MM_MENTION_USER_WHEN_DONE" in env:
            self.mention_user_when_done = env["MM_MENTION_USER_WHEN_DONE"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_AUTO_JOIN" in env:
            self.auto_join_public_channels = env["MM_AUTO_JOIN"].lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_BRIDGE_STATE" in env:
            self.state_file = env["MM_BRIDGE_STATE"]
        if "MM_BRIDGE_SIDECAR_DIR" in env:
            self.sidecar_dir = env["MM_BRIDGE_SIDECAR_DIR"]
        if "MM_BRIDGE_HELD_POSTS" in env:
            self.held_posts_file = env["MM_BRIDGE_HELD_POSTS"]
        if "MM_COALESCE_POSTS" in env:
            self.coalesce_posts = env["MM_COALESCE_POSTS"].strip().lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_COALESCE_MAX_HELD" in env:
            # A typo'd cap keeps the built-in default rather than taking the
            # daemon down on boot — the buffer is a safety feature, so its
            # misconfiguration must not be fatal.
            try:
                self.coalesce_max_held = int(env["MM_COALESCE_MAX_HELD"])
            except ValueError:
                logger.warning(
                    "MM_COALESCE_MAX_HELD=%r is not an integer — keeping %d",
                    env["MM_COALESCE_MAX_HELD"], self.coalesce_max_held,
                )
        if "MM_NAG_ENABLED" in env:
            self.nag_enabled = env["MM_NAG_ENABLED"].strip().lower() in (
                "1", "true", "yes", "on",
            )
        if "MM_AWAITING_NAG_AFTER_SECONDS" in env:
            # A typo'd threshold keeps the built-in default rather than taking
            # the daemon down on boot — same discipline as MM_COALESCE_MAX_HELD.
            try:
                self.awaiting_nag_after_seconds = float(
                    env["MM_AWAITING_NAG_AFTER_SECONDS"]
                )
            except ValueError:
                logger.warning(
                    "MM_AWAITING_NAG_AFTER_SECONDS=%r is not a number — "
                    "keeping %s", env["MM_AWAITING_NAG_AFTER_SECONDS"],
                    self.awaiting_nag_after_seconds,
                )
        if "MM_OPERATOR_USERNAME" in env:
            self.operator_username = env["MM_OPERATOR_USERNAME"].strip().lstrip("@")

        if "MM_BRIDGE_DANGEROUS_PERMISSIONS" in env:
            self.dangerous_permissions = (
                env["MM_BRIDGE_DANGEROUS_PERMISSIONS"].strip().lower()
                in ("1", "true", "yes", "on")
            )


@dataclass(frozen=True)
class Anchor:
    """A conversation anchor: a channel, optionally narrowed to a thread root.

    Channel sessions carry ``root_id=None``; thread-fork sessions carry the
    root post id of the thread they inhabit. The class is frozen so it's
    hashable and safe as a dict key.

    Empty-string ``root_id`` is normalised to ``None`` so JSON round-trips
    and caller shortcuts (``Anchor(ch, "")``) produce canonical anchors.
    """

    channel_id: str
    root_id: str | None = None

    def __post_init__(self) -> None:
        if self.root_id == "":
            # dataclass is frozen — bypass __setattr__ for normalisation.
            object.__setattr__(self, "root_id", None)

    @property
    def is_thread(self) -> bool:
        return self.root_id is not None


# JSON state-file schema version. v2→v3 collapsed channel_to_session +
# thread_mapping into `entries`. v3→v4 added `last_event_seq` for resuming
# the agent-harness SSE stream from a known cursor across bridge restarts.
# v4→v5 adds `adopted_session_ids` so the bootstrap auto-spawn-channel
# path doesn't re-surface external sessions the bridge already replaced.
# v5→v6 adds an optional per-entry `state` object (F2 turn-end state):
# `{kind, on, note, set_at, source}`. It rides the entry rather than living
# in a sibling file because a state's lifetime IS its session's — `unlink()`
# and anchor replacement then drop it for free (see
# specs/20260901-turn-state-fleet/design.md §2).
STATE_SCHEMA_VERSION = 6


@dataclass
class ChannelMapping:
    """Persistent mapping: session ↔ conversation anchor.

    Every session has exactly one :class:`Anchor`. Channel sessions have
    ``root_id=None``; thread-fork sessions carry the root post id. One
    forward map (``anchor → session``) and one reverse (``session → anchor``)
    keep both kinds of session discoverable through the same API.

    On-disk schema:

    * v2 (legacy): ``{"channel_to_session": {...}, "thread_mapping": {...}}``
      — read transparently on load and re-emitted as the current version
      on the next save.
    * v3: ``{"version": 3, "entries": [{"channel_id", "root_id", "session_id"}]}``.
    * v4: adds ``"last_event_seq": <int|null>`` for SSE-cursor resume.
    * v5: adds ``"adopted_session_ids": [...]`` listing harness session ids
      whose channel mapping was replaced by ``_replace_external_session``.
      Bootstrap consults this set before auto-spawning a recovery channel
      for an unmapped external session — otherwise every restart resurrects
      adopted sessions as fresh orphan channels.
    * v6: adds an optional ``"state"`` object per entry — the session's
      turn-end state (F2). Unknown/corrupt state objects are dropped
      individually; a bad state never costs an entry its mapping.
    """

    anchor_to_session: dict[Anchor, str] = field(default_factory=dict)
    session_to_anchor: dict[str, Anchor] = field(default_factory=dict)
    # session_id → its last declared state. Keyed by session and populated
    # ONLY for mapped sessions, so it is bounded by `session_to_anchor` and
    # cannot leak entries for sessions that have gone away.
    session_states: dict[str, SessionState] = field(default_factory=dict)
    last_event_seq: int | None = None
    adopted_session_ids: set[str] = field(default_factory=set)
    _path: str = ""
    _sidecar_dir: Path | None = None

    @classmethod
    def load(
        cls,
        path: str,
        sidecar_dir: Path | str | None = None,
        *,
        reconcile_sidecars: bool = True,
    ) -> "ChannelMapping":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        sd = Path(sidecar_dir) if sidecar_dir else sidecar.DEFAULT_DIR
        m = cls(_path=path, _sidecar_dir=sd)
        if p.exists():
            data = json.loads(p.read_text())
            m._ingest(data)
        if reconcile_sidecars:
            sidecar.reconcile(
                sd,
                {
                    sid: (a.channel_id, a.root_id)
                    for sid, a in m.session_to_anchor.items()
                },
            )
        return m

    def _ingest(self, data: dict) -> None:
        """Populate in-memory maps from any supported on-disk schema."""
        version = data.get("version")
        if version in (3, 4, 5, 6) and isinstance(data.get("entries"), list):
            for entry in data["entries"]:
                sid = entry.get("session_id")
                cid = entry.get("channel_id")
                if not sid or not cid:
                    continue
                rid = entry.get("root_id") or None
                self._add(Anchor(cid, rid), sid)
                if version >= 6:
                    state = SessionState.from_json(entry.get("state"))
                    if state is not None:
                        self.session_states[sid] = state
            if version >= 4:
                seq = data.get("last_event_seq")
                self.last_event_seq = seq if isinstance(seq, int) else None
            if version >= 5:
                adopted = data.get("adopted_session_ids") or []
                if isinstance(adopted, list):
                    self.adopted_session_ids = {
                        s for s in adopted if isinstance(s, str)
                    }
            return
        # Legacy v2 (or the even older v1 that lacked thread_mapping).
        for cid, sid in (data.get("channel_to_session") or {}).items():
            if cid and sid:
                self._add(Anchor(cid), sid)
        for key, sid in (data.get("thread_mapping") or {}).items():
            if not sid or not key:
                continue
            cid, _, rid = key.partition(":")
            if cid and rid:
                self._add(Anchor(cid, rid), sid)

    def _add(self, anchor: Anchor, session_id: str) -> None:
        """Insert an (anchor, session) pair, clearing any stale reverse row."""
        prev_session = self.anchor_to_session.get(anchor)
        if prev_session and prev_session != session_id:
            self.session_to_anchor.pop(prev_session, None)
            # The replaced session is gone (`.model`/`.backend`/`.cwd` swap,
            # external-session adoption). Its state goes with it — that is
            # the whole reason state lives here and not in a sibling file.
            self.session_states.pop(prev_session, None)
        self.anchor_to_session[anchor] = session_id
        self.session_to_anchor[session_id] = anchor

    def save(self) -> None:
        """Write the whole file atomically (temp + ``os.replace``).

        Atomic because this file gained a concurrent out-of-process reader:
        ``mm-bridge fleet`` reads the persisted states while the daemon is
        writing them, and the SSE cursor calls ``save()`` on a 2-second
        throttle for the life of every busy stream. A bare ``write_text``
        would eventually hand the CLI a half-written file. Mirrors
        ``HeldPostStore._save``; the temp file is removed on failure so a
        full disk can't litter the state directory.
        """
        entries = []
        for a, sid in self.anchor_to_session.items():
            entry = {
                "channel_id": a.channel_id,
                "root_id": a.root_id,
                "session_id": sid,
            }
            state = self.session_states.get(sid)
            if state is not None:
                entry["state"] = state.to_json()
            entries.append(entry)
        payload = json.dumps(
            {
                "version": STATE_SCHEMA_VERSION,
                "entries": entries,
                "last_event_seq": self.last_event_seq,
                "adopted_session_ids": sorted(self.adopted_session_ids),
            },
            indent=2,
        )
        path = Path(self._path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(payload)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def set_event_seq(self, seq: int) -> None:
        """Update the persisted SSE cursor and flush to disk.

        Monotonic: callers that observe events out of order — or replay an
        event with a stale sequence on reconnect — won't accidentally
        rewind the cursor.
        """
        if self.last_event_seq is not None and seq <= self.last_event_seq:
            return
        self.last_event_seq = seq
        self.save()

    def reset_event_seq(self, seq: int) -> None:
        """Non-monotonic reset of the cursor — for explicit recovery flows
        (e.g. detected harness restart where the in-memory event bus
        rolled back to 0). Bypasses ``set_event_seq``'s monotonic guard."""
        self.last_event_seq = seq
        self.save()

    def mark_adopted(self, session_id: str) -> None:
        """Record that ``session_id`` was replaced by a fresh harness session.

        Persisted so a subsequent bootstrap (after a bridge restart) doesn't
        treat the still-present external session as a candidate for
        auto-channel-spawn recovery. Idempotent: a repeat call is a no-op.
        """
        if session_id in self.adopted_session_ids:
            return
        self.adopted_session_ids.add(session_id)
        self.save()

    # ----- anchor API -----

    def link(self, anchor: Anchor, session_id: str) -> None:
        """Bind `session_id` to `anchor`, replacing any prior binding."""
        self._add(anchor, session_id)
        self.save()
        if self._sidecar_dir is not None:
            sidecar.write(
                self._sidecar_dir, session_id, anchor.channel_id, anchor.root_id,
            )

    def unlink(self, anchor: Anchor) -> str | None:
        """Remove `anchor`'s binding; return the session_id it held, or None."""
        session_id = self.anchor_to_session.pop(anchor, None)
        if session_id:
            self.session_to_anchor.pop(session_id, None)
            self.session_states.pop(session_id, None)
            self.save()
            if self._sidecar_dir is not None:
                sidecar.delete(self._sidecar_dir, session_id)
        return session_id

    def get_session(self, anchor: Anchor) -> str | None:
        return self.anchor_to_session.get(anchor)

    def get_anchor(self, session_id: str) -> Anchor | None:
        return self.session_to_anchor.get(session_id)

    # ----- turn-end state (F2) -----

    def get_state(self, session_id: str) -> SessionState | None:
        return self.session_states.get(session_id)

    def set_state(self, session_id: str, state: SessionState | None) -> None:
        """Persist `session_id`'s state (or clear it with ``None``).

        A state for an UNMAPPED session is dropped with a debug log rather
        than stored: states ride entries on disk, so it could never be
        persisted anyway, and storing it in memory would be an unbounded
        leak keyed by a session nothing else references.
        """
        if state is not None and session_id not in self.session_to_anchor:
            logger.debug(
                "Ignoring state for unmapped session %s", session_id[:8],
            )
            return
        if state is None:
            if self.session_states.pop(session_id, None) is None:
                return
        else:
            if self.session_states.get(session_id) == state:
                return
            self.session_states[session_id] = state
        self.save()
