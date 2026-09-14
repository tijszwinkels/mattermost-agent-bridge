# External pi sessions in Mattermost

Filed: 2026-09-14
Branch: `external-pi-mattermost` (from `origin/main` @ ac12ef0)
Harness counterpart: agent-harness `specs/2026-09-14-external-pi-sessions.md`

## Problem

`.invite` refused external pi sessions outright:

```python
# External pi sessions aren't resumable (harness 409), so a channel
# would be a dead end — reject up front.
if meta.get("origin") == "external" and backend == "pi":
    ... "it can't be resumed from Mattermost."
```

That was true when written and is no longer: the harness now discovers
external pi sessions and continues them headlessly via
`pi -p --session <transcript-path>`.

Deleting the rejection does nothing on its own, though. Independent review
established that the bridge tags **every** `origin: external` session as
undeliverable, at two separate points, and the first post into such a
channel swaps in a brand-new harness session — discarding exactly the
conversation the user asked to continue. `.invite` creating a channel is
not the feature; the *post* is.

## Changes

### 1. `.invite` no longer special-cases pi (`bridge.py`)

The hardcoded branch is removed. Whether a run can be honoured is a
per-session question the harness answers at `POST /v1/runs` time — the
bridge shouldn't second-guess it from a backend name, and can't: a pi
session's resumability depends on whether its transcript is still on
disk, which the bridge cannot see.

`.sessions` needed no change; it already lists every `origin: external`
row with an `.invite <id>` hint.

### 2. External pi sessions are not replaced on first post

`supports_external_resume(backend)` (in `external_resume.py`) gates the
two places that tag a session undeliverable:

- `_bootstrap_known_sessions` — re-derived from `GET /v1/sessions` on every
  daemon start, so the exemption has to hold across restarts too;
- `_create_channel_for_session` — both auto-mirroring and `.invite`.

Only pi is exempt. claude and codex keep the replacement behaviour they
have always had — this change makes no claim about, and no change to, what
that path does. Scoping it this narrowly was a review request.

Covered by `tests/test_external_pi_continuation.py`, which drives the real
`_on_mm_posted` — a test that stops at "a channel was created" passes
while the feature is still broken.

### 3. Backend-accurate heads-up (`external_resume.py`, new)

The old notice was hardcoded to the claude/codex story:

> this channel becomes a **fork** of the conversation, not a remote
> control of the live TUI

For pi that is wrong in the more dangerous direction. pi appends to the
**same** session file the terminal created, so the channel is not a
branch — it is a second writer on one conversation. (The claude/codex
wording is carried over verbatim rather than restated: their path through
the bridge is untouched here, so re-asserting its mechanics would be a new
claim this change hasn't verified.)

An earlier draft of the pi wording said turns "can interleave". A PTY
probe run during review showed that is wrong in a way that reads as
reassuring: a still-open TUI keeps its own in-memory copy, so its next
turn and the bridge's chain onto the **same parent** as sibling branches,
and whoever reopens the file sees only the branch appended last. The other
side's turns are silently missing from the model's context. The notice
therefore gives a **procedure**, not a caveat:

| backend | wording |
|---|---|
| claude, codex | inherited copy, unchanged — resumes via `--resume`, this channel is a **fork** |
| pi | continues **headlessly** (`pi -p`), appends to the **same transcript**; close the terminal session before posting here and reload it before going back, or the two become **sibling branches** and one side's turns vanish from context |
| anything else | generic "continues from its transcript" |

All three end with "It is **not a remote control** of the live terminal
session." The unknown-backend case deliberately claims *neither* fork nor
headless: guessing the mechanics of an unverified backend is exactly the
failure this module exists to prevent. There is no Herdr adapter here and
none implied — driving a live TUI is out of scope.

### 4. A 409 says why (`backend_errors.format_resume_refusal`)

`HarnessResumeUnsupported` carries the harness's `detail`, which the
bridge was discarding in favour of a flat
":warning: Can't resume this external session from Mattermost."

For the pi case that detail is the actionable part —

> Cannot resume external pi session `ses_…`: its pi transcript (`<path>`)
> is missing, unreadable, or belongs to a different conversation —
> resuming would silently start a new one

— and without it the operator has no way to distinguish "fix the cwd"
from "the bridge is broken", so they retry forever.

`format_resume_refusal(detail)` redacts and condenses the detail like
every other channel-facing error, and leads with **"Your message was not
delivered"** so a refusal is never mistaken for a delivered-but-quiet
turn. The existing silent-drop carve-out is untouched: an unsupported
delivery still parks the post in the silent-drop queue (or names its
author when that queue is disabled), so nothing is lost.

## Limitation to keep in view

The harness observes and resumes on **its own host's filesystem**. A pi
session running on a laptop does not appear in a pillar-hosted harness,
and pillar cannot continue it. Surfacing laptop sessions needs a
harness + bridge on the laptop, or transport that lands the transcripts
under an observe-root. See agent-harness README.md.

## Acceptance criteria

| # | Criterion | Test |
|---|---|---|
| 1 | `.invite` on an external pi session creates a channel and invites the requester | `test_dot_invite_pi_external_creates_a_channel` |
| 2 | The channel's heads-up says headless continuation, not fork | `test_dot_invite_pi_external_warns_headless_not_fork` |
| 2a | The heads-up gives the close/reload procedure, not "interleave" | `test_pi_notice_gives_the_close_and_reload_procedure` |
| 2b | Posting into an invited external pi channel continues it, fresh and after restart | `test_post_to_an_invited_external_pi_channel_continues_it` |
| 2c | claude/codex keep the replacement path | `test_external_claude_still_gets_a_replacement_session` |
| 2d | Auto-mirrored and already-mapped pi channels carry the warning | `test_auto_mirrored_pi_channel_carries_the_warning`, `test_invite_to_an_already_mapped_pi_channel_still_warns` |
| 3 | claude/codex keep the fork wording | `test_dot_invite_unmapped_external_creates_channel_and_warns`, `test_fork_backends_say_fork` |
| 4 | An unknown backend warns without claiming mechanics | `test_unknown_backend_still_warns_without_claiming_mechanics` |
| 5 | `.sessions` lists external pi with an invite hint | `test_dot_sessions_lists_external_pi_with_an_invite_hint` |
| 6 | A harness 409's reason reaches the channel | `test_resume_unsupported_tells_the_channel_the_harness_reason` |
| 7 | A refused post is not silently lost | `test_resume_unsupported_moves_them_to_silent_drops_once` (pre-existing), `test_resume_unsupported_names_them_when_drops_are_disabled` (pre-existing) |
| 8 | Refusal text is redacted | `test_refusal_redacts_secrets_in_the_detail` |

Tests: `tests/test_external_pi_continuation.py` (5),
`tests/test_external_resume_notice.py` (8), `tests/test_resume_refusal.py`
(4), plus additions to `tests/test_bridge.py` and
`tests/test_hold_and_coalesce.py`.

```
uv run pytest -q
```
