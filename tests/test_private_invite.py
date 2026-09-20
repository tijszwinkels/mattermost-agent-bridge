"""Explicit .invite creates private channels without changing other entry points."""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mm_bridge.config import Anchor
from mm_bridge.mm_client import MattermostClient
from doubles import make_bridge


@pytest.mark.parametrize("channel_type", ["O", "P"])
def test_client_sends_requested_channel_type(channel_type):
    driver = Mock()
    with patch("mm_bridge.mm_client.Driver", return_value=driver):
        client = MattermostClient(
            url="mm.example", port=443, scheme="https", token="t", team_name="team",
        )
    client._team_id = "team-id"
    kwargs = {} if channel_type == "O" else {"channel_type": channel_type}
    client.create_channel("session", "Session", "purpose", **kwargs)
    driver.channels.create_channel.assert_called_once_with(options={
        "team_id": "team-id", "name": "session", "display_name": "Session",
        "purpose": "purpose", "type": channel_type,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["pi", "claude", "codex"])
async def test_invite_creates_private_channel_and_adds_requester(tmp_path, backend):
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [{"id": "ses_private", "backend": backend}]
    await bridge._on_mm_posted({
        "channel_id": "c1", "id": "invite-1", "message": ".invite ses_private",
        "user_id": "u1", "type": "",
    })
    anchor = bridge.mapping.get_anchor("ses_private")
    assert anchor is not None
    assert bridge.mm.channels[anchor.channel_id]["type"] == "P"
    assert (anchor.channel_id, "u1") in bridge.mm.invited


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", ["O", "P"])
async def test_invite_reuses_existing_channel_without_changing_visibility(tmp_path, channel_type):
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.mapping.link(Anchor("existing"), "ses_existing")
    bridge.mm.channels["existing"] = {"id": "existing", "type": channel_type}
    before = deepcopy(bridge.mm.channels)
    await bridge._cmd_invite("c1", {"user_id": "u1"}, "ses_existing", None)
    assert bridge.mm.channels == before
    assert bridge.mm.channels["existing"]["type"] == channel_type
    assert bridge.mm.invited == [("existing", "u1")]


@pytest.mark.asyncio
async def test_automatic_channel_creation_keeps_public_default(tmp_path):
    bridge = make_bridge(str(tmp_path), echoing=False)
    channel_id = await bridge._create_channel_for_session({"id": "ses_auto"})
    assert bridge.mm.channels[channel_id]["type"] == "O"


@pytest.mark.asyncio
async def test_private_creation_failure_does_not_map_or_invite(tmp_path):
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [{"id": "ses_private", "backend": "pi"}]
    bridge.mm.fail_create_channel = True
    await bridge._cmd_invite("c1", {"user_id": "u1"}, "ses_private", None)
    assert bridge.mapping.get_anchor("ses_private") is None
    assert bridge.mm.invited == []
    assert bridge.mm.channels == {}
    assert any("Couldn't create a channel" in p.message for p in bridge.mm.posted)
    assert any("No public fallback" in p.message for p in bridge.mm.posted)


@pytest.mark.asyncio
async def test_invite_failure_is_actionable_and_retry_reuses_private_channel(tmp_path):
    bridge = make_bridge(str(tmp_path), echoing=False)
    bridge.harness.sessions_meta = [{"id": "ses_private", "backend": "pi"}]
    with patch.object(bridge.mm, "invite_user", side_effect=RuntimeError("sensitive detail")):
        await bridge._cmd_invite("c1", {"user_id": "u1"}, "ses_private", None)
    anchor = bridge.mapping.get_anchor("ses_private")
    assert bridge.mm.channels[anchor.channel_id]["type"] == "P"
    replies = [p.message for p in bridge.mm.posted if p.channel_id == "c1"]
    assert any("retry `.invite ses_private`" in p and anchor.channel_id in p for p in replies)
    assert all("sensitive detail" not in p for p in replies)
    before = deepcopy(bridge.mm.channels)
    await bridge._cmd_invite("c1", {"user_id": "u1"}, "ses_private", None)
    assert bridge.mm.channels == before
    assert bridge.mm.invited == [(anchor.channel_id, "u1")]


@pytest.mark.asyncio
async def test_invite_rechecks_mapping_after_session_lookup(tmp_path):
    bridge = make_bridge(str(tmp_path), echoing=False)

    async def mirror_during_lookup(session_id):
        bridge.mm.channels["mirrored"] = {"id": "mirrored", "type": "O"}
        bridge.mapping.link(Anchor("mirrored"), session_id)
        return {"id": session_id, "backend": "pi"}

    bridge.harness.get_session = AsyncMock(side_effect=mirror_during_lookup)
    await bridge._cmd_invite("c1", {"user_id": "u1"}, "ses_race", None)
    assert bridge.mm.invited == [("mirrored", "u1")]
    assert bridge.mm.channels == {"mirrored": {"id": "mirrored", "type": "O"}}
