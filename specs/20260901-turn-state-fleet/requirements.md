# Turn-End State, Fleet View, Awaiting-Nag — Requirements

Each requirement traces to a lead ruling (R1–R11). Where a ruling left a call
open, the choice is marked **[decision]** and argued in `design.md`.

## 1. Base and stacking (R1)

1.1 The branch is `feat/turn-state-fleet`, cut from `feat/hold-and-coalesce`
@ `24281c5`. The PR targets `feat/hold-and-coalesce`; it is retargeted to
`main` once #51 merges.

1.2 F1 code (`held_posts.py`, `_hold_post`, `_flush_held`,
`_deliver_to_session`, `_sweep_held_anchors`) is **used, not modified**. The
only permitted F1-adjacent edits are additive hooks:
* the nag reuses `_should_hold` / `_hold_post` / `_deliver_to_session`
  unchanged (§5.4);
* the fleet reads `HeldPostStore.peek()` **read-only** for the held column.

## 2. The `<state/>` directive (R2)

2.1 `directives.extract` gains a third directive kind, `state`, parsed by the
same fence-aware machinery: a `<state .../>` inside a triple-backtick fence or
an inline code span is **documentation** and stays in the visible text. This is
load-bearing — `CLAUDE-include.md` and this spec both show the tag in fences,
and agents quote it when explaining themselves.

2.2 Attributes: `kind` (required), `on` (optional), `note` (optional). Parsed
by the existing `_ATTR_RE`; unknown attributes are ignored.

2.3 Valid kinds: `idle`, `awaiting`, `parked`, `blocked`.

2.4 **Unknown `kind`** → the tag is stripped from the visible post, a warning
is logged, the session's state is **unchanged**, and a short notice is posted
to the channel so the agent learns the vocabulary:
`⚠️ Unknown <state kind="foo"/> — valid kinds: idle, awaiting, parked, blocked. State unchanged.`

2.5 **Only the LAST `<state>` in a reply counts.** Earlier ones are stripped
and ignored (no notice — quoting an earlier state mid-reply is legitimate).

2.6 A `<state/>` tag alone in a reply must not produce an empty post: the
existing blank-run collapsing and empty-body handling of
`_handle_assistant_text_block` applies unchanged.

2.7 `<leaveChannel/>` still takes precedence; a reply carrying both leaves,
and the state of a departed session is discarded with its mapping.

## 3. State model and API (R3)

3.1 One internal entry point:

```python
Bridge.set_session_state(
    session_id: str, kind: str, *, on: str | None = None,
    note: str | None = None, source: str = "agent",
) -> SessionState | None    # returns the PREVIOUS state
```

3.2 `source` ∈ `"agent" | "bridge"`. F3 calls it as
`set_session_state(sid, "blocked", note="anthropic 529", source="bridge")` —
**one call, no other integration** (R3).

3.3 **A reply with no `<state/>` tag sets `idle`.** That is the zero-migration
rule: today's agents emit no tag, so every turn ends idle, which is exactly
today's (unmodelled) behaviour made explicit.

3.4 `working` is **display-only** [decision]. `run.started` never overwrites
the declared kind; the fleet renders `working` while
`_session_has_active_run(session_id)` is true and re-shows the declared kind
when the run ends.

3.5 Every state carries `set_at` (epoch seconds, wall clock — it must survive
a restart) and `source`.

3.6 State is **per session**, so a thread-fork session has its own state,
its own fleet row and its own nag (R9).

3.7 Unlinking a session (`.leave`, channel leave, session replacement) drops
its state with its mapping entry — no orphan rows.

## 4. Persistence (R4)

4.1 State lives in the existing state file as **schema v6**: one optional
`"state"` object per entry. **[decision]** — argued in `design.md` §2 against
a sibling file.

4.2 v5 → v6 is forward-compatible on read (a v5 file loads with no states)
and the file is re-emitted as v6 on the next save. A v6 file read by an older
bridge degrades to "no states", never to a crash: the extra key is ignored by
v5's `_ingest`.

4.3 `ChannelMapping.save()` becomes **atomic** (temp file + `os.replace`),
because F2 adds a concurrent out-of-process reader (`mm-bridge fleet`).
Without it the CLI could read a half-written file. Mirrors
`HeldPostStore._save`.

