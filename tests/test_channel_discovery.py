"""Channel discovery through real WS dispatch, with fake MM/harness services."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from mm_bridge.bridge import WarmingUpChannel
from mm_bridge.config import Anchor
from mm_bridge.mm_client import MattermostClient

from doubles import make_bridge


@pytest.fixture
def bridge(tmp_path):
    bridge = make_bridge(
        str(tmp_path), echoing=False, auto_join_public_channels=False,
    )
    bridge.mm.channels["c1"] = {
        "id": "c1", "type": "P", "team_id": bridge.mm.team_id,
        "creator_id": bridge.mm.bot_user_id, "purpose": "",
    }
    # Creation automatically adds the creator, without a user_added event.
    bridge.mm.bot_channel_ids.add("c1")
    return bridge


async def notify(bridge, event, *, user_id=None):
    await MattermostClient._dispatch_event(bridge.mm, {
        "event": event,
        "data": {"channel_id": "c1", "user_id": user_id or bridge.mm.bot_user_id},
        "broadcast": {"user_id": bridge.mm.bot_user_id},
    }, {
        "channel_created": bridge._on_mm_channel_created,
        "user_added": bridge._on_mm_user_added,
        "user_removed": bridge._on_mm_user_removed,
    })


async def post(bridge, message, **fields):
    await MattermostClient._dispatch_event(bridge.mm, {
        "event": "posted",
        "data": {"post": json.dumps({
            "id": "task-1", "channel_id": "c1", "user_id": "human",
            "message": message, **fields,
        })},
    }, {"posted": bridge._on_mm_posted})


def welcomes(bridge):
    return [p for p in bridge.mm.posted if (p.props or {}).get("from_bridge") == "welcome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_join", [False, True])
@pytest.mark.parametrize("channel_type", ["P", "O"])
async def test_created_membership_engages_without_restart(bridge, auto_join, channel_type):
    bridge.config.auto_join_public_channels = auto_join
    bridge.mm.channels["c1"]["type"] = channel_type

    await notify(bridge, "channel_created")
    await notify(bridge, "user_added", user_id="human")
    await post(bridge, "human joined", type="system_add_to_channel")
    welcome = welcomes(bridge)
    assert len(welcome) == 1
    # The welcome's WS echo must not become an autorespond turn.
    await post(bridge, welcome[0].message, id="p1", user_id=bridge.mm.bot_user_id)
    assert bridge._dormant_channels == {"c1"}
    assert bridge.harness.created == []

    # Reproduce `mm-bridge post --channel <id>` from a sibling session
    # using the same bot account, with an explicit mention.
    await post(bridge, "@claude investigate this", user_id=bridge.mm.bot_user_id, props={
        "from_bridge_cli": "post", "from_bridge_cli_target": "explicit",
        "from_bridge_cli_channel": "parent", "from_bridge_cli_session": "parent-session",
    })

    assert len(bridge.harness.created) == 1
    assert len(bridge.harness.sent) == 1
    assert bridge.harness.sent[0][1].endswith("investigate this")
    assert bridge.mapping.get_session(Anchor("c1")) == bridge.harness.next_session_id
    assert "c1" not in bridge._dormant_channels
    assert "c1" not in bridge.warming_up_sessions
    assert len(welcomes(bridge)) == 1
    assert bridge.mm.joined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,engages", [("", False), ("mention-only", False), ("autorespond", True)])
async def test_discovered_channel_respects_engagement_mode(bridge, purpose, engages):
    bridge.mm.channels["c1"]["purpose"] = purpose
    await notify(bridge, "channel_created")
    assert "c1" in bridge._dormant_channels

    await post(bridge, "ordinary conversation")

    assert len(bridge.harness.created) == int(engages)
    assert len(bridge.harness.sent) == int(engages)
    assert bridge.mm.joined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type,auto_join", [("P", False), ("P", True), ("O", False)])
async def test_nonmember_is_not_registered_or_joined(bridge, channel_type, auto_join):
    bridge.mm.bot_channel_ids.clear()
    bridge.mm.channels["c1"]["type"] = channel_type
    bridge.config.auto_join_public_channels = auto_join

    await notify(bridge, "channel_created")
    await notify(bridge, "user_added", user_id="human")
    await post(bridge, "@claude must not start")

    assert bridge._dormant_channels == set()
    assert bridge.mm.joined == []
    assert bridge.mm.posted == []
    assert bridge.harness.created == []
    assert bridge.harness.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    ["channel_created", "channel_created", "user_added", "user_added"],
    ["user_added", "channel_created", "user_added", "channel_created"],
])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_duplicate_notifications_welcome_and_engage_once(bridge, events, concurrent):
    if concurrent:
        await asyncio.gather(*(notify(bridge, event) for event in events))
    else:
        for event in events:
            await notify(bridge, event)

    assert len(welcomes(bridge)) == 1
    assert bridge.harness.created == []
    await post(bridge, "@claude first task")
    assert len(bridge.harness.created) == 1
    assert len(bridge.harness.sent) == 1
    assert bridge.mm.joined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["mapped", "warming"])
async def test_notifications_preserve_existing_session_state(bridge, state):
    bridge.config.auto_join_public_channels = True
    warming = WarmingUpChannel("c1")
    warming.queued_posts.append({"id": "queued-task"})
    if state == "mapped":
        bridge.mapping.link(Anchor("c1"), "existing-session")
    else:
        bridge.warming_up_sessions["c1"] = warming

    await notify(bridge, "channel_created")
    await notify(bridge, "user_added")

    if state == "mapped":
        assert bridge.mapping.get_session(Anchor("c1")) == "existing-session"
    else:
        assert bridge.warming_up_sessions["c1"] is warming
        assert warming.queued_posts == [{"id": "queued-task"}]
    assert bridge._dormant_channels == set()
    assert bridge.mm.joined == []
    assert bridge.mm.posted == []
    assert bridge.harness.created == []


@pytest.mark.asyncio
async def test_notifications_during_first_engagement_preserve_queued_post(bridge):
    await notify(bridge, "channel_created")
    creating = asyncio.Event()
    release = asyncio.Event()
    create_session = bridge.harness.create_session

    async def delayed_create(**kwargs):
        creating.set()
        await release.wait()
        return await create_session(**kwargs)

    with patch.object(bridge.harness, "create_session", side_effect=delayed_create):
        first = asyncio.create_task(post(bridge, "@claude first task"))
        try:
            await asyncio.wait_for(creating.wait(), timeout=2)
            warming = bridge.warming_up_sessions["c1"]
            await notify(bridge, "channel_created")
            await notify(bridge, "user_added")
            await post(bridge, "@claude second task", id="task-2")
            assert bridge.warming_up_sessions["c1"] is warming
            assert [p["id"] for p in warming.queued_posts] == ["task-2"]
        finally:
            release.set()
            await first

    assert len(bridge.harness.created) == 1
    assert len(bridge.harness.sent) == 2
    assert bridge.harness.sent[0][1].endswith("first task")
    assert bridge.harness.sent[1][1].endswith("second task")
    assert len(welcomes(bridge)) == 1
    assert bridge.mm.joined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_join", [False, True])
async def test_membership_lookup_failure_does_not_register_or_join(bridge, auto_join):
    bridge.config.auto_join_public_channels = auto_join
    with patch.object(bridge.mm, "get_bot_channel_ids", side_effect=RuntimeError("offline")):
        await notify(bridge, "channel_created")

    assert bridge._dormant_channels == set()
    assert bridge.mm.joined == []
    assert bridge.mm.posted == []
    assert bridge.harness.created == []

    # A failed lookup must not mark the channel as already handled.
    await notify(bridge, "channel_created")
    assert bridge._dormant_channels == {"c1"}
    assert len(welcomes(bridge)) == 1


@pytest.mark.asyncio
async def test_membership_notification_racing_discovery_lookup_welcomes_once(bridge):
    async def membership_lookup(*args):
        await notify(bridge, "user_added")
        return {"c1"}

    with patch("mm_bridge.bridge.asyncio.to_thread", new=AsyncMock(side_effect=membership_lookup)):
        # Avoid intercepting the welcome's unrelated get_channel call.
        with patch.object(bridge, "_post_channel_join_welcome", new=AsyncMock()) as welcome:
            await notify(bridge, "channel_created")
            welcome.assert_awaited_once_with("c1")
    assert bridge._dormant_channels == {"c1"}
