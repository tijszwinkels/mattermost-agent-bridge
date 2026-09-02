# Truthful provider errors — Design

## 1. Root cause: a shadowed method, not a bad default

`class Bridge` defines `_backend_for_channel` **twice**:

| Line | Added by | Purpose | Fallback when unknown |
|------|----------|---------|-----------------------|
| `bridge.py:1662` | `6bf1dd2` *feat(bridge): surface backend-invocation failures in the channel* | Name the backend in **error messages** | `None` → template degrades to "the backend" ✅ |
| `bridge.py:4076` | `4f32a2b` *feat(bridge): write Resume line into channel header on claim + startup* | Pick a backend for the **Resume command** | `self.config.default_backend` → `"claude"` |

Python keeps the **last** definition in the class body. Proven, not assumed:

```
$ uv run python -c "import inspect; from mm_bridge.bridge import Bridge; \
    print(inspect.getsourcelines(Bridge._backend_for_channel)[1])"
4076
```

`config.py:47` → `default_backend: str = "claude"`.

So all three error-surfacing call sites — `bridge.py:2036` (create_run),
`bridge.py:2495` (catch-up), `bridge.py:4557` (`run.failed`) — silently inherited the
**resume** resolver's guess. The error path was written to say "the backend" when it
didn't know; the shadowing turned "I don't know" into a confident **"claude"**.

Each resolver's fallback is *correct for its own caller*: a Resume command must name
some backend to be runnable, so guessing the default is right there. An error message
must never invent a fact. One name, two incompatible contracts.

This is the same class of bug as PR #45 (`47d5c7c`, "live session meta wins over a
token-less Channel Purpose"): a reader that treats a cold cache as evidence.

### Why the Purpose cache is cold in the incident

`purpose_by_channel` is populated when this daemon handled an invite/fork/spawn.
Channels created by `mm-bridge spawn --backend pi`, or any channel after a daemon
restart, hit resolution step 2/3 — and a Purpose written without an explicit backend
token resolves to `default_backend`. The pi session's *own record* in agent-harness
knew the truth the whole time (`Session.backend`, `models.py:9`).

### Fix

Split the two contracts and delete the accidental shadowing:

- **`_backend_for_resume(channel_id)`** — the current `:4076` body verbatim, renamed.
  Keeps the `default_backend` fallback. Only caller: `_resume_meta_for`.
- **`_backend_for_error(session_id, channel_id)`** — async, truthful. Resolution order:
  1. live harness session meta (`harness.get_session(session_id)["backend"]`,
     `agent_harness_client.py:152`) — the source of truth, per PR #45's ruling;
  2. cached `PurposeConfig.backend`;
  3. re-parsed Mattermost Purpose;
  4. **`None`** — never `default_backend`.

  Lead condition: step 1 must be **best-effort and bounded**. Path A is often
  "harness unreachable", and the warning has to post promptly either way, so a raising
  `get_session` falls through to the Purpose backend (and to `None` → "the backend"
  when that is absent too).

Harness backend names are wire names (`claude-code`); normalise through
`purpose._BACKEND_ALIASES` (`purpose.py:23`) so the channel sees `claude`, not
`claude-code`. Reuse, do not re-implement. *(DRY)*

The two session bootstrap/restart sites (`bridge.py:1397`, `:1642`) already pass
`cfg.backend` — a real, known value. Unchanged.

## 2. Second gap: the classifier has nothing to classify

`run.failed` payloads, from `agent-harness` `orchestrator.py`:

| Line | Payload | When |
|------|---------|------|
| `:579` | `{"error", "error_type"}` | run process failed to **start** |
| `:603` | `{"error", "error_type"}` | process died **while active** |
| `:629` | `{"returncode"}` | CLI **exited non-zero on its own** |

No backend identity, and — for the incident shape — **no provider text**. A pi run that
hits OpenRouter 403 exits non-zero, so the bridge sees only
`"CLI exited with a non-zero status (1)."`. A classifier over that string alone
classifies the real incident as `unknown`. That is not good enough.

