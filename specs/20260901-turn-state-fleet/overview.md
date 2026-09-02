# Turn-End State, Fleet View and the Awaiting-Nag — Overview

**Round:** F2 (swarm-elegance program) · **Branch:** `feat/turn-state-fleet` ·
**Base:** `feat/hold-and-coalesce` @ `24281c5` (round F1, PR #51, accepted)

## Problem Statement

A builder that ends its turn with *"M1 done, awaiting your GO"* is, to the
bridge, indistinguishable from one that said *"done for the night"*. The
obligation lives entirely in prose:

* **Nothing watches it.** No structure records that a session is blocked on
  someone; nothing retries; nothing expires.
* **Nobody retries.** Recorded incident — Meshenger V2, 2026-09-01: a builder
  sat idle **8 hours** mid-round because its milestone post crossed the lead's
  crunch and was never acked. The lead then reported that lane as "actively
  building": there was no evidence to the contrary anywhere in the system.
* **The lead is blind.** Short of hitting the agent-harness API by hand, a
  lead has no view of which builders are idle, queued, running or erroring.

The asymmetry that makes this fixable: **the bridge is the only always-awake
party**, and *a post into a channel IS a turn*. An agent cannot wake itself
after 30 minutes of silence — but the bridge can wake it, and the wake-up is
the same mechanism a human uses.

## Goal

Three parts, each useful on its own:

**A — a turn-end state directive.** An agent may end a reply with one tag,
stripped from the visible post exactly like `<leaveChannel/>`:

```
<state kind="awaiting" on="lead" note="M0 gate" />
```

`kind ∈ idle | awaiting | parked | blocked`. **A missing tag means `idle`**,
so agents that never learn the tag behave exactly as they do today — zero
migration.

**B — a fleet view** (`.fleet` dot-command, `mm-bridge fleet` CLI), one row
per child channel of the current channel:

```
kestrel   awaiting → lead   47 min   note: C5 gate      held: 0   err: -
grebe     working           run 12m                     held: 2   err: -
wagtail   blocked (quota)   3h 12m                      held: 1   err: -
```

**C — the awaiting-nag.** A session `awaiting` for longer than
`awaiting_nag_after_seconds` (default 1800) triggers **one** post into the
*parent* channel:

> ⏰ ~grebe~ has been awaiting you for 30 min ("M0 gate")

That post is delivered as a **turn** to the parent's session, so it wakes the
party that forgot. At 2× the threshold, one escalation carrying an
@-mention of the operator.

## Scope

**In:**

* `<state .../>` parsing in `directives.py`, fence-aware, last-tag-wins.
* One internal state API (`Bridge.set_session_state`) that F3 will call for
  `blocked` with a provider-error reason.
* Persistence of `(kind, on, note, set_at)` per session in the state file
  (schema v6).
* `.fleet` dot-command and `mm-bridge fleet [--channel <ref>] [--all]`.
* Nag sweep as a **third phase of the existing watchdog task** — no new timer.
* Kill-switch `nag_enabled` (default ON) + `MM_NAG_ENABLED`; threshold config
  + `MM_AWAITING_NAG_AFTER_SECONDS`; `operator_username` +
  `MM_OPERATOR_USERNAME`.
* `CLAUDE-include.md` (the file every bridged agent loads) and README docs.

**Out** (lead ruling):

* Provider-error classification — round F3. F2 leaves the state seam
  (`set_session_state(..., source="bridge")`) and renders the failure column
  as `-`.
* Any agent-harness change.
* Interrupt-and-inject into a live turn.
* Live e2e against the deployed bridge or a second daemon. Tests only.

## Non-Goals

* **Not a scheduler.** The nag is a doorbell, not a retry queue: at most two
  posts per awaiting episode, ever.
* **Not presence.** `working` is derived from the run tracker for display; it
  is never a *declared* state and never overwrites what the agent said.
* **Not a truth oracle.** A state is the agent's own last claim about itself.
  The fleet view renders claims next to observations (run live, held count)
  precisely so a stale claim is visible as one.

## Success Criteria

1. An agent ending a reply with `<state kind="awaiting" on="lead"/>` shows in
   `.fleet` as `awaiting → lead`, and the tag never appears in the channel.
2. An agent that never emits the tag is byte-for-byte unaffected.
3. A session awaiting past the threshold produces exactly ONE nag post in the
   parent channel, and that post arrives as a turn for the parent session.
4. `nag_enabled = false` → A and B still work, C never posts.
5. A daemon restart preserves declared states; at most one re-nag per
   already-overdue episode.

## Documents

* [`requirements.md`](requirements.md) — numbered requirements traced to the
  lead's rulings R1–R11.
* [`design.md`](design.md) — data model, persistence, the nag delivery seam,
  sweep bounding, fleet data sources, test plan.
