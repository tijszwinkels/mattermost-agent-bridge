"""Surfacing a harness 409 on ``POST /v1/runs``.

The harness refuses a run it cannot honour and says why in the response
``detail`` — e.g. an external pi session whose transcript is gone, where
resuming would silently open a *new* conversation. The channel has to see
that reason: "can't resume this session" alone leaves the operator with
no idea whether to retry, fix the cwd, or give up.

Spec: specs/20260914-external-pi-sessions/design.md
"""

from mm_bridge.backend_errors import format_resume_refusal


def test_refusal_carries_the_harness_reason() -> None:
    message = format_resume_refusal(
        "Cannot resume external pi session ses_abc: no pi transcript for "
        "e5a93149-9a70-4aef-a189-2681b4e08525 under /home/me/project — "
        "resuming would silently start a new conversation"
    )

    assert "silently start a new conversation" in message
    assert "/home/me/project" in message
    assert message.startswith(":warning:")


def test_refusal_says_the_message_was_not_delivered() -> None:
    """No silent loss: the operator must know their post did not run."""
    message = format_resume_refusal("nope")

    assert "not" in message.lower()
    assert "nope" in message


def test_refusal_without_detail_still_reads_as_a_refusal() -> None:
    message = format_resume_refusal("")

    assert message.startswith(":warning:")
    assert "(no detail reported)" in message


def test_refusal_redacts_secrets_in_the_detail() -> None:
    message = format_resume_refusal("bad token sk-ant-api03-SUPERSECRETVALUE1234567890")

    assert "SUPERSECRETVALUE1234567890" not in message