The provider's actual message goes to `process.stderr` events
(`orchestrator.py:814`, `{"text": ...}`). The bridge already **receives** them — it
streams `/v1/events` with only an `after` cursor, no server-side type filter
(`agent_harness_client.py:346`) — and then **drops** them at the dispatch `else:`
branch (`bridge.py:3845`, `logger.debug("Unhandled agent-harness event %s")`).

**Design S1 — bounded stderr tail.** Route `process.stderr` into a per-session ring
buffer (default: last 20 lines, hard-capped at 4 KiB, cleared on run terminal and on
session teardown). On `run.failed`, classify over `run_failure_detail(data)` **plus**
the buffered tail. Bridge-side only; no harness change (R2); no network round-trip in
the failure path (a quota failure often coincides with an unhealthy harness).

*Alternative considered and rejected:* pull `GET /v1/sessions/{id}/events` on failure.
Cleaner memory profile, but adds an HTTP call to the one path most likely to be
degraded, and needs a new client method. **Rejected** — S1 is strictly cheaper.

**N3 guard:** the tail feeds the *classifier* only. The channel renders the matched
class + provider + status code, never a raw stderr dump.

> **✅ Lead ruling (M0): S1 ACCEPTED** — "without it the feature ships its least
> valuable half; with it the bridge classifies over data it already receives." Three
> conditions, all implemented red-first:
>
> - **C1 — never quote the raw tail.** At most the single matched line, through
>   `condense_error_detail` *and* `redaction.redact_secrets` (`sk-…`, `Bearer …`,
>   `key=`/`token=` values, long hex/base64 runs). Redaction runs FIRST, on all text,
>   so no path reaches the channel unmasked.
> - **C2 — clear on `run.started` too**, not only on terminal + teardown. A run that
>   dies must never classify on the previous run's stderr.
> - **C3 — bounded and cheap.** 20 lines / 4 KiB per session, updated synchronously in
>   the SSE handler, stderr never logged above `debug`.
>
> The lead additionally ruled that the two **exception-shaped** `run.failed` variants
> (`orchestrator.py:579`, `:603`) carry their own truth — "the harness could not start
> or drive the CLI" — which today's wording buries. Hence `PATH_HARNESS_PROCESS`.

## 2b. Which stream carries a provider error (pi) — verified

The lead required this before S1 could be claimed to cover the two motivating
incidents: the harness publishes only **stderr** (`orchestrator.py:814`); stdout is an
unpublished heartbeat. If pi wrote provider errors to stdout, S1 would miss exactly the
failures it exists for.

**Result: pi writes provider errors to STDERR and exits 1.** Outcome (a) — S1 covers the
pi incidents as designed.

Reproduce it with no credentials and no external calls. A throwaway config dir
(`PI_CODING_AGENT_DIR`) registers a `faux` provider pointing at a local server that
returns the real OpenRouter 403 body:

```sh
# server: 403 + {"error":{"message":"Key limit exceeded (daily limit). …"}}
PI_OFFLINE=1 PI_CODING_AGENT_DIR=/tmp/pi-stream-test \
  pi -p -nt --no-session --offline \
     --provider faux --model faux/faux-model "hi" \
     >/tmp/out.txt 2>/tmp/err.txt </dev/null
```

Observed (pi 0.84.2):

| | |
|---|---|
| exit code | `1` → the harness emits `run.failed {"returncode": 1}` — the incident shape |
| stdout | **0 bytes** |
| stderr | `403: {"message":"Key limit exceeded (daily limit). Manage it using https://openrouter.ai/workspaces/default/keys/2c37…c4b6","code":403}` |

That stderr line is the lead's recorded incident string, character for character. The
`<status>: <body>` composition comes from pi's `normalizeProviderError`
(`@earendil-works/pi-ai/dist/utils/error-body.js`), which probes the SDK error object for
a status and a raw body precisely because provider bodies otherwise collapse to the
opaque `"403 status code (no body)"` form of `openai/core/error.js:makeMessage`.

**Two methodological notes**, because the first attempts were misleading:

