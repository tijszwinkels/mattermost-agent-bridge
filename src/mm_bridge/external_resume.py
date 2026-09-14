"""Explain external-session continuation and select the bridge's routing.

pi resumes the observed file through ``pi -p --session <path>``. A live
TUI retains its own context, so concurrent continuation can create sibling
branches. The notice explains how to switch safely between clients.
Other backends retain the bridge's existing replacement behavior and copy.

Verified against pi v0.84.2 on 2026-09-14; wording and routing are covered
by ``tests/test_external_resume_notice.py``.

Spec: ``specs/20260914-external-pi-sessions/design.md``
"""

from __future__ import annotations

from . import purpose

_COMMON_TAIL = "It is **not a remote control** of the live terminal session."

# Inherited wording, deliberately unchanged. The bridge's handling of
# external claude/codex channels is untouched by this change — the first
# post still swaps in a fresh harness session — so this copy is carried
# over as-is rather than restated more strongly. Verifying and rewording
# it is separate work.
_FORK_NOTICE = (
    "_Heads-up: posting here resumes this session via `--resume`. If it's "
    "still open in a terminal, this channel becomes a **fork** of the "
    f"conversation. {_COMMON_TAIL}_"
)

# The pi wording is load-bearing, so it states the procedure rather than
# just the hazard. "Turns can interleave" was the earlier phrasing and it
# is wrong in a way that reads as reassuring: the two sides do not merge
# into one thread, they SILENTLY DIVERGE, and reopening picks one branch
# and drops the other's turns from the model's context entirely.
_PI_NOTICE = (
    "_Heads-up: posting here continues this pi session **headlessly** "
    "(`pi -p`), appending to the **same transcript** the terminal created "
    f"— it is not a fork. {_COMMON_TAIL}_\n"
    ":warning: **Close the pi/Herdr session in your terminal before "
    "posting here, and reload it before you go back to it.** A terminal "
    "that stays open keeps its own in-memory copy of the conversation: "
    "your turns here and its turns become **sibling branches** of the same "
    "parent, and whoever reopens the file next sees only the branch that "
    "was appended last — the other side's turns are silently missing from "
    "the model's context."
)

_GENERIC_NOTICE = (
    "_Heads-up: posting here asks the harness to continue this session "
    f"from its transcript. {_COMMON_TAIL}_"
)

# Backends that keep the inherited fork wording. Keyed by the canonical
# short names ``purpose.canonical_backend`` produces.
_FORKING_BACKENDS: frozenset[str] = frozenset({"claude", "codex"})

# Backends where posting into the channel CONTINUES the observed
# conversation rather than starting a replacement one.
#
# This is the bridge's oldest assumption about external sessions turned
# into a question. Historically every ``origin: external`` session was
# tagged "cannot receive posts", and the first post swapped in a fresh
# harness session — losing the conversation, which is precisely the thing
# a user asking to continue their terminal session wants kept. pi is the
# backend where the harness can genuinely append to the observed
# transcript (``pi -p --session <path>``), so it opts out of that swap.
# claude and codex keep the replacement behaviour they have always had.
_RESUMABLE_BACKENDS: frozenset[str] = frozenset({"pi"})


def external_resume_notice(backend: str | None) -> str:
    """The heads-up to post when a channel is opened onto an external session.

    Unknown/absent backends get the generic wording rather than a guess:
    claiming "fork" or "headless" about a backend we have not verified is
    exactly the failure this module exists to prevent.
    """
    canonical = purpose.canonical_backend(backend)
    if canonical in _FORKING_BACKENDS:
        return _FORK_NOTICE
    if canonical == "pi":
        return _PI_NOTICE
    return _GENERIC_NOTICE


def supports_external_resume(backend: str | None) -> bool:
    """Whether a post into this backend's external channel continues the
    observed conversation instead of replacing it with a fresh session."""
    return purpose.canonical_backend(backend) in _RESUMABLE_BACKENDS


__all__ = ["external_resume_notice", "supports_external_resume"]
