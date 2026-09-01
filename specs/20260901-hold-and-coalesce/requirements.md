# Hold-and-Coalesce — Requirements

Each requirement traces to a lead ruling (R1–R12). Where the implementation
makes a call the ruling left open, it is marked **[decision]** and restated in
`design.md`.

## 1. Scope of the change (R1)

1.1 The change is **bridge-side only**. `agent-harness` is not modified. Its
`RunManager` FIFO (cap 16) remains as the safety net for every path that
still submits eagerly (overflow, kill-switch OFF, un-tracked runs).

1.2 The bridge stops *eagerly* submitting while a run is live. It never
cancels, dequeues, or inspects harness-side queued runs.

## 2. What gets held, and where (R2)

2.1 Holding is keyed by **anchor** `(channel_id, thread_root)` — the same
key `_silent_drops` uses. A thread-fork session holds independently of its
parent channel session.

2.2 Only posts that **would have been forwarded** are held. Everything that
short-circuits before `create_run` today still short-circuits, in the same
order, before holding is considered:

| Gate | Where today | Behaviour under F1 |
| --- | --- | --- |
| System / bot-CLI-marker posts | `_on_mm_posted` head | unchanged — dropped |
| Empty post (no text, no files) | `_on_mm_posted` | unchanged — dropped |
| Dot-commands (`.stop`, `.status`, …) | `_on_mm_posted` / `_handle_thread_post` | unchanged — dispatched immediately, **never held** |
| `@claude catch up` / `leave` / `stop` phrases | `_on_mm_posted` | unchanged — dispatched immediately |
| Mention-only gate | `_forward_user_post` head | unchanged — `_enqueue_silent_drop`, **not held** |
| Dormant channel (no session) | `_on_mm_posted` | unchanged — engagement path |
| Warming-up channel | `_on_mm_posted` | unchanged — warming queue (see §10) |

2.3 A post is held **after** the mention gate and **before** attachment
download. Attachment download, attribution, silent-drop peek and the
first-message preamble all happen **at flush time** (R3).

2.4 A post already present in the buffer (matched by post id) is never held
twice. Defensive: the warming-queue replay path re-dispatches post dicts.

## 3. Flush (R3)

3.1 The buffer for an anchor flushes as **one** `create_run` through the
normal forwarding pipeline: attribution, attachment download at flush time,
silent-drop peek, first-message preamble rules unchanged.

3.2 Flush is triggered from **four** places:

* **T1 — run-terminal SSE event.** `_on_harness_run_lifecycle` for any of
  `HARNESS_RUN_TERMINAL_EVENTS` (`run.completed` / `run.failed` /
  `run.interrupted`).
* **T2 — out-of-band dead-run discovery.** The typing watchdog's reconcile
  probe (`_typing_watchdog_tick` → `_active_run_is_alive` returns False)
  already pops `active_run_by_session` when it finds a run that died without
  a terminal event reaching us. It flushes too.
* **T3 — post-enqueue liveness re-check.** See §4.3.
* **T4 — periodic held-anchor sweep** (lead condition **C1**). A second
  phase of the existing watchdog tick that reconciles anchors with held
  posts **regardless of typing-loop state**. §4.5 explains why T2 alone is
  not sufficient; design §2.5 gives the rate-limit discipline. T4 is also
  the retry engine for §3.8.

3.3 **Body format.** With more than one held post:

```
[<N> posts arrived while you were working — the newest governs]
HH:MM <username>: <body>
HH:MM <username>: <body>
[End of held posts]
```

A **single** held post flushes **without** the header and closing marker —
just the one `HH:MM <username>: <body>` line. **[decision]** the `HH:MM
<username>:` prefix is applied to held posts *unconditionally*, not via
`PosterTracker`: the timestamp is the whole point (it tells the agent how
stale the message is), and a bare body would hide that. `PosterTracker.note_post`
is still called so its per-session poster set stays accurate.

3.4 Order is **arrival order** (oldest first). The header states that the
newest governs; the bridge does not itself reorder or drop anything.

