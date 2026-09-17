"""Channel discovery through real WS dispatch, with fake MM/harness services."""

from __future__ import annotations

import asyncio
import contextlib
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
    # A daemon that finished booting before this channel existed: it has a
    # complete membership picture, and nothing dormant in it yet.
    bridge._membership_baseline = True
    return bridge


async def notify(bridge, event, *, user_id=None, channel_id="c1"):
    await MattermostClient._dispatch_event(bridge.mm, {
        "event": event,
        "data": {"channel_id": channel_id, "user_id": user_id or bridge.mm.bot_user_id},
        "broadcast": {"user_id": bridge.mm.bot_user_id},
    }, {
        "channel_created": bridge._on_mm_channel_created,
        "user_added": bridge._on_mm_user_added,
        "user_removed": bridge._on_mm_user_removed,
    })


async def post(bridge, message, *, channel_id="c1", **fields):
    await MattermostClient._dispatch_event(bridge.mm, {
        "event": "posted",
        "data": {"post": json.dumps({
            "id": "task-1", "channel_id": channel_id, "user_id": "human",
            "message": message, **fields,
        })},
    }, {"posted": bridge._on_mm_posted})


def welcomes(bridge, channel_id=None):
    return [
        p for p in bridge.mm.posted
        if (p.props or {}).get("from_bridge") == "welcome"
        and (channel_id is None or p.channel_id == channel_id)
    ]


class LookupSpy:
    """Counts membership listings and can fail the first ``failures`` of them.

    The reconciler's whole job is retrying this call, so every recovery test
    needs to know how many times it ran and to make it fail on demand.
    """

    def __init__(self, bridge, *, failures=0):
        self.calls = 0
        self._failures = failures
        self._real = bridge.mm.get_bot_channel_ids
        bridge.mm.get_bot_channel_ids = self

    def __call__(self):
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("membership listing unavailable")
        return self._real()


@contextlib.asynccontextmanager
async def reconciler_running(bridge):
    """Run the REAL reconciler loop for the duration of the block.

    Recovery has to be provable through the mechanism that actually ships,
    not through a hand-called sweep, so these tests drive the loop itself
    and cancel it the way daemon shutdown does.
    """
    task = asyncio.create_task(bridge._run_membership_reconciler())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert task.cancelled(), "reconciler did not stop cleanly on cancellation"


