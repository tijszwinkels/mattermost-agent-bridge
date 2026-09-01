"""Shared template + helpers for surfacing backend-invocation failures.

When a backend/harness interaction fails (harness unreachable, session create
fails, the CLI errors on boot, a run fails), the bridge logs the full error at
ERROR level AND posts a concise, human-facing message into the channel/thread
the user is looking at. These pure helpers shape that message so every failure
path reads the same: lead with *what was attempted*, name the backend, then the
trimmed error. The raw error/traceback stays in the log; the channel sees only
the meaningful line(s).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .redaction import redact_secrets

_ERROR_DETAIL_MAX_LEN = 500


def condense_error_detail(raw: str, *, max_len: int = _ERROR_DETAIL_MAX_LEN) -> str:
    """Reduce a raw error string to the human-relevant line(s).

    A multi-line blob (e.g. a traceback that leaked through) collapses to its
    final, non-blank line — for exceptions that's the actual cause. Over-long
    detail is truncated with an ellipsis. Empty input yields a placeholder so
    the fenced block is never blank. The full error is preserved in the log by
    the caller; this only shapes what the channel sees.
    """
    text = (raw or "").strip()
    if not text:
        return "(no detail reported)"
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        text = lines[-1].strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def exception_detail(exc: BaseException) -> str:
    """Human-relevant detail for a caught exception.

    ``str(exc)`` is preferred (for httpx status errors it's already a clean,
    single line like ``agent-harness POST /v1/sessions -> 500: …``); a
    message-less exception falls back to its type name so the block is never
    empty.
    """
    msg = str(exc).strip()
    return msg or type(exc).__name__


def run_failure_detail(data: dict) -> str:
    """Human-relevant detail from a ``run.failed`` SSE payload.

    The harness emits one of two shapes (agent-harness ``orchestrator``):
    ``{error, error_type}`` when the run process couldn't start or crashed
    mid-run (e.g. the CLI binary wasn't found), or ``{returncode}`` when the
    CLI exited non-zero on its own. Falls back to a generic note if neither is
    present.
    """
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        etype = data.get("error_type")
        return f"{etype}: {error.strip()}" if etype else error.strip()
    returncode = data.get("returncode")
    if returncode is not None:
        return f"CLI exited with a non-zero status ({returncode})."
    return "The run failed before producing a reply."


def backend_phrase(backend: str | None) -> str:
    """Name the backend, or stay honestly vague.

    There is no third option: guessing a default here is what made two real
    incidents blame `claude` for a `pi` session (see
    ``specs/20260901-truthful-provider-errors/``).
    """
    return f"the `{backend}` backend" if backend else "the backend"


def format_backend_error(
    action: str,
    backend: str | None,
    detail: str,
    *,
    retry_truth: str | None = None,
) -> str:
    """Build the channel-facing backend-error message.

    ``action`` is the attempted operation phrased as an infinitive
    ("start a session", "run your message", "fork this thread"). ``backend``
    is named when known. ``detail`` is passed through :func:`condense_error_detail`.

    ``retry_truth`` states whether the message was processed and whether
    anything will retry it. It defaults to ``None`` — omitted — so the two
    session bootstrap/restart callers keep today's exact wording; the delivery
    and run-failure callers always pass one (see :func:`format_provider_failure`).
    """
    detail = condense_error_detail(detail)
    truth_line = f"{retry_truth}\n" if retry_truth else ""
    return (
        f":warning: I tried to {action} with {backend_phrase(backend)} and got this error:\n"
        f"```\n{detail}\n```\n"
        f"{truth_line}"
        "The full error is in the bridge log. "
        "`mm-bridge doctor` on the host can diagnose config/connectivity issues."
    )


# ─────────────────── Provider-failure classification (F3) ──────────────────
#
# Two recorded incidents (Meshenger V2, 2026-08-31 and 2026-09-01) surfaced a
# provider quota wall as an anonymous "CLI exited with a non-zero status (1)".
# The operator read the resulting silence as "busy" and a lane starved for
# hours. Classification exists so that a wall the operator can ACT on never
# again looks like a bug they have to debug.
#
# Ruling R4: the classifier is DATA. One ordered table, one row per signature,
# each row carrying the string that motivated it. Adding a provider or a
# signature is one row here and one test in `tests/test_failure_classifier.py`.

QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
AUTH = "auth"
CONTEXT_OVERFLOW = "context_overflow"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Signature:
    kind: str
    needle: str   # case-folded substring
    label: str    # short phrase for the message's parenthetical
    seen_in: str  # the real-world string this row exists for


# ORDER IS LOAD-BEARING: first match wins, and quota is tested before auth so
# that a 403 carrying "daily limit" reads as a wall to wait out rather than a
# broken key to go hunting for.
_SIGNATURES: tuple[_Signature, ...] = (
    _Signature(QUOTA_EXHAUSTED, "daily limit", "daily quota",
               "incident 2026-08-31: openrouter 403 'Key limit exceeded: daily limit'"),
    _Signature(QUOTA_EXHAUSTED, "monthly limit", "monthly quota",
               "openrouter/anthropic monthly caps, same 403 shape as the daily one"),
    _Signature(QUOTA_EXHAUSTED, "quota exceeded", "quota exceeded",
               "google: 'Quota exceeded for quota metric …'"),
    _Signature(QUOTA_EXHAUSTED, "insufficient_quota", "quota exhausted",
               "openai: {'code': 'insufficient_quota'}"),
    _Signature(QUOTA_EXHAUSTED, "out of credits", "out of credits",
               "openrouter prepaid balance hitting zero"),
    _Signature(QUOTA_EXHAUSTED, "credit balance", "credit balance too low",
               "anthropic: 'Your credit balance is too low to access the API'"),
    _Signature(RATE_LIMITED, "usage limit", "usage limit",
               "incident 2026-09-01: ollama-cloud 429 'session usage limit'"),
    _Signature(RATE_LIMITED, "rate limit", "rate limit",
               "every provider's 429 body"),
    _Signature(RATE_LIMITED, "too many requests", "too many requests",
               "bare HTTP 429 reason phrase, body-less proxies"),
    _Signature(RATE_LIMITED, "overloaded", "provider overloaded",
               "anthropic: {'type': 'overloaded_error'}"),
    _Signature(AUTH, "invalid api key", "invalid key",
               "openai/openrouter 401 after a key rotation"),
    _Signature(AUTH, "no auth credentials", "missing credentials",
               "openrouter 401 when the env var never made it to the CLI"),
    _Signature(AUTH, "authentication_error", "authentication failed",
               "anthropic: {'type': 'authentication_error'}"),
    _Signature(AUTH, "unauthorized", "unauthorized",
               "bare HTTP 401 reason phrase"),
    _Signature(CONTEXT_OVERFLOW, "context length", "context length",
               "openai: \"This model's maximum context length is …\""),
    _Signature(CONTEXT_OVERFLOW, "maximum context", "context length",
               "same family, wording varies by SDK version"),
    _Signature(CONTEXT_OVERFLOW, "prompt is too long", "prompt too long",
               "anthropic: 'prompt is too long: N tokens > M maximum'"),
    _Signature(CONTEXT_OVERFLOW, "too many tokens", "too many tokens",
               "google/vertex phrasing of the same wall"),
)

# Provider tokens → (canonical name, display name). ``openrouter`` precedes
# ``openai`` defensively: the names overlap in the eye if not in substring
# terms, and a future alias could make the ordering matter for real.
_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("openrouter", "openrouter", "OpenRouter"),
    ("ollama", "ollama", "ollama"),
    ("anthropic", "anthropic", "Anthropic"),
    ("openai", "openai", "OpenAI"),
    ("gemini", "google", "Google"),
    ("google", "google", "Google"),
)

_PROVIDER_DISPLAY: dict[str, str] = {name: display for _, name, display in _PROVIDERS}

# HTTP status extraction. Deliberately NOT a bare `\b[45]\d\d\b` scan: "used
# 500 tokens" must never become HTTP 500. Every pattern requires a marker that
# only appears around a real status code — including this repo's own httpx
# detail shape ("… /v1/sessions -> 500: …").
_STATUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bHTTP[ /]([45]\d\d)\b", re.I),
    re.compile(r"\bstatus(?:_code)?[\"' ]*[:=][\"' ]*([45]\d\d)\b", re.I),
    re.compile(r"[\"']code[\"']\s*:\s*([45]\d\d)\b"),
    re.compile(
        r"\b([45]\d\d)\s+(?:Forbidden|Unauthorized|Too Many Requests|Bad Request"
        r"|Not Found|Internal Server Error|Service Unavailable|Bad Gateway"
        r"|Gateway Timeout)\b",
        re.I,
    ),
    re.compile(r"->\s*([45]\d\d)\b"),
    re.compile(r"\bError:\s*([45]\d\d)\b", re.I),
)


@dataclass(frozen=True)
class ProviderFailure:
    """What we can honestly say about a failed backend interaction.

    ``detail`` is always safe to show (redacted + condensed). ``evidence`` is
    the single stderr line that supplied the classification, and is set ONLY
    when the failure payload itself said nothing useful — so the channel never
    carries stderr it didn't need.
    """

    kind: str
    provider: str | None
    status: int | None
    detail: str
    evidence: str | None = None
    # Short phrase from the matched signature row ("daily quota"), used in the
    # message's parenthetical. ``None`` for the `unknown` class.
    label: str | None = None

    @property
    def provider_display(self) -> str | None:
        return _PROVIDER_DISPLAY.get(self.provider or "")


def _match_signature(text: str) -> _Signature | None:
    folded = text.casefold()
    for sig in _SIGNATURES:
        if sig.needle in folded:
            return sig
    return None


def _extract_provider(text: str) -> str | None:
    folded = text.casefold()
    for token, canonical, _display in _PROVIDERS:
        if token in folded:
            return canonical
    return None


def _extract_status(text: str) -> int | None:
    for pattern in _STATUS_PATTERNS:
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    return None


def classify_failure(
    detail: str, *, stderr_tail: Sequence[str] = (),
) -> ProviderFailure:
    """Classify a backend failure from its detail text and recent stderr.

    Redaction runs FIRST, on everything, so there is no path where classified
    text reaches the channel unmasked (lead condition C1). The signatures are
    chosen to survive it — status codes and limit wording are not
    credential-shaped.

    ``stderr_tail`` is consulted only when the payload itself doesn't classify:
    the harness reports the commonest provider failure as a bare returncode
    (agent-harness ``orchestrator.py:629``), and the tail is the only place the
    provider's own words appear. It is scanned NEWEST-FIRST — the line that
    killed the run is the last one written, not the first.
    """
    safe_detail = condense_error_detail(redact_secrets(detail))

    hit = _match_signature(safe_detail)
    evidence: str | None = None
    if hit is None:
        for line in reversed(list(stderr_tail)):
            safe_line = condense_error_detail(redact_secrets(line))
            found = _match_signature(safe_line)
            if found is not None:
                hit, evidence = found, safe_line
                break

    # Provider and status are read from whatever text we actually have, even
    # when the class is `unknown` — naming the provider is useful on its own.
    haystack = safe_detail if evidence is None else f"{safe_detail}\n{evidence}"
    return ProviderFailure(
        kind=hit.kind if hit else UNKNOWN,
        provider=_extract_provider(haystack),
        status=_extract_status(haystack),
        detail=safe_detail,
        evidence=evidence,
        label=hit.label if hit else None,
    )

# ────────────────── The retry truth, per failure path (R3/R5) ──────────────
#
# The single most expensive omission in both incidents was that nothing said
# the message would never be retried. These templates make that structural:
# every path has an entry, so a new failure site cannot ship without stating
# its truth. All three were read out of the code before being written down —
# see `specs/20260901-truthful-provider-errors/design.md` § Retry truth.

# `create_run` raised: the harness never accepted the message. The bridge does
# retain the post and replays it as catch-up on the NEXT forwarded message
# (`_enqueue_silent_drop`), but that is neither automatic nor guaranteed
# (`initial_catch_up_n <= 0` disables it; a session recreate wipes it), so we
# promise nothing about it.
PATH_NOT_ACCEPTED = "not_accepted"
# `run.failed` with a returncode: the CLI accepted the turn and exited non-zero.
# "started but died" rather than "reached the model" — a run can die before any
# model call at all (auth on the first request), and the bridge cannot tell.
PATH_CLI_EXIT = "cli_exit"
# `run.failed` with an exception (orchestrator.py:579/:603): the harness could
# not start, or could not keep driving, the CLI process. A different truth from
# a clean non-zero exit, and today's wording buries it.
PATH_HARNESS_PROCESS = "harness_process"

_RETRY_TRUTH: dict[str, str] = {
    PATH_NOT_ACCEPTED:
        "Your message was NOT processed and won't retry by itself — {remedy}",
    PATH_CLI_EXIT:
        "The run started but died before finishing — anything already posted "
        "above is all there is. Nothing will retry it; {remedy}",
    PATH_HARNESS_PROCESS:
        "The harness couldn't start or keep the CLI running — anything already "
        "posted above is all there is. Nothing will retry it; {remedy}",
}

# What the operator can actually DO, by failure class. This is the half that
# turns "quota-starved" back into something distinguishable from "busy".
_REMEDY: dict[str, str] = {
    QUOTA_EXHAUSTED: "repost it once the limit resets, or `.model` / `.backend` to switch.",
    RATE_LIMITED: "repost it in a moment, or `.model` / `.backend` to switch.",
    AUTH: "fix the provider credentials on the host, then repost.",
    CONTEXT_OVERFLOW: "start a fresh session (or trim the conversation), then repost.",
    UNKNOWN: "repost it once you've had a look.",
}

_HEADLINE: dict[str, str] = {
    QUOTA_EXHAUSTED: "Provider limit hit",
    RATE_LIMITED: "Provider rate limit hit",
    AUTH: "Provider auth failed",
    CONTEXT_OVERFLOW: "Context window exceeded",
}


def retry_truth(path: str, kind: str) -> str:
    """The "was it processed / will it retry" sentence for a failure path."""
    template = _RETRY_TRUTH.get(path, _RETRY_TRUTH[PATH_NOT_ACCEPTED])
    return template.format(remedy=_REMEDY.get(kind, _REMEDY[UNKNOWN]))


def _qualifier(failure: ProviderFailure) -> str:
    """The parenthetical: "OpenRouter: daily quota, HTTP 403"."""
    inner = failure.label or "unclassified"
    if failure.status is not None:
        inner = f"{inner}, HTTP {failure.status}"
    display = failure.provider_display
    return f"{display}: {inner}" if display else inner


def format_provider_failure(
    *,
    action: str,
    backend: str | None,
    failure: ProviderFailure,
    path: str,
) -> str:
    """Build the channel message for a classified backend failure.

    A recognised class leads with what went wrong and what to do about it. An
    UNRECOGNISED one keeps today's message verbatim (ruling R6) and merely
    gains the retry sentence — we don't dress up a failure we can't name.
    """
    truth = retry_truth(path, failure.kind)

    if failure.kind == UNKNOWN:
        return format_backend_error(action, backend, failure.detail, retry_truth=truth)

    message = (
        f":warning: {_HEADLINE[failure.kind]} ({_qualifier(failure)}) on "
        f"{backend_phrase(backend)}. {truth}"
    )
    if failure.evidence:
        # At most ONE stderr line, already redacted and condensed (C1). It is
        # here because the payload itself said nothing — without it the reader
        # has only our classification and no way to check our work.
        message += f"\n```\n{failure.evidence}\n```"
    return message


def run_failure_path(data: dict) -> str:
    """Which truth a ``run.failed`` payload calls for.

    The harness distinguishes the two by shape, not by a field: an exception
    while starting or driving the process carries ``error``/``error_type``
    (orchestrator.py:579, :603); a clean non-zero exit carries ``returncode``
    (:629).
    """
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        return PATH_HARNESS_PROCESS
    return PATH_CLI_EXIT
