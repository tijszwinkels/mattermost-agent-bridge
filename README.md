# mm-bridge

**Chat-ops for coding agents.** Run, steer, fork, and spawn Claude Code / Codex sessions straight from your team chat — one Mattermost channel (or thread) per session.

## Quickstart — agent-first install

The install is itself an agent task. Clone this repo, open it in **Claude Code** (or **Codex**), and say:

> **install this**

The agent follows [`INSTALL.md`](INSTALL.md): it interviews you for the few things it can't infer (Mattermost URL + bot token, the host user, which projects dir) and stands up the full stack — Mattermost, agent-harness, and this bridge — verifying every step with `mm-bridge doctor`. Prefer to drive it yourself? `INSTALL.md` is a plain numbered runbook you can follow by hand.

## How it works

A daemon connects a Mattermost bot account (conventionally `@b3mo`) to an agent-harness instance and maps conversations to sessions:

- **A channel** the bot joins or is invited to → a dormant, configurable presence. The first conversational message creates one agent-harness session; assistant replies stream back as posts.
- **A thread** inside a channel → its own *forked* session, so side-quests don't pollute the main conversation.
- **`mm-bridge spawn "<prompt>"`** from inside a session → a fresh sibling channel with its own session, with the parent announced via a post that links the child's `~channel~` header back upstream.

Each session gets a small **sidecar file** (`~/.mm-bridge/sessions/<session_id>`) so a Claude Code or codex session running on the same machine can self-identify as "live in Mattermost" and use the CLI helpers (`invite`, `spawn`, `channel`).

The CLI discovers the current session id from one of four sources, in order:

1. **`CLAUDE_SESSION_ID`** — populated by Claude Code's SessionStart hook (`~/.claude/hooks/export-session-id.sh`).
2. **`MM_BRIDGE_SESSION_ID`** — backend-agnostic env var. agent-harness pins this into backend tool-shell environments when available.
3. **Live-codex parent (`/proc` tie-breaker)** — Linux-only. When env vars miss, walk the parent-pid chain (depth ≤ 8) for a process whose `/proc/<pid>/comm` is `codex` and read the rollout filename out of its open fds. The UUID embedded in that filename is adopted directly. This is what disambiguates the "multiple codex sessions in the same cwd" case: only the codex actually in our ancestor chain is the one we belong to. Returns nothing on macOS (no `/proc`), background tasks where the codex parent already exited, or when the codex ancestor has no rollout fd held open — those cases fall through to step 4.
4. **Cwd-matched codex rollout** — final fallback that scans `~/.codex/sessions/.../rollout-*.jsonl` in most-recently-active order and walks candidates whose `payload.cwd` matches the (canonicalised) caller cwd, adopting the first one whose sidecar reads back as a valid channel anchor. Helps tool shells whose codex launcher couldn't pre-pin the env var (typically the very first turn of a fresh session) and tool shells that outlive their codex parent. Note: there's a brief startup race between codex starting and the daemon writing the sidecar — if mm-bridge is invoked in that window the fallback fails cleanly with a "not in MM channel" error, which is the same behaviour the Claude Code path has had.

## Requirements

mm-bridge is glue between three things you must already run yourself — it does **not** bundle or install any of them:

