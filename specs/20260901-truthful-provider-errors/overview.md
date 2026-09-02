# Truthful provider errors — Overview

**Round F3 of the swarm-elegance program. Base: `main` @ `3e28e53`. Branch: `feat/truthful-provider-errors`.**

## Problem statement

Two recorded incidents in the Meshenger V2 program, both on the **pi** backend:

| # | Date | What actually happened | What the channel said |
|---|------|------------------------|-----------------------|
| 1 | 2026-08-31 13:51 | An OpenRouter key hit its **daily limit** (HTTP 403). Every subsequent run failed. | `:warning: I tried to run your message with the `claude` backend and got this error: ``` CLI exited with a non-zero status (1). ```` |
| 2 | 2026-09-01 | ollama-cloud returned **429 "session usage limit"** mid-round. | Same shape. |

Three lies in one line:

1. **Wrong backend.** The session was `pi`; the message named `claude`.
2. **No failure class.** "non-zero status" reads like a bug in the agent, not a quota wall the operator can act on.
3. **The expensive omission — no retry statement.** Nothing said the message had *not* been processed and would *never* be retried. The lead saw silence, read it as "busy", and the lane starved for hours.

The third is the one that cost time. **Quota-starved and busy are currently the same silence.**

## Goal

Every backend failure the channel sees must be true about three things:

1. **Which backend** actually ran (or say "the backend" — never guess).
2. **What class of failure** it was, and which provider, when the evidence supports it.
3. **Whether the message was processed, and whether anything will retry it.** Never imply a retry that does not happen.

## Scope

- Root-cause and fix the backend misattribution (bridge-side only).
- A pure, table-driven failure classifier in `backend_errors.py`.
- Reworked channel messages at both failure sites, with per-site retry truth.
- A single guarded call seam for round F2's per-session state API, so `.fleet` can eventually show `blocked (quota)`.

## Explicitly out of scope

- Automatic retries or backoff.
- The fleet view itself (round F2, branch `feat/turn-state-fleet`).
- Any change to `agent-harness`.
- Live e2e against the deployed bridge. Tests only; no second daemon.

## Related work

- **PR #45** (`0ad79f3`, commit `47d5c7c` "live session meta wins over a token-less Channel Purpose") — the same family of cold-cache bugs. Its ruling is the precedent this spec applies to a different reader.
- **Round F1** (PR #51, `feat/hold-and-coalesce`) renames `_forward_user_post` → `_deliver_to_session`. We touch the failure-surfacing paths minimally so the merge is trivial in either order.
- **Round F2** (`feat/turn-state-fleet`) owns `set_session_state(...)`. See `design.md` § State seam.
