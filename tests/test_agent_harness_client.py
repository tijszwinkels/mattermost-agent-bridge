from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mm_bridge.agent_harness_client import (
    SSE_READ_TIMEOUT_SECONDS,
    AgentHarnessClient,
    HarnessForkUnsupported,
    HarnessInterruptUnsupported,
    HarnessResumeUnsupported,
    HarnessRunNotFound,
    HarnessSessionNotFound,
)


pytestmark = pytest.mark.asyncio


def _client(handler) -> AgentHarnessClient:
    return AgentHarnessClient.with_transport(
        "http://harness.test",
        httpx.MockTransport(handler),
    )


async def test_create_session_request_shape_aliases_backend_and_derives_project_name():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": "claude_abc",
                "backend": "claude-code",
                "model": "opus",
                "project": {"path": "/tmp/project", "name": "project"},
                "title": None,
                "origin": "harness",
                "status": "idle",
            },
        )

    client = _client(handler)
    session = await client.create_session(
        backend="claude",
        model="opus",
        cwd="/tmp/project",
    )

    # bypass_permissions defaults to True — MM has no UI to surface or
    # accept Claude Code permission prompts, so every bridge-spawned
    # session must run with --dangerously-skip-permissions or it stalls.
    assert seen == {
        "method": "POST",
        "path": "/v1/sessions",
        "body": {
            "backend": "claude-code",
            "model": "opus",
            "project": {"path": "/tmp/project", "name": "project"},
            "bypass_permissions": True,
        },
    }
    assert session["id"] == "claude_abc"


async def test_create_session_drops_none_model_and_includes_title_when_present():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "codex_abc"})

    client = _client(handler)
    await client.create_session(
        backend="codex",
        model=None,
        cwd="/tmp/project",
        title="My session",
    )

    assert seen["body"] == {
        "backend": "codex",
        "project": {"path": "/tmp/project", "name": "project"},
        "title": "My session",
        "bypass_permissions": True,
    }


async def test_create_session_includes_effort_when_set():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "s1"})

    client = _client(handler)
    await client.create_session(
        backend="codex", model=None, cwd="/tmp/project", effort="xhigh",
    )
    assert seen["body"]["effort"] == "xhigh"


async def test_create_session_omits_effort_when_unset():
    """Unset means "emit nothing" — the backend CLI's own default applies."""
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "s1"})

    client = _client(handler)
    await client.create_session(backend="codex", model=None, cwd="/tmp/project")
    assert "effort" not in seen["body"]


# --- update_session (PATCH) — mutate a LIVE session without recreating it ---


async def test_update_session_patches_effort():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "s1", "effort": "max"})

    client = _client(handler)
    out = await client.update_session("s1", effort="max")

    assert seen == {
        "method": "PATCH",
        "path": "/v1/sessions/s1",
        "body": {"effort": "max"},
    }
    assert out["effort"] == "max"


async def test_update_session_omits_unset_fields_never_sends_null():
    """The harness REJECTS explicit nulls — omission is the only "unchanged"."""
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"id": "s1"})

    client = _client(handler)
    await client.update_session("s1")
    assert seen["body"] == {}


async def test_update_session_missing_session_is_typed():
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    client = _client(handler)
    with pytest.raises(HarnessSessionNotFound):
        await client.update_session("nope", effort="low")


async def test_update_session_server_error_raises_not_swallowed():
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"detail": "boom"}})

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.update_session("s1", effort="low")


async def test_create_session_allows_caller_to_disable_bypass_permissions():
    """Callers with out-of-band permission UX can opt out of the default
    bypass_permissions=True."""
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "claude_abc"})

    client = _client(handler)
    await client.create_session(
        backend="claude",
        model="opus",
        cwd="/tmp/project",
        bypass_permissions=False,
    )
    assert seen["body"]["bypass_permissions"] is False


async def test_create_run_posts_message_only_and_tracks_accepted_response():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            202,
            json={"session_id": "codex_abc", "run_id": "run_123"},
        )

    client = _client(handler)
    run = await client.create_run("codex_abc", "hello")

    assert seen == {
        "method": "POST",
        "path": "/v1/sessions/codex_abc/runs",
        "body": {"message": "hello"},
    }
    assert run == {"session_id": "codex_abc", "run_id": "run_123"}


