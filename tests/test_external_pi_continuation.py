"""Posting into an external pi channel must CONTINUE that conversation.

The bridge's long-standing rule was that every ``origin: external``
session is undeliverable — the first post swaps in a fresh harness
session (``_replace_external_session``). For pi that throws away the
conversation the user asked to continue, which is the whole point of the
channel.

These exercise the real ``_on_mm_posted`` path rather than just channel
creation: the swap happens on the *post*, so a test that stops at
".invite created a channel" passes while the feature is still broken.
Both entry points that tag a session as undeliverable are covered —
bootstrap and channel creation.

Adapted from the independent reviewer's reproducers (2026-09-14).
Spec: specs/20260914-external-pi-sessions/design.md
"""

import pytest

from mm_bridge.config import Anchor

from doubles import make_bridge

PI_SESSION = "ses_08d12f773ff141678ba3f51d3f084763"
CLAUDE_SESSION = "ses_11111111222233334444555555555555"


def _external(session_id: str, backend: str, cwd: str) -> dict:
    return {
        "id": session_id,
        "backend": backend,
        "origin": "external",
        "project": {"path": cwd, "name": "test"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["fresh", "after-restart"])
async def test_post_to_an_invited_external_pi_channel_continues_it(tmp_path, restart) -> None:
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [_external(PI_SESSION, "pi", str(tmp_path))]

    await bridge._on_mm_posted(
        {"channel_id": "c1", "id": "invite-1", "message": f".invite {PI_SESSION}",
         "user_id": "u1", "type": ""}
    )
    anchor = bridge.mapping.get_anchor(PI_SESSION)
    assert anchor is not None

    if restart:
        # A daemon restart re-derives undeliverability from GET /v1/sessions;
        # the exemption has to hold there too, not just at channel creation.
        bridge._external_sessions.clear()
        await bridge._bootstrap_known_sessions()

    await bridge._on_mm_posted(
        {"channel_id": anchor.channel_id, "id": "post-1",
         "message": "CONTINUE_ORIGINAL_CONTEXT", "user_id": "u1", "type": ""}
    )

    assert bridge.harness.created == [], "must not create a fresh conversation"
    assert any(
        target == PI_SESSION and "CONTINUE_ORIGINAL_CONTEXT" in body
        for target, body in bridge.harness.sent
    ), bridge.harness.sent
    assert bridge.mapping.get_session(anchor) == PI_SESSION


@pytest.mark.asyncio
async def test_external_claude_still_gets_a_replacement_session(tmp_path) -> None:
    """The exemption is scoped to pi. claude/codex external channels keep
    the replacement behaviour they have always had — the harness resumes
    them into a NEW transcript, so there is no shared conversation to
    preserve and swapping is the established, tested path."""
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [_external(CLAUDE_SESSION, "claude", str(tmp_path))]

    await bridge._on_mm_posted(
        {"channel_id": "c1", "id": "invite-1", "message": f".invite {CLAUDE_SESSION}",
         "user_id": "u1", "type": ""}
    )
    anchor = bridge.mapping.get_anchor(CLAUDE_SESSION)
    await bridge._on_mm_posted(
        {"channel_id": anchor.channel_id, "id": "post-1", "message": "hello",
         "user_id": "u1", "type": ""}
    )

    assert bridge.harness.created, "external claude keeps the replacement path"


@pytest.mark.asyncio
async def test_auto_mirrored_pi_channel_carries_the_warning(tmp_path) -> None:
    """Nobody runs ``.invite`` for an auto-mirrored channel, so if the
    notice only lived at the invite call site the operator most likely to
    be surprised would never see it."""
    bridge = make_bridge(str(tmp_path), echoing=False)
    meta = _external(PI_SESSION, "pi", str(tmp_path))
    bridge.harness.sessions_meta = [meta]

    channel_id = await bridge._create_channel_for_session(meta)

    notices = [
        p.message for p in bridge.mm.posted
        if p.channel_id == channel_id and "sibling branches" in p.message
    ]
    assert notices, [p.message for p in bridge.mm.posted]
    assert "Close the pi/Herdr session" in notices[0]


@pytest.mark.asyncio
async def test_invite_to_an_already_mapped_pi_channel_still_warns(tmp_path) -> None:
    """The mapped-anchor shortcut returns before channel creation, so it
    has to post the notice itself — otherwise the second person invited to
    a channel is the one who never gets told."""
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [_external(PI_SESSION, "pi", str(tmp_path))]
    bridge.mapping.link(Anchor("c-existing"), PI_SESSION)

    await bridge._on_mm_posted(
        {"channel_id": "c1", "id": "invite-1", "message": f".invite {PI_SESSION}",
         "user_id": "u2", "type": ""}
    )

    assert any(
        p.channel_id == "c-existing" and "sibling branches" in p.message
        for p in bridge.mm.posted
    ), [(p.channel_id, p.message[:60]) for p in bridge.mm.posted]
    assert bridge.mm.invited[-1] == ("c-existing", "u2")
