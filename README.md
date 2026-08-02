# Mattermost agent bridge

Use your Claude Code, Codex, and pi coding agents from Mattermost! Each channel is a
session. Interact with the same session with multiple people. Attach files and images to
give them to the coding agent, and the coding agent can send files back as well. Create
new sessions by opening a new channel, or open previous coding agent sessions as a
Mattermost channel.

The coding agent knows it lives in a Mattermost channel. It can create new channels, and
send and receive messages to and from other channels. This way, a mattermost channel
can be used as an orchestrator for other agents living in other channels, creating an agent-swarm.

## Install

Clone this repo, open it in Claude Code, Codex, or pi on the machine that should run your
agents, and say:

> **Install this.** I don't have a Mattermost server yet — set one up on this machine too.

…or point it at a Mattermost you already run. The agent interviews you first, then follows
[`INSTALL.md`](INSTALL.md) — a plain numbered runbook you can also drive yourself. Just
the Python package: `uv sync && uv run mm-bridge serve` (Python 3.11+). The package, the
command, and the daemon are all called `mm-bridge`.

**Be aware:** the agent runs as you, on your machine, with your credentials. Anyone who can
post in a bridged channel can drive it. Treat channel membership like shell access.

## Talking to the bot

- **Mention it** (`@b3mo …`) — the default. Or `.autorespond on` and every message in the
  channel reaches the agent.
- **`.stop`** interrupts the running turn.
- **`@b3mo catch up 50`** feeds the last 50 channel messages into the session. (Happens
  automatically on the first message.)
- **`@b3mo leave`** sends the bot out of the channel.

A channel the bot has joined but nobody has engaged yet is **dormant**: no session, no
model, no cost. Configure it (`.model`, `.backend`, `.autorespond`) before the first real
message and those settings — stored in the Channel Purpose — apply when the session is
created.

### Which directory a session starts in

Four levers, broadest to narrowest:

| Lever | Applies to | Where you set it |
|---|---|---|
| `default_cwd` | every new session | `config.toml` (or `MM_BRIDGE_DEFAULT_CWD`) |
| `mm-bridge spawn --cwd <path>` | the one session being spawned | at spawn time |
| `cwd=<path>` in the **Channel Purpose** | that channel's sessions | Channel Menu → Edit Channel Purpose |
| `.cwd <path>` | that channel's sessions | typed in the channel |

The last two are one setting from two directions: `.cwd <path>` validates the path, then
writes `cwd=<path>` into the Purpose. Prefer `.cwd` — it checks before committing, expands
`~`, and says which check failed, whereas the Purpose parser is strict (absolute only, no
`~`) and a value it can't use degrades silently to `default_cwd`. A path containing a comma
can't be stored either way, since the Purpose is a comma-separated list; `.cwd` refuses it
rather than writing something that reparses into a truncated path.

A cwd only takes effect when a session is **created**, so changing it in an active channel
recreates the session — the harness has no cwd-mutate endpoint. That's why `.cwd <path>`
refuses while a run is in flight (`.stop` it first) and inside a thread fork, exactly like
`.model` / `.backend`. With `allowed_attachment_roots` configured, a cwd must sit under one
of those roots: `.cwd` rejects an out-of-root path inline and names the roots, and an
out-of-root `cwd=` in the Purpose is dropped with a warning. `.status` shows what's in
effect.

### In-channel dot-commands

The **bridge** handles these itself — they bypass the mention gate and are never
forwarded to the agent. An unknown `.word` gets a "try `.help`" reply.