async def test_create_run_409_maps_to_resume_unsupported():
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "external_resume_unsupported", "detail": "nope"}},
        )

    client = _client(handler)

    with pytest.raises(HarnessResumeUnsupported):
        await client.create_run("codex_abc", "hello")


async def test_fork_session_unwraps_session_and_optional_run():
    seen: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "session": {"id": "codex_child"},
                "run": {"id": "run_child"},
            },
        )

    client = _client(handler)
    response = await client.fork_session("codex_parent", message="continue")

    assert seen == {
        "path": "/v1/sessions/codex_parent/forks",
        "body": {"message": "continue"},
    }
    assert response["session"]["id"] == "codex_child"
    assert response["run"]["id"] == "run_child"


@pytest.mark.parametrize("status", [404, 409])
async def test_fork_session_unsupported_errors_are_typed(status: int):
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": "fork_unsupported", "detail": "no fork"}},
        )

    client = _client(handler)

    with pytest.raises(HarnessForkUnsupported):
        await client.fork_session("codex_parent", message="continue")


async def test_interrupt_run_maps_terminal_errors():
    responses = [
        httpx.Response(409, json={"error": {"code": "external_interrupt_unsupported"}}),
        httpx.Response(404, json={"error": {"code": "run_not_found"}}),
    ]

    async def handler(req: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = _client(handler)

    with pytest.raises(HarnessInterruptUnsupported):
        await client.interrupt_run("codex_abc", "run_123")
    with pytest.raises(HarnessRunNotFound):
        await client.interrupt_run("codex_abc", "run_123")


async def test_get_run_returns_run_row_and_none_on_404():
    async def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        if req.url.path == "/v1/sessions/ses_a/runs/run_1":
            return httpx.Response(200, json={"id": "run_1", "status": "running"})
        if req.url.path == "/v1/sessions/ses_a/runs/missing":
            return httpx.Response(404, json={"error": {"code": "run_not_found"}})
        raise AssertionError(f"unexpected request: {req.url}")

    client = _client(handler)

    assert await client.get_run("ses_a", "run_1") == {"id": "run_1", "status": "running"}
    assert await client.get_run("ses_a", "missing") is None


async def test_get_run_raises_on_5xx():
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "boom"}})

    client = _client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_run("ses_a", "run_1")


async def test_list_session_runs_unwraps_data():
    async def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/v1/sessions/ses_a/runs"
        return httpx.Response(
            200,
            json={"data": [{"id": "run_1", "status": "completed"},
                           {"id": "run_2", "status": "running"}]},
        )

    client = _client(handler)

    assert await client.list_session_runs("ses_a") == [
        {"id": "run_1", "status": "completed"},
        {"id": "run_2", "status": "running"},
    ]


async def test_get_session_list_sessions_models_messages_and_health():
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/sessions/missing":
            return httpx.Response(404, json={"error": {"code": "session_not_found"}})
        if req.url.path == "/v1/sessions/codex_abc":
            return httpx.Response(200, json={"id": "codex_abc"})
        if req.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": [{"id": "codex_abc"}]})
        if req.url.path == "/v1/backends/claude-code/models":
            return httpx.Response(200, json={"data": ["opus"]})
        if req.url.path == "/v1/backends/unknown/models":
            return httpx.Response(404, json={"error": {"code": "backend_not_found"}})
        if req.url.path == "/v1/sessions/codex_abc/messages":
            return httpx.Response(200, json={"data": [{"id": "msg_1"}]})
        if req.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {req.url}")

    client = _client(handler)

    assert await client.get_session("missing") is None
    assert await client.get_session("codex_abc") == {"id": "codex_abc"}
    assert await client.list_sessions() == [{"id": "codex_abc"}]
    assert await client.list_backend_models("claude") == ["opus"]
    assert await client.list_backend_models("unknown") == []
    assert await client.list_session_messages("codex_abc") == [{"id": "msg_1"}]
    assert await client.health() == {"status": "ok"}


