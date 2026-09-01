# Turn-End State, Fleet View, Awaiting-Nag — Design

Base: `feat/hold-and-coalesce` @ `24281c5`. Every seam below is named with the
file and symbol it attaches to, so the diff is reviewable against this doc.

---

## 1. Data model

New module `src/mm_bridge/session_state.py` — pure, no I/O, modelled on
`purpose.py` / `commands.py`:

```python
KINDS = ("idle", "awaiting", "parked", "blocked")

@dataclass(frozen=True)
class SessionState:
    kind: str                 # one of KINDS
    on: str | None = None     # "lead" | an MM username | None
    note: str | None = None   # free text, agent-authored
    set_at: float = 0.0       # epoch seconds (wall clock)
    source: str = "agent"     # "agent" | "bridge"  (F3 uses "bridge")

    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, raw: dict) -> "SessionState | None": ...   # None on junk
    def describe(self) -> str:      # "awaiting → lead"  /  "blocked (quota)"
```

Why a frozen dataclass and not a dict: the fleet renderer, the nag sweep, the
CLI and the persistence layer all read it, and three of those live outside
`bridge.py`. A typed value with one `describe()` keeps the rendering rule in
one place.

**Wall clock, not `time.monotonic()`.** Everything else in the bridge that
measures elapsed time (`last_activity_ts`, `_held_probe_ts`) uses monotonic,
deliberately. `set_at` cannot: it is the one field that must survive a restart
and be renderable by an out-of-process CLI. The cost is that a large clock
step can make one nag early or late; the threshold is 30 minutes, so this is
noise. Documented at the field.

## 2. Persistence — v6 in the state file, not a sibling (R4)

**Decision: extend the state file to schema v6.**

Kestrel's F1 argument for a sibling file was specifically about *volume and
lifetime*: up to `coalesce_max_held = 50` full post dicts per anchor, riding a
`save()` that the SSE cursor already calls on a 2-second throttle for the life
of every busy stream. Re-serialising a backlog of post dicts every 2s is a
real cost, and queue state has a different lifetime from channel↔session
topology.

Neither half of that argument transfers:

| | F1 held posts | F2 session state |
| --- | --- | --- |
| Size | up to 50 **post dicts** per anchor (KBs each) | 4 scalars per session (~90 bytes) |
| Cardinality | grows with traffic | one per session, bounded by the mapping itself |
| Lifetime | transient — minutes | exactly the session's lifetime |
| Ownership | the bridge's queue | a **property of the session**, like its anchor |

The state file is already rewritten in full every ≤2s while any stream is
busy; adding ~90 bytes × N sessions to that write is not measurable. And
colocation buys correctness for free: `ChannelMapping.unlink()` /
`_add()`-with-replacement already remove the session's row, so an unlinked or
swapped session cannot leave an orphan state behind (requirement 3.7). A
sibling file would need its own `forget_session` calls at four call sites —
exactly the class of bug F1's `forget_anchor`/`forget_channel` pair exists to
avoid.

On-disk (v6, one new optional key per entry):

```json
{
  "version": 6,
  "entries": [
    {"channel_id": "c1", "root_id": null, "session_id": "ses_ab",
     "state": {"kind": "awaiting", "on": "lead",
               "note": "M0 gate", "set_at": 1756704000.0, "source": "agent"}}
  ],
  "last_event_seq": 41,
  "adopted_session_ids": []
}
```

`_ingest` accepts `version in (3, 4, 5, 6)`; the `state` key is read only at
v6 and tolerated-but-ignored otherwise. Junk in a state object (`from_json`
returns `None`) drops that one state and keeps the entry — a corrupt state
must never cost a channel its session mapping.

**Atomicity (requirement 4.3).** `ChannelMapping.save()` currently does a
bare `Path.write_text()`. That was safe while the daemon was the only reader.
`mm-bridge fleet` makes the CLI a concurrent out-of-process reader, so `save()`
moves to temp-file + `os.replace()` — the same 5 lines as
`HeldPostStore._save`. This is the only change to an existing F1-adjacent
write path, and it is strictly a robustness improvement for existing readers.

