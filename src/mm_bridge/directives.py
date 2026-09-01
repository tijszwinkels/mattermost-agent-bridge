"""Parse assistant output for mm-bridge XML directives.

Supported directives:
    <openFile path="..." [line="..."] [follow="..."] />
    <leaveChannel [reason="..."] />
    <state kind="..." [on="..."] [note="..."] />

The parser is markdown-fence-aware: directives appearing inside
triple-backtick fenced blocks or inline backtick code spans are treated
as documentation and left intact in the visible text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class Directive:
    kind: Literal["open_file", "leave_channel", "state"]
    attrs: dict[str, str]


_OPEN_FILE_RE = re.compile(r"<openFile\s+([^>]*)/>", re.IGNORECASE)
_LEAVE_CHANNEL_RE = re.compile(
    r"<leaveChannel(?:\s+([^>]*?))?\s*/>", re.IGNORECASE
)
# Turn-end state. Attributes are validated by the caller, not here: an
# unknown `kind` must still be STRIPPED from the visible post (and answered
# with a notice), which means the parser has to match it first.
_STATE_RE = re.compile(r"<state(?:\s+([^>]*?))?\s*/>", re.IGNORECASE)

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Collapse 3+ consecutive newlines (possibly with blank whitespace) to two.
_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")

# Triple-backtick fenced block (with optional language tag on the open line).
# Non-greedy body so adjacent fences don't merge.
_FENCE_RE = re.compile(r"```[^\n]*\n.*?\n```", re.DOTALL)

# Inline single-backtick code span — must not span lines or contain backticks.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Sentinel char used to mask code regions so the directive regex can't match
# inside them. NUL never appears in normal markdown and is not `<`/`>`.
_MASK_CHAR = "\x00"


def _mask_code_regions(text: str) -> str:
    """Return ``text`` with fenced blocks and inline code spans replaced by
    same-length runs of NUL, so directive regexes won't fire inside them
    while character offsets are preserved for slicing the original text.
    """

    def _blank(match: re.Match[str]) -> str:
        return _MASK_CHAR * (match.end() - match.start())

    masked = _FENCE_RE.sub(_blank, text)
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    return masked


def _parse_attrs(inner: str | None) -> dict[str, str]:
    if not inner:
        return {}
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(inner)}


def extract(text: str) -> tuple[str, list[Directive]]:
    """Extract directives from ``text`` and return (cleaned_text, directives).

    Directives are stripped (replaced with ""). Order in the returned list
    reflects textual order. Runs of blank lines introduced by stripping are
    collapsed to a single blank line. Other whitespace around directives is
    preserved so the caller can decide how to present surrounding prose.
    """

    matches: list[tuple[int, int, Directive]] = []
    masked = _mask_code_regions(text)

    for m in _OPEN_FILE_RE.finditer(masked):
        matches.append(
            (m.start(), m.end(), Directive("open_file", _parse_attrs(m.group(1))))
        )

    for m in _LEAVE_CHANNEL_RE.finditer(masked):
        matches.append(
            (m.start(), m.end(), Directive("leave_channel", _parse_attrs(m.group(1))))
        )

    for m in _STATE_RE.finditer(masked):
        matches.append(
            (m.start(), m.end(), Directive("state", _parse_attrs(m.group(1))))
        )

    if not matches:
        return text, []

    matches.sort(key=lambda t: t[0])

    out: list[str] = []
    cursor = 0
    directives: list[Directive] = []
    for start, end, directive in matches:
        out.append(text[cursor:start])
        directives.append(directive)
        cursor = end
    out.append(text[cursor:])
    cleaned = "".join(out)

    # Collapse runs of blank lines that can appear when a directive occupied
    # its own line (e.g. ``"foo\n<openFile .../>\nbar"`` → ``"foo\n\nbar"``
    # stays fine, but three+ newlines get squashed back to a single blank).
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)

    return cleaned, directives