3.5 `HH:MM` is derived from the MM post's `create_at` (epoch ms) in the
daemon's **local** timezone. **[decision]** — local, not UTC: the operator
and the daemon share a host, and MM renders local times, so a UTC timestamp
would read as wrong to a human reading the same thread.

3.6 Attachments on held posts are downloaded at flush time into the session
cwd, exactly as `_peek_silent_drops_as_block` does for replayed drops, and
their `[MM attachment …]` notes are prepended to that post's body.

3.7 If the anchor has **no session** at flush time (teardown raced the
flush), the held posts are **not silently dropped**: the bridge logs at
`warning` with the full author/timestamp list and posts a channel warning
naming them.

3.8 **Flush failure** (lead condition **C2**). When the flush's own
`create_run` raises, the held posts **stay held** — the buffer is not
discarded, in memory or on disk:

* a loud channel error via `format_backend_error` (the shape the single-post
  path already uses);
* the next trigger — T1, T2, T3 or the T4 sweep — retries the delivery;
* the T3 probe direction makes this the *expected* interaction: an
  unreachable harness makes `_active_run_is_alive` return False, so the
  bridge flushes into precisely the state that can fail. C1 + C2 together
  make the sweep the retry engine: no strand, no loss, no silent drop.

3.9 **The one non-transient failure.** `HarnessResumeUnsupported` means the
session can *never* accept a run, so retrying forever would strand the posts
and re-post the warning every sweep interval. In that single case the held
posts are moved into `_silent_drops` — where they are replayed verbatim as a
catch-up block on the next successful forward — and the existing "can't
resume this external session" warning is posted once. That is exactly what
the single-post path does today (bridge.py:2022), applied to N posts.
**[decision]** — a deliberate carve-out from §3.8's "posts remain held",
honoring its intent (no strand, no loss) rather than its letter.

3.10 **The carve-out's durability downgrade** (lead condition **C3**). At the
moment §3.9 fires, the posts leave `held_posts.json` for the in-memory
`_silent_drops` deque. **A daemon restart in that window loses them.** This is
a real, accepted downgrade, recorded here as a judgment rather than left
implicit:

* It matches today's contract *for this exact failure*. The single-post path
  already calls `_enqueue_silent_drop` on `HarnessResumeUnsupported`
  (bridge.py:2014), and that queue has never been persistent. F1 is not making
  this case worse than it was — it is declining to make it better.
* The alternative costs more than it buys. Keeping them held would either
  re-post the "can't resume" warning every sweep interval forever (a nag storm
  of our own making) or require a new frozen-hold state — a whole extra
  lifecycle for a rare terminal condition.
* The window is narrow and self-closing. Silent drops replay on the next
  successful forward, which for a resume-unsupported session is exactly when
  the anchor gets a replacement session (`_replace_external_session`).

If this ever needs closing, the cheap version is to persist `_silent_drops`
alongside the holds file — a strictly larger change than F1, and one that
would improve the mention-only drop path too.

## 4. The enqueue/terminal race (R3)

4.1 **The race.** A post is held on the strength of a liveness check; the run
reaches terminal state around the same instant. If the terminal handler ran
its flush *before* the post landed in the buffer, nothing is left to trigger
a second flush and the post is held forever.

4.2 **Closure, part 1 — atomicity.** The liveness check and the buffer append
are a single synchronous span with **no `await` between them**. asyncio is
single-threaded, so no other coroutine can interleave inside that span, and
the terminal handler's `active_run_by_session.pop()` is likewise synchronous
and runs before its first `await`. This makes the "check said live, buffer
was already flushed" interleaving unrepresentable *within one daemon tick*.

4.3 **Closure, part 2 — post-enqueue re-check (T3).** Atomicity does not
cover the case where the in-memory tracker is simply **wrong** — the run died
and its terminal event was lost (SSE gap, harness restart, missed event).
After the atomic enqueue, the bridge asks the harness whether the run is
actually alive (`_active_run_is_alive`). If it is not, it flushes
immediately. This is one HTTP GET per held post, paid only on the rare
hold path, and it is the deterministic red-first test for R3.