## 3. The nag delivery seam (R5) — the hard part

### 3.1 What actually suppresses what

Two distinct mechanisms, often conflated:

1. **`mm_client.is_own_post(post_id)`** — the daemon records every post id it
   creates and drops the websocket echo *upstream*, in `mm_client.py:521`,
   before `_on_mm_posted` ever runs. This is why a bridge post never becomes a
   turn.
2. **`props.from_bridge_cli` handling** in `_on_mm_posted` — the CLI's
   self-post loop-back suppression, keyed on intent
   (`tests/test_self_post_contract.py`). Irrelevant to a daemon-authored post.

So a nag posted with `self.mm.post(...)` is invisible to the forwarding path
**by construction**, not by a filter we could politely ask to let it through.

### 3.2 Options considered

**(a) Marked post the suppression lets through.** Requires either not
recording the nag's post id as own (breaking the generic echo dedup for that
id, and inviting a double-handling bug if MM redelivers) or adding a props
marker checked *before* the id dedup in `mm_client`. Both punch a hole in an
invariant that has a cross-layer contract test, to re-enter a path we can call
directly. Rejected.

**(b) Post to MM **and** submit through the normal forwarding tail.**
Recommended. The post is authored for the human's eyes; the turn is submitted
explicitly through the same tail F1 factored out.

### 3.3 The shape (confirmed by the lead)

```python
async def _deliver_nag(self, target: Anchor, session_id: str, body: str) -> None:
    post = await asyncio.to_thread(self.mm.post, target.channel_id, body,
                                   root_id=target.root_id)
    # Route exactly as a user post would be routed: a busy target holds
    # (F1 coalescing folds the nag into the next flush), an idle one runs.
    if self._should_hold(target, session_id):
        if await self._hold_post(target, session_id, post, body):
            return                      # held → F1 owns delivery from here
        # overflow → fall through to the eager path, same as _forward_user_post
    await self._deliver_to_session(
        target.channel_id, session_id, body,
        thread_root=target.root_id,
        first_message=target.channel_id in self._awaiting_first_forward,
        exclude_post_id=post.get("id"),
        requeue_on_failure=[],
    )
```

Everything this inherits is deliberate:

* **Holding** — a nag that lands on a lead mid-crunch is folded into the next
  coalesced flush instead of queueing a stale ghost turn. This is the brief's
  "F1's coalescing also folds a nag that lands on a busy lead", achieved by
  *reusing* F1 rather than by special-casing.
* **Silent-drop peek / first-message preamble** — a nag that happens to be the
  first thing ever forwarded to a channel still carries the MM-context
  preamble, so the woken agent is not confused about where it is.
* **`exclude_post_id`** — the nag's own post is excluded from the silent-drop
  catch-up block, so it cannot appear twice in the same turn.
* **`requeue_on_failure=[]`** — a failed nag is not re-enqueued as a silent
  drop. A doorbell is not a user message; replaying it later would ring at an
  irrelevant time (requirement 5.9).

Visibility to the human is guaranteed by the MM post, which happens first and
unconditionally.

**Self-identifying body (lead ruling).** The nag body starts `⏰ bridge nag:`.
When F1 holds it and flushes it later, `HeldPost.render()` prefixes
`HH:MM <bot-username>: `, and without the marker the woken agent would read a
bridge doorbell as a peer agent's post.

**Placement by `on` (lead ruling on Q2).** `on="lead"` (or unset) →
parent channel, delivered as a turn. `on="<known MM username>"` → level 1 goes
to the **awaiting session's own channel** with the @-mention and **no delivery
call at all**: `is_own_post` guarantees the post cannot wake the child, and
the summoned human is where the context is; level 2 escalates to the parent
channel, mentioning the same user and delivering as a turn so the lead learns
its builder is stuck on a human. An unknown username is treated as `lead`, and
the nag says so.

### 3.4 Ghost-turn safety