4.4 **Restart story.** Declared states survive. Nag bookkeeping (which
thresholds already fired for which episode) is **in-memory only**: after a
restart, a session that is still awaiting past its threshold gets ONE
re-nag. This is accepted and documented — a duplicate doorbell after a daemon
restart is strictly better than persisting bookkeeping that can go stale
against a state that changed while the daemon was down.

## 5. The nag (C, R5/R6)

5.1 **Trigger.** A session whose state is `awaiting`, with **no active run**,
whose `set_at` is older than `awaiting_nag_after_seconds` (default 1800).

5.2 **Target.** The *parent* channel — the channel whose slug appears in this
session's channel header as `Parent: ~<slug>~` (the header
`mm-bridge spawn` writes; `spawn.py:121`). Resolution order:
1. thread-fork session → its own channel's channel-session (R9, §5.7);
2. `Parent: ~<slug>~` header → that channel;
3. no parent → the session's **own** channel, with the mention (R5).

5.3 **Body.**
`⏰ ~<child-slug>~ has been awaiting you for 30 min ("M0 gate")` — the note
clause is omitted when there is no note.

5.4 **Delivery (R5).** The nag is posted to Mattermost **and** delivered as a
turn through the normal forwarding tail. Mechanism: create the MM post, then
route it through the same busy/idle decision a user post takes —
`_should_hold()` → `_hold_post()` when the target has a live run (F1's
coalescing folds it into the next flush), else `_deliver_to_session()`. The
self-post suppression is **not weakened**; see `design.md` §3.

5.5 **Escalation.** At `2 ×` the threshold, ONE further post: the same body
prefixed with an @-mention. Mentioned party:
* `on="<username>"` → that user (also at level 1 — a human is only woken by a
  mention) [decision];
* otherwise `operator_username` from config;
* otherwise the most recent non-bot poster in the *awaiting* session's
  channel.
* If none can be determined, the escalation posts without a mention and logs
  a warning — a visible nag beats a silent one.

5.6 **Single-shot per threshold per episode.** An awaiting *episode* is
identified by `(session_id, set_at)`. Each of levels 1 and 2 fires at most
once per episode. An episode ends when:
* the state changes (any `set_session_state` that changes kind or stamps a new
  `set_at`), or
* a run starts for that session — `run.started` re-stamps `set_at = now` and
  clears the episode's bookkeeping, because a run means the awaited party
  *did* engage. **[decision]**, argued in `design.md` §4.2. This also makes
  the age column mean "time since the state was set or the last run started",
  which is what the fleet column is for.

5.7 **Thread forks (R9).** A thread-fork session's nag goes to its own
channel's channel-session (the conversation that owns the thread), posted at
channel root — not into the thread, where only the forked session is looking.

5.8 **Bounding (R6).**
* at most one nag per level per episode;
* at most **one nag post per watchdog tick** (`typing_refresh_seconds`, 3s
  default) across the entire fleet — a fleet of 40 overdue sessions drains at
  one doorbell per tick, never a burst;
* `nag_enabled = false` (or `MM_NAG_ENABLED=0`) → no nag ever; A and B
  unaffected;
* a nag is never sent to a session that has an active run *at delivery time*
  without going through the hold path (5.4), so it can never jump a queue.

5.9 A nag that fails to post or to deliver is logged and **not** retried
within the episode — the level is marked fired regardless. A doorbell that
rings twice because of a transient MM error is worse than one that is missed;
the sweep will pick up level 2 later.

## 6. Fleet view (B, R8)

6.1 `.fleet` renders one row per **child channel** of the current channel:
channels whose header starts `Parent: ~<this channel's slug>~`. `.fleet all`
and `mm-bridge fleet --all` render every mapped session.

6.2 Columns:

| Column | Source |
| --- | --- |
| title | MM channel `display_name` (fallback `name`) |
| state | declared kind, `→ on`, `(note)`; `working` while a run is live |
| run | `run <N>m` when a run is tracked live |
| age | now − `set_at`, humanised (`47 min`, `3h 12m`) |
| held | `len(HeldPostStore.peek(anchor))` — read-only |
| err | `-` until F3 (column and seam present) |