4.4 **Re-entrancy.** While a flush is in progress for an anchor, further
posts to that anchor are held (not eagerly submitted), and the flush re-checks
the buffer when it finishes, looping if new posts arrived. Without this, a
post arriving during a flush's `await`s would race its own `create_run`
ahead of the flush's and reorder the conversation.

4.5 **Why T2 alone does not cover held anchors** (lead condition C1).
`_typing_watchdog_tick` early-returns when `self.typing` is unset
(bridge.py:813) and sweeps only `self.typing.running_sessions()`. A session
whose `run.started` was lost never enters `active_run_by_session`, so the
tick's silence-stop branch (bridge.py:830-838) stops its typing loop and the
session **leaves the sweep list while its run is still alive**. Lost
`run.started` *and* lost terminal event ⇒ T1 blind, T2 blind, T3 long past ⇒
posts stranded until the next inbound post to that anchor. T4 closes this by
sweeping `running_sessions() ∪ sessions-with-held-posts` under the same probe
rate-limit discipline, in the same watchdog task — no new timer.

## 5. Durability (R4)

5.1 Held posts **survive a daemon restart**. Today a post to a busy session
lands in the harness FIFO, which survives a bridge restart; pure in-memory
holding would be a durability *regression*.

5.2 Persistence lives in a **sibling file** next to the state file, not in
the state file itself (rationale in `design.md` §5). Default path:
`<dirname(state_file)>/held_posts.json`; config key `held_posts_file`, env
`MM_BRIDGE_HELD_POSTS`.

5.3 **Full post envelopes** are stored, not post ids for later refetch: a
refetch costs an MM round-trip per post at flush time and *fails outright if
the post was deleted*, losing the user's message — the exact regression 5.1
forbids. The envelope also carries the resolved `username` and `held_at`, so
`mm-bridge inbox` needs no MM API call and no bot token.

5.4 Writes are atomic (temp file + `os.replace`) so a concurrent CLI read
never sees a torn file.

5.5 **Rehydration.** On `start()`, after bootstrap, each held anchor is
resolved to its *current* session:

* session exists and the harness reports a live run → stay held (T1/T2 will
  flush);
* session exists and no run is live → flush immediately;
* anchor no longer maps to a session → §3.7 (log loudly + channel warning).

## 6. `.stop` and explicit control (R5)

6.1 `.stop` does **not** drop the buffer. It interrupts the running run only
(`_run_stop_command`), which produces `run.interrupted` → T1 → the backlog
flushes as one cheap annotated turn.

6.2 New dot-command `.queue` — list the posts held for this anchor
(`HH:MM username: <first line>`), or `(empty)`.

6.3 New dot-command `.queue clear` — drop the held posts for this anchor. The
confirmation **names the dropped posts' authors and timestamps**, so a clear
is never a silent loss. Held ⏳ reactions are removed.

