# Hold-and-Coalesce — Design

All line references are against base `3e28e53`.

## 0. Module layout

| File | Action |
| --- | --- |
| `src/mm_bridge/held_posts.py` | **NEW** — `HeldPost`, `HeldPostStore` (buffer + atomic persistence + rendering). Pure-ish: no MM/harness I/O, so it unit-tests standalone. |
| `src/mm_bridge/bridge.py` | **EDIT** — hold decision in `_forward_user_post`; delivery tail extracted to `_deliver_to_session`; flush triggers; `.queue`; reactions; teardown cleanup; rehydration in `start()`. |
| `src/mm_bridge/commands.py` | **EDIT** — one `CommandSpec("queue", …)`. |
| `src/mm_bridge/config.py` | **EDIT** — `coalesce_posts`, `coalesce_max_held`, `held_posts_file` (+ TOML/env wiring). |
| `src/mm_bridge/mm_client.py` | **EDIT** — `add_reaction` / `remove_reaction`. |
| `src/mm_bridge/cli.py` | **EDIT** — `cmd_inbox` + `inbox` subparser. |
| `tests/doubles.py` | **EDIT** — reaction recording on `FakeMattermostClient`. |
| `tests/test_hold_and_coalesce.py` | **NEW** — R11 matrix. |
| `tests/test_cli_inbox.py` | **NEW** — R8. |
| `agent-harness` | **NO CHANGE** (R1). |

## 1. Data structures

```python
@dataclass(frozen=True)
class HeldPost:
    """One Mattermost post held while its session's run was in flight.

    Carries the FULL post dict (not just an id) so a flush never needs to
    refetch from Mattermost — a refetch costs a round trip per post and
    fails outright on a deleted post, losing the user's message. ``username``
    is resolved once at hold time so `mm-bridge inbox` can render without
    any Mattermost access at all.
    """
    post: dict
    username: str
    held_at_ms: int      # post["create_at"], the arrival time the user sees

    @property
    def post_id(self) -> str: ...
    def timestamp(self) -> str:   # "HH:MM", daemon-local (requirements §3.5)
```

```python
class HeldPostStore:
    """Anchor-keyed hold buffers with atomic on-disk persistence.

    Owns ONLY the buffer + the file. Rendering the flush body, downloading
    attachments and calling ``create_run`` stay in the Bridge, which has the
    MM / harness clients.
    """
    def __init__(self, path: str | Path, *, cap: int) -> None: ...

    # buffer API — all synchronous, so hold decisions stay atomic (§4)
    def add(self, anchor: Anchor, held: HeldPost) -> bool   # False = at cap
    def peek(self, anchor: Anchor) -> list[HeldPost]
    def take(self, anchor: Anchor) -> list[HeldPost]        # pop + persist
    def clear(self, anchor: Anchor) -> list[HeldPost]       # for `.queue clear`
    def forget_channel(self, channel_id: str) -> None       # teardown
    def anchors(self) -> list[Anchor]
    def __len__(self) -> int

    # persistence
    def load(self) -> None
    def _save(self) -> None                                 # tmp + os.replace
```

`Bridge.__init__` gains one field, next to `_silent_drops` (they are siblings
conceptually — both anchor-keyed, both replayed later):

```python
# Posts held because the anchor's session had a run in flight. Sibling of
# `_silent_drops`: same (channel_id, thread_root) key, but these WOULD have
# been forwarded — they are deferred, not dropped, and are flushed as ONE
# coalesced run when the live run reaches a terminal state.
self._held = held_posts.HeldPostStore(
    config.held_posts_file, cap=config.coalesce_max_held,
)
# Anchors with a flush in progress. Posts arriving during a flush's awaits
# must be held (not eagerly submitted) or their run would race ahead of the
# flush's own create_run and reorder the conversation.
self._flushing: set[Anchor] = set()
```

## 2. Seams

### 2.1 Hold decision — `_forward_user_post` (bridge.py:1938)

Today's head:

```python
cfg = self.purpose_by_channel.get(channel_id)
bot_mention = f"@{self.mm.bot_username}"
if cfg and cfg.mention_only and bot_mention not in message:
    self._enqueue_silent_drop(channel_id, thread_root, post)
    return
cleaned = self._strip_bot_mention(message)
# ... attachment download (awaits) ...
```

The hold check is inserted **immediately after the mention gate, before the
mention strip and the attachment download**:

```python
anchor = Anchor(channel_id, thread_root)
if self._should_hold(anchor, session_id):
    await self._hold_post(anchor, session_id, post)
    return
```

Why here (requirements §2.3):

* **After** the mention gate — a mention-only drop is a *drop*, not a hold;
  it must keep going to `_silent_drops` so the existing catch-up replay works.
* **Before** the attachment download — R3 puts attachment download at flush
  time, and downloading now would waste work on a post that may be superseded.
* The `not cleaned and not attachment_notes` empty-check that follows is
  *deferred to flush time*: a post that is literally just `@claude` is held
  and then renders as an empty body, which the flush renderer skips exactly
  like `_peek_silent_drops_as_block` skips empty drops (bridge.py:2323).

`first_message` is deliberately **not** captured at hold time. The
`_awaiting_first_forward` slot is consumed further down (bridge.py:2002), so
holding early leaves it intact and the flush recomputes
`channel_id in self._awaiting_first_forward`. This is the only correct
reading: if the flush happens after a config restart, the slot may legitimately
have changed.

```python
def _should_hold(self, anchor: Anchor, session_id: str) -> bool:
    """True when this post must wait rather than start its own run.

    Three conditions, cheapest first — all synchronous, so the caller can
    decide and enqueue without an intervening await (see §4).
    """
    if not self.config.coalesce_posts:      # R7 kill-switch
        return False
    if anchor in self._flushing:            # §4.4 re-entrancy
        return True
    return self._session_has_active_run(session_id)
```

`_session_has_active_run` (bridge.py:2888) already exists and already reads
both trackers — the origin-agnostic `active_run_by_session` (fed by
`run.started` SSE, so it also sees runs the bridge did not submit) and
`current_run_id_by_session` (runs this daemon submitted). Reusing it keeps
one definition of "is the coding agent busy?" in the codebase, shared with
`.status`.

### 2.2 Delivery tail extraction

`_forward_user_post`'s tail — silent-drop peek, first-message preamble,
`_record_harness_send`, `create_run`, and its three error branches — is
extracted verbatim into:

```python
async def _deliver_to_session(
    self,
    channel_id: str,
    session_id: str,
    body: str,
    *,
    thread_root: str | None,
    first_message: bool,
    exclude_post_id: str | None,
    on_failure_requeue: list[dict],
) -> bool:
```

Both callers use it:

* `_forward_user_post` — single live post (`on_failure_requeue=[post]`).
* `_flush_held` — coalesced body (`on_failure_requeue=[h.post for h in held]`).

`on_failure_requeue` preserves today's contract that a failed send does not
lose the user's message: the existing code calls `_enqueue_silent_drop` for
the post it failed to deliver. The flush path does the same for every post in
the batch, so a harness outage degrades a coalesced flush into a replayable
catch-up block rather than a silent loss.

### 2.3 Flush trigger T1 — terminal SSE events

`_on_harness_run_lifecycle` (bridge.py:4499) already handles every member of
`HARNESS_RUN_TERMINAL_EVENTS`. One call is appended after the existing
cleanup, *after* `_mention_triggerer_on_done`:

```python
await self._maybe_flush_held_for_session(session_id)
```

Ordering matters: the `@username` completion ping belongs to the run that
just ended, so it fires before the next turn's content is submitted.

### 2.4 Flush trigger T2 — out-of-band dead-run discovery

`_typing_watchdog_tick` (bridge.py:813) already discovers runs that died
without a terminal event reaching the bridge: after
`_active_run_is_alive(session_id)` returns False it stops typing and pops
`active_run_by_session`. The same branch flushes.

This trigger is what makes T1 non-load-bearing: **every** live run starts
typing (`run.started` → `_start_typing_for_activity`, bridge.py:3888, which
requires only a mapped anchor — and every held post comes from a mapped
anchor), so any session with held posts is on the watchdog's sweep list.
No additional reaper task is needed; adding one would be a second
timer with no case of its own.

### 2.5 Flush trigger T3 — post-enqueue liveness re-check

See §4.3.

### 2.6 `_flush_held` — the coalesced run

```python
async def _flush_held(self, anchor: Anchor) -> None:
    """Deliver every post held for `anchor` as ONE run."""
```

