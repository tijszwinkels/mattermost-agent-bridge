# Hold-and-Coalesce Posts While a Run Is In Flight — Overview

**Round:** F1 (swarm-elegance program) · **Branch:** `feat/hold-and-coalesce` · **Base:** `3e28e53`

## Problem Statement

Every Mattermost post that clears the mention gate becomes its own
eagerly-submitted harness run. `_forward_user_post` (bridge.py) ends in a
bare `await self.harness.create_run(session_id, body)` with **no check for
whether a run is already in flight**. The harness accepts it, FIFOs it
(`RunManager`, cap 16), and later fires it as a FULL turn — however stale
the message has become by the time it runs.

That is fine for a quiet channel. It is actively harmful for a busy one:

* **Stale turns fire anyway.** A post written while the agent was mid-turn
  is executed verbatim minutes later, against a world that has moved on.
* **Farewells become ghost turns.** Every "thanks, we're done" post to a
  parked session spawns another full turn.
* **Manual surgery.** Recorded incidents in the Meshenger V2 program needed
  `DELETE /v1/sessions/<id>/runs/<run_id>` by hand to clear stale queues.
* **Token bill.** One round's spend was dominated by one-full-turn-per-lead-post.
* **Ordering hazard for the agent's own output.** A builder's FINAL shipped
  without the ruling that was queued behind its long turn — the agent could
  not see that anything was waiting.

The common root cause: the bridge treats *arrival* as *submission*. There is
no place in the system where "messages that arrived while you were working"
exists as a first-class thing — for the bridge, for the operator, or for the
agent.

## Goal

While a session has a live run, **hold** would-be-forwarded posts instead of
creating runs. When the run reaches a terminal state, flush the whole buffer
as **one** combined run:

```
[3 posts arrived while you were working — the newest governs]
14:02 tijs: can you also check the deploy logs?
14:07 bittern: R6 changed — cap is 50, not 20.
14:09 tijs: ignore the logs, the deploy was fine. Do R6.
[End of held posts]
```

Three stale full turns become one cheap annotated turn, and the agent sees
the *whole* backlog at once — including that the newest post supersedes the
first.

## Scope

**In:**

* Bridge-side hold buffer keyed by anchor `(channel_id, thread_root)`.
* Flush on run-terminal SSE events **and** on out-of-band dead-run discovery.
* Durable holds across a daemon restart (parity with today's harness FIFO).
* `.queue` / `.queue clear` dot-commands.
* `mm-bridge inbox` CLI — the agent-facing "is anything waiting for me?".
* ⏳ / ✅ reactions on held posts.
* Kill-switch `coalesce_posts` (default ON); OFF = byte-for-byte today.

**Out** (explicitly, by lead ruling):

* Turn-end state directives / `.fleet` (round F2).
* Provider-error attribution (round F3).
* Interrupt-and-inject into a live turn.
* Any agent-harness change — its FIFO stays as the safety net.
* Live e2e against the deployed bridge. Tests only; live validation happens
  at deploy after merge.

## Non-Goals

* **Not a replacement for the harness FIFO.** Overflow and kill-switch-OFF
  both fall back to it deliberately.
* **Not a message queue with delivery guarantees.** A held post is a
  best-effort improvement over an immediately-stale turn, not a durable bus.
* **Not summarization.** Held posts are replayed verbatim, in order. The
  agent decides what supersedes what; the header only tells it to look.

## Success Criteria

1. Three posts to a busy session produce **one** `create_run`, not three.
2. `.stop` on a busy session leaves the backlog intact and delivers it as one
   annotated turn (the interrupt itself is what triggers the flush).
3. A daemon restart with held posts loses nothing.
4. `mm-bridge inbox` inside a session prints what is waiting, or `(empty)`.
5. `coalesce_posts = false` reproduces today's behavior exactly.

## Documents

* [`requirements.md`](requirements.md) — numbered requirements, traced to the
  lead's rulings R1–R12.
* [`design.md`](design.md) — data structures, seams, flush triggers, race
  analysis, persistence schema, test plan.