async def wait_until(predicate, *, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


def discovered(bridge, channel_id="c1"):
    """Registered dormant AND welcomed.

    Registration lands one await before the welcome post, so a test that
    stops the reconciler on the first half would pass with a channel nobody
    was ever greeted in.
    """
    return channel_id in bridge._dormant_channels and bool(welcomes(bridge, channel_id))


async def settle(seconds=0.05):
    """Give a loop under test room to misbehave (spin) if it's going to."""
    await asyncio.sleep(seconds)


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
    # Mattermost never re-sends `channel_created`, so the only honest thing
    # to do with the dropped event is hand it to the reconciler.
    assert bridge._membership_recheck.is_set()


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


# ───────────────────────── membership reconciliation ──────────────────────
#
# Everything above is the fast path: Mattermost tells the bridge about a
# channel and the bridge registers it. These tests cover what happens when
# that message never arrives or can't be acted on — the failure mode whose
# only previous cure was restarting the daemon.


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_join", [False, True])
async def test_failed_discovery_lookup_recovers_without_a_second_event(
    bridge, auto_join,
):
    """The headline case: one transient lookup failure, full recovery."""
    bridge.config.auto_join_public_channels = auto_join
    bridge.config.membership_reconcile_seconds = 3600  # only the nudge fires
    spy = LookupSpy(bridge, failures=1)

    await notify(bridge, "channel_created")
    assert bridge._dormant_channels == set()
    assert spy.calls == 1

    async with reconciler_running(bridge):
        # No further creation event, no restart — just the retry.
        await wait_until(lambda: discovered(bridge))

    assert len(welcomes(bridge)) == 1
    await post(bridge, "@claude investigate this")
    assert len(bridge.harness.created) == 1
    assert len(bridge.harness.sent) == 1
    assert bridge.mapping.get_session(Anchor("c1")) == bridge.harness.next_session_id
    assert len(welcomes(bridge)) == 1
    assert bridge.mm.joined == []


@pytest.mark.asyncio
async def test_missed_creation_event_is_recovered(bridge):
    """A WS reconnect gap swallows the event entirely — nothing is ever sent."""
    bridge.config.membership_reconcile_seconds = 0.01

    async with reconciler_running(bridge):
        await wait_until(lambda: discovered(bridge))

    assert len(welcomes(bridge)) == 1
    await post(bridge, "@claude investigate this")
    assert len(bridge.harness.created) == 1
    assert bridge.mm.joined == []


@pytest.mark.asyncio
async def test_stale_membership_listing_recovers_on_a_later_sweep(bridge):
    """The listing lags behind the channel it was asked about."""
    bridge.config.membership_reconcile_seconds = 0.01
    bridge.mm.bot_channel_ids.clear()          # not visible yet

    await notify(bridge, "channel_created")
    assert bridge._dormant_channels == set()

    async with reconciler_running(bridge):
        await settle()                          # sweeps see the stale listing
        assert bridge._dormant_channels == set()
        bridge.mm.bot_channel_ids.add("c1")     # replica catches up
        await wait_until(lambda: discovered(bridge))

    assert len(welcomes(bridge)) == 1
    await post(bridge, "@claude investigate this")
    assert len(bridge.harness.created) == 1


@pytest.mark.asyncio
async def test_repeated_reconciliation_welcomes_and_engages_once(bridge):
    """Sweeping forever must stay a no-op once a channel is known."""
    for _ in range(3):
        await bridge._reconcile_memberships_once()
    assert bridge._dormant_channels == {"c1"}
    assert len(welcomes(bridge)) == 1
    assert bridge.harness.created == []

    await post(bridge, "@claude first task")
    session = bridge.mapping.get_session(Anchor("c1"))

    for _ in range(3):
        await bridge._reconcile_memberships_once()
    assert len(bridge.harness.created) == 1
    assert len(bridge.harness.sent) == 1
    assert len(welcomes(bridge)) == 1
    assert bridge.mapping.get_session(Anchor("c1")) == session
    assert bridge.mm.joined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_join", [False, True])
async def test_reconciliation_never_joins_or_adopts_nonmembers(bridge, auto_join):
    """Discovery is not permission to join: the sweep only reads memberships."""
    bridge.config.auto_join_public_channels = auto_join
    bridge.mm.bot_channel_ids.clear()
    bridge.mm.public_channels = [
        {"id": "c-public", "type": "O", "team_id": bridge.mm.team_id},
    ]

    await bridge._reconcile_memberships_once()

    assert bridge._dormant_channels == set()
    assert bridge.mm.joined == []
    assert bridge.mm.posted == []
    assert bridge.harness.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["leave", "removed"])
async def test_reconciliation_does_not_revive_a_left_channel(bridge, how):
    """A leave the listing hasn't caught up on must not walk the bot back in."""
    await notify(bridge, "channel_created")
    assert bridge._dormant_channels == {"c1"}

    if how == "leave":
        await post(bridge, "@claude leave")
        assert bridge.mm.removed == ["c1"]
    else:
        await notify(bridge, "user_removed")
    assert bridge._dormant_channels == set()
    # Mattermost still reports the membership for a moment.
    assert "c1" in bridge.mm.bot_channel_ids

    await bridge._reconcile_memberships_once()
    assert bridge._dormant_channels == set()
    assert len(welcomes(bridge)) == 1

    # Once the listing agrees, the tombstone retires rather than accumulating.
    bridge.mm.bot_channel_ids.discard("c1")
    await bridge._reconcile_memberships_once()
    assert bridge._left_channels == set()
    assert bridge._dormant_channels == set()

    # A real re-invite is still a fresh arrival.
    bridge.mm.bot_channel_ids.add("c1")
    await notify(bridge, "user_added")
    assert bridge._dormant_channels == {"c1"}
    assert len(welcomes(bridge)) == 2


@pytest.mark.asyncio
async def test_a_leave_does_not_blind_the_sweep_to_a_missed_re_invite(bridge):
    """The tombstone shields the lag window, not the channel forever.

    Leave, then get re-invited while the WS is down: nobody tells the bridge,
    so only the sweep can notice — and it must not still be honouring a leave
    that Mattermost has long since superseded.
    """
    await notify(bridge, "channel_created")
    await post(bridge, "@claude leave")
    assert bridge._left_channels == {"c1"}

    await bridge._reconcile_memberships_once()      # shielded: listing may lag
    assert bridge._dormant_channels == set()

    await bridge._reconcile_memberships_once()      # re-invited, event lost
    assert bridge._dormant_channels == {"c1"}
    assert len(welcomes(bridge)) == 2

    await post(bridge, "@claude first task")
    assert len(bridge.harness.created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["mapped", "warming"])
