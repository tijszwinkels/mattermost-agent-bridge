"""A small bounded per-session buffer of recent CLI stderr lines.

WHY the bridge keeps this at all: the harness reports the commonest backend
failure as ``run.failed {"returncode": N}`` (agent-harness
``orchestrator.py:629``) — no provider, no status, no message. The text that
says *why* ("403 … daily limit") goes out on ``process.stderr`` events, which
the bridge already receives over ``/v1/events`` and used to drop. Buffering the
last few lines is what lets :func:`mm_bridge.backend_errors.classify_failure`
tell a quota wall from a crash.

WHY it is aggressively bounded: this is diagnostic breadcrumbs, not a
transcript. One entry per session with a live run, capped in both lines and
bytes, cleared at the start of every run (so a failure can never be explained
by the *previous* run's stderr) and again when the run ends.
"""
from __future__ import annotations

from collections import deque

DEFAULT_MAX_LINES = 20
DEFAULT_MAX_BYTES = 4096
DEFAULT_MAX_LINE_LEN = 500


class SessionStderrTails:
    """Per-session ring of recent stderr lines, bounded in lines and bytes."""

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_line_len: int = DEFAULT_MAX_LINE_LEN,
    ) -> None:
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._max_line_len = max_line_len
        self._tails: dict[str, deque[str]] = {}

    def append(self, session_id: str, text: str) -> None:
        """Add ``text`` (which may be a multi-line chunk) to the session's tail.

        Blank lines are dropped — they carry no classification signal and would
        evict lines that do. An over-long single line is TRUNCATED rather than
        discarded: a provider error is often one very long JSON line, and its
        first few hundred characters carry the status and the wording.
        """
        lines = [ln.strip() for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if ln]
        if not lines:
            return
        tail = self._tails.get(session_id)
        if tail is None:
            tail = deque(maxlen=self._max_lines)
            self._tails[session_id] = tail
        for line in lines:
            tail.append(line[: self._max_line_len])
        self._trim_bytes(tail)

    def _trim_bytes(self, tail: deque[str]) -> None:
        """Evict oldest lines until the tail fits the byte cap.

        Always keeps at least one line: a single line over the cap has already
        been length-truncated, and an empty tail is strictly less useful than a
        slightly oversized one.
        """
        while len(tail) > 1 and sum(len(ln.encode()) for ln in tail) > self._max_bytes:
            tail.popleft()

    def lines(self, session_id: str) -> tuple[str, ...]:
        """Buffered lines, oldest first. Unknown session → empty."""
        return tuple(self._tails.get(session_id, ()))

    def clear(self, session_id: str) -> None:
        """Forget the session's tail. A no-op for a session never seen."""
        self._tails.pop(session_id, None)
