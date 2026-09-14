"""The heads-up posted into a channel opened onto an external session.

An external session is one a human started in a terminal. Posting into
its Mattermost channel does NOT drive that terminal — what actually
happens differs per backend, and saying the wrong thing is how an
operator ends up believing they are remote-controlling a live TUI.

Spec: specs/20260914-external-pi-sessions/design.md
"""

import pytest

from mm_bridge.external_resume import external_resume_notice


@pytest.mark.parametrize("backend", ["claude", "claude-code", "Claude Code", "codex"])
def test_fork_backends_say_fork(backend) -> None:
    notice = external_resume_notice(backend)

    assert "fork" in notice.lower()
    assert "not a remote control" in notice.lower()


def test_pi_says_headless_continuation_not_a_fork() -> None:
    """pi appends to the operator's own transcript rather than branching
    off it, so calling it a fork would be the wrong warning entirely."""
    notice = external_resume_notice("pi")

    assert "headless" in notice.lower()
    assert "same transcript" in notice.lower()
    # Explicitly denies the fork framing rather than merely omitting it —
    # "fork" is what an operator who has used the claude/codex channels
    # would otherwise assume.
    assert "is not a fork" in notice.lower()
    assert "becomes a **fork**" not in notice.lower()


def test_pi_notice_gives_the_close_and_reload_procedure() -> None:
    """An open terminal keeps its own in-memory copy of the conversation:
    its turns and the bridge's become sibling branches of the same parent,
    and reopening shows only whichever branch was appended last. That is
    silent context loss, so the notice has to state the procedure — not
    merely that something "may" go wrong.
    """
    notice = external_resume_notice("pi").lower()

    assert "close" in notice and "before posting here" in notice
    assert "reload" in notice
    assert "sibling branches" in notice
    # The earlier wording. It reads as reassuring and is wrong: the two
    # sides do not merge into one thread, they diverge.
    assert "interleave" not in notice
    # The concrete risk the operator needs to know about.
    assert "terminal" in notice.lower()


def test_unknown_backend_still_warns_without_claiming_mechanics() -> None:
    notice = external_resume_notice("herdr")

    assert notice
    assert "not a remote control" in notice.lower()
    assert "fork" not in notice.lower()
    assert "headless" not in notice.lower()


def test_missing_backend_is_tolerated() -> None:
    assert external_resume_notice(None)
    assert external_resume_notice("")
