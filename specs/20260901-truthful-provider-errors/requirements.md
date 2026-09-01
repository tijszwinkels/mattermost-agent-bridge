# Truthful provider errors — Requirements

Numbered requirements. Each traces to a lead ruling (R1–R8) where one applies.

## Functional

**F1 — Correct backend attribution.** *(R2)*
A surfaced failure names the backend that actually ran the session. When the backend
cannot be established from evidence, the message says "the backend" — it MUST NOT
fall back to `config.default_backend`.

**F2 — Failure classification.** *(R4)*
A pure classifier maps failure detail text to exactly one class:
`quota_exhausted`, `rate_limited`, `auth`, `context_overflow`, `unknown`.
Signatures live in **one** table. Adding a provider or signature is one row + one test.

**F3 — Provider extraction.** *(R4)*
When the detail names a provider (`openrouter`, `ollama`, `anthropic`, `openai`,
`google`), the classifier reports it. Absent evidence, the provider is `None` and the
message omits it rather than guessing.

**F4 — The retry sentence.** *(R3)*
EVERY surfaced failure message states (a) whether the message was processed and
(b) whether anything will retry it. No message may imply a retry that does not happen.
The statement must be verified against the code path that produced it — see
`design.md` § Retry truth.

**F5 — Two failure sites, two truths.** *(R5)*
`create_run` failures (message never accepted) and `run.failed` (accepted, then died)
get different wording. Both are classified.

**F6 — Unknown class keeps today's behaviour.** *(R6)*
An unclassifiable failure renders today's detail text, plus the F4 retry sentence.
Nothing else changes.

**F7 — No behaviour change for successful runs.** *(R6)*
`run.completed` / `run.interrupted` / watchdog notices are untouched.

**F8 — State seam.** *(F2 round)*
Exactly ONE call site notifies a per-session state API of a classified failure,
guarded by feature presence so this branch stands alone on `main`.

## Non-functional

**N1 — Pure functions.** Classification and message construction are side-effect-free
and unit-testable without a Bridge instance.

**N2 — Backward compatible.** `tests/test_backend_error_format.py` and
`tests/test_backend_error_surfacing.py` keep passing, or are updated with a stated
reason in the commit message. Never deleted. *(R6)*

**N3 — No secret leakage.** Classification may read process stderr; the channel sees
the *classification*, never a raw stderr dump. Provider error bodies can carry key
prefixes and org ids.

**N4 — Bounded memory.** Any per-session buffer introduced for classification is
bounded in both entries and bytes, and is cleared on run end and session teardown.

**N5 — Minimal diff at the F1-round seam.** *(R1)* Failure-surfacing paths are touched
as little as possible so PR #51 merges trivially in either order.

## Test requirements *(R7 — TDD, watched red first)*

| # | Test |
|---|------|
| T1 | Reproduction: a `pi` session with a cold/token-less Purpose cache surfaces `claude`. **Red first.** |
| T2 | After the fix, the same scenario surfaces `pi`. |
| T3 | Unknown backend renders "the backend", not `default_backend`. |
| T4 | Each classifier signature — one test per row, using the row's motivating string. |
| T5 | The two real incident strings classify as `quota_exhausted`/OpenRouter and `rate_limited`/ollama. |
| T6 | Provider extraction, including "no provider named → `None`". |
| T7 | `create_run` failure wording states not-processed + no-auto-retry. |
| T8 | `run.failed` wording states accepted-then-died + no-retry. |
| T9 | Unknown class keeps today's detail text and still carries the retry sentence. |
| T10 | The state seam is a no-op when F2's API is absent. |
| T11 | Full suite green. Baseline at `3e28e53`: **1022 passed, 1 skipped, 42 subtests**. |