Three independent bounds, any one of which is sufficient:
one post per level per episode (5.6); one nag post per watchdog tick across
the whole fleet (5.8); and the hold path, which means a busy target never gets
an extra run at all — the nag merges into a flush it was going to receive
anyway.

## 4. State transitions

### 4.1 Where state is set

| Trigger | Call | Kind |
| --- | --- | --- |
| assistant text block with `<state/>` | `set_session_state(sid, kind, on, note, source="agent")` | declared |
| assistant text block **without** a tag | `set_session_state(sid, "idle", source="agent")` | `idle` |
| `run.started` | re-stamp `set_at`, clear episode bookkeeping | unchanged |
| F3 (next round) | `set_session_state(sid, "blocked", note="<class> (<provider>, HTTP <status>)", source="bridge")` | `blocked` |

F3's `<class>` vocabulary (`quota_exhausted | rate_limited | auth |
context_overflow | harness_process | unknown`) is pinned by the lead. F2
surfaces its first token in the fleet's state column —
`blocked (quota_exhausted)` — while an agent-declared `blocked` stays bare,
because agent prose is not a class name. `set_session_state` never raises, so
F3's own error handling cannot be derailed by our bookkeeping.

Applied per **assistant text block**, in `_handle_assistant_text_block`, right
after `directives.extract`. A multi-block reply therefore ends in the state its
LAST block declared — identical to turn-end semantics, with no per-run buffer
to keep. Mid-run flapping is invisible: the fleet shows `working` while a run
is live, and the nag sweep skips sessions with an active run.

### 4.2 Episodes and the `run.started` re-stamp [decision]

An episode is `(session_id, set_at)`; `_nagged_levels: dict[str, set[int]]`
is keyed by session and cleared whenever `set_at` changes. `run.started`
re-stamps `set_at = now` for a session in `awaiting`.

Why re-stamp rather than only clearing bookkeeping: if a run starts and dies
without emitting a text block (interrupt, provider error — F3's territory),
the state stays `awaiting` with its original `set_at`. Clearing bookkeeping
alone would then re-fire level 1 immediately, on a wait that just had activity
in it. Re-stamping says the honest thing: *the wait has been running since the
last time anything happened in this session* — which is exactly what the fleet's
age column is specified to mean ("time since the state was set or the last run
ended").

### 4.3 What clears state

`ChannelMapping.unlink()` and session replacement drop the entry and its state.
No separate cleanup path exists, by design (§2).

## 5. Fleet view

### 5.1 Children

`mm.list_bot_channels()` returns full channel records — `id`, `name`,
`display_name`, `header` — for every channel the bot is in, in **one** API
call. Children of channel C = records whose `header` starts with
`Parent: ~<C.name>~` (the exact string `spawn.format_parent_header` writes;
the optional `([thread](url))` suffix is why this is a prefix match, not
equality).

Called via `asyncio.to_thread` — the MM client is synchronous, and `.fleet`
must not block the event loop (R8).

### 5.2 Row assembly — ONE bounded parallel probe pass (lead condition C1)

| Field | Source | Staleness |
| --- | --- | --- |
| session | `mapping.get_session(Anchor(cid))` | live |
| state | `mapping.get_state(sid)` | live |
| run | harness probe, tracker as fallback | live, or `?` |
| held | `self._held.peek(anchor)` | live |
| err | `-` (F3 seam) | — |

M0 proposed zero GETs, arguing the tracker is reconciled every watchdog tick.
The lead rejected that on the evidence: `_typing_watchdog_tick` iterates only
`self.typing.running_sessions()` (bridge.py:896). A session whose `run.started`
event was lost never enters `active_run_by_session`, so it never enters that
list either — F1's own C1 finding — and is never reconciled. (The lead
corrected their own wording afterwards: the `self.typing`-unset early return
at :892 covers only the pre-login window, since the indicator is created
unconditionally at :568. The ruling stands on the lost-`run.started` leg
alone.) The tracker can therefore be stale in both directions, and a fleet
that shows a working builder as `idle` is precisely the lie this feature
exists to remove.

Shape (`_fleet_rows`, the seam M0 already planned):

```python
FLEET_PROBE_TIMEOUT_S = 2.0