async def test_stream_events_dispatches_parsed_events_and_reconnects_after_sequence():
    requests: list[str] = []
    events: list[tuple[str, dict]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        if len(requests) == 1:
            payload = {"sequence": 41, "event": "session.updated", "data": {"x": 1}}
        else:
            assert req.url.params.get("after") == "41"
            payload = {"sequence": 42, "event": "message", "data": {"x": 2}}
        body = (
            f"event: {payload['event']}\n"
            f"data: {json.dumps(payload)}\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(event_name: str, data: dict) -> None:
        events.append((event_name, data))
        if len(events) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event)

    assert [name for name, _ in events] == ["session.updated", "message"]
    assert requests[0] == "http://harness.test/v1/events"
    assert requests[1] == "http://harness.test/v1/events?after=41"


async def test_stream_events_starts_from_after_sequence_when_provided():
    seen_params: list[str | None] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen_params.append(req.url.params.get("after"))
        payload = {"sequence": 101, "event": "ping", "data": {}}
        body = (f"event: ping\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)
    stopped = asyncio.Event()

    async def on_event(_name: str, _data: dict) -> None:
        stopped.set()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, after_sequence=100)

    assert seen_params[0] == "100", "cold-start cursor must be forwarded to harness"


async def test_stream_events_fires_on_progress_per_event():
    """on_progress is the bridge's hook to persist last_event_seq."""
    progress: list[int] = []
    events_seen = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal events_seen
        events_seen += 1
        seq = 50 + events_seen
        payload = {"sequence": seq, "event": "message", "data": {}}
        body = (f"event: message\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(_name: str, _data: dict) -> None:
        if len(progress) >= 2:
            raise asyncio.CancelledError

    async def on_progress(seq: int) -> None:
        progress.append(seq)

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, on_progress=on_progress)

    assert progress == [51, 52]


async def test_probe_current_sequence_returns_highest_seq_and_stops_idle():
    """Probe drains the SSE replay quickly and returns the highest seq."""
    async def handler(req: httpx.Request) -> httpx.Response:
        body = (
            "event: session.updated\n"
            "data: " + json.dumps({"sequence": 7, "event": "session.updated"}) + "\n\n"
            "event: message\n"
            "data: " + json.dumps({"sequence": 12, "event": "message"}) + "\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    client = _client(handler)
    seq = await client.probe_current_sequence(idle_window=0.05, hard_timeout=1.0)
    assert seq == 12


async def test_probe_current_sequence_returns_zero_on_empty_stream():
    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = _client(handler)
    seq = await client.probe_current_sequence(idle_window=0.05, hard_timeout=0.5)
    assert seq == 0


async def test_probe_current_sequence_propagates_connect_failure():
    """Connect-time HTTP failure must NOT be silently turned into 0 — the
    bootstrap + reconnect reset paths both interpret a successful probe of 0
    as 'harness was restarted, replay from 0'. Returning 0 on a transient
    network blip would cause spurious replays."""
    async def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connect failure")

    client = _client(handler)
    with pytest.raises(httpx.HTTPError):
        await client.probe_current_sequence(idle_window=0.05, hard_timeout=0.5)


async def test_probe_current_sequence_propagates_5xx_status():
    """5xx response on the events stream must propagate so callers can
    distinguish 'harness sequence is 0' from 'harness errored on probe'."""
    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal error")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.probe_current_sequence(idle_window=0.05, hard_timeout=0.5)


async def test_stream_events_skips_reset_when_probe_raises(monkeypatch):
    """If the post-disconnect probe raises (transient network, harness 5xx),
    we cannot confidently say the harness was reset. Skip the cursor reset
    and the on_reset callback rather than blast-replaying from 0."""
    requests: list[tuple[str, str | None]] = []
    events_seen: list[int] = []
    reset_called = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append((str(req.url), req.url.params.get("after")))
        attempt = len(requests)
        if attempt == 1:
            return httpx.Response(
                200,
                content=(
                    f"event: message\ndata: {json.dumps({'sequence': 8001, 'event': 'message'})}\n\n"
                ).encode(),
            )
        if attempt == 2:
            raise httpx.ReadError("simulated harness disconnect")
        return httpx.Response(
            200,
            content=(
                f"event: message\ndata: {json.dumps({'sequence': 8002, 'event': 'message'})}\n\n"
            ).encode(),
        )

    client = _client(handler)

    async def fake_probe(**_kwargs):
        raise httpx.ConnectError("probe failed")

    monkeypatch.setattr(client, "probe_current_sequence", fake_probe)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    async def on_event(_name: str, data: dict) -> None:
        events_seen.append(data.get("sequence"))
        if len(events_seen) >= 2:
            raise asyncio.CancelledError

    async def on_reset() -> None:
        nonlocal reset_called
        reset_called += 1

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(
            on_event, after_sequence=8000, on_reset=on_reset,
        )

    assert events_seen == [8001, 8002]
    assert reset_called == 0, "on_reset must not fire when probe could not confirm a reset"
    # Reconnect must resume from latest observed seq, not from 0.
    assert requests[2][1] == "8001", f"expected after=8001 on reconnect, got {requests[2][1]!r}"


async def test_stream_events_preserves_cursor_across_reconnects():
    """Reconnect after a mid-stream error must continue from the latest seq
    observed — NOT from the original ``after_sequence``. Regression for the
    2026-05-12 cutover #3 bug where every 30s SSE ReadTimeout caused a
    reconnect with the stale cold-start cursor, re-streaming every event
    in between and re-mirroring user messages."""
    requests: list[str] = []
    events_seen: list[int] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        if len(requests) == 1:
            # First connection: emit two events, then close.
            payload_a = {"sequence": 101, "event": "message", "data": {}}
            payload_b = {"sequence": 102, "event": "message", "data": {}}
            body = (
                f"event: message\ndata: {json.dumps(payload_a)}\n\n"
                f"event: message\ndata: {json.dumps(payload_b)}\n\n"
            ).encode()
            return httpx.Response(200, content=body)
        # Second connection: must come with after=102, not the original 100.
        assert req.url.params.get("after") == "102", (
            f"expected after=102 on reconnect, got after="
            f"{req.url.params.get('after')!r}"
        )
        payload = {"sequence": 103, "event": "message", "data": {}}
        body = (f"event: message\ndata: {json.dumps(payload)}\n\n").encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(_name: str, data: dict) -> None:
        events_seen.append(data.get("sequence"))
        if len(events_seen) >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event, after_sequence=100)

    assert events_seen == [101, 102, 103]
    assert requests[0].endswith("after=100")
    assert "after=102" in requests[1]


async def test_stream_events_resets_cursor_on_detected_harness_restart(monkeypatch):
    """Mid-session harness restart: in-memory event bus rolls back to a low
    sequence. The bridge's cursor is now ahead of harness max — without
    detection, the bridge would silently skip every event the new harness
    emits until it catches back up. We detect on reconnect by probing the
    harness max, and if cursor > max, reset to 0 + fire on_reset."""
    requests: list[tuple[str, str | None]] = []
    events_seen: list[int] = []
    reset_called = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append((str(req.url), req.url.params.get("after")))
        attempt = len(requests)
        # 1: emit two events then drop the connection mid-stream
        if attempt == 1:
            return httpx.Response(
                200,
                content=(
                    f"event: message\ndata: {json.dumps({'sequence': 8001, 'event': 'message'})}\n\n"
                    f"event: message\ndata: {json.dumps({'sequence': 8002, 'event': 'message'})}\n\n"
                ).encode(),
            )
        if attempt == 2:
            raise httpx.ReadError("simulated harness disconnect")
        # 3: reconnect after probe — must use after=0 (cursor reset).
        return httpx.Response(
            200,
            content=(
                f"event: message\ndata: {json.dumps({'sequence': 13, 'event': 'message'})}\n\n"
            ).encode(),
        )

    client = _client(handler)

    # Stub probe_current_sequence to return a low value, simulating
    # a freshly-restarted harness whose bus is below our cursor.
    async def fake_probe(**_kwargs):
        return 12

    monkeypatch.setattr(client, "probe_current_sequence", fake_probe)
    # No-op sleep so the test doesn't pause 2s on the disconnect retry.
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    async def on_event(_name: str, data: dict) -> None:
        events_seen.append(data.get("sequence"))
        if len(events_seen) >= 3:
            raise asyncio.CancelledError

    async def on_reset() -> None:
        nonlocal reset_called
        reset_called += 1

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(
            on_event, after_sequence=8000, on_reset=on_reset,
        )

    assert events_seen == [8001, 8002, 13]
    assert reset_called == 1
    # Third connect (after probe + reset) must request from 0.
    assert requests[2][1] == "0", f"expected after=0 on reset, got {requests[2][1]!r}"


async def test_stream_events_ignores_sse_comment_keepalive_frames():
    """The harness emits ``: ka`` comment frames during bus silence. The
    SSE parser must skip them entirely — they must not show up as events,
    must not perturb the next real event's parsing, and must not break
    cursor tracking on the surrounding event."""
    requests: list[str] = []
    events: list[tuple[str, dict]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        # Interleave keepalive comments before, between, and after a real
        # event. Both ``: ka`` and ``:ka`` (no space) are valid SSE
        # comments — exercise both.
        body = (
            ": ka\n\n"
            f"event: message\n"
            f"data: {json.dumps({'sequence': 7, 'event': 'message', 'data': {}})}\n\n"
            ":ka\n\n"
            f"event: message\n"
            f"data: {json.dumps({'sequence': 8, 'event': 'message', 'data': {}})}\n\n"
        ).encode()
        return httpx.Response(200, content=body)

    client = _client(handler)

    async def on_event(name: str, data: dict) -> None:
        events.append((name, data))
        if len(events) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(on_event)

    assert [name for name, _ in events] == ["message", "message"]
    assert [data.get("sequence") for _, data in events] == [7, 8]


async def test_stream_once_uses_finite_read_timeout():
    """The SSE stream must run with a finite read timeout so a silently
    stuck stream (e.g. our cursor "in the future" after a harness restart,
    or a half-open TCP connection) eventually trips ``httpx.ReadTimeout``
    and falls into the reconnect+reset path. Regression for the 2026-05-13
    incident: ``read=None`` left the bridge listening forever to a stream
    that the new harness silently filtered to zero events."""
    captured: dict[str, object] = {}

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = _client(handler)

    original = client._http.stream

    def capture_stream(method, url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return original(method, url, **kwargs)

    # Patch on the instance so MockTransport still runs.
    object.__setattr__(client._http, "stream", capture_stream)

    cursor: list[int | None] = [None]
    await client._stream_once(lambda *_: None, cursor, None)

    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read is not None and timeout.read == SSE_READ_TIMEOUT_SECONDS


async def test_stream_events_resets_cursor_on_read_timeout(monkeypatch):
    """A ``httpx.ReadTimeout`` (idle-stream death) must reach the same
    reconnect + sequence-reset probe as ``httpx.ReadError``. With the
    bridge's finite read timeout, this is the new primary recovery path
    for harness restarts that go undetected because the reset rolls the
    bus back without dropping the TCP connection."""
    requests: list[tuple[str, str | None]] = []
    events_seen: list[int] = []
    reset_called = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        requests.append((str(req.url), req.url.params.get("after")))
        attempt = len(requests)
        if attempt == 1:
            return httpx.Response(
                200,
                content=(
                    f"event: message\ndata: {json.dumps({'sequence': 9001, 'event': 'message'})}\n\n"
                ).encode(),
            )
        if attempt == 2:
            raise httpx.ReadTimeout("simulated idle-stream timeout")
        return httpx.Response(
            200,
            content=(
                f"event: message\ndata: {json.dumps({'sequence': 5, 'event': 'message'})}\n\n"
            ).encode(),
        )

    client = _client(handler)

    async def fake_probe(**_kwargs):
        return 4

    monkeypatch.setattr(client, "probe_current_sequence", fake_probe)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    async def on_event(_name: str, data: dict) -> None:
        events_seen.append(data.get("sequence"))
        if len(events_seen) >= 2:
            raise asyncio.CancelledError

    async def on_reset() -> None:
        nonlocal reset_called
        reset_called += 1

    with pytest.raises(asyncio.CancelledError):
        await client.stream_events(
            on_event, after_sequence=9000, on_reset=on_reset,
        )

    assert events_seen == [9001, 5]
    assert reset_called == 1
    assert requests[2][1] == "0", f"expected after=0 on reset, got {requests[2][1]!r}"


async def _noop_sleep(_seconds):  # pragma: no cover — helper
    return None