- **A self-hosted Mattermost** you control, plus a **bot account** on it and a **personal access token** for that bot (the `MM_BOT_TOKEN` the daemon authenticates with). A hosted/Cloud Mattermost you can't create bot tokens on won't work.
- **A running [agent-harness](https://github.com/tijszwinkels/agent-harness) instance** — the daemon subscribes to its SSE stream and drives sessions/runs through it. mm-bridge is useless without one reachable.
- **The agent CLIs themselves — Claude Code and/or Codex — installed on the *same host* as the harness.** Sessions run as local processes and self-identify via the sidecar file, so the bridge, the harness, and the agent CLIs must be co-located.
- **Linux is preferred.** The `/proc` codex-session tie-breaker (source #3 above) is Linux-only; on macOS the CLI falls back to the cwd-matched rollout scan, which is less precise when multiple codex sessions share a working directory.

## Install from source (manual)

Prefer the agent-first [Quickstart](#quickstart--agent-first-install) for the full stack;
this is the bare Python-package install. Requires Python 3.11+. Using
[`uv`](https://github.com/astral-sh/uv):

```bash
uv sync
uv run mm-bridge --help
```

Or install the console script into a venv:

```bash
uv pip install -e .
mm-bridge --help
```

## Configure

The bridge reads, in order of precedence: **class defaults < TOML file < environment variables**.

### TOML

Default path: `~/.config/mm-bridge/config.toml` (override with `MM_BRIDGE_CONFIG=/path/to/config.toml`).

```toml
# ── Top-level session defaults ──────────────────────────────────────────────
# These keys are read from the TOP LEVEL of the file, so they MUST appear before
# the [mattermost] / [agent_harness] section headers further down. In TOML every
# key after a `[section]` header belongs to that section — put these under one
# and they're silently ignored (you fall back to the built-in defaults).

# Session defaults applied when a new session is created.
default_backend   = "claude"   # or "codex"
default_cwd       = "~/projects"
default_autorespond = false

# Per-backend default model, applied when a channel / spawn doesn't pin one
# explicitly. (The old scalar `default_model = "opus"` is deprecated but still
# honoured — it maps onto `claude`.)
default_models = { claude = "opus", codex = "gpt-5.5" }

# Optional per-backend model catalog surfaced by the in-channel `.models`
# command. The agent-harness `/v1/backends/{b}/models` endpoint returns []
# for every backend today, so this operator-maintained list is the source
# `.models` shows (merged with the harness catalog once it's populated).
# `.model <name>` accepts any free-text name regardless of this list.
models = { claude = ["opus", "sonnet", "haiku"], codex = ["gpt-5.5", "gpt-5.4-mini"] }

# Coalesce Claude's tool-use events into one per-turn placeholder post
# (edited as more tools run, left as a compact summary when the turn
# ends). Set false to hide them entirely — channels then carry only
# real assistant replies and tool errors.
show_tool_use = true

# Mirror user turns typed directly into the agent's UI/CLI back into the
# bound MM channel as ``_via coding agent:_ <body>`` posts so MM watchers
# see the full conversation. Bridge-originated sends and tool results are
# never mirrored. Set false to keep direct-typed turns invisible to MM.
mirror_direct_user_messages = true
direct_user_message_dedup_window_seconds = 30.0

# Auto-join: silently join every public channel the bot can see.
# Sessions are NOT created until someone actually engages the bot.
auto_join_public_channels  = false
auto_join_reconcile_seconds = 5.0

# Attachment safety — <openFile path="..."> directives only resolve
# files under these roots.
allowed_attachment_roots = ["~/projects"]

# State + sidecar paths.
state_file  = "~/.config/mm-bridge/state.json"
sidecar_dir = "~/.mm-bridge/sessions"

# Catch-up: inject the last N channel messages into a newly-created
# session so the model sees prior context (0 disables).
initial_catch_up_n = 50
catch_up_default_n = 50
catch_up_max_n     = 500

# ── Sections (must come last, after all the top-level keys above) ────────────
# Mattermost server the daemon talks to.
[mattermost]
url = "localhost"
port = 8065
scheme = "http"
team = "workspace"

# Optional user-facing base URL used when the daemon embeds permalinks
# in channel headers / messages. Handy when the daemon reaches MM at
# localhost but humans reach it via a Tailscale hostname.
public_url = "http://mm.example.com:8065"

[agent_harness]
url = "http://localhost:8877"
```

### Environment

`.env` is not committed. The daemon reads these env vars (all optional except `MM_BOT_TOKEN`):

| Variable                 | Purpose                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `MM_BOT_TOKEN`           | **Required.** Personal-access or bot token for the Mattermost bot. |
| `MM_URL`                 | Bare hostname or full URL (`http://host:port`).                    |
| `MM_PORT`, `MM_SCHEME`   | Override parts of the URL.                                         |
| `MM_TEAM`                | Team slug the bot operates in.                                     |
| `MM_PUBLIC_URL`          | User-facing base URL for permalinks (see TOML `public_url`).       |
| `AH_URL`                 | agent-harness server URL.                                          |
| `MM_BRIDGE_DEFAULT_CWD`  | Default working directory for new sessions.                        |
| `MM_BRIDGE_DEFAULT_BACKEND` | `claude` or `codex`.                                            |
| `MM_BRIDGE_DEFAULT_MODEL` | Model slug (empty string → unset).                                |
| `MM_BRIDGE_DEFAULT_AUTORESPOND` | `1/true/yes/on` to enable autorespond by default.           |
| `MM_SHOW_TOOL_USE`       | Toggle `show_tool_use` without editing TOML.                       |
| `MM_MIRROR_DIRECT_USER_MESSAGES` | Toggle `mirror_direct_user_messages` without editing TOML. |
| `MM_AUTO_JOIN`           | Toggle `auto_join_public_channels` without editing TOML.           |
| `MM_BRIDGE_STATE`        | Path to the state JSON.                                            |
| `MM_BRIDGE_SIDECAR_DIR`  | Sidecar directory.                                                 |
| `MM_BRIDGE_CONFIG`       | Path to the TOML file.                                             |

## Commands

### `mm-bridge serve`

Runs the daemon. Connects to Mattermost (WebSocket + REST), subscribes to agent-harness SSE, and relays messages in both directions.

```bash
mm-bridge serve
```

### `mm-bridge invite <username>`

Inside a Claude Code or codex session that already has a sidecar, invites a Mattermost user to the session's channel:

```bash
mm-bridge invite alice
```

### `mm-bridge channel`

Prints the current session's `channel_id` (debug / scripting).

### `mm-bridge spawn [opts] "<prompt>"`

Creates a fresh sibling channel with its own agent-harness session and kicks off `<prompt>`. Options:

- `--title "<name>"` — display name for the new channel (default: derived from the prompt).
- `--cwd <path>` — working directory for the new session.
- `--backend claude|codex` — backend for the new session.
- `--model <model>` — model for the new session (e.g. `claude-fable-5`), overriding the per-backend config default.
- `--invite <user>` — invite a user to the new channel.
- `--no-forward-prompt` — don't post the kickoff message in the parent channel.

Pass `-` as the `<prompt>` to read it from stdin — the way to dispatch a long structured brief without shell-quoting it:

```sh
mm-bridge spawn --title "Refactor" - <<'EOF'
Multi-line brief…
EOF
```

The full stdin prompt is delivered to the sub-session verbatim; only the quoted preview posted to the parent/child channels is truncated (the rendered quote is capped at ~12k chars) so it stays under Mattermost's post-size limit. An empty or terminal (non-piped) stdin is rejected with an explicit error instead of dispatching a blank brief or hanging.

The parent channel gets a `:thread: Spawned **Title** in ~slug~` announcement (threaded under the originating thread when spawning from a thread-fork). The new channel's header is set to `Parent: ~parent-slug~`, with a `[thread](permalink)` suffix when spawned from a thread-fork.

## In-channel dot-commands

Type these in any active bridged channel (or thread) — the **bridge** handles
them itself, bypassing the mention-only gate, and never forwards them to the
agent. An unknown `.word` gets an "unknown command — try `.help`" reply rather
than reaching the agent.

Manual invites and auto-joins use the same **dormant channel** state: no
harness session or LLM turn exists yet. Channel-local commands — `.help`,
`.status`, `.stop`, `.backend`, `.model`, `.cwd`, `.models`, `.autorespond` — work
immediately without a mention (the config commands persist their settings in
the Channel Purpose). The first conversational post creates exactly one session
with the final configuration. Only global listings/actions (`.sessions`,
`.running`, `.invite`) reveal operator-wide state, so in a dormant channel they
require an explicit `@b3mo` mention; bare global or unknown dot-words are
ignored. Which commands need a mention is driven entirely by each command's
`global_scope` flag, so **every parsed dot-command is intercepted by the
bridge** — none can become or silence the first LLM turn. Posts arriving while
the first session is warming up are routed normally once mapping completes.

| Command | What it does |
|---|---|
| `.help` | List these commands. |
| `.stop` | Interrupt the running turn in this channel. |
| `.autorespond [on\|off]` | Reply to every message, or only when @mentioned (bare = toggle). Persisted in the Channel Purpose. |
| `.status` | Session id, backend, model, cwd, autorespond flag, run state, harness status. |
| `.model [<name>]` | Show or select the model. In a dormant channel it configures the future session without creating one. In an active channel it recreates the session, so `.stop` any active run first. Names are free text; a bad one fails loudly when the backend starts. |
| `.backend [<name>]` | Show or select the backend. In a dormant channel it configures the future session without creating one. In an active channel it recreates the session. Validated against known backends (`claude`, `codex`, …); changing it **resets the model to that backend's default**. |
| `.cwd [<path>]` | Show or set the working directory — see [Which directory a session starts in](#which-directory-a-session-starts-in). In a dormant channel it configures the future session without creating one. In an active channel it recreates the session there, so `.stop` any active run first. The path must be absolute (`~` is expanded), be an existing directory, and contain no comma; the absolute result is persisted as `cwd=<path>` in the Channel Purpose. |
| `.models` | List the available models for this channel's backend (from the `[models]` config table + the harness catalog), marking the current one. |
| `.running` | Sessions with a run in flight right now. |
| `.sessions [N]` | The N most recent sessions across all agents — including terminal (TUI) sessions not yet on Mattermost. Each shows its channel or an `.invite` hint. |
| `.invite <session-id>` | Get added to a session's Mattermost channel, creating it first for unmapped/terminal sessions. Posting into a resumed terminal session **forks** it (see the channel's bootstrap note). |

Before first engagement, `.status` reports the config the first message will
start with (backend, model, cwd, autorespond flag); `.stop` replies "No session
in this channel". `.model`, `.backend` and `.cwd` configure that future session
instead. Inside a **thread fork**, bare `.model` / `.backend` / `.cwd`
(read-only) work, but a *switch* (`.model <name>` / `.backend <name>` /
`.cwd <path>`) is refused — a restart would replace the channel's session, not
the thread's. Switch from the channel.

## Which directory a session starts in

Four levers, from broadest to narrowest:

| Lever | Applies to | Where you set it |
|---|---|---|
| `default_cwd` | every new session | `config.toml` (or `MM_BRIDGE_DEFAULT_CWD`) |
| `mm-bridge spawn --cwd <path>` | the one session being spawned | at spawn time (defaults to the caller's cwd) |
| `cwd=<path>` in the **Channel Purpose** | that channel's sessions | Channel Menu → Edit Channel Purpose |
| `.cwd <path>` | that channel's sessions | typed in the channel |

The last two are the same setting from two directions: `.cwd <path>` validates
the path and then writes `cwd=<path>` into the Channel Purpose, so a channel
configured either way behaves identically. Prefer `.cwd` — it checks the path
before committing to it, expands `~`, and says which check failed, whereas the
Purpose parser is strict (absolute paths only, no `~` expansion) and a value it
can't use degrades silently to `default_cwd`.

Because the Purpose is a **comma-separated** config list, a working directory
whose path contains a comma can't be stored either way — `.cwd` refuses it
outright rather than writing something that reparses into a truncated path.

A cwd only takes effect when a session is **created**, so changing it in an
active channel recreates the session — the harness has no cwd-mutate endpoint.
That's why `.cwd <path>` refuses while a run is in flight (`.stop` it first) and
inside a thread fork, exactly like `.model` / `.backend`.

When `allowed_attachment_roots` is configured, a session's cwd must sit under
one of those roots: `.cwd` rejects an out-of-root path inline and names the
roots, and an out-of-root `cwd=` token in the Purpose is dropped with a warning
posted to the channel.

## Inside-a-session directives

When a Claude / Codex session runs on the same host as the daemon, a couple of directives are recognized inside the assistant's reply:

- `<openFile path="/abs/path" [line="N"] />` — the bridge uploads the file (after checking it's under an allowed root) and strips the directive from the visible post.

See `CLAUDE-include.md` for the prompt snippet that teaches Claude how to use these.

## State & sidecar layout

- **State file** — canonical `session ↔ Anchor(channel_id, root_id?)` map. JSON, v3 schema; v2 is read transparently and re-emitted as v3 on the next save.
- **Sidecar dir** — one file per session: `<session_id>` containing the `channel_id` (one line for channel sessions, two for thread-forks). `0700` directory, `0600` files. Reconciled from the state file at startup.

## Specs

Design docs for the current architecture live under `specs/`.

## Tests

```bash
uv run -m pytest
```