6.3 **No probe loop (R8).** Row data comes from tracked state
(`active_run_by_session`, `current_run_id_by_session`), the mapping, and the
holds file. Channel titles/headers come from **one** `list_bot_channels()`
call, off the event loop via `asyncio.to_thread`. **[decision]:** F2 issues
**zero** harness GETs for `.fleet`; the run tracker is already reconciled
every `typing_refresh_seconds` by the watchdog, so a row can be at most one
tick stale. Argued in `design.md` §5.

6.4 A channel with a `Parent:` header but no mapped session renders with state
`-` (dormant) rather than being hidden: "the channel exists and nothing is
running there" is exactly the fact a lead needs.

6.5 `mm-bridge fleet` reads the same persisted state and prints the same
columns. Its **staleness contract** is documented in the docstring, like
`mm-bridge inbox`: the daemon owns the files, writes are atomic, so the CLI
reads a snapshot that may be one tick old, never a torn one. Every failure
mode (missing file, corrupt JSON, unknown schema) renders as an empty fleet,
not a traceback.

6.6 `mm-bridge fleet` needs a bot token (channel titles come from MM);
`--channel <ref>` accepts the same channel refs as `inbox` / `post`.

## 7. Sweep placement (R7)

7.1 The nag sweep is a **third phase** of `_run_typing_watchdog`, after the
typing tick and the held-anchor sweep, wrapped in its own `try/except` so a
sweep failure can never kill the watchdog task (same discipline as F1's
phase 2).

7.2 Cheap when idle: the sweep returns immediately when `nag_enabled` is off
or no session is in `awaiting`. It makes **no** network call unless a nag
actually fires.

## 8. Documentation (D)

8.1 `CLAUDE-include.md` gains a `<state/>` section: the tag, when to use each
kind, one example, and the "missing tag = idle" rule.

8.2 `CLAUDE-include.md` gains one new agentcom bullet: **"If a turn's input
contains a question addressed to you, answer it in this reply or explicitly
defer with a time."**

8.3 `README.md` documents `.fleet`, `mm-bridge fleet`, the nag, and the
`nag_enabled` / `awaiting_nag_after_seconds` / `operator_username` config
(plus their env overrides), and bumps the documented state schema version.

## 9. Testing (R10)

Every behaviour below gets a test written and **watched fail** before its
implementation. Every gate wait is bounded (F1's `_await_gate` pattern).

| # | Behaviour |
| --- | --- |
| T1 | `<state/>` parsed; tag stripped from the visible post |
| T2 | fenced / inline-code `<state/>` is left intact and sets nothing |
| T3 | unknown kind → stripped, state unchanged, notice posted |
| T4 | last tag wins |
| T5 | reply with no tag → state becomes `idle` |
| T6 | state round-trips through save/load (v6), v5 file loads clean |
| T7 | `.fleet` renders children only, with state / on / note |
| T8 | `.fleet` shows `working` for a live run and re-shows the declared kind after it ends |
| T9 | `.fleet` held column reads the F1 buffer |
| T10 | `.fleet` includes thread-fork rows; `all` widens to every session |
| T11 | `.fleet` issues zero harness GETs |
| T12 | nag fires at the threshold, **not** one second before |
| T13 | nag posts to the parent channel and is delivered as a turn |
| T14 | nag into a BUSY parent is held (F1), not submitted |
| T15 | escalation at 2×, with the operator mention |
| T16 | `on="<user>"` mentions that user instead |
| T17 | single-shot: two sweeps past the threshold → one nag |
| T18 | episode reset on state change; on `run.started` |
| T19 | `nag_enabled = false` → no nag; A and B still work |
| T20 | at most one nag post per tick across the fleet |
| T21 | restart: states survive; one re-nag for an already-overdue episode |
| T22 | no parent header → nag lands in the session's own channel, with mention |
| T23 | `mm-bridge fleet` renders from disk; corrupt/missing files → empty, rc 0 |

9.2 Full suite green at FINAL. Base at `24281c5`: **1135 passed, 1 skipped,
52 subtests passed**.

## 10. Explicitly out of scope

Provider-error classification (F3); agent-harness changes; interrupt-and-
inject; any live run against the deployed bridge, its Mattermost or its state
file.