async def _probe_run_live(self, sid: str) -> bool | None:
    """True/False from the harness; None = unknown (caller falls back)."""
    try:
        return await asyncio.wait_for(
            self._active_run_is_alive(sid), FLEET_PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):
        return None

probed = await asyncio.gather(*(self._probe_run_live(s) for s in sids),
                              return_exceptions=True)
```

Parallel, so the *total* bound is one timeout (≈2 s), not N of them. A row
whose probe returns `None` (timeout, error, harness down) falls back to
`_session_has_active_run(sid)` and is rendered with a trailing `?`, so an
uncertain row is visibly uncertain rather than quietly wrong.

The **nag sweep does not probe**: it runs every 3 s against every awaiting
session, and a nag mistakenly sent to a session with a lost `run.started` is
harmless — it is one post, bounded by every rule in §3.4.

### 5.3 Rendering

```
kestrel   awaiting → lead   47 min   note: C5 gate      held: 0   err: -
grebe     working           run 12m                     held: 2   err: -
wagtail   blocked (quota)   3h 12m                      held: 1   err: -
```

Rendered in a fenced block for Mattermost so columns line up in the mobile
client too. The renderer is a pure function in `session_state.py`
(`render_fleet(rows) -> str`) so the dot-command and the CLI produce byte-
identical output and the test asserts once.

### 5.4 CLI

`mm-bridge fleet [--channel <ref>] [--all] [--json]`, next to `inbox` in
`cli.py`. Loads `ChannelMapping.load(path, reconcile_sidecars=False)` —
**never** reconciling, because the CLI must not mutate the daemon's sidecars —
plus the holds file, plus one `list_bot_channels()` for titles. It probes the
harness per row too (best-effort, bounded, local HTTP) and renders `?` on
failure — unlike the daemon it has no tracker to fall back to. Same staleness
contract as `inbox`, stated in the docstring.

## 6. Sweep (R7)

Third phase of `_run_typing_watchdog`:

```python
await self._typing_watchdog_tick()
try: await self._sweep_held_anchors()
except Exception: logger.exception(...)
try: await self._sweep_awaiting_nags()
except Exception: logger.exception("Awaiting-nag sweep iteration failed")
```

`_sweep_awaiting_nags` returns immediately when `nag_enabled` is false or no
session's state is `awaiting`. Otherwise it walks awaiting sessions oldest
first, skips those with a live run, computes the due level (2 → escalation,
1 → nag), skips levels already fired for the episode, and **sends at most one
nag per tick** (5.8) — oldest first, so the longest-waiting session is never
starved by a newer one.

## 7. Config

| Key | Default | Env |
| --- | --- | --- |
| `nag_enabled` | `true` | `MM_NAG_ENABLED` |
| `awaiting_nag_after_seconds` | `1800` | `MM_AWAITING_NAG_AFTER_SECONDS` |
| `operator_username` | `""` | `MM_OPERATOR_USERNAME` |

Added to `_apply_toml`'s key list and to the env overlay, following
`coalesce_max_held`'s precedent: a non-numeric threshold logs a warning and
keeps the default rather than taking the daemon down on boot.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Nag becomes a ghost-turn generator | three independent bounds (§3.4) |
| Agent declares `awaiting` and then goes quiet forever | that is the point; two doorbells, then the fleet row carries the age |
| Clock step skews `set_at` | 30-minute threshold absorbs it; documented |
| `list_bot_channels` slow on a large team | one call, off-loop, only on `.fleet` |
| State claims diverge from reality | fleet renders claims *next to* run/held observations |
| v6 file read by an older bridge | extra key ignored; degrades to "no states" |

## 9. Test plan

The 23 behaviours in `requirements.md` §9, in
`tests/test_turn_state_fleet.py` (+ parser cases in the existing
`tests/test_directives.py`, + a v5→v6 persistence case in the existing
mapping tests). Every asynchronous wait is bounded with F1's `_await_gate`
pattern — a red test that hangs is not a red test.
