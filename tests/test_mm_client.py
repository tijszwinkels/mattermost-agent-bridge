"""Tests for mm_bridge.mm_client.MattermostClient."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from mm_bridge.mm_client import MattermostClient


@dataclass
class FakeHttpxResponse:
    """Stand-in for httpx.Response — only exposes .content."""

    content: bytes


@dataclass
class FakeDriverClient:
    """Stand-in for mattermostautodriver.client.Client."""

    responses: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def make_request(self, method: str, endpoint: str, **_: Any) -> FakeHttpxResponse:
        self.calls.append((method, endpoint))
        return FakeHttpxResponse(content=self.responses.get(endpoint, b""))

    def delete(self, endpoint: str) -> None:
        self.calls.append(("delete", endpoint))


@dataclass
class FakePostsApi:
    """Stand-in for Driver.posts — records create/update calls and
    returns the id callers hand back via ``next_id``."""
    created: list[dict] = field(default_factory=list)
    updated: list[tuple[str, dict]] = field(default_factory=list)
    channel_responses: list[dict] = field(default_factory=list)
    channel_calls: list[tuple[str, dict]] = field(default_factory=list)
    next_id: str = "new-id"

    def create_post(self, options):
        self.created.append(options)
        return {"id": self.next_id, **options}

    def update_post(self, post_id, options):
        self.updated.append((post_id, options))
        return {"id": post_id, **options}

    def get_posts_for_channel(self, channel_id, params=None):
        self.channel_calls.append((channel_id, params or {}))
        idx = len(self.channel_calls) - 1
        if idx >= len(self.channel_responses):
            raise AssertionError("get_posts_since did not stop after repeated page")
        return self.channel_responses[idx]


@dataclass
class FakeChannelsApi:
    """Stand-in for Driver.channels — records pagination calls + returns
    pre-staged pages from ``page_responses``."""
    page_responses: list[list[dict]] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    # slug → channel record, for get_channel_by_name_for_team_name.
    named: dict = field(default_factory=dict)
    name_calls: list[tuple[str, str]] = field(default_factory=list)

    def get_public_channels_for_team(self, team_id, params=None):
        self.calls.append((team_id, dict(params or {})))
        idx = len(self.calls) - 1
        if idx >= len(self.page_responses):
            raise AssertionError(
                "list_public_team_channels did not stop paginating after "
                "the last staged response"
            )
        return self.page_responses[idx]

    def get_channel_by_name_for_team_name(
        self, team_name, channel_name, include_deleted=False,
    ):
        self.name_calls.append((team_name, channel_name))
        if channel_name in self.named:
            return self.named[channel_name]
        return {"id": channel_name, "name": channel_name}


@dataclass
class FakeDriver:
    client: FakeDriverClient = field(default_factory=FakeDriverClient)
    posts: FakePostsApi = field(default_factory=FakePostsApi)
    channels: FakeChannelsApi = field(default_factory=FakeChannelsApi)


def _make_client_with_driver(driver: FakeDriver) -> MattermostClient:
    """Build a MattermostClient wired to a fake driver (skips real login)."""
    with patch("mm_bridge.mm_client.Driver", return_value=driver):
        return MattermostClient(
            url="mm.example", port=443, scheme="https",
            token="t", team_name="team",
        )


def test_download_file_returns_raw_bytes_for_json_attachment():
    """Regression: JSON attachments must round-trip byte-for-byte.

    The underlying mattermostautodriver.Client.get() auto-parses any
    application/json response into a dict, which would mangle JSON file
    downloads. download_file() must bypass that path.
    """
    gcp_key = (
        b'{\n  "type": "service_account",\n'
        b'  "project_id": "example-poc",\n'
        b'  "private_key_id": "cd77b561f2e4abc"\n}\n'
    )
    driver = FakeDriver()
    driver.client.responses["/api/v4/files/fid123"] = gcp_key
    client = _make_client_with_driver(driver)

    data = client.download_file("fid123")

    assert data == gcp_key
    assert driver.client.calls == [("get", "/api/v4/files/fid123")]


def test_download_file_returns_raw_bytes_for_binary_attachment():
    """Non-JSON content types (PDFs, images) still work."""
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nbinary-content"
    driver = FakeDriver()
    driver.client.responses["/api/v4/files/pdf99"] = pdf_bytes
    client = _make_client_with_driver(driver)

    assert client.download_file("pdf99") == pdf_bytes


def test_remove_self_uses_stable_rest_endpoint():
    """The driver removed ``Channels.remove_channel_member`` in v2.

    Leaving must use the stable REST endpoint exposed by the low-level client
    so both active and dormant ``@claude leave`` paths continue to work.
    """
    driver = FakeDriver()
    client = _make_client_with_driver(driver)
    client._bot_user_id = "bot-1"

    client.remove_self_from_channel("channel-1")

    assert driver.client.calls == [
        ("delete", "/api/v4/channels/channel-1/members/bot-1"),
    ]


def test_get_posts_since_breaks_when_pagination_repeats_page():
    """Regression: repeated full pages must not loop forever."""
    page = {
        "order": ["p1", "p2"],
        "posts": {
            "p1": {"id": "p1", "create_at": 100},
            "p2": {"id": "p2", "create_at": 200},
        },
    }
    driver = FakeDriver()
    driver.posts.channel_responses = [page, page]
    client = _make_client_with_driver(driver)

    posts = client.get_posts_since("c1", 123, per_page=2)

    assert [p["id"] for p in posts] == ["p1", "p2"]
    assert [params["page"] for _, params in driver.posts.channel_calls] == [0, 1]


# ───────────────────── list_public_team_channels pagination ─────────────


def test_list_public_team_channels_paginates_until_short_page():
    """Regression: a single un-paginated call returned only the first 60
    channels, so newly-created public channels beyond that prefix never
    showed up in the auto-join reconciler. The client must page through
    until a short page signals the end of the list."""
    full_page_a = [{"id": f"c-a{i}", "type": "O"} for i in range(200)]
    full_page_b = [{"id": f"c-b{i}", "type": "O"} for i in range(200)]
    last_page = [{"id": "c-tail", "type": "O"}]

    driver = FakeDriver()
    driver.channels.page_responses = [full_page_a, full_page_b, last_page]
    client = _make_client_with_driver(driver)

    out = client.list_public_team_channels()

    assert [c["id"] for c in out] == (
        [f"c-a{i}" for i in range(200)]
        + [f"c-b{i}" for i in range(200)]
        + ["c-tail"]
    )
    assert [params["page"] for _, params in driver.channels.calls] == [0, 1, 2]
    # Per-page large enough to keep the round-trip count bounded on big teams.
    assert all(params["per_page"] >= 100 for _, params in driver.channels.calls)


def test_list_public_team_channels_stops_on_empty_page():
    """If the last page returns exactly per_page items, the next call
    yields an empty list — the loop must stop there, not loop forever."""
    full = [{"id": f"c{i}", "type": "O"} for i in range(200)]
    empty: list[dict] = []

    driver = FakeDriver()
    driver.channels.page_responses = [full, empty]
    client = _make_client_with_driver(driver)

    out = client.list_public_team_channels()

    assert len(out) == 200
    assert [params["page"] for _, params in driver.channels.calls] == [0, 1]


def test_list_public_team_channels_safety_cap_aborts_runaway():
    """Defensive cap: if MM keeps returning full pages forever (server bug
    or malicious response), the loop terminates at the cap instead of
    spinning. Test stages enough full pages to exceed any reasonable cap
    and asserts the call count is bounded."""
    page = [{"id": f"c{i}", "type": "O"} for i in range(200)]
    driver = FakeDriver()
    # Stage way more pages than the cap to prove the loop bails.
    driver.channels.page_responses = [page] * 1000
    client = _make_client_with_driver(driver)

    out = client.list_public_team_channels()

    # The method returns whatever it collected before hitting the cap;
    # we don't pin the exact cap value here, only that the loop did NOT
    # consume all 1000 staged pages.
    assert len(driver.channels.calls) < 1000
    assert len(out) == len(driver.channels.calls) * 200


# ───────────────────── Own-post tracking ──────────────────────────────────


def test_post_records_returned_id_as_own():
    driver = FakeDriver()
    driver.posts.next_id = "own-123"
    client = _make_client_with_driver(driver)

    client.post("c1", "hi")

    assert client.is_own_post("own-123") is True
    assert client.is_own_post("") is False
    assert client.is_own_post("someone-else") is False


def test_post_passes_props_through_to_create_post():
    """``props=...`` lands in ``options['props']`` on the underlying
    create_post call so the daemon dispatcher can recognise CLI-authored
    posts and skip user-turn injection."""
    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    client.post("c1", "hi", props={"from_bridge_cli": "spawn-announcement"})

    assert len(driver.posts.created) == 1
    assert driver.posts.created[0].get("props") == {
        "from_bridge_cli": "spawn-announcement",
    }


def test_post_omits_props_key_when_not_provided():
    """No ``props`` kwarg → no ``props`` key in the create_post options.
    Keeps the wire payload identical to the pre-feature behaviour for the
    common (non-marker) case."""
    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    client.post("c1", "hi")

    assert len(driver.posts.created) == 1
    assert "props" not in driver.posts.created[0]


def test_post_message_records_returned_id_as_own():
    driver = FakeDriver()
    driver.posts.next_id = "own-456"
    client = _make_client_with_driver(driver)

    client.post_message("c1", "hello")

    assert client.is_own_post("own-456") is True


def test_update_post_records_post_id_as_own():
    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    client.update_post("edited-789", "new body")

    assert client.is_own_post("edited-789") is True


def test_own_post_history_evicts_oldest_when_full():
    """The tracker is a bounded LRU-ish deque — once full, the oldest id
    ages out so the lookup set doesn't grow without bound."""
    import mm_bridge.mm_client as mc

    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    first = "first-id"
    client._record_own_post(first)
    for i in range(mc._OWN_POST_HISTORY_MAX):
        client._record_own_post(f"pid-{i}")

    assert client.is_own_post(first) is False
    assert client.is_own_post(f"pid-{mc._OWN_POST_HISTORY_MAX - 1}") is True


