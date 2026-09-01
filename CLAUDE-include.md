## Mattermost integration

If `~/.mm-bridge/sessions/$CLAUDE_SESSION_ID` exists (Claude Code) or `~/.mm-bridge/sessions/$MM_BRIDGE_SESSION_ID` exists (codex via agent-harness), you're running inside a Mattermost channel. Your assistant replies flow to the channel automatically — no explicit "post" needed. The bot account you post as is `@claude` in Mattermost (`/invite @claude` pulls you into a channel).

The bridge CLI resolves the current session id via four sources, in order: `CLAUDE_SESSION_ID` env → `MM_BRIDGE_SESSION_ID` env → live-codex parent (`/proc` walk, Linux-only — finds the rollout fd held open by a `codex` ancestor) → cwd-matched codex rollout file (mtime walk, only adopted when a sidecar exists). The `/proc` tie-breaker disambiguates the case where multiple codex sessions share a cwd — only the one in our actual ancestor chain wins. Codex tool shells self-identify whether or not agent-harness pinned the env var.

- **CLI env (`MM_BOT_TOKEN` etc.).** Every `mm-bridge` subcommand that hits the MM API (`invite`, `spawn`, `channels`, `post`, `read`) needs `MM_BOT_TOKEN` (and `MM_URL`, `MM_TEAM`) in the environment. If you get `Error: MM_BOT_TOKEN environment variable is required`, source your bridge secrets: `set -a; source ~/.config/mm-bridge/env; set +a` (or the repo-local `.env` fallback, if that's where this host keeps them).
- **In-channel dot-commands.** Anyone in a bridged channel can type `.commands` that the bridge handles directly (no `@claude` mention needed; never forwarded to you as a turn): `.help`, `.stop` (interrupt the run), `.status`, `.autorespond [on|off]`, `.model [<name>]` (switch model — recreates the session, so `.stop` first), `.backend [<name>]` (switch backend — validated against the known set, recreates the session and resets the model to that backend's default, so `.stop` first), `.cwd [<path>]` (show/set the working directory — bare reads it, a path must be absolute (`~` expanded), exist, and contain no comma; recreates the session, so `.stop` first, and persists as `cwd=<path>` in the Channel Purpose), `.models` (list models for the backend), `.running` (sessions with a live run), `.sessions [N]` (recent sessions across all agents, incl. terminal ones), `.fleet [all]` (state of the channels you spawned — see below), `.invite <session-id>` (get added to a session's channel, creating it if needed). An unknown `.word` gets an "unknown command — try `.help`" reply.
- **Declaring how your turn ends (`<state/>`).** End a reply with one tag and the bridge
  remembers it, per session, with the moment you said it. It is stripped from the visible
  post like `<openFile/>`:

  ```
  <state kind="awaiting" on="lead" note="M0 gate" />
  ```

  `kind` is one of: `idle` (nothing outstanding — the default when you omit the tag, so
  you never *have* to use it), `awaiting` (you are blocked on a reply from someone and
  cannot proceed without it), `parked` (deliberately stopped, nothing expected — e.g. work
  is done for the night), `blocked` (something outside the conversation is stopping you:
  quota, a broken dependency, a failing service). `on` names who you are waiting on —
  `lead`, or a Mattermost username. `note` is a few words on what you are waiting for.

  Why it matters: `awaiting` is the only claim the bridge acts on. A session that stays
  `awaiting` past the nag threshold (30 min by default) causes ONE post into its parent
  channel — and a post IS a turn, so it wakes the party that forgot. At twice the threshold
  it escalates with an @-mention. So use `awaiting` when someone genuinely owes you an
  answer, and `parked` when nobody does. The last tag in a reply wins; an unknown `kind`
  leaves your state alone and gets you a one-line reply naming the valid ones.

- **Seeing what your children are doing.** `.fleet` (in-channel) or `mm-bridge fleet`
  (CLI, `--all`, `--json`) prints one row per channel you spawned: its declared state, how
  long it has been in it, whether a run is live right now, and how many posts are held for
  it. Use it before reporting on a lane — a builder that says `awaiting → lead   47 min` is
  waiting on *you*.

- **Getting human attention.** If the operator is in the channel, @mention them (`@username`). Otherwise — or if you're unsure — run `mm-bridge invite <username>` to pull them in. When multiple users are in the channel, each message is prefixed with `username:` — use the prefix to decide who to mention.
- **Attaching local files.** Emit `<openFile path="..." [line="N"] />` anywhere in your reply; the bridge uploads the file, strips the directive from the visible text, and attaches it to the post. The file must live inside an **allowed root**. The standard install provides **`~/mm-attachments/`** as a scratch dir for exactly this — write or copy the file there and attach it by path (e.g. `<openFile path="~/mm-attachments/report.html" />`), rather than handing it a `/tmp` path that the bridge can't read. Your session's working directory is always an allowed root too. If an attach is rejected, the bridge's warning lists the roots that *are* allowed (operators configure the set via `allowed_attachment_roots`).
- **Spawning sub-sessions.** Run `mm-bridge spawn [--title "<name>"] [--cwd <path>] [--backend claude|codex] [--model <model>] [--invite <user>] [--no-forward-prompt] "<prompt>"` to start a fresh agent-harness session in a new sibling Mattermost channel. `--model` picks the sub-session's model (e.g. `--model claude-fable-5`), overriding the per-backend config default. Pass `-` in place of `"<prompt>"` to read the prompt from stdin — the way to dispatch a long structured brief without shell-quoting it (`mm-bridge spawn --title "…" - <<'EOF'`). The full stdin prompt reaches the sub-session; only the quoted preview posted to the channels is truncated if it's very long. By default the parent channel gets a post linking to the new channel with the prompt quoted. The new channel's `header` is set to `Parent: ~<parent-channel>~` so context is discoverable from the sub-channel.
- **Discovering channels.** `mm-bridge channels [--title <kw>] [-n N]` lists channels the bot can see, sorted by recent activity. Filter by title substring; pipe the first column (channel_id) into `post` / `read` — or use the channel's **slug** directly (both `post` and `read` accept a 26-char channel id, a bare slug resolved against `MM_TEAM`, a channel URL `https://host/<team>/channels/<slug>`, or a permalink `https://host/<team>/pl/<post>` which also targets that post's thread). `mm-bridge channel` (singular) prints the current session's channel_id for scripts that want to target "myself".
- **Reading scrollback.** `mm-bridge read [--channel <channel>] [-n N] [--since 1h|2d|ISO]` prints recent posts from a channel (or the current session's channel if `--channel` is omitted). `--thread <root_post_id>` (or a permalink URL) restricts to a thread; an agent running inside a thread-forked session reads its own thread by default.
- **Posting ad-hoc.** `mm-bridge post [--channel <channel>] [--thread <root>] [--file <path>]... "<message>"` sends a one-off message. Omit `--channel` to post into the current session's channel. Use `-` in place of the message to read the body from stdin.
- **agentcom — talking to another bridge-backed agent in a different channel.** `mm-bridge post --channel <id>` and `mm-bridge read --channel <id>` are enough — no special protocol needed at the bridge level (the receiving agent just sees your post as a normal user turn). But because both halves typically post as the same `@claude` MM identity, a watching human can't tell speakers apart. Follow this convention so the transcript stays readable:
  - **First post into a foreign channel.** Open with a self-chosen persona name and your own channel id (look it up with `mm-bridge channel`). E.g. `Aster (from <channel_id>): hi — I'm a Claude session in another channel. Pick a name for yourself and prepend it to your replies; you can answer me by posting into my channel.`
  - **Every subsequent post in that channel.** Prefix the body with `<your-name>: ` so each post is attributable.
  - **Receiving an intro in your own channel.** Adopt the same convention for your replies in that conversation, and use the channel id the other agent gave you to talk back.
  - **If a turn's input contains a question addressed to you, answer it in this reply or
    explicitly defer with a time.** An unanswered question in a channel looks identical to
    an answered one until someone notices hours later; "I'll come back to this after M1"
    is a complete answer, silence is not.
  - **Don't poll for cross-channel replies.** The bridge delivers the other agent's reply to your session as a normal user turn. Calling `mm-bridge read` to fetch it as well leads to double-handling. Wait for the user turn.
  - **Sender-side transcript visibility.** `mm-bridge post --channel <other>` automatically also posts a short mirror into your own channel (`_→ also sent to ~<other-slug>~_`, plus an attachment count when you used `--file`) so a human watching your channel sees the body, not just the tool-use placeholder. Nothing extra to do — the bridge tags the mirror so it doesn't loop back into your session.
  - **Track `cslhi` ("counter since last human intervention").** End every cross-channel post with `[cslhi: N]` on its own line. To compute `N`: look at recent scrollback (`mm-bridge read --channel <id>`), find the most recent post by any MM user *other than* `@claude` (those posts get auto-prefixed with `username: ` in your forwarded view), count the `@claude` posts since, and add 1. The intro post is `[cslhi: 1]`. A human reply in either channel resets the count.
  - **Soft cap at 12.** Before posting your reply, check what `cslhi` would be. If it would exceed 12, do **not** post into the other agent's channel. Instead, write what you were going to say verbatim and post it in **your own** channel addressed to the operator (whichever MM user that is), so the human can decide whether to resume the exchange or end it. Don't truncate — the human deserves to see the move you would have made. Until you see a human reply, you stay paused.
  - **Within the cap, use your judgement.** While `cslhi ≤ 12`, keep going as long as you're still adding signal toward solving a problem; stop earlier if the conversation has reached a natural close. The cap is a safety net, not a target.
- **Summarizing a channel.** There's no dedicated subcommand; pipe `read` into an ephemeral Claude run. For example:

  ```sh
  mm-bridge read --channel <id> --since 1d --no-bot --format text | \
    claude -p --model haiku --no-session-persistence \
      "Summarize this Mattermost channel transcript in 3-5 bullets, focusing on decisions, questions, and action items."
  ```

  Codex equivalent:

  ```sh
  mm-bridge read --channel <id> --since 1d --no-bot --format text | \
    codex exec --model gpt-5.4-mini --ephemeral --sandbox read-only \
      "Summarize this Mattermost channel transcript in 3-5 bullets, focusing on decisions, questions, and action items."
  ```

  `claude -p` (alias for `--print`) runs a one-shot, non-interactive completion. `--model haiku` keeps summaries cheap. `--no-session-persistence` skips writing the conversation to `~/.claude/projects/.../<id>.jsonl` — only valid with `-p`. For Codex, `exec` runs non-interactively; when stdin is piped and a prompt is provided, the transcript is appended as a `<stdin>` block, `--ephemeral` avoids persisting session files, and `--model gpt-5.4-mini` keeps the run lightweight. `--no-bot` on the `read` side strips Claude's own prior replies so it doesn't re-read itself as context. Tune the prompt per use case.