1. `PI_OFFLINE=1` / `--offline` and a closed stdin are required. Without them pi hangs on
   a startup catalog fetch — the 60 s hang the lead hit, and which my first two runs
   reproduced. It is a startup artefact, not error-path behaviour.
2. A **control run was mandatory** to establish that. With `MODE=ok` the same invocation
   returns `exit 0`, stdout `pong`, and the local server logs one request. The first
   control failed with the server seeing *zero* requests, which is what exposed the
   startup hang; without it I would have wrongly concluded that pi swallows provider
   errors.

The degraded `403 status code (no body)` form is also reachable (when the body has no
`{"error": …}` envelope for the SDK to fold in). It classifies as `unknown` with the
status still extracted — an honest degradation, and covered by a test.

## 3. Classifier — data, not code (R4)

`backend_errors.py` gains one table. Each row carries the string that motivated it.
Order matters: the first matching row wins, so `quota_exhausted` is tested before
`rate_limited` before `auth` (a 403 daily-limit must not read as an auth failure).

| # | Class | Signatures (substring, case-folded) | Motivating string |
|---|-------|-------------------------------------|-------------------|
| 1 | `quota_exhausted` | `daily limit`, `monthly limit`, `quota exceeded`, `insufficient_quota`, `out of credits`, `credit balance` | **Incident 1 (verbatim)** — `403: {"message":"Key limit exceeded (daily limit). Manage it using https://openrouter.ai/…"}` |
| 2 | `rate_limited` | `usage limit`, `rate limit`, `too many requests`, `overloaded` | **Incident 2 (verbatim)** — `429: {"message":"you (<user>) have reached your session usage limit, upgrade … https://ollama.com/upgrade …","code":null}` |
| 3 | `auth` | `invalid api key`, `no auth credentials`, `unauthorized`, `authentication_error`, `401` | Rotated/absent key |
| 4 | `context_overflow` | `context length`, `maximum context`, `prompt is too long`, `too many tokens` | Long session death |
| 5 | `unknown` | — (fallback) | Everything else |

HTTP status is extracted separately. The real strings forced two corrections a
reconstruction would have missed:

- **The status arrives as a leading `403:` / `429:` prefix.** Incident 1 also carries
  `"code":403`, but incident 2 carries `"code":null` — the prefix is the only reliable
  source. Three anchored patterns cover pi's `<status>: <body>`, the SDK's
  `<status> <body>`, and the degraded `<status> status code (no body)`. Each requires a
  following `:`, `{`/`[` or the literal "status code", so a leading count
  ("500 tokens used") is still not read as a status — there is a test for both directions.
- **The provider is named only inside a URL** (`openrouter.ai`, `ollama.com`) in BOTH
  incidents, never as a bare word. Provider rows are therefore matched as plain
  substrings, which subsumes hostname matching; a word-boundary match would have found
  nothing.

Providers, one row each: `openrouter`, `ollama`, `anthropic`, `openai`, `google`
(`gemini` aliased onto it). No provider named → `None` → omitted from the message.

**Redaction decisions on the real strings** (lead ruling, M1): the 64-hex key id in
incident 1 IS masked; the ref UUID and the username in incident 2 are NOT — they are not
credentials, and a ref UUID is exactly what an operator would quote to provider support.
Both directions are pinned by tests so the outcome is a decision, not an accident of the
regexes.

```python
@dataclass(frozen=True)
class ProviderFailure:
    kind: str                 # one of the five classes
    provider: str | None
    status: int | None
    detail: str               # condensed, as today
```

`classify_failure(detail: str) -> ProviderFailure` — pure, no Bridge, no I/O (N1).

## 4. Retry truth, verified per path (R3, R5)

Both paths were read, not assumed.

### Path A — `create_run` raised (`bridge.py:2026`, `:2049`)

The message was **never accepted** by the harness. The bridge calls
`_enqueue_silent_drop(channel_id, thread_root, post)` (`:2262`), which retains the post
and replays it as a **catch-up block** on the *next* forwarded message
(`_peek_silent_drops_as_block`, `:2283`).