# ───────────────────── WS dispatch echo suppression ───────────────────────


def test_ws_posted_suppresses_our_own_echo():
    """A ``posted`` WS event whose post id matches one this process
    just authored is filtered before dispatch — that's how we swallow
    the MM-side echo of our own posts without help from a user_id
    check."""
    import json as _json

    driver = FakeDriver()
    driver.posts.next_id = "echo-1"
    client = _make_client_with_driver(driver)
    client.post("c1", "outbound")  # records "echo-1" as own

    captured: list[dict] = []

    async def handler(post):
        captured.append(post)

    echo_event = {
        "event": "posted",
        "data": {"post": _json.dumps({"id": "echo-1", "user_id": "bot", "message": "outbound"})},
    }
    asyncio.run(client._dispatch_event(echo_event, {"posted": handler}))

    assert captured == []


def test_ws_posted_marker_does_not_disable_own_echo_suppression():
    """The ``from_bridge_cli`` marker is an *additional* skip path
    layered on top of ``is_own_post`` echo suppression, not a
    replacement. A post that's both marker-stamped *and* one this
    process just authored must still be dropped at the
    ``_dispatch_event`` layer — i.e. the existing echo path is
    untouched. (The marker's job is the *cross-process* case the
    deque can't see, not weakening the in-process echo guard.)"""
    import json as _json

    driver = FakeDriver()
    driver.posts.next_id = "echo-1"
    client = _make_client_with_driver(driver)
    client.post(
        "c1", "outbound",
        props={"from_bridge_cli": "spawn-announcement"},
    )  # records "echo-1" as own

    captured: list[dict] = []

    async def handler(post):
        captured.append(post)

    echo_event = {
        "event": "posted",
        "data": {"post": _json.dumps({
            "id": "echo-1", "user_id": "bot", "message": "outbound",
            "props": {"from_bridge_cli": "spawn-announcement"},
        })},
    }
    asyncio.run(client._dispatch_event(echo_event, {"posted": handler}))

    assert captured == []