1. Guard: `anchor in self._flushing` → return (a flush is already looping).
2. Mark `self._flushing.add(anchor)`; `try/finally` discards it.
3. Loop while the buffer is non-empty:
   1. `held = self._held.take(anchor)` (synchronous pop + persist).
   2. Resolve `session_id = self.mapping.get_session(anchor)`.
      None → requirements §3.7: log `warning` with authors+timestamps, post a
      channel warning naming them, drop out of the loop.
   3. For each `HeldPost`, in order: download attachments into the session
      cwd (reusing `_save_mm_attachments`, one cwd lookup for the whole
      batch), call `posters.note_post` to keep the tracker accurate, and
      render `HH:MM username: <notes+body>`. Skip a post that renders empty.
   4. Set `_session_triggerer[session_id]` from the **last** held post's
      `user_id` — the newest governs, so the completion ping goes to whoever
      spoke last.
   5. Build the body: single post → the one line; N > 1 → header + lines +
      `[End of held posts]`.
   6. `await self._deliver_to_session(...)` with
      `first_message=channel_id in self._awaiting_first_forward`.
   7. On success, swap ⏳ → ✅ on each post (best-effort).
4. The `while` re-check in (3) is §4.4: posts that arrived during the awaits
   are flushed in a second iteration rather than racing their own run.

### 2.7 `.queue`

`commands.py` gains one spec, appended to `_SPECS` (insertion order drives
`.help`):

```python
CommandSpec(
    "queue", ".queue [clear]",
    "Show posts held while the agent is working (`clear` drops them).",
    session_scoped=True,
),
```

`_dispatch_command` gains an `elif spec.name == "queue"` branch →
`_cmd_queue(channel_id, session_id, parsed.arg, thread_root)`. `.queue clear`
is the only accepted argument; anything else replies with the usage line.

### 2.8 Reactions

```python
def add_reaction(self, post_id: str, emoji_name: str) -> None:
    """React to `post_id` as the bot. Best-effort — callers must not let a
    reaction failure affect delivery."""
    self._driver.reactions.save_reaction({
        "user_id": self.bot_user_id, "post_id": post_id,
        "emoji_name": emoji_name,
    })

def remove_reaction(self, post_id: str, emoji_name: str) -> None: ...
    # self._driver.reactions.delete_reaction(user_id, post_id, emoji_name)
```

Verified against the installed `mattermostautodriver`: the `reactions`
endpoint exposes `save_reaction`, `delete_reaction`, `get_reactions`,
`get_bulk_reactions`. Emoji names are `hourglass_flowing_sand` (⏳) and
`white_check_mark` (✅). Both calls are wrapped in a bridge-side
`_react_best_effort` helper that swallows and `logger.debug`s failures, and
both run via `asyncio.to_thread` like every other blocking MM call.

## 3. Body format

```
[3 posts arrived while you were working — the newest governs]
14:02 tijs: can you also check the deploy logs?
14:07 bittern: R6 changed — cap is 50, not 20.
14:09 tijs: ignore the logs, the deploy was fine. Do R6.
[End of held posts]
```

Bracketed-marker style deliberately matches `_format_catch_up_block`
(bridge.py:2430) so the agent sees one consistent "this is bridge-injected
context, not a user turn" convention. A single held post renders as the bare
line, no markers.

Interaction with the silent-drop catch-up block: `_deliver_to_session` runs
`_peek_silent_drops_as_block` as it does today, so a flush whose anchor also
has mention-only drops emits the catch-up block **first**, then the held
block — oldest context first, newest instruction last.

## 4. Race analysis

### 4.1 The named race (R3)

> post held just as the run dies → nothing left to trigger the flush

### 4.2 Why atomicity is necessary but not sufficient

`_should_hold` and `_held.add` are both synchronous and adjacent, so no
coroutine can interleave between them; `_on_harness_run_lifecycle` likewise
pops `active_run_by_session` before its first `await`. Within one daemon's
event loop the interleaving "check saw live, flush already ran" cannot occur.

But atomicity only protects against *interleaving*. It does nothing about the
tracker being **wrong**: a lost SSE event, a harness restart, or a run that
ended before `run.started` was even processed all leave
`active_run_by_session` populated for a run that is already dead. Then T1
never fires, T2 only fires if typing is running, and the post is stranded.

### 4.3 Closure — the post-enqueue re-check (T3)

```python
async def _hold_post(self, anchor, session_id, post) -> None:
    # ATOMIC SPAN — no await between the liveness decision (in
    # _should_hold, already made by the caller) and the append. See §4.2.
    accepted = self._held.add(anchor, HeldPost(...))
    if not accepted:
        await self._overflow_submit(...)      # R6
        return
    await self._react_best_effort(post["id"], HOURGLASS, add=True)
    # T3 — the tracker said "live"; ask the harness whether that is still
    # true. Covers the case atomicity cannot: a tracker that is simply
    # stale because a terminal event never reached us. One GET, paid only
    # on the (rare) hold path.
    if not await self._active_run_is_alive(session_id):
        logger.info(
            "Held post for %s but the run is already dead — flushing now",
            session_id[:8],
        )
        await self._flush_held(anchor)
```