async def test_reconciliation_leaves_live_sessions_alone(bridge, state):
    warming = WarmingUpChannel("c1")
    warming.queued_posts.append({"id": "queued-task"})
    if state == "mapped":
        bridge.mapping.link(Anchor("c1"), "existing-session")
    else:
        bridge.warming_up_sessions["c1"] = warming

    await bridge._reconcile_memberships_once()

    if state == "mapped":
        assert bridge.mapping.get_session(Anchor("c1")) == "existing-session"
    else:
        assert bridge.warming_up_sessions["c1"] is warming
        assert warming.queued_posts == [{"id": "queued-task"}]
    assert bridge._dormant_channels == set()
    assert bridge.mm.posted == []
    assert bridge.harness.created == []


@pytest.mark.asyncio
async def test_failed_bootstrap_recovers_silently_then_welcomes_new_arrivals(bridge):
    """A boot with no membership picture must not welcome every old channel."""
    bridge.config.membership_reconcile_seconds = 3600
    bridge._membership_baseline = False
    spy = LookupSpy(bridge, failures=1)

    assert await bridge._bootstrap_dormant_channels() is False
    assert bridge._membership_baseline is False
    bridge._membership_recheck.set()            # what `start()` does on failure

    async with reconciler_running(bridge):
        await wait_until(lambda: bridge._membership_baseline)
        # Pre-existing memberships are re-registered the way the bootstrap
        # would have: dormant, and without a second welcome years later.
        assert bridge._dormant_channels == {"c1"}
        assert welcomes(bridge) == []

        # Anything that shows up AFTER the baseline is genuinely new.
        bridge.mm.channels["c2"] = {
            "id": "c2", "type": "P", "team_id": bridge.mm.team_id, "purpose": "",
        }
        bridge.mm.bot_channel_ids.add("c2")
        bridge._membership_recheck.set()
        await wait_until(lambda: discovered(bridge, "c2"))

    assert [p.channel_id for p in welcomes(bridge)] == ["c2"]
    assert spy.calls == 3                        # failed boot + two sweeps


@pytest.mark.asyncio
async def test_bootstrap_keeps_memberships_learned_while_it_was_blind(bridge):
    """The bootstrap retry must not discard invites that arrived meanwhile."""
    bridge._membership_baseline = False
    LookupSpy(bridge, failures=1)
    assert await bridge._bootstrap_dormant_channels() is False

    await notify(bridge, "user_added")           # invite lands during the gap
    assert bridge._dormant_channels == {"c1"}

    bridge.mm.bot_channel_ids.clear()            # listing still behind
    await bridge._reconcile_memberships_once()

    assert bridge._dormant_channels == {"c1"}
    assert len(welcomes(bridge)) == 1


@pytest.mark.asyncio
async def test_repeated_failures_do_not_busy_loop(bridge):
    """One wake, one sweep — a failing sweep must not re-arm itself."""
    bridge.config.membership_reconcile_seconds = 3600
    spy = LookupSpy(bridge, failures=1)

    async with reconciler_running(bridge):
        bridge._membership_recheck.set()
        await wait_until(lambda: spy.calls == 1)
        await settle()
        assert spy.calls == 1                    # failure did not respin
        assert bridge._dormant_channels == set()

        bridge._membership_recheck.set()         # next failed discovery
        await wait_until(lambda: discovered(bridge))
        await settle()
        assert spy.calls == 2

    assert len(welcomes(bridge)) == 1


@pytest.mark.asyncio
async def test_transient_failures_recover_on_the_interval_alone(bridge):
    """No nudge at all: the periodic sweep rides out a string of failures."""
    bridge.config.membership_reconcile_seconds = 0.01
    spy = LookupSpy(bridge, failures=3)

    async with reconciler_running(bridge):
        await wait_until(lambda: discovered(bridge))

    assert spy.calls == 4
    assert len(welcomes(bridge)) == 1
