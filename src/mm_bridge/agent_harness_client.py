"""agent-harness HTTP + SSE client."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


_BACKEND_WIRE: dict[str, str] = {"claude": "claude-code"}

# Idle read timeout on the SSE stream. The harness emits an SSE keepalive
# comment (`:ka`) every ~15s; missing several in a row almost certainly
# means the connection is dead or the harness is stuck. The reconnect
# path that fires on `httpx.ReadTimeout` also runs the sequence-reset
# probe, so this doubles as the recovery trigger for the "stale future
# cursor" case where the bus filters every event into silence.
SSE_READ_TIMEOUT_SECONDS = 45.0


def _wire_backend(name: str) -> str:
    return _BACKEND_WIRE.get(name.lower(), name)


class HarnessError(Exception):
    """Base class for typed agent-harness errors."""


class HarnessSessionNotFound(HarnessError):
    """The requested session does not exist."""


class HarnessRunNotFound(HarnessError):
    """The requested run does not exist or is already terminal."""


class HarnessResumeUnsupported(HarnessError):
    """The harness cannot create a run for this session."""


class HarnessInterruptUnsupported(HarnessError):
    """The harness cannot interrupt this run."""


class HarnessForkUnsupported(HarnessError):
    """The harness cannot fork this session/backend."""


class AgentHarnessClient:
    """Async client for agent-harness REST API and SSE event stream."""

    def __init__(
        self,
        base_url: str,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30,
            transport=_transport,
        )

    @classmethod
    def with_transport(
        cls,
        base_url: str,
        transport: httpx.AsyncBaseTransport,
    ) -> AgentHarnessClient:
        return cls(base_url, _transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    async def create_session(
        self,
        *,
        backend: str,
        model: str | None,
        cwd: str,
        title: str | None = None,
        bypass_permissions: bool = True,
        effort: str | None = None,
    ) -> dict:
        # Default ``bypass_permissions=True``: every session this client
        # creates is on behalf of a Mattermost user who can't see — let
        # alone click "Allow" on — Claude Code / Codex permission prompts.
        # Without this flag the harness omits ``--dangerously-skip-permissions``
        # from the spawned argv and tool calls silently stall waiting for
        # an approval that will never come. Callers can override to False
        # only if they have an out-of-band channel for permission UX.
        payload: dict = {
            "backend": _wire_backend(backend),
            "project": {"path": cwd, "name": Path(cwd).name},
            "bypass_permissions": bypass_permissions,
        }
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title
        # Omitted when unset so the harness leaves the flag off the argv
        # entirely and each backend CLI applies its own configured default.
        if effort is not None:
            payload["effort"] = effort

        resp = await self._http.post("/v1/sessions", json=payload)
        self._raise_for_status(resp)
        return resp.json()

    async def update_session(
        self,
        session_id: str,
        *,
        effort: str | None = None,
    ) -> dict:
        """PATCH a LIVE session's mutable settings, keeping its conversation.

        Only fields explicitly passed are sent. The harness treats an omitted
        field as "leave unchanged" and REJECTS an explicit null, so there is
        no way to clear a setting back to unset through this route.

        WHY this exists rather than a restart: the harness rebuilds the
        backend argv fresh on every run, so a PATCH lands on the next turn
        with the conversation intact — unlike `model`/`backend`/`cwd`, which
        have no mutate endpoint and force a destroy-and-recreate.
        """
        payload: dict = {}
        if effort is not None:
            payload["effort"] = effort

        resp = await self._http.patch(f"/v1/sessions/{session_id}", json=payload)
        if resp.status_code == 404:
            raise HarnessSessionNotFound(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def create_run(self, session_id: str, message: str) -> dict:
        resp = await self._http.post(
            f"/v1/sessions/{session_id}/runs",
            json={"message": message},
        )
        if resp.status_code == 409:
            raise HarnessResumeUnsupported(_error_detail(resp))
        if resp.status_code == 404:
            raise HarnessSessionNotFound(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def fork_session(
        self,
        session_id: str,
        *,
        message: str | None,
        title: str | None = None,
    ) -> dict:
        payload: dict = {}
        if message is not None:
            payload["message"] = message
        if title is not None:
            payload["title"] = title

        resp = await self._http.post(f"/v1/sessions/{session_id}/forks", json=payload)
        if resp.status_code in (404, 409):
            raise HarnessForkUnsupported(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def interrupt_run(self, session_id: str, run_id: str) -> dict:
        resp = await self._http.delete(f"/v1/sessions/{session_id}/runs/{run_id}")
        if resp.status_code == 409:
            raise HarnessInterruptUnsupported(_error_detail(resp))
        if resp.status_code == 404:
            raise HarnessRunNotFound(_error_detail(resp))
        self._raise_for_status(resp)
        return resp.json()

    async def get_session(self, session_id: str) -> dict | None:
        resp = await self._http.get(f"/v1/sessions/{session_id}")
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        return resp.json()

    async def get_run(self, session_id: str, run_id: str) -> dict | None:
        resp = await self._http.get(f"/v1/sessions/{session_id}/runs/{run_id}")
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        return resp.json()

    async def list_sessions(self) -> list[dict]:
        resp = await self._http.get("/v1/sessions")
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def list_session_runs(self, session_id: str) -> list[dict]:
        resp = await self._http.get(f"/v1/sessions/{session_id}/runs")
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def list_backend_models(self, backend: str) -> list[str]:
        try:
            resp = await self._http.get(f"/v1/backends/{_wire_backend(backend)}/models")
            if resp.status_code == 404:
                return []
            self._raise_for_status(resp)
            return resp.json().get("data", []) or []
        except httpx.HTTPError as exc:
            logger.warning("list_backend_models(%s) failed: %s", backend, exc)
            return []

    async def list_session_messages(self, session_id: str) -> list[dict]:
        resp = await self._http.get(f"/v1/sessions/{session_id}/messages")
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def health(self) -> dict:
        resp = await self._http.get("/v1/health")
        self._raise_for_status(resp)
        return resp.json()

    async def probe_current_sequence(
        self,
        *,
        idle_window: float = 0.5,
        hard_timeout: float = 5.0,
    ) -> int:
        """Briefly tail ``/v1/events`` to find the highest sequence so far.

        The harness stream emits its full history on connect — there is no
        documented cursor endpoint (``?from=now`` is presently ignored;
        verified 2026-05-12). We open a stream, read until ``idle_window``
        seconds pass with no fresh event, and return the highest sequence
        observed. The caller uses that as ``after_sequence`` so new
        connections only see events that arrived after this point.

        ``hard_timeout`` is a safety ceiling for the probe overall. Returns
        ``0`` when no events were observed (e.g. empty harness).

        Connect-time and HTTP-status failures propagate as ``httpx.HTTPError``
        so callers can distinguish "harness reported 0 events" from "probe
        could not confirm anything". Both the bootstrap and reconnect-reset
        paths interpret a confirmed 0 as a harness restart; silently
        returning 0 on a network blip caused spurious cursor resets and
        replay floods.
        """
        max_seq = 0
        loop = asyncio.get_event_loop()
        start = loop.time()
        last_recv = start
        async with self._http.stream(
            "GET", "/v1/events", timeout=hard_timeout,
        ) as resp:
            resp.raise_for_status()
            data_buf = ""
            try:
                async for line in resp.aiter_lines():
                    now = loop.time()
                    if line.startswith("data:"):
                        data_buf += line[5:].strip()
                    elif line == "" and data_buf:
                        try:
                            payload = json.loads(data_buf)
                            seq = payload.get("sequence")
                            if isinstance(seq, int) and seq > max_seq:
                                max_seq = seq
                                last_recv = now
                        except json.JSONDecodeError:
                            pass
                        data_buf = ""
                    if now - last_recv > idle_window or now - start > hard_timeout:
                        break
            except (httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                # Mid-stream interruption after a successful connect — return
                # what we've already observed. This is the "bus went idle"
                # path; connect-time errors raised above never reach here.
                logger.debug("probe_current_sequence mid-stream: %s (max_seq=%d)", exc, max_seq)
        return max_seq

    async def stream_events(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        *,
        after_sequence: int | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
        on_reset: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        # ``last_sequence`` is updated by ``_stream_once`` via an inline mutable
        # cell so transient errors mid-stream don't erase the cursor —
        # otherwise a 30s idle ReadTimeout would replay every event since
        # ``after_sequence`` on each reconnect, re-mirroring user messages.
        #
        # ``on_reset`` fires when we detect the harness restarted mid-session
        # (cursor > harness's current max sequence). Caller uses it to
        # update its persisted cursor; this client also resets the in-memory
        # cursor to 0 so events that landed in the new harness's in-memory
        # bus are picked up.
        cursor: list[int | None] = [after_sequence]
        while True:
            try:
                await self._stream_once(on_event, cursor, on_progress)
            except (
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.ConnectError,
            ) as exc:
                logger.warning(
                    "SSE connection lost at seq=%s (%s), reconnecting in 2s...",
                    cursor[0], exc,
                )
                await self._maybe_reset_cursor_on_harness_restart(cursor, on_reset)
                await asyncio.sleep(2)
            except Exception:
                logger.exception(
                    "SSE stream error at seq=%s, reconnecting in 5s...", cursor[0],
                )
                await self._maybe_reset_cursor_on_harness_restart(cursor, on_reset)
                await asyncio.sleep(5)

    async def _maybe_reset_cursor_on_harness_restart(
        self,
        cursor: list[int | None],
        on_reset: Callable[[], Awaitable[None]] | None,
    ) -> None:
        current = cursor[0]
        if not isinstance(current, int) or current <= 0:
            return
        try:
            harness_max = await self.probe_current_sequence()
        except Exception:
            logger.debug("Reset probe failed during SSE reconnect", exc_info=True)
            return
        if current <= harness_max:
            return
        logger.warning(
            "Detected harness sequence reset during SSE reconnect "
            "(cursor=%d > harness_max=%d). Resetting cursor to 0 to pick "
            "up events from the new harness's in-memory bus.",
            current, harness_max,
        )
        cursor[0] = 0
        if on_reset is not None:
            try:
                await on_reset()
            except Exception:
                logger.exception("on_reset callback failed")

    async def _stream_once(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        cursor: list[int | None],
        on_progress: Callable[[int], Awaitable[None]] | None,
    ) -> None:
        params = (
            {"after": str(cursor[0])} if cursor[0] is not None else None
        )
        # Finite read timeout pairs with the harness's `:ka` keepalive
        # comment (emitted every ~15s). A healthy idle stream still
        # produces bytes well within ``SSE_READ_TIMEOUT_SECONDS``; only
        # a truly silent stream (dead connection, stuck server, or our
        # cursor "in the future" after a harness restart) trips this
        # and triggers the reconnect + reset-probe path.
        stream_timeout = httpx.Timeout(
            connect=10.0,
            read=SSE_READ_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        )
        async with self._http.stream(
            "GET", "/v1/events", params=params, timeout=stream_timeout,
        ) as resp:
            resp.raise_for_status()
            event_type = ""
            data_buf = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_buf += line[5:].strip()
                elif line == "":
                    if data_buf:
                        cursor[0] = await self._dispatch_sse_event(
                            event_type, data_buf, on_event, cursor[0],
                            on_progress,
                        )
                    event_type = ""
                    data_buf = ""

    async def _dispatch_sse_event(
        self,
        event_type: str,
        data_buf: str,
        on_event: Callable[[str, dict], Awaitable[None]],
        after_sequence: int | None,
        on_progress: Callable[[int], Awaitable[None]] | None,
    ) -> int | None:
        try:
            data = json.loads(data_buf)
        except json.JSONDecodeError:
            logger.warning("Bad JSON in SSE event %s: %s", event_type, data_buf[:200])
            return after_sequence

        sequence = data.get("sequence")
        if isinstance(sequence, int):
            after_sequence = max(after_sequence or sequence, sequence)

        await on_event(data.get("event") or event_type, data)
        if on_progress is not None and isinstance(sequence, int):
            try:
                await on_progress(sequence)
            except Exception:
                logger.exception("on_progress callback failed for seq=%s", sequence)
        return after_sequence

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if not resp.is_error:
            return
        detail = _error_detail(resp)
        raise httpx.HTTPStatusError(
            f"agent-harness {resp.request.method} {resp.request.url.path} "
            f"-> {resp.status_code}: {detail}",
            request=resp.request,
            response=resp,
        )


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:500]

    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        detail = error.get("detail") or error.get("message")
        if code and detail:
            return f"{code}: {detail}"
        if code:
            return str(code)
        if detail:
            return str(detail)
    detail = body.get("detail")
    if detail:
        return str(detail)
    return str(body)[:500]