`_active_run_is_alive` (bridge.py:859) already returns False on every failure
mode (404, HTTP error, dead harness), so an unreachable harness flushes rather
than strands — the conservative direction.

### 4.4 Flush re-entrancy

Covered in §2.6 (3)/(4) and `_should_hold`'s `_flushing` check. A post
arriving during a flush is held and picked up by the flush's own loop, so it
can never submit a run that overtakes the flush's `create_run`.

### 4.5 Accepted, documented non-closures

| Case | Behaviour | Why acceptable |
| --- | --- | --- |
| Daemon restarts mid-run, trackers empty, cursor replay does not re-deliver `run.started` | Next post submits eagerly → harness FIFO | Exactly today's behavior; rehydration (§5.4) still flushes anything already held |
| Overflow post ordering (§7 of requirements) | Overflowing post reaches the FIFO ahead of the later flush | Degenerate case, explicit lead ruling |
| Reaction crash between remove ⏳ and add ✅ | Post shows neither | Cosmetic only |

## 5. Persistence (R4)

### 5.1 Why a sibling file, not the state file

The lead's brief says "extend the v3 state file". Two corrections:

1. **The state file is at v5**, not v3 (`STATE_SCHEMA_VERSION = 5`,
   config.py). v3 collapsed the mapping into `entries`; v4 added
   `last_event_seq`; v5 added `adopted_session_ids`.
2. **Extending it is the wrong shape.** `ChannelMapping.save()` rewrites the
   whole file, and `set_event_seq` calls it on a **2-second throttle for the
   duration of every busy SSE stream** (`EVENT_SEQ_FLUSH_INTERVAL_SECONDS`,
   bridge.py:110). Putting up to 50 full post dicts per anchor in that file
   means re-serializing the entire hold backlog every 2 seconds for the
   lifetime of the daemon — pure waste, and it couples transient queue state
   to durable topology. It also means `mm-bridge inbox` would parse (and
   risk racing) the file that decides channel→session identity.

**Recommendation: a sibling file.** `held_posts.json` beside `state.json`,
written only when a hold is added, taken, or cleared.

### 5.2 Schema

```json
{
  "version": 1,
  "anchors": [
    {
      "channel_id": "c1...",
      "root_id": null,
      "session_id": "ses_abc...",
      "posts": [
        {
          "held_at_ms": 1788291539000,
          "username": "tijs",
          "post": { "id": "p1", "channel_id": "c1...", "user_id": "u1",
                    "message": "…", "file_ids": [], "create_at": 1788291539000 }
        }
      ]
    }
  ]
}
```

`session_id` is recorded for diagnostics and for the rehydration probe. The
flush always uses the anchor's **current** session from `self.mapping` — if
`_replace_external_session` or a `.model` restart swapped the session under
the anchor, the conversation's owner is the new session, not the recorded one.

### 5.3 Atomicity

`_save()` writes `held_posts.json.tmp` in the same directory and
`os.replace()`s it into place — the same-filesystem rename is atomic on
POSIX, so a concurrent `mm-bridge inbox` reads either the old or the new
file, never a torn one. `load()` tolerates a missing file, invalid JSON, and
an unknown `version` (logs `warning`, starts empty) — a corrupt hold file
must never stop the daemon from booting.

### 5.4 Restart rehydration

`Bridge.start()` calls `await self._rehydrate_held()` after
`_bootstrap_known_sessions` / `_bootstrap_dormant_channels` (so the mapping
and session state are populated) and before the listeners start:

```
for anchor in self._held.anchors():
    session_id = self.mapping.get_session(anchor)
    if not session_id:                       -> §3.7 warn + name them
    elif await self._active_run_is_alive(session_id):  -> stay held (T1/T2)
    else:                                    -> await self._flush_held(anchor)
```

Net effect: a restart *delivers* the backlog instead of losing it. Compared
to today (posts sit in the harness FIFO and survive a bridge restart) this is
parity on durability and better on staleness — they arrive annotated.

## 6. Config (R7, R6, R4)