That is **not a retry**: nothing re-runs on its own, and the replay only happens when
the user next mentions the bot. It is also not a guarantee — `initial_catch_up_n <= 0`
disables the queue entirely, and `_forget_channel_silent_drops` (`:1209`, `:1528`,
`:1599`, `:3786`) wipes it on session recreate.

**Wording (true in every case):**

> ⚠️ Provider limit hit (OpenRouter: daily quota, HTTP 403) on the `pi` backend. Your
> message was **NOT processed** and won't retry by itself — repost it once the limit
> resets, or `.model` / `.backend` to switch.

### Path B — `run.failed` (`bridge.py:4536`)

The message **was** accepted; the run then died. Terminal in the harness
(`repository.py:433` → status `failed`); a grep of `orchestrator.py` / `api.py` finds
**no retry or requeue** anywhere — the only `retry` hit is a queue-full 429 *message*
(`api.py:324`). `_surface_run_failure` does **not** enqueue a silent drop, so the post
is gone.

**Wording:**

> ⚠️ Provider limit hit (OpenRouter: daily quota, HTTP 403) on the `pi` backend. Your
> message reached the model but the run died before finishing — anything already posted
> above is all there is. Nothing will retry it; repost once the limit resets, or
> `.model` / `.backend` to switch.

> **✅ Lead ruling (M0): ACCEPTED with one edit.** "Your message reached the model"
> was replaced with **"The run started but died before finishing"** — a run can die
> before any model call (auth at the first request), so the message must not claim
> what the bridge cannot know. "Anything already posted above is all there is" was
> kept, agreed as true regardless of output and needing no new per-run tracking.
> Path A keeps the brief's sentence verbatim, and the reading that the silent-drop
> replay is neither a retry nor guaranteed — so it must not be promised — was
> confirmed.

### Unknown class (R6)

Today's template and detail text, unchanged, **plus** the path's retry sentence.

## 5. State seam (F2 round)

One helper, one call site, guarded:

```python
def _note_run_failure(self, session_id: str, failure: ProviderFailure) -> None:
    """Publish a classified failure to round F2's per-session state API.

    WHY guarded: F2 (`feat/turn-state-fleet`) owns `set_session_state`; this
    branch must stand alone on `main`, where that API does not exist yet. When
    F2 lands the guard falls through and `.fleet` shows `blocked (quota)` with
    no further change here.
    """
    setter = getattr(self, "set_session_state", None)
    if setter is None:
        return
    ...
```

Called from `_surface_run_failure` only. Wiring completes when F2 lands; until then
it is a proven no-op (T10).

**Call shape**, pinned with F2 (Nuthatch) at M1 — no longer an assumption:

```python
set_session_state(session_id, "blocked", on=None,
                  note="<class> (<provider>, HTTP <status>)", source="bridge")
```

`on=None` (F2 owns the on/off lifecycle and creates the row if missing), and the note's
FIRST TOKEN is the class because F2 renders `blocked (<first token>)`. Class vocabulary:
`quota_exhausted | rate_limited | auth | context_overflow | harness_process | unknown`;
provider and status are omitted when unknown. `harness_process` is a *path* in this
design rather than a class, so it is used as the class token only when the text never
classified — a real class always wins over it. F2 guarantees the API never raises; the
raising-API test is kept anyway, since it pins *this* side of the seam.

## 6. Commit plan (M1)

1. `docs(spec)` — this directory.
2. `test` — red: the misattribution reproduction (T1).
3. `fix(bridge)` — split `_backend_for_error` / `_backend_for_resume` (T2, T3).
4. `feat(backend_errors)` — classifier table + `ProviderFailure` (T4–T6).
5. `feat(bridge)` — the guarded state seam (T10).
6. `docs` — README.

As shipped, commits 3–4 merged: deleting the shadowed method and rewiring its three
callers is one atomic change, and the stderr routing rides with it because the same
`run.failed` site both resolves the backend and reads the tail. The state seam and the
two pure modules (`redaction`, `stderr_tail`) are separate commits.