| Command | What it does |
|---|---|
| `.help` | List these commands. |
| `.stop` | Interrupt the running turn in this channel. |
| `.autorespond [on\|off]` | Reply to every message, or only when mentioned (bare = toggle). |
| `.status` | Session id, backend, model, cwd, autorespond flag, run state, harness health. |
| `.model [<name>]` | Show or switch the model. Names are free text; a bad one fails loudly when the backend starts. |
| `.backend [<name>]` | Show or switch the backend (`claude`, `codex`, `pi`, …). Switching **resets the model** to that backend's default. |
| `.cwd [<path>]` | Show or set the working directory — see [above](#which-directory-a-session-starts-in). The path must be absolute (`~` is expanded), exist, and contain no comma; it's persisted as `cwd=<path>` in the Channel Purpose. |
| `.models` | Models available for this channel's backend, current one marked. |
| `.running` | Sessions with a run in flight right now. |
| `.sessions [N]` | The N most recent sessions across all agents, including terminal ones. Each shows its channel or an `.invite` hint. |
| `.invite <session-id>` | Get added to a session's channel, creating it for unmapped/terminal sessions. |

Switching model, backend or directory in an **active** channel recreates the session, so
`.stop` a running turn first. Inside a **thread fork**, reading works but switching is
refused — a restart would replace the *channel's* session, not the thread's; switch from
the channel. A switch is also refused when the bridge can't read the Channel Purpose
(Mattermost unreachable): the settings it isn't changing live there, so restarting would
have to guess them — and it couldn't write the result back either.
The global listings (`.sessions`, `.running`, `.invite`) reveal operator-wide state, so in
a dormant channel they need an explicit mention.

## Commands the agent (or you) can run

These work inside any session that has a sidecar — i.e. an agent running on the same host
as the daemon. All of them accept `--channel <id>` to target another channel.

| Command | What it does |
|---|---|
| `mm-bridge serve` | Run the daemon (Mattermost WebSocket + REST ⇄ harness SSE). |
| `mm-bridge doctor` | Diagnose the local install: config, Mattermost auth, harness, sidecar dir. |
| `mm-bridge invite <user>` | Invite a Mattermost user into this session's channel. |
| `mm-bridge channel` | Print this session's `channel_id` (scripting/debug). |
| `mm-bridge channels [--title <kw>]` | List channels the bot can see, most recently active first. |
| `mm-bridge post [--file <path>] "<msg>"` | Post a message (`-` reads the body from stdin). |
| `mm-bridge read [-n N] [--since 1h]` | Print recent posts — how one agent reads another's channel. |
| `mm-bridge spawn "<prompt>"` | Start a sub-session in a new sibling channel. |

### `mm-bridge spawn`

```bash
mm-bridge spawn --title "Refactor the parser" --cwd ~/projects/foo --invite alice "…"
```

- `--title` — channel display name (default: derived from the prompt).
- `--cwd` — working directory for the new session.
- `--backend claude|codex|pi` and `--model <model>` — override the config defaults.
- `--invite <user>` — pull someone into the new channel.
- `--no-forward-prompt` — don't echo the kickoff message into the parent channel.

Pass `-` as the prompt to read it from stdin — the way to dispatch a long structured
brief without shell-quoting it:

```sh
mm-bridge spawn --title "Refactor" - <<'EOF'
Multi-line brief…
EOF
```

The full prompt reaches the sub-session verbatim; only the preview quoted into the
channels is capped (~12k chars) to stay under Mattermost's post limit. An empty or
non-piped stdin is rejected rather than dispatching a blank brief.

The parent channel gets a `:thread: Spawned **Title** in ~slug~` announcement, and the new
channel's header points back at its parent — so the tree is walkable from either end.

### Directives inside a reply

When the agent runs on the same host as the daemon, the bridge acts on directives in its
reply and strips them from the visible post:

- `<openFile path="/abs/path" [line="N"] />` — upload that file (must live under an
  allowed root; see `allowed_attachment_roots`).

[`CLAUDE-include.md`](CLAUDE-include.md) is the prompt snippet that teaches Claude how to
use all of this — drop it into your `CLAUDE.md`.

## Configure

Precedence: **class defaults < TOML file < environment variables**.

### TOML

Default path `~/.config/mm-bridge/config.toml` (override with `MM_BRIDGE_CONFIG`).

```toml
# ── Top-level session defaults ──────────────────────────────────────────────
# These keys are read from the TOP LEVEL of the file, so they MUST appear before
# the [mattermost] / [agent_harness] section headers further down. In TOML every
# key after a `[section]` header belongs to that section — put these under one
# and they're silently ignored (you fall back to the built-in defaults).

# Applied when a new session is created.
default_backend   = "claude"   # or "codex", "pi"
default_cwd       = "~/projects"   # your CODE root, not the install dir.
                                   # Unset, this falls back to your home directory —
                                   # set it explicitly. Must exist.
default_autorespond = false

# Per-backend default model, used when a channel / spawn doesn't pin one.
# This table also decides which backends get advertised in the welcome post —
# a backend with no default model here isn't offered to users.
# (The old scalar `default_model = "opus"` still works and maps onto `claude`.)
default_models = { claude = "opus", codex = "gpt-5.5" }

# Optional per-backend model catalog for the in-channel `.models` command.
# agent-harness's /v1/backends/{b}/models returns [] for every backend today,
# so this operator-maintained list is what `.models` shows (merged with the
# harness catalog once it's populated). `.model <name>` accepts free text
# regardless of this list.
models = { claude = ["opus", "sonnet", "haiku"], codex = ["gpt-5.5", "gpt-5.4-mini"] }

# Coalesce tool-use events into one per-turn placeholder post (edited as more
# tools run, left as a compact summary when the turn ends). Set false to hide
# them entirely — channels then carry only real replies and tool errors.
show_tool_use = true

# Mirror turns typed directly into the agent's own UI/CLI back into the bound
# channel as `_via coding agent:_ <body>` posts, so chat watchers see the full
# conversation. Bridge-originated sends and tool results are never mirrored.
mirror_direct_user_messages = true
direct_user_message_dedup_window_seconds = 30.0

# Auto-join: silently join every public channel the bot can see. Sessions are
# NOT created until someone actually engages the bot.
auto_join_public_channels  = false
auto_join_reconcile_seconds = 5.0

# Attachment safety — <openFile path="..."> only resolves files under these.
allowed_attachment_roots = ["~/projects"]

# State + sidecar paths.
state_file  = "~/.config/mm-bridge/state.json"
sidecar_dir = "~/.mm-bridge/sessions"

# Catch-up: inject the last N channel messages into a newly-created session
# so the model sees prior context (0 disables).
initial_catch_up_n = 50
catch_up_default_n = 50
catch_up_max_n     = 500

# ── Sections (must come last, after all the top-level keys above) ────────────
[mattermost]
url = "localhost"
port = 8065
scheme = "http"
team = "workspace"

# Optional user-facing base URL for permalinks the daemon embeds in headers and
# messages. Handy when the daemon reaches MM on localhost but humans reach it
# via a Tailscale hostname.
public_url = "http://mm.example.com:8065"

[agent_harness]
url = "http://localhost:8877"
```

### Environment

`.env` is not committed. All optional except `MM_BOT_TOKEN`:

| Variable | Purpose |
| --- | --- |
| `MM_BOT_TOKEN` | **Required.** Personal-access or bot token for the Mattermost bot. |
| `MM_URL` | Bare hostname or full URL (`http://host:port`). |
| `MM_PORT`, `MM_SCHEME` | Override parts of the URL. |
| `MM_TEAM` | Team slug the bot operates in. |
| `MM_PUBLIC_URL` | User-facing base URL for permalinks (see TOML `public_url`). |
| `AH_URL` | agent-harness server URL. |
| `MM_BRIDGE_DEFAULT_CWD` | Default working directory for new sessions. |
| `MM_BRIDGE_DEFAULT_BACKEND` | `claude`, `codex`, `pi`, … |
| `MM_BRIDGE_DEFAULT_MODEL` | Model slug (empty string → unset). |
| `MM_BRIDGE_DEFAULT_AUTORESPOND` | `1/true/yes/on` to enable autorespond by default. |
| `MM_SHOW_TOOL_USE` | Toggle `show_tool_use` without editing TOML. |
| `MM_MIRROR_DIRECT_USER_MESSAGES` | Toggle `mirror_direct_user_messages` without editing TOML. |
| `MM_AUTO_JOIN` | Toggle `auto_join_public_channels` without editing TOML. |
| `MM_BRIDGE_STATE` | Path to the state JSON. |
| `MM_BRIDGE_SIDECAR_DIR` | Sidecar directory. |
| `MM_BRIDGE_CONFIG` | Path to the TOML file. |

## Under the hood

**State file** — the canonical `session ↔ Anchor(channel_id, root_id?)` map. JSON, v3
schema; v2 is read transparently and re-emitted as v3 on the next save.

**Sidecar dir** — one file per session (`~/.mm-bridge/sessions/<session_id>`) holding the
channel id: one line for a channel session, two for a thread fork. `0700` directory,
`0600` files, reconciled from the state file at startup. This file is how an agent process
knows it's "live in Mattermost" and can use `invite` / `spawn` / `channel`.

<details>
<summary><b>How the CLI figures out which session it's running in</b> (four sources, in order)</summary>

1. **`CLAUDE_SESSION_ID`** — set by Claude Code's SessionStart hook
   (`~/.claude/hooks/export-session-id.sh`).
2. **`MM_BRIDGE_SESSION_ID`** — backend-agnostic env var. agent-harness pins it into
   backend tool-shell environments where it can.
3. **Live-codex parent (`/proc` tie-breaker)** — Linux-only. When the env vars miss, walk
   the parent-pid chain (depth ≤ 8) for a process whose `/proc/<pid>/comm` is `codex` and
   read the rollout filename out of its open fds; the UUID in that filename is adopted
   directly. This is what disambiguates *multiple codex sessions in the same cwd* — only
   the codex in our actual ancestor chain wins. Returns nothing on macOS (no `/proc`), for
   background tasks whose codex parent already exited, or when the ancestor holds no
   rollout fd — those fall through to step 4.
4. **Cwd-matched codex rollout** — scans `~/.codex/sessions/**/rollout-*.jsonl` in
   most-recently-active order and walks candidates whose `payload.cwd` matches the
   canonicalised caller cwd, adopting the first whose sidecar reads back as a valid
   channel anchor. Covers tool shells whose launcher couldn't pre-pin the env var
   (typically the first turn of a fresh session) and shells that outlive their parent.

There's a brief startup race between an agent starting and the daemon writing the sidecar.
Invoked in that window, the CLI fails cleanly with a "not in MM channel" error.

</details>

## Development

```bash
uv run -m pytest
```

Design docs for the current architecture live under [`specs/`](specs/) — one directory per
feature, overview + requirements + design.

## License

MIT — see [`LICENSE`](LICENSE).
