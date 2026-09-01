"""Mask credentials in text that is about to be shown in a channel.

WHY this exists as its own module: round F3 lets the bridge quote a line of
CLI **stderr** into Mattermost so a provider failure can be named ("daily
quota, HTTP 403") instead of guessed. Stderr from coding CLIs routinely echoes
request URLs, ``Authorization`` headers and key prefixes, and a real key has
leaked through a bridge session once already. The quoting is only safe because
this redaction is unconditional, so it lives apart from the message templates
and is tested on its own.

Rules are ordered most-specific-first, and every rule keeps the *shape* of the
text intact — status codes, provider names and limit wording must survive,
because :mod:`mm_bridge.backend_errors` classifies on exactly those tokens
AFTER redaction runs.
"""
from __future__ import annotations

import re

PLACEHOLDER = "[redacted]"

# Each rule is (compiled pattern, replacement). ``\1`` back-references keep the
# key NAME visible ("api_key=[redacted]") so the reader still learns which
# credential the CLI was using — just not its value.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Provider key prefixes: sk-, sk-ant-…, sk-or-v1-… . Matched before the
    # Bearer rule so a key inside an Authorization header is masked by value.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), PLACEHOLDER),
    # Bearer / Basic tokens in an echoed header.
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 " + PLACEHOLDER),
    # key=VALUE / token: VALUE, in query strings, env dumps and JSON alike.
    # ``key`` requires a following ``:``/``=``, so ordinary prose such as
    # "Key limit exceeded" is left alone.
    (
        re.compile(
            r"\b(api[_-]?key|apikey|key|token|secret|password|passwd)\b"
            r"(\s*[:=]\s*)[\"']?[A-Za-z0-9._~+/=-]{8,}[\"']?",
            re.I,
        ),
        r"\1\2" + PLACEHOLDER,
    ),
    # Bare high-entropy runs: session keys, hex digests, base64 blobs. Long
    # enough that ordinary words and numbers can't trip them.
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), PLACEHOLDER),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), PLACEHOLDER),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    Safe on empty input. Idempotent: the placeholder contains characters no
    rule matches, so re-running never compounds.
    """
    if not text:
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