def test_ws_posted_forwards_cross_identity_bot_post():
    """Regression: posts by another actor sharing the bot identity
    (sibling mm-bridge sessions, other scripts using MM_BOT_TOKEN,
    humans posting as ``@claude``) reach the handler — they are *not*
    echoes of our own posts."""
    import json as _json

    driver = FakeDriver()
    driver.posts.next_id = "own-1"
    client = _make_client_with_driver(driver)
    client.post("c1", "ours")  # tracks "own-1"

    captured: list[dict] = []

    async def handler(post):
        captured.append(post)

    event = {
        "event": "posted",
        "data": {"post": _json.dumps({
            "id": "foreign-1", "user_id": "bot",
            "message": "from another session using the same token",
        })},
    }
    asyncio.run(client._dispatch_event(event, {"posted": handler}))

    assert len(captured) == 1
    assert captured[0]["id"] == "foreign-1"


# ───────────────────── WS connection liveness ─────────────────────────────


def test_ws_connect_passes_heartbeat_to_aiohttp():
    """The WS opens with an aiohttp ``heartbeat`` so half-open TCP
    sockets are detected and the outer reconnect loop can recover.

    Without it, a silently-dropped TCP connection leaves the client
    blocked in ``async for msg in ws`` forever — the symptom we hit
    where MM events stopped flowing while HTTP API calls kept working.
    """
    driver = FakeDriver()
    client = _make_client_with_driver(driver)

    captured_kwargs: dict = {}

    class FakeWS:
        async def send_json(self, _):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeWSCtx:
        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *_):
            return False

    class FakeSession:
        def ws_connect(self, url, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["__url"] = url
            return FakeWSCtx()

    class FakeSessionCtx:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_):
            return False

    with patch("aiohttp.ClientSession", return_value=FakeSessionCtx()):
        asyncio.run(client._ws_connect("wss://mm.example/ws", {}))

    assert "heartbeat" in captured_kwargs, (
        "ws_connect must be called with heartbeat= so half-open sockets are detected"
    )
    # Pin the value: aiohttp pongs wait heartbeat/2, so 30s gives ~45s
    # worst-case detection — a deliberate trade between aggressive
    # reconnects and silently stalled MM events. Drift here should be
    # an explicit decision, not a silent edit.
    assert captured_kwargs["heartbeat"] == 30


def test_get_channel_by_name_delegates_to_name_endpoint():
    """"--channel <slug>" resolution must hit
    GET /teams/name/{team}/channels/name/{channel} via the driver,
    passing the team + slug through verbatim."""
    driver = FakeDriver()
    driver.channels.named["meshenger-v2"] = {
        "id": "chan-mesh", "name": "meshenger-v2",
    }
    client = _make_client_with_driver(driver)

    ch = client.get_channel_by_name("tinkertank", "meshenger-v2")

    assert ch["id"] == "chan-mesh"
    assert driver.channels.name_calls == [("tinkertank", "meshenger-v2")]