```python
# Hold posts that arrive while the anchor's session already has a run in
# flight, and deliver them as ONE annotated run when it finishes. Off =
# exactly the pre-F1 behavior: every post becomes its own eager run and the
# harness FIFO decides the order.
coalesce_posts: bool = True
# Per-anchor cap on held posts. On overflow the overflowing post falls back
# to an eager create_run and the bridge logs loudly — never a silent drop.
coalesce_max_held: int = 50
# Where held posts are persisted across daemon restarts. Empty = derived
# from `state_file`'s directory. Deliberately NOT inside the state file:
# that one is rewritten on a 2s throttle for the SSE cursor.
held_posts_file: str = ""
```

TOML: top-level `coalesce_posts`, `coalesce_max_held`, `held_posts_file` —
appended to the `_apply_toml` key loop. Env: `MM_COALESCE_POSTS`,
`MM_COALESCE_MAX_HELD`, `MM_BRIDGE_HELD_POSTS`, following the existing
boolean parsing idiom (`in ("1","true","yes","on")`). `held_posts_file` is
`_expand`ed alongside `state_file`, and defaulted in `Config.load()` after
expansion so a custom `state_file` carries the holds file with it.

## 7. CLI — `mm-bridge inbox` (R8)

```
mm-bridge inbox [--channel <ref>] [--json]
```

```python
def cmd_inbox(args) -> int:
    """Print the posts held-but-not-yet-delivered for this session.

    Read-only and best-effort by construction: the daemon may append to the
    holds file between our read and our print, so the output is a snapshot,
    not a lock. Writes are atomic (tmp + os.replace) so we can never read a
    torn file — the worst case is a snapshot one post stale.
    """
```

Resolution: `--channel` → existing `_validate_channel_ref` /
`_resolve_explicit_anchor` chain (id, slug, channel URL, permalink — the four
forms from PR #50). No `--channel` → `_current_session_id(cfg.sidecar_dir)` →
`_resolve_anchor_from_session`, i.e. the same chain `mm-bridge post`/`read`
use, and **no bot token needed**: with the username stored in the envelope
the command touches only the local file. `_require_bot_token` is therefore
called only on the `--channel` path.

Output:

```
2 posts held for ~mm-f1-hold (not yet delivered):
  14:07  bittern  R6 changed — cap is 50, not 20.
  14:09  tijs     ignore the logs, the deploy was fine. Do R6.
```

or `(empty)`. `--json` emits the envelopes verbatim for scripting.

## 8. Teardown and cleanup

Held buffers are forgotten wherever `_silent_drops` are, so a torn-down
channel cannot leak a previous session's backlog into the next one:

* `_leave_channel` (bridge.py:3760) — next to `_forget_channel_silent_drops`.
* `_on_mm_user_removed` (bridge.py:1197) — bot kicked from the channel.
* `_run_leave_command` thread path — the thread anchor only.

`.queue clear` removes the ⏳ reactions of the posts it drops.

## 9. Test plan

`tests/test_hold_and_coalesce.py`, using the existing `make_bridge` double
harness (`tests/doubles.py:336`). `FakeAgentHarnessClient` already records
`create_run` calls and serves `list_session_runs`, which is exactly what the
liveness probe and the "one run, not three" assertions need.

The 16 rows of requirements §12.1, each written red-first. Two need new
double surface:

* `FakeMattermostClient.add_reaction` / `remove_reaction` recording into a
  `reactions: dict[post_id, set[str]]` for test 13.
* `FakeAgentHarnessClient.list_session_runs` returning a **terminal** row
  while `active_run_by_session` still says live — the T3 fixture for test 9.

`tests/test_cli_inbox.py` writes a holds file directly and runs `cmd_inbox`
against it — no daemon, no MM, matching how `test_cli_read.py` drives
`cmd_read`.

## 10. Open questions for the lead

1. **Unconditional `HH:MM username:` on held posts** (requirements §3.3) —
   this bypasses `PosterTracker`, so a single-human channel now sees a
   username prefix it would not see today. Recommended anyway: the timestamp
   is the payload, and a naked line hides the staleness. Confirm.
2. **Local-time `HH:MM`** (§3.5) rather than UTC.
3. **Single held post keeps the timestamp line** — only the header and the
   `[End of held posts]` marker are dropped, per "flushes without the
   multi-post header".
4. **`✅` on delivery** is a second reaction API call per post. Kept (R9 says
   in scope if cheap), but it is the one piece that is pure polish and the
   cheapest thing to cut if the lead wants a smaller diff.