6.4 `.queue` is `session_scoped` (holding presupposes a live run, which
presupposes a session) and is not `global_scope` (it reveals only this
anchor's state).

## 7. Overflow (R6)

7.1 The buffer is capped per anchor. Default **50**; config key
`coalesce_max_held`, env `MM_COALESCE_MAX_HELD`.

7.2 On overflow the *overflowing* post falls back to today's eager submit
(`create_run` → harness FIFO) and the bridge logs at `warning` with the
anchor, the cap, and the post id. **Never silently dropped.**

7.3 Accepted consequence: an overflowing post reaches the harness FIFO
*ahead* of the later coalesced flush, so an overflowed conversation can be
delivered out of order. Overflow is a degenerate case (50 held posts to one
anchor) and the ruling is explicit that the fallback is today's behavior.

## 8. Kill-switch (R7)

8.1 Config `coalesce_posts`, TOML **top-level** key, env `MM_COALESCE_POSTS`.
Default **true** (ON).

8.2 OFF ⇒ exactly today's behavior. The old path is kept intact behind the
flag rather than reimplemented: the hold decision is a single early return in
`_forward_user_post`, and everything downstream of it is the existing code.

8.3 Turning the flag OFF at runtime does not strand an existing buffer — the
flush triggers do not consult the flag. (Only *holding* is gated.)

## 9. `mm-bridge inbox` (R8)

9.1 `mm-bridge inbox [--channel <ref>] [--json]` prints the posts held but
not yet delivered for the session, or `(empty)`.

9.2 With no `--channel`, the anchor is resolved from the current session's
sidecar (`_current_session_id` → `_resolve_anchor_from_session`) — the same
chain `mm-bridge post` / `read` use. **No bot token required** on this path:
the command reads only the local holds file.

9.3 With `--channel`, the existing channel-ref resolution applies (id, slug,
channel URL, permalink), which may require MM access exactly as `read` does.

9.4 Read-only and best-effort. A missing file, an unreadable file, or a
mid-write torn read all render as `(empty)` with a stderr note rather than a
traceback. Staleness is documented in `--help`: the daemon may append
between the read and the print.

## 10. Warming-up interplay (R10)

10.1 During session warm-up there is **no anchor→session mapping**, so
`_forward_user_post` is never reached and holding cannot engage. The warming
queue (`WarmingUpChannel.queued_posts`) is the sole owner of those posts. No
double-handling.

10.2 `_flush_queued` pops the warming entry **before** re-dispatching, and
re-dispatches sequentially with `await`. By then the mapping exists and the
session's initial run is live, so each replayed post is *held*, in arrival
order. They flush as one turn when the initial run ends — a strict
improvement over today's N eager runs.

10.3 A terminal event landing mid-drain produces a partial flush followed by
a second flush of the remainder. Within each flush order is preserved, and
the flushes are themselves ordered, so the conversation never reorders.

10.4 §2.4's post-id dedupe is the belt-and-braces guard for any replay path.

## 11. Held-post visibility (R9)

11.1 When a post is held, react ⏳ (`hourglass_flowing_sand`) on it.

11.2 When it is delivered, remove ⏳ and add ✅ (`white_check_mark`).

11.3 When `.queue clear` drops it, remove ⏳ and add nothing.

11.4 Every reaction call is best-effort: a failure is logged at `debug` and
**never** blocks or fails delivery. Mattermost has no atomic swap, so a
crash between remove and add leaves a post with neither reaction — cosmetic.

## 12. Testing (R11)

12.1 Every behavior below gets a test that was **run and watched fail**
before the code that makes it pass:

| # | Behavior | Ruling |
| --- | --- | --- |
| 1 | Hold while a run is live — no `create_run` | R2 |
| 2 | Single-post flush — no header | R3 |
| 3 | Multi-post coalesced flush — one run, order, attribution, header | R3 |
| 4 | Mention-gate interplay — silent drop is not held; its catch-up block still lands | R2 |
| 5 | Dot-command bypass — dispatched, never held | R2 |
| 6 | `.queue` lists; `.queue clear` drops and names authors/timestamps | R5 |
| 7 | Overflow — the overflowing post submits eagerly + warns | R6 |
| 8 | Restart rehydration — holds reload and flush | R4 |
| 9 | The R3 race — dead run at enqueue time flushes immediately | R3 |
| 10 | Kill-switch OFF ⇒ legacy behavior (one run per post) | R7 |
| 11 | Thread-fork anchor isolation | R2 |
| 12 | `.stop` does not drop the buffer; interrupt → flush | R5 |
| 13 | ⏳ on hold, ✅ on delivery | R9 |
| 14 | `mm-bridge inbox` renders held / `(empty)` | R8 |
| 15 | Warming-queue posts land held and flush as one | R10 |
| 16 | Attachments on held posts download at flush time | R3 |
| 17 | Both lifecycle events lost (no `run.started`, no terminal, typing loop gone) ⇒ the T4 sweep flushes within one interval | C1 |
| 18 | Flush's `create_run` raises ⇒ buffer unchanged on disk, error posted, next sweep delivers | C2 |

12.2 Full suite green at FINAL (`uv run -m pytest`), with before/after counts
reported. Baseline at branch point (`3e28e53`): **1022 passed, 1 skipped, 42
subtests passed**.

## 13. Spec-first (R12)

13.1 This directory (`overview.md`, `requirements.md`, `design.md`) is
committed on the branch as the first commit of M1, before implementation.
