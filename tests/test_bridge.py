"""Bridge dispatch tests — use fake MM/VD clients and drive events in."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from mm_bridge.bridge import Bridge
from mm_bridge.config import Anchor, Config

# The fake MM / agent-harness clients, the active ``EventEchoingMattermostClient``
# double, and ``make_bridge`` live in ``doubles.py`` so any test module can reuse
# them (see Item 2 of the drop-first-message-config work).
from doubles import (
    EventEchoingMattermostClient,
    FakeAgentHarnessClient,
    FakeMattermostClient,
)

# ───────────────────── Test fixtures ──────────────────────────────────────


class _BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # type: ignore[override]
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            mm_bot_token="t",
            default_cwd="/tmp/proj",
            state_file=f"{self.tmp.name}/state.json",
            sidecar_dir=f"{self.tmp.name}/sidecar",
            default_backend="claude",
        )
        self.bridge = Bridge(self.config)
        self.bridge.mm = FakeMattermostClient()
        self.bridge.harness = FakeAgentHarnessClient()
        self.bridge.vd = self.bridge.harness
        self.bridge.warming_up_sessions = self.bridge.warming_up_sessions

        async def _legacy_event(event_type: str, data: dict) -> None:
            if event_type == "session_added":
                # VD's session_added semantically meant "a CLI-launched
                # session became visible" — always external in the harness
                # model. Synthesize origin so the new channel-spawn filter
                # accepts the translated event.
                session = {
                    "id": data.get("id") or data.get("session_id"),
                    "backend": data.get("backend"),
                    "project": {
                        "path": data.get("projectPath"),
                        "name": data.get("projectName") or data.get("project") or "",
                    },
                    "title": data.get("summaryTitle"),
                    "origin": "external",
                }
                await self.bridge._on_harness_event(
                    "session.updated",
                    {"data": {"session_id": session["id"], "session": session}},
                )
                return
            if event_type == "message":
                await self.bridge._on_harness_event("message", {"data": data})
                return
            if event_type == "session_status":
                lifecycle = "run.started" if data.get("running") else "run.completed"
                await self.bridge._on_harness_event(lifecycle, {"data": data})

        async def _legacy_session_added(data: dict) -> None:
            await _legacy_event("session_added", data)

        self.bridge._on_vd_event = _legacy_event
        self.bridge._on_vd_session_added = _legacy_session_added
        from mm_bridge.typing_indicator import TypingIndicator
        self.bridge.typing = TypingIndicator(self.bridge.mm, refresh_s=0.01)

    async def asyncTearDown(self):  # type: ignore[override]
        self.tmp.cleanup()


# ───────────────────── Tests ──────────────────────────────────────────────


class AgentHarnessBridgeTests(_BridgeTestCase):
    async def test_forwarded_post_creates_run_and_tracks_run_id(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        await self.bridge._on_mm_posted({
            "id": "p1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "hello harness",
        })

        self.assertEqual(self.bridge.harness.sent, [("codex_s1", "hello harness")])
        self.assertEqual(
            self.bridge.current_run_id_by_session["codex_s1"],
            "run-1",
        )

    async def test_run_completed_clears_current_run_id(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")
        self.bridge.current_run_id_by_session["codex_s1"] = "run-1"

        await self.bridge._on_harness_event(
            "run.completed",
            {"data": {"session_id": "codex_s1", "run_id": "run-1"}},
        )

        self.assertNotIn("codex_s1", self.bridge.current_run_id_by_session)

    async def test_session_updated_unknown_session_creates_channel_once(self):
        session = {
            "id": "codex_new",
            "backend": "codex",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "External project",
            "origin": "external",
        }

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "codex_new", "session": session}},
        )
        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "codex_new", "session": session}},
        )

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(self.bridge.mapping.get_anchor("codex_new"))

    async def test_bootstrap_creates_channel_for_unmapped_external_session(self):
        self.bridge.harness.sessions_meta = [{
            "id": "codex_existing",
            "backend": "codex",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Existing external",
            "origin": "external",
        }]

        await self.bridge._bootstrap_known_sessions()

        self.assertIsNotNone(self.bridge.mapping.get_anchor("codex_existing"))
        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(len(created), 1)

    async def test_bootstrap_marks_harness_origin_sessions_as_known(self):
        """Pre-existing harness-origin sessions (test leftovers, prior
        spawn-CLI records whose mapping survived in state.json, or future
        IPC-created sessions) must be marked ``known`` at bootstrap time
        WITHOUT auto-creating an MM channel.

        Without this, the SSE bootstrap replay (especially after a cursor
        reset following a detected harness restart) would re-emit
        ``session.updated`` for every leftover row and trip the live-create
        path in ``_on_harness_session_seen`` — the 2026-05-12 ghost-channel
        burst. The post-spawn-fix invariant is: live ``session.updated``
        events freely spawn channels, but bootstrap pre-registers every
        already-existing session as known so the replay can't double-spawn."""
        self.bridge.harness.sessions_meta = [{
            "id": "ses_harness_leftover",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Ghost leftover",
            "origin": "harness",
        }]

        await self.bridge._bootstrap_known_sessions()

        # Marked known so the SSE replay's session.updated is a no-op.
        self.assertIn("ses_harness_leftover", self.bridge._known_sessions)
        # But NOT auto-mapped to a channel — only external sessions get
        # the bootstrap-time auto-spawn treatment.
        self.assertIsNone(self.bridge.mapping.get_anchor("ses_harness_leftover"))
        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])

        # And the SSE bootstrap-replay of the same session.updated must
        # remain a no-op now (no channel-create from the live handler).
        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {
                "session_id": "ses_harness_leftover",
                "session": self.bridge.harness.sessions_meta[0],
            }},
        )
        created_after = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created_after, [])

    async def test_failed_channel_create_does_not_block_future_retry(self):
        """A transient MM failure on first ``session.updated`` must NOT
        leave the session permanently in ``_known_sessions`` — otherwise
        the bridge silently abandons it. Subsequent ``session.updated``
        events for the same id must trigger another channel-create
        attempt."""
        session = {
            "id": "codex_retry",
            "backend": "codex",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }
        self.bridge.mm.fail_create_channel = True

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session": session}, "session_id": "codex_retry"},
        )

        self.assertNotIn("codex_retry", self.bridge._known_sessions)
        self.assertIsNone(self.bridge.mapping.get_anchor("codex_retry"))

        self.bridge.mm.fail_create_channel = False
        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session": session}, "session_id": "codex_retry"},
        )

        self.assertIn("codex_retry", self.bridge._known_sessions)
        self.assertIsNotNone(self.bridge.mapping.get_anchor("codex_retry"))

    async def test_bootstrap_does_not_recreate_already_mapped_session(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_existing")
        self.bridge.harness.sessions_meta = [{
            "id": "codex_existing",
            "backend": "codex",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Existing external",
            "origin": "external",
        }]

        await self.bridge._bootstrap_known_sessions()

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])
        self.assertIn("codex_existing", self.bridge._known_sessions)

    async def test_bootstrap_skips_external_sessions_when_mirroring_disabled(self):
        """``mirror_external_sessions = false``: bootstrap must NOT create
        channels for pre-existing external (CLI/terminal) sessions. On a
        shared MM server every such channel is public and auto-joining
        bots start answering into it — the operator opts out wholesale."""
        self.bridge.config.mirror_external_sessions = False
        self.bridge.harness.sessions_meta = [{
            "id": "ses_terminal",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Terminal session",
            "origin": "external",
        }]

        await self.bridge._bootstrap_known_sessions()

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])
        self.assertIsNone(self.bridge.mapping.get_anchor("ses_terminal"))
        # Marked known so the SSE bootstrap replay can't trip the
        # live-create path for the same session either.
        self.assertIn("ses_terminal", self.bridge._known_sessions)

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {
                "session_id": "ses_terminal",
                "session": self.bridge.harness.sessions_meta[0],
            }},
        )
        created_after = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created_after, [])

    async def test_live_external_session_skipped_when_mirroring_disabled(self):
        """A NEW terminal session appearing while the daemon runs must be
        skipped too — bootstrap-only filtering would still mirror every
        session started after daemon boot."""
        self.bridge.config.mirror_external_sessions = False
        session = {
            "id": "ses_live_terminal",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Live terminal session",
            "origin": "external",
        }

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_live_terminal", "session": session}},
        )

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])
        self.assertIsNone(self.bridge.mapping.get_anchor("ses_live_terminal"))
        # Known, so repeated session.updated events don't retry-create.
        self.assertIn("ses_live_terminal", self.bridge._known_sessions)

    async def test_spawned_harness_session_still_gets_channel_when_mirroring_disabled(self):
        """The knob must never break Mattermost-initiated flows:
        ``mm-bridge spawn`` creates a harness-origin session and relies on
        the live path spawning its channel (otherwise the spawn CLI's
        ``_wait_for_new_sidecar`` times out)."""
        self.bridge.config.mirror_external_sessions = False
        session = {
            "id": "ses_spawned",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "title": "Spawned session",
            "origin": "harness",
        }

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_spawned", "session": session}},
        )

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(self.bridge.mapping.get_anchor("ses_spawned"))

    async def test_harness_origin_session_spawns_channel_when_unknown(self):
        """A fresh harness-origin ``session.updated`` (e.g. from ``mm-bridge
        spawn``, an integration test, or a future IPC client that created
        the session via the harness API) DOES auto-spawn an MM channel —
        otherwise the spawn CLI's ``_wait_for_new_sidecar`` times out and
        the new session is left orphaned with no channel.

        Pre-existing harness-origin sessions are protected from re-spawn
        by ``_bootstrap_known_sessions`` marking them ``known`` before the
        SSE replay reaches this handler; see
        ``test_bootstrap_marks_harness_origin_sessions_as_known``."""
        session = {
            "id": "ses_harness_origin",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "harness",
        }

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_harness_origin", "session": session}},
        )

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(len(created), 1)
        anchor = self.bridge.mapping.get_anchor("ses_harness_origin")
        self.assertIsNotNone(anchor)
        self.assertIn("ses_harness_origin", self.bridge._known_sessions)

    async def test_claude_agent_subagent_session_is_suppressed(self):
        """Claude-code subagent transcripts (``claude_agent-<hex>``) are internal
        to a parent run and must never spawn an MM channel. Regression for the
        retry-loop bug observed on 2026-05-12 where ~50 subagent transcripts
        each caused failed channel-create retries every SSE event."""
        session = {
            "id": "claude_agent-a08d0dc68b3b89d9a",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": session["id"], "session": session}},
        )

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])
        self.assertIsNone(self.bridge.mapping.get_anchor(session["id"]))
        self.assertIn(session["id"], self.bridge._known_sessions)

    async def test_repeated_create_failures_eventually_give_up(self):
        """A session whose channel-create permanently fails (collision, bad
        slug) must NOT loop forever on every SSE event. After
        ``MAX_CHANNEL_CREATE_ATTEMPTS`` the session is treated as known so
        retries stop. Regression for the W2 retry-loop flood."""
        from mm_bridge.bridge import MAX_CHANNEL_CREATE_ATTEMPTS
        session = {
            "id": "codex_perma_fail",
            "backend": "codex",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }
        self.bridge.mm.fail_create_channel = True

        for _ in range(MAX_CHANNEL_CREATE_ATTEMPTS):
            await self.bridge._on_harness_event(
                "session.updated",
                {"data": {"session": session}, "session_id": session["id"]},
            )

        self.assertIn(session["id"], self.bridge._known_sessions)
        # Once given up on, the per-session attempt counter is dropped — and
        # future events short-circuit on the ``_known_sessions`` membership
        # check before ever calling create_channel.
        self.assertNotIn(session["id"], self.bridge._channel_create_attempts)
        channels_before = len(self.bridge.mm.channels)
        self.bridge.mm.fail_create_channel = False  # would succeed if reached
        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session": session}, "session_id": session["id"]},
        )
        # No new channel created — short-circuited by ``_known_sessions``.
        self.assertEqual(len(self.bridge.mm.channels), channels_before)

    async def test_session_to_channel_name_strips_backend_prefix(self):
        """Channel slugs must include entropy from the actual session UUID,
        not just the backend prefix — otherwise all ``claude_agent-*`` (or
        any same-prefix sessions) collide on the same slug."""
        from mm_bridge.bridge import _session_to_channel_name
        a = _session_to_channel_name("claude_agent-a08d0dc68b3b89d9a")
        b = _session_to_channel_name("claude_agent-deadbeef12345678")
        self.assertNotEqual(a, b)

        a = _session_to_channel_name("claude_aa4bf742-4d79-45a6-a470")
        b = _session_to_channel_name("claude_bb5c6800-1234-1234-1234")
        self.assertNotEqual(a, b)

    async def test_bootstrap_suppresses_claude_agent_sessions(self):
        """``GET /v1/sessions`` may surface subagent transcripts on cold
        start; bootstrap must filter them just like the SSE path does."""
        self.bridge.harness.sessions_meta = [{
            "id": "claude_agent-deadbeef12345678",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }]

        await self.bridge._bootstrap_known_sessions()

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [])
        self.assertIn("claude_agent-deadbeef12345678", self.bridge._known_sessions)

    async def test_bootstrap_event_cursor_uses_persisted_seq(self):
        """Warm restart: bridge resumes SSE from the persisted cursor."""
        self.bridge.mapping.set_event_seq(4242)
        cursor = await self.bridge._bootstrap_event_cursor()
        self.assertEqual(cursor, 4242)

    async def test_bootstrap_event_cursor_resets_when_harness_sequence_reset(self):
        """Detected harness restart: persisted cursor is above the harness's
        current max sequence (the harness's in-memory event bus rolled back to
        0 on restart). The bridge must reset to 0 — otherwise it would skip
        every event the new harness emits until its sequence catches back up,
        silently dropping run events for active channels."""
        self.bridge.mapping.set_event_seq(8439)

        async def fake_probe(**_kwargs):
            return 2911  # well below persisted 8439 → harness was reset

        self.bridge.harness.probe_current_sequence = fake_probe  # type: ignore[assignment]
        cursor = await self.bridge._bootstrap_event_cursor()
        self.assertEqual(cursor, 0)
        # The reset must also persist so the next restart doesn't see the
        # stale value again.
        self.assertEqual(self.bridge.mapping.last_event_seq, 0)

    async def test_bootstrap_event_cursor_keeps_persisted_when_harness_seq_higher(self):
        """Normal warm restart: persisted cursor still <= harness max, resume
        from the persisted position."""
        self.bridge.mapping.set_event_seq(2900)

        async def fake_probe(**_kwargs):
            return 2911

        self.bridge.harness.probe_current_sequence = fake_probe  # type: ignore[assignment]
        cursor = await self.bridge._bootstrap_event_cursor()
        self.assertEqual(cursor, 2900)

    async def test_bootstrap_event_cursor_keeps_persisted_if_probe_fails(self):
        """If the probe blows up (harness down, network blip), fall back to
        the persisted value rather than guessing — better to risk skipping a
        few events than blast-replay everything every restart."""
        self.bridge.mapping.set_event_seq(4242)

        async def fake_probe(**_kwargs):
            raise RuntimeError("connection refused")

        self.bridge.harness.probe_current_sequence = fake_probe  # type: ignore[assignment]
        cursor = await self.bridge._bootstrap_event_cursor()
        self.assertEqual(cursor, 4242)

    async def test_bootstrap_event_cursor_probes_on_cold_start(self):
        """Cold start: bridge probes the harness for current max seq so
        only NEW events are streamed (no full-history replay)."""
        self.assertIsNone(self.bridge.mapping.last_event_seq)

        async def fake_probe(**_kwargs):
            return 7259

        self.bridge.harness.probe_current_sequence = fake_probe  # type: ignore[assignment]
        cursor = await self.bridge._bootstrap_event_cursor()
        self.assertEqual(cursor, 7259)
        # Probed value must be persisted so subsequent restarts also resume.
        self.assertEqual(self.bridge.mapping.last_event_seq, 7259)

    async def test_persist_event_seq_throttles_writes(self):
        """on_progress fires per SSE event; disk write is throttled."""
        import time as _t
        self.bridge._last_seq_flush_ts = _t.monotonic()
        await self.bridge._persist_event_seq(100)
        # Throttled → in-memory unchanged (cursor lives in mapping, only
        # written on flush).
        self.assertIsNone(self.bridge.mapping.last_event_seq)
        # Reset throttle window to force a flush.
        self.bridge._last_seq_flush_ts = 0.0
        await self.bridge._persist_event_seq(101)
        self.assertEqual(self.bridge.mapping.last_event_seq, 101)

    async def test_stop_flushes_pending_event_seq(self):
        """The 2s persist throttle means a clean shutdown inside the window
        leaves the latest seq pending only in memory; restart would replay
        up to ~2s of events. ``stop()`` must flush ``_pending_seq`` so the
        next boot resumes exactly where we left off."""
        self.bridge._pending_seq = 9999
        await self.bridge.stop()
        self.assertEqual(self.bridge.mapping.last_event_seq, 9999)
        # Idempotent: no pending value after flush.
        self.assertIsNone(self.bridge._pending_seq)

    async def test_stop_is_safe_with_no_pending_seq(self):
        """Stop without any pending seq must not regress the persisted
        cursor (or crash)."""
        self.bridge.mapping.set_event_seq(123)
        self.bridge._pending_seq = None
        await self.bridge.stop()
        self.assertEqual(self.bridge.mapping.last_event_seq, 123)

    async def test_bootstrap_tracks_mapped_external_sessions(self):
        """Bootstrap populates ``_external_sessions`` with mapped session
        ids whose harness ``origin`` is ``external``. Such sessions can't
        receive injected user turns, so ``_on_mm_posted`` must route
        around them rather than calling ``create_run``."""
        self.bridge.mapping.link(Anchor("c1"), "claude_legacy")
        self.bridge.harness.sessions_meta = [{
            "id": "claude_legacy",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }, {
            "id": "ses_managed",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "harness",
        }]
        self.bridge.mapping.link(Anchor("c2"), "ses_managed")

        await self.bridge._bootstrap_known_sessions()

        self.assertIn("claude_legacy", self.bridge._external_sessions)
        self.assertNotIn("ses_managed", self.bridge._external_sessions)

    async def test_bootstrap_tracks_external_sessions_after_channel_create(self):
        """External sessions that arrive at bootstrap WITHOUT an anchor go
        through ``_create_channel_for_session`` (which links them) — but
        they're still external, so the resulting channel cannot deliver
        injected user turns. The bridge must tag the new mapping in
        ``_external_sessions`` so the first MM reply routes through
        ``_replace_external_session`` instead of silently calling
        ``create_run`` on a session the harness has no stdin for."""
        self.bridge.harness.sessions_meta = [{
            "id": "claude_orphan",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }]
        # No anchor yet — bootstrap must create the channel.
        self.assertIsNone(self.bridge.mapping.get_anchor("claude_orphan"))

        await self.bridge._bootstrap_known_sessions()

        anchor = self.bridge.mapping.get_anchor("claude_orphan")
        self.assertIsNotNone(anchor, "bootstrap must create a channel")
        self.assertIn(
            "claude_orphan", self.bridge._external_sessions,
            "newly-mapped external sessions must be tagged so the first "
            "MM post triggers replacement rather than a vanishing run",
        )

        # End-to-end: a user post to the new channel must route via
        # _replace_external_session, NOT a silent create_run on the
        # un-reachable external session id.
        self.bridge.harness.next_session_id = "ses_fresh"
        self.bridge.mm.channels[anchor.channel_id] = {
            "id": anchor.channel_id, "purpose": "",
        }

        await self.bridge._on_mm_posted({
            "id": "p1",
            "channel_id": anchor.channel_id,
            "user_id": "u1",
            "message": "first post after restart",
            "type": "",
        })

        # Old external mapping is replaced.
        self.assertNotIn("claude_orphan", self.bridge._external_sessions)
        # Message landed on the fresh harness session, not the dead one.
        forwarded = list(self.bridge.harness.sent)
        self.assertTrue(
            any(sid == "ses_fresh" and "first post after restart" in body
                for sid, body in forwarded),
            f"expected user message on the fresh session; got {forwarded}",
        )
        self.assertFalse(
            any(sid == "claude_orphan" for sid, _ in forwarded),
            "no create_run to the dead external session",
        )

    async def test_bootstrap_skips_adopted_external_sessions(self):
        """Once an external session has been replaced via adoption, the
        harness still lists it (sessions aren't deleted) — bootstrap
        must NOT auto-spawn a fresh recovery channel for it on restart.
        Regression for 2026-05-12 where a restart after adoption created
        an extra orphan channel for the replaced session id."""
        self.bridge.mapping.mark_adopted("claude_already_adopted")
        self.bridge.harness.sessions_meta = [{
            "id": "claude_already_adopted",
            "backend": "claude-code",
            "project": {"path": "/tmp/project", "name": "project"},
            "origin": "external",
        }]

        await self.bridge._bootstrap_known_sessions()

        created = [c for c in self.bridge.mm.channels if c.startswith("c-s-")]
        self.assertEqual(created, [], "no recovery channel for adopted session")
        self.assertIsNone(
            self.bridge.mapping.get_anchor("claude_already_adopted"),
        )
        self.assertIn("claude_already_adopted", self.bridge._known_sessions)

    async def test_external_session_replacement_failure_preserves_state(self):
        """If ``_start_invited_session`` fails to bring up a fresh session
        (harness error, MM outage, etc.), ``_replace_external_session``
        must NOT permanently break the channel:

        - The channel mapping must remain pointing at the old external
          session so the next MM post can retry the replacement.
        - ``_external_sessions`` must still contain the old session so
          the retry path takes ``_replace_external_session`` rather than
          a silent ``create_run`` against the un-reachable session.
        - ``mark_adopted`` must NOT have been called, otherwise the next
          bootstrap would refuse to spawn a recovery channel for it.

        Without this, a transient harness failure during replacement
        would require manual state.json surgery to recover.
        """
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mapping.link(Anchor("c1"), "claude_dead")
        self.bridge._external_sessions.add("claude_dead")
        self.bridge._known_sessions.add("claude_dead")

        # Inject a harness failure on the next create_session call.
        original_create = self.bridge.harness.create_session

        async def boom(**_kwargs):
            raise RuntimeError("simulated harness create_session failure")

        self.bridge.harness.create_session = boom  # type: ignore[assignment]
        try:
            await self.bridge._on_mm_posted({
                "id": "p1",
                "channel_id": "c1",
                "user_id": "u1",
                "message": "still here?",
                "type": "",
            })
        finally:
            self.bridge.harness.create_session = original_create  # type: ignore[assignment]

        # Old mapping preserved for retry.
        self.assertEqual(
            self.bridge.mapping.get_session(Anchor("c1")), "claude_dead",
            "channel must remain mapped to the dead session so retry routes "
            "through _replace_external_session again",
        )
        self.assertIn(
            "claude_dead", self.bridge._external_sessions,
            "external-session tag must survive a failed replacement",
        )
        self.assertNotIn(
            "claude_dead", self.bridge.mapping.adopted_session_ids,
            "mark_adopted must NOT fire when no fresh session was linked",
        )
        # Channel was not left in a warming-up state.
        self.assertNotIn("c1", self.bridge.warming_up_sessions)

    async def test_external_session_replaced_on_user_post(self):
        """When a channel is mapped to a pre-cutover external session,
        an inbound MM message must NOT silently fail via ``create_run``.
        The bridge unmaps the dead session, creates a fresh harness
        session for the same channel, and the user's current message
        becomes the new session's first turn. Regression for the
        cutover-day silent-drop where 3 of 4 MM posts never reached the
        running Claude Code process."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mapping.link(Anchor("c1"), "claude_dead")
        self.bridge._external_sessions.add("claude_dead")
        self.bridge._known_sessions.add("claude_dead")
        self.bridge.harness.next_session_id = "ses_fresh"

        await self.bridge._on_mm_posted({
            "id": "p1",
            "channel_id": "c1",
            "user_id": "u1",
            "message": "still here?",
            "type": "",
        })

        # Old mapping is gone.
        self.assertNotIn("claude_dead", self.bridge._external_sessions)
        # Fresh harness session created and mapped to the channel.
        self.assertEqual(len(self.bridge.harness.created), 1)
        new_session_id = self.bridge.mapping.get_session(Anchor("c1"))
        self.assertEqual(new_session_id, "ses_fresh")
        # User's message routed to the NEW session, not the dead one.
        forwarded = [(sid, body) for sid, body in self.bridge.harness.sent]
        self.assertTrue(
            any(sid == "ses_fresh" and "still here?" in body
                for sid, body in forwarded),
            f"expected user message to land on the new session; got {forwarded}",
        )
        # Nothing was forwarded to the dead session.
        self.assertFalse(
            any(sid == "claude_dead" for sid, _ in forwarded),
            "no forwards to the dead external session",
        )
        # Persisted so a future bootstrap (after restart) won't try to
        # re-spawn a recovery channel for the dead session id.
        self.assertIn(
            "claude_dead", self.bridge.mapping.adopted_session_ids,
        )


class DormantChannelTests(_BridgeTestCase):
    """Manual invites and auto-joins share one pre-session lifecycle."""

    async def _join_dormant(self, *, auto_join: bool, purpose_text: str = "") -> None:
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": purpose_text, "display_name": "Test channel",
        }
        if auto_join:
            self.bridge._self_joined_channels.add("c1")
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

    async def test_manual_invite_stays_sessionless_until_engagement(self):
        await self._join_dormant(auto_join=False)

        self.assertIn("c1", self.bridge._dormant_channels)
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_manual_invite_can_configure_backend_and_model_before_session(self):
        await self._join_dormant(auto_join=False)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model gpt-5.4",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])
        self.assertIn("codex", self.bridge.mm.channels["c1"]["purpose"])
        self.assertIn("gpt-5.4", self.bridge.mm.channels["c1"]["purpose"])

        await self.bridge._on_mm_posted({
            "id": "engage-1", "channel_id": "c1",
            "message": "@claude inspect the failing build",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.harness.created), 1)
        self.assertEqual(self.bridge.harness.created[0]["backend"], "codex")
        self.assertEqual(self.bridge.harness.created[0]["model"], "gpt-5.4")
        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertTrue(
            self.bridge.harness.sent[0][1].endswith("inspect the failing build"),
        )
        self.assertNotIn("c1", self.bridge._dormant_channels)

    async def test_auto_join_backend_command_never_becomes_first_llm_turn(self):
        await self._join_dormant(auto_join=True, purpose_text="autorespond")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.bridge.purpose_by_channel["c1"].backend, "codex")
        self.assertIn("c1", self.bridge._dormant_channels)

    async def test_help_works_without_session_for_both_join_paths(self):
        for auto_join in (False, True):
            with self.subTest(auto_join=auto_join):
                await self._join_dormant(auto_join=auto_join)
                self.bridge.mm.posted.clear()

                await self.bridge._on_mm_posted({
                    "channel_id": "c1", "message": ".help",
                    "user_id": "u1", "type": "",
                })

                self.assertTrue(any(".backend" in p.message for p in self.bridge.mm.posted))
                self.assertTrue(any(
                    "Before the first session" in p.message
                    for p in self.bridge.mm.posted
                ))
                self.assertEqual(self.bridge.harness.created, [])

                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge.mm.posted.clear()

    async def test_bare_sensitive_command_is_silent_in_dormant_channel(self):
        await self._join_dormant(auto_join=False, purpose_text="autorespond")
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".sessions",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.mm.posted, [])
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_unknown_dot_word_is_swallowed_under_dormant_autorespond(self):
        await self._join_dormant(auto_join=True, purpose_text="autorespond")
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".gitignore build/",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.mm.posted, [])
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_bootstrap_recovers_dormant_memberships(self):
        self.bridge.mm.bot_channel_ids = {"c-dormant", "c-mapped"}
        self.bridge.mapping.link(Anchor("c-mapped"), "s1")

        await self.bridge._bootstrap_dormant_channels()

        self.assertEqual(self.bridge._dormant_channels, {"c-dormant"})

    async def test_concurrent_first_messages_create_exactly_one_session(self):
        await self._join_dormant(auto_join=False, purpose_text="autorespond")

        load_started = asyncio.Event()
        allow_load = asyncio.Event()
        original_load = self.bridge._load_channel_config

        async def blocking_load(channel_id, *, force=False):
            if force and not load_started.is_set():
                load_started.set()
                await allow_load.wait()
            return await original_load(channel_id, force=force)

        self.bridge._load_channel_config = blocking_load
        first = asyncio.create_task(self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "first",
            "user_id": "u1", "type": "",
        }))
        await load_started.wait()
        second = asyncio.create_task(self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "second",
            "user_id": "u1", "type": "",
        }))
        allow_load.set()
        await asyncio.gather(first, second)

        self.assertEqual(len(self.bridge.harness.created), 1)
        self.assertEqual(len(self.bridge.harness.sent), 2)
        self.assertTrue(self.bridge.harness.sent[0][1].endswith("first"))
        # "second" was queued by the warming-up path, replayed after the
        # mapping existed, and then held-and-coalesced behind the initial
        # run — so it arrives stamped, exactly once, after "first".
        self.assertTrue(
            self.bridge.harness.sent[1][1].endswith("second"),
            self.bridge.harness.sent[1][1],
        )

    async def test_external_purpose_edit_refreshes_dormant_config(self):
        await self._join_dormant(auto_join=False)
        self.bridge.mm.channels["c1"]["purpose"] = "codex, autorespond"

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "Test channel",
            "purpose": "codex, autorespond",
        })

        self.assertEqual(self.bridge.purpose_by_channel["c1"].backend, "codex")
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_self_written_purpose_marker_is_drained_while_dormant(self):
        await self._join_dormant(auto_join=False)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn("c1", self.bridge._self_written_purpose)

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "Test channel", "purpose": written,
        })

        self.assertNotIn("c1", self.bridge._self_written_purpose)

    async def test_failed_dormant_leave_preserves_recoverable_state(self):
        await self._join_dormant(auto_join=False)

        def fail_leave(_channel_id):
            raise RuntimeError("simulated leave failure")

        self.bridge.mm.remove_self_from_channel = fail_leave
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude leave",
            "user_id": "u1", "type": "",
        })

        self.assertIn("c1", self.bridge._dormant_channels)
        self.assertIn("c1", self.bridge.purpose_by_channel)
        self.assertTrue(any("Failed to leave" in p.message for p in self.bridge.mm.posted))

    # ── Pre-session `.status` (regression: bare `.status` was swallowed) ──

    async def test_bare_status_replies_before_any_session_both_paths(self):
        for auto_join in (False, True):
            with self.subTest(auto_join=auto_join):
                await self._join_dormant(auto_join=auto_join)
                self.bridge.mm.posted.clear()

                await self.bridge._on_mm_posted({
                    "channel_id": "c1", "message": ".status",
                    "user_id": "u1", "type": "",
                })

                # A reply is posted — the bug was total silence here.
                self.assertTrue(
                    self.bridge.mm.posted,
                    "bare `.status` must get a reply in a dormant channel",
                )
                self.assertTrue(
                    any("Status" in p.message for p in self.bridge.mm.posted),
                )
                # `.status` must never create or feed a session.
                self.assertEqual(self.bridge.harness.created, [])
                self.assertEqual(self.bridge.harness.sent, [])
                self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
                self.assertIn("c1", self.bridge._dormant_channels)

                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge.mm.posted.clear()

    async def test_dormant_status_reports_effective_future_config(self):
        await self._join_dormant(auto_join=False)
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".status",
            "user_id": "u1", "type": "",
        })

        body = "\n".join(p.message for p in self.bridge.mm.posted).lower()
        self.assertIn("no session", body)
        self.assertIn("claude", body)   # effective backend
        self.assertIn("opus", body)     # effective model (per-backend default)
        self.assertIn("/tmp/proj", body)  # effective cwd (config default)
        self.assertIn("mention-only", body)  # effective autorespond state

    async def test_dormant_status_cwd_matches_actual_create_path(self):
        # A Purpose `cwd=` outside allowed roots is rejected at session-create
        # time and replaced by the default. `.status` must report the SAME
        # effective cwd — not the raw rejected value — and must not mutate the
        # cached config's warnings (which would duplicate the warning later).
        self.config.allowed_attachment_roots = [self.tmp.name]
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, cwd=/etc/passwd-dir",
            "display_name": "Test channel",
        }
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".status",
            "user_id": "u1", "type": "",
        })

        body = "\n".join(p.message for p in self.bridge.mm.posted)
        self.assertIn("/tmp/proj", body)          # the effective (default) cwd
        self.assertNotIn("/etc/passwd-dir", body)  # never the rejected value
        # `.status` is read-only — it must not append a rejection warning.
        self.assertEqual(self.bridge.purpose_by_channel["c1"].warnings, [])
        self.assertEqual(self.bridge.harness.created, [])

    async def test_dormant_status_tracks_backend_switch(self):
        await self._join_dormant(auto_join=False)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".status",
            "user_id": "u1", "type": "",
        })

        body = "\n".join(p.message for p in self.bridge.mm.posted).lower()
        self.assertIn("codex", body)
        self.assertIn("gpt-5.5", body)  # codex per-backend default model
        self.assertEqual(self.bridge.harness.created, [])

    async def test_live_regression_sequence_status_then_first_message(self):
        """Reproduces the exact live channel sequence for both join paths:
        bare `.status` (reply, no session) → an ordinary message creates
        exactly one session → `.status` again reports the live session."""
        for auto_join in (False, True):
            with self.subTest(auto_join=auto_join):
                await self._join_dormant(auto_join=auto_join, purpose_text="autorespond")
                self.bridge.mm.posted.clear()

                # 1) Bare `.status` — replies, creates nothing.
                await self.bridge._on_mm_posted({
                    "id": "s0", "channel_id": "c1", "message": ".status",
                    "user_id": "u1", "type": "",
                })
                self.assertEqual(self.bridge.harness.created, [])
                self.assertEqual(self.bridge.harness.sent, [])
                self.assertTrue(
                    any("Status" in p.message for p in self.bridge.mm.posted),
                )

                # 2) First ordinary message — creates exactly one session.
                await self.bridge._on_mm_posted({
                    "id": "m1", "channel_id": "c1", "message": "Are you there?",
                    "user_id": "u1", "type": "",
                })
                self.assertEqual(len(self.bridge.harness.created), 1)
                self.assertEqual(len(self.bridge.harness.sent), 1)
                self.assertTrue(self.bridge.harness.sent[0][1].endswith("Are you there?"))
                self.assertNotIn("c1", self.bridge._dormant_channels)
                session_id = self.bridge.mapping.get_session(Anchor("c1"))
                self.assertIsNotNone(session_id)

                # 3) `.status` now reports the live session (not swallowed).
                self.bridge.mm.posted.clear()
                await self.bridge._on_mm_posted({
                    "id": "s1", "channel_id": "c1", "message": ".status",
                    "user_id": "u1", "type": "",
                })
                self.assertTrue(
                    any("Status" in p.message and session_id[:12] in p.message
                        for p in self.bridge.mm.posted),
                )
                # Still exactly one session — `.status` never spawned another.
                self.assertEqual(len(self.bridge.harness.created), 1)

                # Reset for the next join path.
                self.bridge.mapping.unlink(Anchor("c1"))
                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge._awaiting_first_forward.discard("c1")
                self.bridge.harness.created.clear()
                self.bridge.harness.sent.clear()
                self.bridge.harness.session_create_count = 0
                self.bridge.mm.posted.clear()

    async def test_bare_stop_replies_no_session_in_dormant_both_paths(self):
        for auto_join in (False, True):
            with self.subTest(auto_join=auto_join):
                await self._join_dormant(auto_join=auto_join)
                self.bridge.mm.posted.clear()

                await self.bridge._on_mm_posted({
                    "channel_id": "c1", "message": ".stop",
                    "user_id": "u1", "type": "",
                })

                self.assertTrue(
                    any("No session" in p.message for p in self.bridge.mm.posted),
                )
                self.assertEqual(self.bridge.harness.created, [])
                self.assertEqual(self.bridge.harness.sent, [])

                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge.mm.posted.clear()

    # ── Privacy contract: operator-wide commands need an @mention ──

    async def test_bare_global_commands_stay_silent_in_dormant(self):
        for cmd in (".sessions", ".running", ".invite ses_x"):
            with self.subTest(cmd=cmd):
                await self._join_dormant(auto_join=False, purpose_text="autorespond")
                self.bridge.mm.posted.clear()

                await self.bridge._on_mm_posted({
                    "channel_id": "c1", "message": cmd,
                    "user_id": "u1", "type": "",
                })

                self.assertEqual(
                    self.bridge.mm.posted, [],
                    f"bare {cmd} must stay silent (no operator-wide leak)",
                )
                self.assertEqual(self.bridge.harness.created, [])
                self.assertEqual(self.bridge.harness.sent, [])

                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge.mm.posted.clear()

    async def test_mentioned_global_command_runs_in_dormant(self):
        await self._join_dormant(auto_join=False)
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude .running",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(
            self.bridge.mm.posted,
            "an @mentioned global command should run in a dormant channel",
        )
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_mentioned_unknown_dot_word_gets_hint_in_dormant(self):
        # Bare `.foo` chatter is swallowed, but explicitly addressing the bot
        # with an unknown dot-word should get the "unknown command" hint
        # rather than silence — and never reach the LLM.
        await self._join_dormant(auto_join=True, purpose_text="autorespond")
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude .frobnicate",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(
            any("Unknown command" in p.message for p in self.bridge.mm.posted),
        )
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_bare_global_command_in_thread_stays_silent_in_dormant(self):
        # A user replies in a thread (e.g. on the welcome post) with a bare
        # global command. A dormant channel has no session, so this must not
        # leak operator-wide state without an @mention — same rule as the
        # channel path.
        await self._join_dormant(auto_join=True, purpose_text="autorespond")
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "welcome1", "message": ".sessions",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.mm.posted, [])
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_status_in_thread_reports_dormant_config(self):
        # A channel-local command replied in a thread of a dormant channel is
        # still answered (in-thread), and creates no session.
        await self._join_dormant(auto_join=True, purpose_text="autorespond")
        self.bridge.mm.posted.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "welcome1", "message": ".status",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(
            any("Status" in p.message and p.root_id == "welcome1"
                for p in self.bridge.mm.posted),
        )
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])

    # ── Contract: no parsed dot-command ever becomes an LLM first turn ──

    async def test_no_dot_command_forwards_to_llm_in_dormant(self):
        commands_under_test = [
            ".help", ".status", ".stop", ".autorespond", ".model", ".models",
            ".backend", ".sessions", ".running", ".invite ses_x",
            ".frobnicate now",  # unknown dot-word
            "@claude .sessions",  # mentioned global command
        ]
        for cmd in commands_under_test:
            with self.subTest(cmd=cmd):
                await self._join_dormant(auto_join=True, purpose_text="autorespond")

                await self.bridge._on_mm_posted({
                    "channel_id": "c1", "message": cmd,
                    "user_id": "u1", "type": "",
                })

                self.assertEqual(
                    self.bridge.harness.sent, [],
                    f"{cmd!r} must never be forwarded as an LLM turn",
                )
                self.assertEqual(
                    self.bridge.harness.created, [],
                    f"{cmd!r} must never create a session",
                )

                self.bridge._dormant_channels.clear()
                self.bridge.purpose_by_channel.clear()
                self.bridge.mm.posted.clear()


class InviteFlowTests(_BridgeTestCase):
    async def _engage(self, message: str = "@claude hello") -> None:
        await self.bridge._on_mm_posted({
            "id": "engage", "channel_id": "c1", "message": message,
            "user_id": "u1", "type": "",
        })

    async def test_bot_invited_to_unmapped_channel_waits_for_engagement(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.created, [])
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
        self.assertIn("c1", self.bridge._dormant_channels)
        self.assertTrue(any(".backend" in p.message for p in self.bridge.mm.posted))

        await self._engage()

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/tmp/proj")
        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")
        self.assertEqual(
            self.bridge.mapping.get_session(Anchor("c1")),
            self.bridge.vd.next_session_id,
        )
        self.assertNotIn("c1", self.bridge.warming_up_sessions)
        self.assertNotIn("c1", self.bridge._dormant_channels)

    async def test_invited_channel_has_no_session_until_first_user_message(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        session_id = self.bridge.mapping.get_session(Anchor("c1"))
        self.assertIsNone(session_id)
        self.assertEqual(
            self.bridge.harness.sent, [],
            "inviting the bot must not spend an LLM turn on a placeholder",
        )
        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("c1", self.bridge._dormant_channels)

    async def test_backend_and_model_can_change_before_first_user_message(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model gpt-5.4",
            "user_id": "u1", "type": "",
        })
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(
            self.bridge.harness.sent, [],
            "configuration commands must not become LLM turns",
        )

        await self.bridge._on_mm_posted({
            "id": "engage",
            "channel_id": "c1", "message": "@claude Please inspect the failing build",
            "user_id": "u1", "type": "",
        })

        configured_session = self.bridge.mapping.get_session(Anchor("c1"))
        self.assertEqual(self.bridge.harness.created[-1]["backend"], "codex")
        self.assertEqual(self.bridge.harness.created[-1]["model"], "gpt-5.4")
        self.assertEqual(len(self.bridge.harness.sent), 1)
        sent_session, sent_message = self.bridge.harness.sent[0]
        self.assertEqual(sent_session, configured_session)
        self.assertTrue(sent_message.endswith("Please inspect the failing build"))

    async def test_unmentioned_post_does_not_consume_first_forward_slot(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "talking to the room",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertIn("c1", self.bridge._dormant_channels)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude now start",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn(
            "Running inside Mattermost channel",
            self.bridge.harness.sent[0][1],
        )
        self.assertNotIn("c1", self.bridge._dormant_channels)

    async def test_help_posted_during_warmup_is_dispatched_after_mapping(self):
        self.config.auto_join_public_channels = True
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        create_started = asyncio.Event()
        allow_create = asyncio.Event()
        original_create = self.bridge.harness.create_session

        async def blocking_create(**kwargs):
            create_started.set()
            await allow_create.wait()
            return await original_create(**kwargs)

        self.bridge.harness.create_session = blocking_create
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        invite = asyncio.create_task(self._engage())
        await create_started.wait()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".help",
            "user_id": "u1", "type": "",
        })

        allow_create.set()
        await invite

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertNotIn(
            ".help", self.bridge.harness.sent[0][1],
            "a dot-command queued during warm-up must never reach the LLM",
        )
        self.assertTrue(
            any(".stop" in p.message and ".backend" in p.message
                for p in self.bridge.mm.posted),
            "queued .help should be dispatched once the session is mapped",
        )

    async def test_bot_invited_to_mapped_channel_is_noop(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self.bridge.vd.created), 0)

    async def test_purpose_with_model_passes_model_verbatim(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, sonnet"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")
        self.assertEqual(self.bridge.vd.created[0]["model"], "sonnet")

    async def test_unknown_purpose_token_posts_warning_and_uses_defaults(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "opusz"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertTrue(any(":warning:" in p.message for p in self.bridge.mm.posted))
        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")

    async def test_mention_only_token_is_cached_on_channel(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, mention-only"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_purpose_cwd_inside_allowed_roots_is_applied(self):
        self.config.allowed_attachment_roots = [self.tmp.name]
        project = f"{self.tmp.name}/myproj"
        import os; os.makedirs(project, exist_ok=True)
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"claude, cwd={project}",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertEqual(self.bridge.vd.created[0]["cwd"], project)
        # No warning about the cwd itself (welcome post is the only notice).
        self.assertFalse(
            any(":warning:" in p.message and "cwd" in p.message
                for p in self.bridge.mm.posted)
        )

    async def test_purpose_cwd_outside_allowed_roots_warns_and_uses_default(self):
        self.config.allowed_attachment_roots = [self.tmp.name]
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, cwd=/etc/passwd-dir",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/tmp/proj")
        self.assertTrue(
            any(":warning:" in p.message and "cwd" in p.message
                for p in self.bridge.mm.posted),
            f"expected a cwd rejection warning; got {[p.message for p in self.bridge.mm.posted]}",
        )

    async def test_purpose_cwd_trusted_when_no_allowed_roots(self):
        self.config.allowed_attachment_roots = []
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, cwd=/opt/whatever",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self._engage()

        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/opt/whatever")


class ForwardingTests(_BridgeTestCase):
    async def test_posted_in_mapped_channel_forwards_to_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hi")])

    async def test_posted_in_unmapped_channel_is_dropped(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_with_marker_and_matching_channel_is_skipped(self):
        """Channel-equality drop for the artifact markers (``spawn-announcement``
        / ``spawn-kickoff`` / ``cross-post-mirror``): when the recorded origin
        channel matches the channel the post landed in, the dispatcher drops it
        so the linked session doesn't read its own bridge artifact back as a
        user turn. (The ``post`` marker is handled separately — intent-keyed —
        see the self-post matrix below.)"""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ":thread: Spawned ...",
            "user_id": "u1", "type": "",
            "props": {
                "from_bridge_cli": "spawn-announcement",
                "from_bridge_cli_channel": "c1",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_with_marker_and_mismatching_channel_forwards(self):
        """Cross-channel agentcom: a CLI ``mm-bridge post --channel <other>``
        records the SENDER's own channel id. When the post lands in a
        different channel (the destination), the dispatcher must forward
        it as a normal user turn so the recipient session receives the
        message."""
        self.bridge.mapping.link(Anchor("c-target"), "s-target")

        await self.bridge._on_mm_posted({
            "channel_id": "c-target", "message": "Hello from sibling",
            "user_id": "u1", "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_channel": "c-sender",
                "from_bridge_cli_session": "s-sender",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [("s-target", "Hello from sibling")])

    async def test_bridge_forwards_agentcom_post_even_with_channel_equality(self):
        """Regression for the RC1 incident (post id
        ``tc8ssq5j7jdr3y18qgu9t5nmuw``): a poisoned resolver mis-stamped the
        sender's ``from_bridge_cli_channel`` as the destination, and the old
        channel-equality predicate dropped the post, silently breaking
        agentcom. Under the intent-keyed rule this exact wire state forwards:
        suppression requires ``from_bridge_cli_target=="self"`` AND a matching
        session, and this post carries neither (no target tag → forward; and
        the session doesn't match either). Real agentcom additionally carries
        ``target="explicit"`` for defense-in-depth."""
        self.bridge.mapping.link(Anchor("c-destination"), "s-destination")

        await self.bridge._on_mm_posted({
            "channel_id": "c-destination",
            "message": "Hello agent in c-destination",
            "user_id": "u1", "type": "",
            "props": {
                # Pathological stamp: sender mis-resolved its own
                # channel as the destination. Reproduces the wire-level
                # state of post `tc8ssq5j7jdr3y18qgu9t5nmuw` from today.
                "from_bridge_cli": "post",
                "from_bridge_cli_channel": "c-destination",
                "from_bridge_cli_session": "s-misresolved",
            },
        })

        self.assertEqual(
            self.bridge.vd.sent,
            [("s-destination", "Hello agent in c-destination")],
        )

    async def test_bridge_drops_cross_post_mirror_in_same_channel(self):
        """Spec test 8: ``cross-post-mirror`` posts are the only
        sender-side artifacts that the recipient should suppress
        without forwarding. The mirror lives in the sender's own
        channel by design (transcript visibility); the linked session
        must not read it back as a user turn."""
        self.bridge.mapping.link(Anchor("c-sender"), "s-sender")

        await self.bridge._on_mm_posted({
            "channel_id": "c-sender",
            "message": "hello\n\n_→ also sent to ~other~_",
            "user_id": "u1", "type": "",
            "props": {
                "from_bridge_cli": "cross-post-mirror",
                "from_bridge_cli_channel": "c-sender",
                "from_bridge_cli_session": "s-sender",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [])

    # ---- self-post loop-back guard (intent-keyed) ----
    # `mm-bridge post` stamps `from_bridge_cli_target`: "self" for the default
    # post-into-my-own-channel path, "explicit" when --channel/--thread was
    # given. Only a "self" post whose origin session == the target anchor's
    # session is dropped (belt and braces). Explicit posts ALWAYS forward, so
    # agentcom can never be silently dropped — even if a poisoned resolver
    # leaks a parent session id (the RC1 class of bug).

    async def test_cli_self_post_same_session_is_suppressed(self):
        """target=self + origin == channel's session → status update looping
        back → dropped (the bug this closes)."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "MILESTONE: step 2 done",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "self",
                "from_bridge_cli_channel": "c1",
                "from_bridge_cli_session": "s1",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_cli_self_post_same_session_in_thread_is_suppressed(self):
        """target=self also covers a thread-fork session posting into its own
        thread via the default (no-flag) path."""
        self.bridge.mapping.link(Anchor("c1", "root1"), "s-fork")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "MILESTONE from the fork",
            "root_id": "root1",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "self",
                "from_bridge_cli_channel": "c1",
                "from_bridge_cli_session": "s-fork",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_cli_self_post_other_session_is_forwarded(self):
        """A `self`-tagged post whose origin session differs from the target
        anchor's session is anomalous — forward conservatively (belt and
        braces: never drop something that isn't clearly a self-echo)."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "odd self stamp",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "self",
                "from_bridge_cli_channel": "c1",
                "from_bridge_cli_session": "s-other",
            },
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "odd self stamp")])

    async def test_cli_explicit_post_other_session_is_forwarded(self):
        """Normal agentcom: `mm-bridge post --channel` (target=explicit) from a
        different session forwards."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "Aster (from c-other): ping",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "explicit",
                "from_bridge_cli_channel": "c-other",
                "from_bridge_cli_session": "s-other",
            },
        })

        self.assertEqual(
            self.bridge.vd.sent, [("s1", "Aster (from c-other): ping")],
        )

    async def test_cli_explicit_post_same_session_is_forwarded(self):
        """Accepted gap: `--channel <your own channel>` (explicit) from the
        channel's own session is forwarded — a deliberate override, and the
        robustness win: an explicit agentcom post is NEVER silently dropped,
        even with a poisoned session resolver."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "explicit into own channel",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "explicit",
                "from_bridge_cli_channel": "c1",
                "from_bridge_cli_session": "s1",
            },
        })

        self.assertEqual(
            self.bridge.vd.sent, [("s1", "explicit into own channel")],
        )

    async def test_cli_post_without_target_tag_is_forwarded(self):
        """Old CLI (no `from_bridge_cli_target`) → forward conservatively; only
        an explicit `self` tag ever triggers suppression."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "legacy post",
            "user_id": self.bridge.mm.bot_user_id, "type": "",
            "props": {
                "from_bridge_cli": "post",
                "from_bridge_cli_channel": "c1",
                "from_bridge_cli_session": "s1",  # same session, but no target tag
            },
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "legacy post")])

    async def test_posted_with_marker_and_no_channel_field_is_skipped(self):
        """Backwards compat: posts that carry the marker but no
        ``from_bridge_cli_channel`` (older CLI in flight, or any future
        marker without an origin channel) preserve the original
        drop-on-marker behaviour. The set of marker authors is
        contained to this codebase, so we err on the safe side."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "legacy artifact",
            "user_id": "u1", "type": "",
            "props": {"from_bridge_cli": "spawn-announcement"},
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_without_from_bridge_cli_prop_forwards_normally(self):
        """Regression guard: posts whose ``props`` dict is missing or
        lacks the marker still forward to the linked session."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "human msg",
            "user_id": "u1", "type": "",
            "props": {"unrelated": "value"},
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "human msg")])

    async def test_posted_queues_while_session_pending(self):
        from mm_bridge.bridge import WarmingUpChannel
        self.bridge.warming_up_sessions["c1"] = WarmingUpChannel(channel_id="c1")

        post = {
            "channel_id": "c1", "message": "waiting msg", "user_id": "u1", "type": "",
        }
        await self.bridge._on_mm_posted(post)

        self.assertIn(post, self.bridge.warming_up_sessions["c1"].queued_posts)
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_attribution_kicks_in_on_second_user(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "first", "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "second", "user_id": "u2", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent[0], ("s1", "first"))
        # The second post arrives while the first run is still tracked, so
        # hold-and-coalesce buffers it and flushes it as a stamped held post
        # (the harness reports no live run, so the flush is immediate).
        # Attribution still applies — that's what this test is about.
        self.assertIn("u-u2: second", self.bridge.vd.sent[1][1])

    async def test_mention_only_drops_are_replayed_on_next_mention(self):
        """Non-mentions are held in an in-memory queue and prepended to
        the next forwarded message as a catch-up block — the session
        should see the conversation it missed while mention-only was
        silencing it."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "just chatting",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "more chatter",
            "user_id": "u2", "type": "",
        })
        self.assertEqual(self.bridge.vd.sent, [])

        await self.bridge._on_mm_posted({
            "id": "p3", "channel_id": "c1", "message": "@claude help please",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        session_id, body = self.bridge.vd.sent[0]
        self.assertEqual(session_id, "s1")
        self.assertIn("Catch-up context", body)
        self.assertIn("u-u1: just chatting", body)
        self.assertIn("u-u2: more chatter", body)
        self.assertTrue(body.rstrip().endswith("help please"))

    async def test_mention_only_drops_cleared_after_replay(self):
        """The silent-drop queue is drained on replay — a second mention
        after a quiet gap should not re-include earlier drops."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "earlier",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "@claude first",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p3", "channel_id": "c1", "message": "@claude second",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        self.assertIn("earlier", self.bridge.vd.sent[0][1])
        self.assertNotIn("earlier", self.bridge.vd.sent[1][1])
        self.assertNotIn("Catch-up context", self.bridge.vd.sent[1][1])

    async def test_mention_only_drops_capped_by_initial_catch_up_n(self):
        """If more than ``initial_catch_up_n`` messages pile up, only
        the most recent N survive."""
        self.config.initial_catch_up_n = 3
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        for i in range(5):
            await self.bridge._on_mm_posted({
                "id": f"d{i}", "channel_id": "c1", "message": f"drop{i}",
                "user_id": "u1", "type": "",
            })
        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude ping",
            "user_id": "u1", "type": "",
        })

        body = self.bridge.vd.sent[0][1]
        self.assertNotIn("drop0", body)
        self.assertNotIn("drop1", body)
        self.assertIn("drop2", body)
        self.assertIn("drop3", body)
        self.assertIn("drop4", body)

    async def test_mention_only_drops_disabled_when_catch_up_is_zero(self):
        """``initial_catch_up_n = 0`` disables auto-replay too — matches
        the knob that disables initial session catch-up."""
        self.config.initial_catch_up_n = 0
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "silent",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "@claude hi",
            "user_id": "u1", "type": "",
        })

        body = self.bridge.vd.sent[0][1]
        self.assertNotIn("silent", body)
        self.assertNotIn("Catch-up context", body)

    async def test_explicit_catch_up_drains_silent_drop_queue(self):
        """`@claude catch up` already surfaces the missed conversation
        from MM history; silent-drop entries that appear in the
        catch-up block get cleared so the next mention doesn't replay
        the same context twice."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        # The MM history used by `_run_catch_up` mirrors the incoming
        # post ids so `_drain_silent_drops_matching` can align them.
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "d1", "user_id": "u1", "message": "ambient chatter",
             "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "ambient chatter",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_posted({
            "id": "cu", "channel_id": "c1", "message": "@claude catch up",
            "user_id": "u1", "type": "",
        })

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

        # Next mention must not prepend another catch-up block —
        # history has already been delivered by the catch-up command.
        await self.bridge._on_mm_posted({
            "id": "next", "channel_id": "c1", "message": "@claude what next",
            "user_id": "u1", "type": "",
        })

        last_body = self.bridge.vd.sent[-1][1]
        self.assertNotIn("Catch-up context", last_body)
        self.assertNotIn("ambient chatter", last_body)

    async def test_partial_catch_up_preserves_unsurfaced_drops(self):
        """``@claude catch up 1`` surfaces only the most recent message;
        earlier queued drops that weren't in the block must remain so
        the next mention still replays them."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        # `get_posts` is served in `oldest-first` order and the
        # collector scans as-is, taking up to n entries.
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "older", "user_id": "u1", "message": "older chatter",
             "type": ""},
            {"id": "newer", "user_id": "u1", "message": "newer chatter",
             "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "id": "older", "channel_id": "c1", "message": "older chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "newer", "channel_id": "c1", "message": "newer chatter",
            "user_id": "u1", "type": "",
        })
        self.assertEqual(len(self.bridge._silent_drops[("c1", None)]), 2)

        await self.bridge._on_mm_posted({
            "id": "cu", "channel_id": "c1", "message": "@claude catch up 1",
            "user_id": "u1", "type": "",
        })

        # Only the single surfaced post ("older", first by get_posts
        # order) is removed; "newer" stays queued.
        remaining = self.bridge._silent_drops.get(("c1", None))
        self.assertIsNotNone(remaining)
        remaining_ids = [p.get("id") for p in remaining]
        self.assertEqual(remaining_ids, ["newer"])

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude what now",
            "user_id": "u1", "type": "",
        })

        last_body = self.bridge.vd.sent[-1][1]
        self.assertIn("newer chatter", last_body)
        self.assertNotIn("older chatter", last_body)

    async def test_mention_only_drops_survive_send_failure(self):
        """If the VD send fails while replaying silent drops, the queue
        must not be lost AND the failed trigger itself is re-queued —
        otherwise a transient outage would eat the user's actual
        request."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "drop-1", "channel_id": "c1", "message": "dropped once",
            "user_id": "u1", "type": "",
        })

        # First mention: VD raises. Queue must preserve the old drop
        # and also enqueue the failed trigger.
        async def failing_send(session_id, message):
            raise RuntimeError("simulated VD outage")

        self.bridge.vd.create_run = failing_send  # type: ignore[assignment]
        await self.bridge._on_mm_posted({
            "id": "trigger-1", "channel_id": "c1", "message": "@claude first try",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(any(
            ":warning:" in p.message for p in self.bridge.mm.posted
        ))
        self.assertIn(("c1", None), self.bridge._silent_drops)
        self.assertEqual(len(self.bridge._silent_drops[("c1", None)]), 2)

        # Second mention: VD back online. Catch-up block carries BOTH
        # the original drop and the failed trigger; queue clears only
        # after a successful send.
        captured: list[tuple[str, str]] = []

        async def ok_send(session_id, message):
            captured.append((session_id, message))
            return {"status": "sent"}

        self.bridge.vd.create_run = ok_send  # type: ignore[assignment]
        await self.bridge._on_mm_posted({
            "id": "trigger-2", "channel_id": "c1", "message": "@claude second try",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(captured), 1)
        _, body = captured[0]
        self.assertIn("dropped once", body)
        self.assertIn("first try", body)
        self.assertTrue(body.rstrip().endswith("second try"))
        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_cleared_on_user_removed(self):
        """Silent drops accumulated under the old session must not leak
        into the next session that takes over the same channel — clear
        on ``user_removed`` (the bot being kicked out / leaving)."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "stale",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_cleared_on_leave(self):
        """``@claude leave`` clears any queued drops in that channel."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "stale",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_posted({
            "id": "d2", "channel_id": "c1", "message": "@claude leave done",
            "user_id": "u1", "type": "",
        })

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_thread_cleared_on_thread_leave(self):
        """Leaving a thread clears that thread's queue without touching
        the channel-root queue."""
        self.bridge.mapping.link(Anchor("c1"), "s-root")
        self.bridge.mapping.link(Anchor("c1", "t1"), "s-thread")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "dr", "channel_id": "c1", "message": "root chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "dt", "channel_id": "c1", "message": "thread chatter",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        await self.bridge._on_mm_posted({
            "id": "lv", "channel_id": "c1", "message": "@claude leave",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        self.assertNotIn(("c1", "t1"), self.bridge._silent_drops)
        self.assertIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_replay_downloads_queued_attachments(self):
        """File uploads silently dropped before a mention must be
        downloaded on replay so the session sees the file it's being
        asked about, not just the message text."""
        from pathlib import Path

        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.vd.sessions_meta.append(
            {"id": "s1", "projectPath": self.tmp.name, "backend": "claude"},
        )
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        self.bridge.mm.files_by_id["fid-silent"] = b"# uploaded before mention\n"

        # Drop: file upload with no @claude mention.
        await self.bridge._on_mm_posted({
            "id": "drop-1", "channel_id": "c1", "message": "",
            "user_id": "u1", "type": "",
            "file_ids": ["fid-silent"],
            "metadata": {"files": [{"id": "fid-silent", "name": "silent.md",
                                     "size": 25}]},
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        # Mention triggers replay — attachment should be saved and
        # the catch-up block should reference the saved path.
        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude please look",
            "user_id": "u1", "type": "",
        })

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "silent.md"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"# uploaded before mention\n")

        self.assertEqual(len(self.bridge.vd.sent), 1)
        body = self.bridge.vd.sent[0][1]
        self.assertIn("Catch-up context", body)
        self.assertIn(f"[User attached file: {saved}]", body)

    async def test_mention_only_drops_keyed_per_thread(self):
        """Silent drops in a thread do not leak into the channel root
        (or vice versa) — each anchor has its own queue."""
        self.bridge.mapping.link(Anchor("c1"), "s-root")
        self.bridge.mapping.link(Anchor("c1", "t1"), "s-thread")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "pr1", "channel_id": "c1", "message": "root chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "pt1", "channel_id": "c1", "message": "thread chatter",
            "user_id": "u1", "type": "", "root_id": "t1",
        })
        await self.bridge._on_mm_posted({
            "id": "pr2", "channel_id": "c1", "message": "@claude root",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "pt2", "channel_id": "c1", "message": "@claude thread",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        by_session = {sid: body for sid, body in self.bridge.vd.sent}
        self.assertIn("root chatter", by_session["s-root"])
        self.assertNotIn("thread chatter", by_session["s-root"])
        self.assertIn("thread chatter", by_session["s-thread"])
        self.assertNotIn("root chatter", by_session["s-thread"])


class InboundAttachmentTests(_BridgeTestCase):
    """User attaches files in MM → bridge downloads them into the session cwd."""

    def _set_session_cwd(self, session_id: str, cwd: str) -> None:
        self.bridge.vd.sessions_meta.append(
            {"id": session_id, "projectPath": cwd, "backend": "claude"},
        )

    def _post_with_attachment(
        self, channel_id: str, file_id: str, name: str, body: bytes, *,
        message: str = "", root_id: str | None = None,
    ) -> dict:
        self.bridge.mm.files_by_id[file_id] = body
        return {
            "channel_id": channel_id,
            "message": message,
            "user_id": "u1",
            "type": "",
            "root_id": root_id or "",
            "file_ids": [file_id],
            "metadata": {"files": [{"id": file_id, "name": name, "size": len(body)}]},
        }

    async def test_attachment_saved_to_inbox_and_note_prepended(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment(
            "c1", "fid-1", "notes.md", b"# hello\n", message="please look at this",
        )
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "notes.md"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"# hello\n")
        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent_body = self.bridge.vd.sent[0][1]
        self.assertIn(f"[User attached file: {saved}]", sent_body)
        self.assertIn("please look at this", sent_body)
        self.assertTrue(sent_body.startswith("[User attached file: "))

    async def test_attachment_only_post_still_forwards(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment("c1", "fid-1", "photo.png", b"\x89PNG...")
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "photo.png"
        self.assertTrue(saved.exists())
        self.assertEqual(len(self.bridge.vd.sent), 1)
        self.assertIn(f"[User attached file: {saved}]", self.bridge.vd.sent[0][1])

    async def test_filename_conflict_gets_suffix(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)
        existing = Path(self.tmp.name) / ".mattermost-inbox"
        existing.mkdir()
        (existing / "data.txt").write_bytes(b"original")

        post = self._post_with_attachment("c1", "fid-1", "data.txt", b"second")
        await self.bridge._on_mm_posted(post)

        self.assertEqual((existing / "data.txt").read_bytes(), b"original")
        self.assertEqual((existing / "data-1.txt").read_bytes(), b"second")
        self.assertIn("data-1.txt", self.bridge.vd.sent[0][1])

    async def test_traversal_filename_is_sanitized(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment(
            "c1", "fid-1", "../../etc/passwd", b"evil",
        )
        await self.bridge._on_mm_posted(post)

        inbox = Path(self.tmp.name) / ".mattermost-inbox"
        saved = list(inbox.iterdir())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].parent, inbox)
        self.assertEqual(saved[0].read_bytes(), b"evil")

    async def test_download_failure_yields_skipped_note(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)
        self.bridge.mm.download_failures.add("fid-bad")

        post = self._post_with_attachment(
            "c1", "fid-bad", "doc.pdf", b"unused", message="hi",
        )
        await self.bridge._on_mm_posted(post)

        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent_body = self.bridge.vd.sent[0][1]
        self.assertIn("[MM attachment skipped: `doc.pdf` download failed]", sent_body)
        self.assertIn("hi", sent_body)

    async def test_thread_fork_downloads_to_parent_cwd(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s-parent")
        self._set_session_cwd("s-parent", self.tmp.name)
        self.bridge.mm.posts_by_id["root-post"] = {"message": "original msg"}

        post = self._post_with_attachment(
            "c1", "fid-t", "thread.txt", b"fork payload",
            message="replying", root_id="root-post",
        )
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "thread.txt"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"fork payload")

        self.assertEqual(len(self.bridge.vd.forks), 1)
        fork_body = self.bridge.vd.forks[0][1]
        self.assertIn(f"[User attached file: {saved}]", fork_body)
        self.assertIn("replying", fork_body)
        self.assertIn("Mattermost thread context", fork_body)

    async def test_first_engagement_message_downloads_attachment(self):
        """A file attached to the very first message in a dormant channel —
        the message that CREATES the session — must be downloaded and
        referenced in the session's opening turn, not silently dropped."""
        self.config.default_cwd = self.tmp.name
        # Dormant channel: bot joined, autorespond → any message engages.
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "autorespond", "display_name": "Test channel",
        }
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        self.bridge.mm.posted.clear()
        self.bridge.mm.files_by_id["fid-boot"] = b"\xff\xd8\xff-jpeg"

        await self.bridge._on_mm_posted({
            "id": "m1", "channel_id": "c1", "message": "what hardware is this?",
            "user_id": "u1", "type": "",
            "file_ids": ["fid-boot"],
            "metadata": {"files": [{"id": "fid-boot", "name": "device.jpg",
                                     "size": 8}]},
        })

        # Exactly one session created and one opening turn sent.
        self.assertEqual(len(self.bridge.harness.created), 1)
        self.assertEqual(len(self.bridge.harness.sent), 1)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "device.jpg"
        self.assertTrue(
            saved.exists(),
            "attachment on the session-creating message was not downloaded",
        )
        self.assertEqual(saved.read_bytes(), b"\xff\xd8\xff-jpeg")

        body = self.bridge.harness.sent[0][1]
        self.assertIn(f"[User attached file: {saved}]", body)
        self.assertIn("what hardware is this?", body)

    async def test_external_session_replacement_downloads_attachment(self):
        """When a post with an attachment adopts a channel mapped to an
        unreachable external session, the fresh session's first turn must
        include the downloaded file, not just the text."""
        self.config.default_cwd = self.tmp.name
        self.bridge.mapping.link(Anchor("c1"), "ext-old")
        self.bridge._external_sessions.add("ext-old")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "", "display_name": "Adopted channel",
        }
        self.bridge.mm.files_by_id["fid-adopt"] = b"adopted-bytes"

        await self.bridge._on_mm_posted({
            "id": "m1", "channel_id": "c1", "message": "please analyse",
            "user_id": "u1", "type": "",
            "file_ids": ["fid-adopt"],
            "metadata": {"files": [{"id": "fid-adopt", "name": "report.pdf",
                                     "size": 13}]},
        })

        self.assertEqual(len(self.bridge.harness.created), 1)
        self.assertEqual(len(self.bridge.harness.sent), 1)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "report.pdf"
        self.assertTrue(
            saved.exists(),
            "attachment dropped when adopting an external session",
        )
        body = self.bridge.harness.sent[0][1]
        self.assertIn(f"[User attached file: {saved}]", body)
        self.assertIn("please analyse", body)


class CatchUpTests(_BridgeTestCase):
    async def test_catch_up_command_sends_block_to_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        # All bot-identity posts are filtered from history replay —
        # restart-safe (is_own_post() is empty on a fresh process, so we
        # can't rely on it for catch-up).
        self.bridge.mm.posts_by_channel["c1"] = [
            {"user_id": "u1", "message": "m1", "type": ""},
            {"user_id": self.bridge.mm.bot_user_id, "message": "bot echo", "type": ""},
            {"user_id": "u2", "message": "m2", "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude catch up 50",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        block = self.bridge.vd.sent[0][1]
        self.assertIn("m1", block)
        self.assertIn("m2", block)
        self.assertNotIn("bot echo", block)


class InitialCatchUpTests(_BridgeTestCase):
    """On first engagement, prepend the dormant channel's history as context."""

    async def test_invite_session_defers_catch_up_until_first_real_turn(self):
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "earlier chat", "type": ""},
            {"id": "p2", "user_id": "u2", "message": "more chat", "type": ""},
        ]

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self.bridge.vd.created), 0)
        self.assertEqual(self.bridge.vd.sent, [])

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude begin",
            "user_id": "u1", "type": "",
        })

        first_msg = self.bridge.vd.sent[0][1]
        self.assertIn("Catch-up context", first_msg)
        self.assertIn("earlier chat", first_msg)
        self.assertIn("more chat", first_msg)
        self.assertTrue(first_msg.rstrip().endswith("begin"))

    async def test_explicit_catch_up_before_session_explains_automatic_catch_up(self):
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "earlier chat", "type": ""},
        ]

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self.bridge._on_mm_posted({
            "id": "catch-up", "channel_id": "c1",
            "message": "@claude catch up 50", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertTrue(any(
            "included automatically" in p.message for p in self.bridge.mm.posted
        ))
        self.assertIn("c1", self.bridge._dormant_channels)

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude begin",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.harness.sent), 1)
        first_conversation = self.bridge.harness.sent[0][1]
        self.assertIn("Catch-up context", first_conversation)
        self.assertIn("earlier chat", first_conversation)
        self.assertIn("Running inside Mattermost channel", first_conversation)

    async def test_engagement_excludes_triggering_post(self):
        self.config.auto_join_public_channels = True
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "old message", "type": ""},
            {"id": "trigger", "user_id": "u1", "message": "@claude hi", "type": ""},
        ]
        self.bridge._dormant_channels.add("c1")

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude hi",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        first_msg = self.bridge.vd.sent[0][1]
        self.assertIn("old message", first_msg)
        self.assertNotIn("@claude hi\n[End of catch-up]", first_msg)
        # The engagement msg itself is still the post-catch-up payload.
        self.assertTrue(first_msg.rstrip().endswith("hi"))

    async def test_zero_disables_auto_catch_up(self):
        self.config.initial_catch_up_n = 0
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "history", "type": ""},
        ]

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude begin",
            "user_id": "u1", "type": "",
        })

        first_msg = self.bridge.vd.sent[0][1]
        self.assertNotIn("Catch-up context", first_msg)

    async def test_empty_channel_skips_block(self):
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = []

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude begin",
            "user_id": "u1", "type": "",
        })

        first_msg = self.bridge.vd.sent[0][1]
        self.assertNotIn("Catch-up context", first_msg)


class LeaveTests(_BridgeTestCase):
    async def test_leave_command_removes_bot_and_unlinks(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude leave done",
            "user_id": "u1", "type": "",
        })

        self.assertIn("c1", self.bridge.mm.removed)
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))

    async def test_user_removed_unlinks_mapping(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))

    async def test_leave_dormant_invited_channel_clears_dormant_state(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "history", "type": ""},
        ]
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertIn("c1", self.bridge._dormant_channels)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude leave",
            "user_id": "u1", "type": "",
        })

        self.assertNotIn("c1", self.bridge._dormant_channels)
        self.assertIn("c1", self.bridge.mm.removed)


class StopCommandTests(_BridgeTestCase):
    async def test_stop_command_in_channel_interrupts_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_stop_command_case_insensitive(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@Claude STOP",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])

    async def test_stop_command_in_thread_interrupts_thread_session(self):
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")
        self.bridge.current_run_id_by_session["fork-s"] = "run-fork"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("fork-s", "run-fork")])

    async def test_stop_with_trailing_text_is_regular_message(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop doing that",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertEqual(len(self.bridge.vd.sent), 1)

    async def test_stop_in_unmapped_channel_is_ignored(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])

    async def test_bare_stop_in_autorespond_mode_interrupts(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])

    async def test_bare_stop_in_mention_only_mode_is_not_interrupt(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])

    async def test_bare_stop_in_thread_autorespond_interrupts_thread_session(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )
        self.bridge.current_run_id_by_session["fork-s"] = "run-fork"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("fork-s", "run-fork")])

    # ----- `.stop` finds runs the bridge itself didn't submit -----

    async def test_stop_interrupts_run_tracked_only_via_active_run(self):
        # A spawned session's initial run is created by the `mm-bridge spawn`
        # CLI process, so the daemon never populates
        # ``current_run_id_by_session``; it only learns of the run from the
        # ``run.started`` SSE event → ``active_run_by_session``. `.stop` must
        # still find and interrupt it. Regression for the codex "Nothing to
        # stop" report (spawned + autorespond → long un-submitted run).
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.active_run_by_session["s1"] = "run-active"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-active")])

    async def test_stop_falls_back_to_harness_when_trackers_empty(self):
        # Bridge restarted mid-run: both in-memory trackers are empty, but
        # the harness still owns a live run. `.stop` consults the harness
        # (authoritative) and interrupts the single running harness-origin run.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-old", "status": "completed", "origin": "harness"},
            {"id": "run-live", "status": "running", "origin": "harness"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-live")])

    async def test_stop_falls_back_to_harness_when_active_run_id_is_none(self):
        # `run.started` may omit the run_id, so `active_run_by_session` can hold
        # a None value for a live session (key present, value None). `.stop`
        # must treat that as "known live but id unknown" and recover the id from
        # the harness — unlike `.status`, which reports "running" off the key
        # alone. This is the asymmetry the 3-source resolution hinges on.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.active_run_by_session["s1"] = None
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-live", "status": "running", "origin": "harness"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-live")])

    async def test_stop_harness_fallback_ignores_external_run(self):
        # An external (TUI-resumed) run isn't ours to interrupt — the harness
        # fallback is guarded to harness-origin runs only, so it reports
        # "Nothing to stop" rather than attempting a kill that would 409.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-ext", "status": "running", "origin": "external"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertIn(
            ":octagonal_sign: Nothing to stop.",
            [p.message for p in self.bridge.mm.posted],
        )

    async def test_stop_harness_fallback_does_not_guess_between_running_runs(self):
        # More than one running run for the session is unexpected; don't
        # guess which to kill — report nothing rather than pick wrong.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-a", "status": "running", "origin": "harness"},
            {"id": "run-b", "status": "running", "origin": "harness"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])

    async def test_stop_reports_nothing_when_no_run_anywhere(self):
        # No tracked run and the harness reports nothing running → the honest
        # "Nothing to stop." (prior behavior preserved).
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertIn(
            ":octagonal_sign: Nothing to stop.",
            [p.message for p in self.bridge.mm.posted],
        )

    async def test_stop_harness_fallback_survives_dead_harness(self):
        # If the harness probe itself errors, `.stop` degrades to "Nothing to
        # stop" rather than raising.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.run_probe_error = RuntimeError("harness down")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertIn(
            ":octagonal_sign: Nothing to stop.",
            [p.message for p in self.bridge.mm.posted],
        )


class CommandTests(_BridgeTestCase):
    """Dot-commands (`.stop`, `.help`, `.autorespond`, `.status`, ...) are
    handled by the bridge itself — dispatched before `_forward_user_post`,
    bypassing the mention-only gate, and never forwarded to the agent.
    Mirrors StopCommandTests."""

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    # ----- .help (global — works with or without a session) -----

    async def test_help_lists_commands_and_is_not_forwarded(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".help", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn(".stop", joined)
        self.assertIn(".help", joined)

    async def test_help_works_in_channel_without_session(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".help",
            "user_id": "u1", "type": "",
        })

        self.assertIn(".stop", "\n".join(self._posted_texts()))

    async def test_help_is_case_insensitive_and_strips_mention(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude .HELP",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])
        self.assertIn(".stop", "\n".join(self._posted_texts()))

    # ----- unknown dot-word intercepted, not forwarded -----

    async def test_unknown_command_replies_and_is_not_forwarded(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".frobnicate now",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("Unknown command", joined)
        self.assertIn(".help", joined)

    # ----- .stop bypasses the mention-only gate -----

    async def test_dot_stop_interrupts_without_mention_in_mention_only(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".stop", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_dot_stop_with_claude_mention_also_works(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude .stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])

    async def test_dot_stop_without_session_replies_no_session(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertIn("No session", "\n".join(self._posted_texts()))

    # ----- .autorespond on/off/bare -----

    async def test_dot_autorespond_off_sets_mention_only(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond off",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)
        self.assertIn("mention-only", self.bridge.mm.channels["c1"]["purpose"])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_dot_autorespond_on_clears_mention_only(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=True,
        )
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, opus, mention-only",
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond on",
            "user_id": "u1", "type": "",
        })

        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_dot_autorespond_bare_toggles(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond",
            "user_id": "u1", "type": "",
        })

        # Was autorespond (mention_only False) → bare toggle turns it off.
        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_dot_autorespond_cold_cache_keeps_the_rest_of_the_config(self):
        # The third instance of the cold-cache bug. `.autorespond` restarts
        # nothing, so the damage is invisible at the time: it fabricates a
        # default config, flips the flag on THAT, and persists it — wiping
        # backend, model and `cwd=` from the Channel Purpose. The next session
        # the channel creates then starts on the wrong settings.
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"codex, gpt-5.4, mention-only, cwd={target}",
        }
        self.assertNotIn("c1", self.bridge.purpose_by_channel)  # cold cache

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond on",
            "user_id": "u1", "type": "",
        })

        cfg = self.bridge.purpose_by_channel["c1"]
        self.assertFalse(cfg.mention_only)  # the one thing the command changes
        self.assertEqual(cfg.backend, "codex")
        self.assertEqual(cfg.model, "gpt-5.4")
        self.assertEqual(cfg.cwd, target)
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn("codex", written)
        self.assertIn("gpt-5.4", written)
        self.assertIn(f"cwd={target}", written)
        self.assertIn("autorespond", written)

    async def test_dot_autorespond_bare_toggle_reads_the_purpose_not_defaults(self):
        # A bare `.autorespond` toggles relative to the CURRENT state. On a
        # cold cache that state was read from `config.default_autorespond`
        # instead of the channel's Purpose, so the toggle could run the wrong
        # way — here the channel is on autorespond, so bare must turn it off.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, opus, autorespond",
        }
        self.assertFalse(self.config.default_autorespond)  # opposite of the channel

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)
        self.assertIn("mention-only", self.bridge.mm.channels["c1"]["purpose"])

    async def test_dot_autorespond_unreadable_config_refuses_without_persisting(self):
        # Nothing restarts here, but `_persist_purpose` writes unconditionally
        # — and it swallows its own failed channel read, so persisting a
        # guessed config could wipe the channel's resume block too.
        self.bridge.mapping.link(Anchor("c1"), "s1")

        def _boom(_channel_id):
            raise RuntimeError("mattermost unreachable")

        self.bridge.mm.get_channel = _boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".autorespond on",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.mm.purposes, [])
        self.assertNotIn("c1", self.bridge.purpose_by_channel)
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("configuration", joined)

    # ----- .status -----

    async def test_dot_status_reports_session_and_run(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "opus",
            "project": {"path": "/tmp/proj", "name": "proj"},
            "origin": "harness", "status": "idle",
        }]
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".status", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("s1", joined)
        self.assertIn("claude", joined)
        self.assertIn("run-s1", joined)

    async def test_dot_status_without_session_replies_no_session(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".status",
            "user_id": "u1", "type": "",
        })

        self.assertIn("No session", "\n".join(self._posted_texts()))

    # ----- thread dispatch parity -----

    async def test_dot_stop_in_thread_interrupts_thread_session(self):
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")
        self.bridge.current_run_id_by_session["fork-s"] = "run-fork"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".stop",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("fork-s", "run-fork")])

    async def test_dot_help_in_thread_replies_in_thread(self):
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".help",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertTrue(
            any(p.root_id == "r1" and ".stop" in p.message
                for p in self.bridge.mm.posted)
        )
        self.assertEqual(self.bridge.vd.forks, [])

    # ----- backward compat: non-dot messages untouched -----

    async def test_plain_message_still_forwarded(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hello world",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hello world")])

    async def test_natural_language_stop_still_interrupts(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [("s1", "run-s1")])

    # ----- auto-join "silent presence" is preserved -----

    async def test_dot_command_without_mention_silent_in_autojoined_channel(self):
        # In auto-join mode the bot lurks in public channels with no session.
        # A dot-shaped message with no mention must not pull it out of silence
        # or leak internal listings (`.sessions`).
        self.bridge.config.auto_join_public_channels = True
        self.bridge._dormant_channels.add("c-lurk")
        self.bridge.harness.sessions_meta = [
            {"id": "s9", "backend": "claude", "title": "SecretSession",
             "project": {"path": "/x", "name": "x"}, "origin": "harness"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c-lurk", "message": ".sessions",
            "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertNotIn("Recent sessions", joined)
        self.assertNotIn("SecretSession", joined)

    async def test_dot_command_with_mention_works_in_autojoined_channel(self):
        self.bridge.config.auto_join_public_channels = True
        self.bridge._dormant_channels.add("c-lurk")
        self.bridge.harness.sessions_meta = [
            {"id": "s9", "backend": "claude", "title": "Sess9",
             "project": {"path": "/x", "name": "x"}, "origin": "harness"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c-lurk", "message": "@claude .sessions",
            "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn("Recent sessions", joined)
        # Handled as a command, not forwarded / engaged: no session created.
        self.assertEqual(self.bridge.harness.created, [])


class CommandPhase2Tests(_BridgeTestCase):
    """`.model`, `.models`, `.running` — phase 2 of the dot-command set."""

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    # ----- .model <name> (free text → session restart) -----

    async def test_dot_model_switches_via_session_restart(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })

        # A new session was created with the requested (free-text) model.
        self.assertTrue(self.bridge.harness.created)
        self.assertEqual(self.bridge.harness.created[-1]["model"], "claude-sonnet")
        # Backend kept; model persisted to Channel Purpose.
        self.assertEqual(self.bridge.harness.created[-1]["backend"], "claude")
        self.assertIn("claude-sonnet", self.bridge.mm.channels["c1"]["purpose"])

    async def test_dot_model_restart_is_quiet_no_greeting_run(self):
        from mm_bridge.bridge import INVITE_PLACEHOLDER
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })

        # New session created, but the restart does not burn a greeting run.
        self.assertTrue(self.bridge.harness.created)
        sent_messages = [m for (_sid, m) in self.bridge.harness.sent]
        self.assertNotIn(INVITE_PLACEHOLDER, sent_messages)

    async def test_dot_model_refuses_while_run_active(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })

        # No restart happened.
        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("run is active", joined)

    async def test_dot_model_restart_failure_keeps_old_session_no_false_success(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        async def boom(*a, **k):
            raise RuntimeError("harness down")
        self.bridge.harness.create_session = boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })

        # Old, still-live session is restored — the channel isn't orphaned.
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s1")
        self.assertEqual(self.bridge.purpose_by_channel["c1"].model, "opus")
        joined = "\n".join(self._posted_texts())
        # A failure was surfaced (with backend + detail) and NO false
        # "Model set" confirmation posted.
        self.assertIn("restart the session", joined)
        self.assertIn("harness down", joined)
        self.assertNotIn("Model set", joined)

    async def test_dot_model_cold_cache_keeps_cwd_and_autorespond(self):
        # `purpose_by_channel` is EMPTY for every mapped channel after a daemon
        # restart — `_bootstrap_known_sessions` never populates it, and the
        # dot-command preload only covers dormant channels. So this is the
        # ORDINARY first `.model` in a restarted channel, not a race. Building
        # the config from bare defaults here restarts the session in
        # `default_cwd` (the agent silently changes repository), resets the
        # autorespond flag, and writes a Channel Purpose with no `cwd=` token —
        # losing the setting permanently, not just for this one switch.
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"codex, gpt-5.4, autorespond, cwd={target}",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": target, "name": "repo"}, "origin": "harness",
        }]
        self.assertNotIn("c1", self.bridge.purpose_by_channel)  # cold cache

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model gpt-5.4-mini",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["model"], "gpt-5.4-mini")
        self.assertEqual(created["backend"], "codex")
        # Only the model moves — the channel's directory comes along.
        self.assertEqual(created["cwd"], target)
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn(f"cwd={target}", written)
        self.assertIn("autorespond", written)

    async def test_dot_model_token_less_purpose_keeps_the_live_backend(self):
        # Channels created AROUND an existing session (`mm-bridge spawn`,
        # `.invite <id>`, bootstrap discovery) get a Purpose of
        # "agent-harness session <id>" — no config tokens — and nothing ever
        # fills `purpose_by_channel` for them. That Purpose LOADS fine and
        # parses to pure defaults, so trusting its backend would recreate a
        # codex session on claude and persist the flip. The live session's
        # meta is the ground truth for what this session actually runs on.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "agent-harness session s1",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model gpt-5.4-mini",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["backend"], "codex")
        self.assertEqual(created["model"], "gpt-5.4-mini")
        self.assertIn("codex", self.bridge.mm.channels["c1"]["purpose"])

    async def test_dot_model_unreadable_config_refuses_the_switch(self):
        # With the Channel Purpose unreadable we don't know the channel's cwd
        # or autorespond flag. Restarting anyway would guess them, and
        # `_persist_purpose` needs the same channel read that just failed —
        # so refuse instead of silently relocating the session.
        self.bridge.mapping.link(Anchor("c1"), "s1")

        def _boom(_channel_id):
            raise RuntimeError("mattermost unreachable")

        self.bridge.mm.get_channel = _boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.mm.purposes, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("configuration", joined)
        self.assertNotIn("model set", joined)

    async def test_bare_model_shows_current_and_hints_models(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "opus",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("opus", joined)
        self.assertIn(".models", joined)

    # ----- .models (list, mark current) -----

    async def test_dot_models_lists_configured_and_marks_current(self):
        self.bridge.config.models = {"claude": ["opus", "sonnet", "haiku"]}
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "sonnet",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".models", "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn("opus", joined)
        self.assertIn("sonnet", joined)
        self.assertIn("haiku", joined)
        # Current model flagged.
        self.assertRegex(joined, r"sonnet.*current")

    async def test_dot_models_merges_harness_catalog(self):
        # No config list; harness enumerates two models.
        self.bridge.config.models = {}
        self.bridge.harness.models_by_backend = {"claude": ["opus", "sonnet"]}
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "opus",
            "project": {"path": "/x", "name": "x"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".models", "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn("opus", joined)
        self.assertIn("sonnet", joined)

    async def test_dot_models_empty_explains_free_text_still_works(self):
        self.bridge.config.models = {}
        self.bridge.harness.models_by_backend = {"claude": []}
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": None,
            "project": {"path": "/x", "name": "x"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".models", "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn(".model", joined)

    async def test_dot_models_works_without_session_using_default_backend(self):
        self.bridge.config.models = {"claude": ["opus", "sonnet"]}

        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".models",
            "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn("opus", joined)

    # ----- .running -----

    async def test_dot_running_lists_active_sessions(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mapping.link(Anchor("c2"), "s2")
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "backend": "claude", "title": "Alpha",
             "project": {"path": "/a", "name": "a"}, "origin": "harness"},
            {"id": "s2", "backend": "codex", "title": "Beta",
             "project": {"path": "/b", "name": "b"}, "origin": "harness"},
        ]
        # Only s1 has an in-flight run.
        self.bridge.active_run_by_session["s1"] = "run-1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".running", "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        self.assertIn("Alpha", joined)
        self.assertNotIn("Beta", joined)

    async def test_dot_running_none_active(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".running", "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("no runs", joined)


class CommandBackendTests(_BridgeTestCase):
    """`.backend [<name>]` — mirrors `.model`, but validates against
    KNOWN_BACKENDS (backends ARE enumerable) and drops the carried model on
    a switch (models are backend-specific)."""

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    async def test_dot_backend_switches_and_drops_carried_model(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })

        # A new session was created on the requested backend...
        self.assertTrue(self.bridge.harness.created)
        self.assertEqual(self.bridge.harness.created[-1]["backend"], "codex")
        # ...with the carried claude model DROPPED → per-backend codex default.
        self.assertEqual(self.bridge.harness.created[-1]["model"], "gpt-5.5")
        # Persisted purpose names the new backend, not the old model.
        self.assertIn("codex", self.bridge.mm.channels["c1"]["purpose"])
        self.assertNotIn("opus", self.bridge.mm.channels["c1"]["purpose"])
        # In-memory config reflects the drop.
        self.assertEqual(self.bridge.purpose_by_channel["c1"].backend, "codex")
        self.assertIsNone(self.bridge.purpose_by_channel["c1"].model)

    async def test_dot_backend_alias_is_canonicalized(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="codex", model="gpt-5.5", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "codex"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend claude-code",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(self.bridge.harness.created)
        self.assertEqual(self.bridge.harness.created[-1]["backend"], "claude")

    async def test_dot_backend_unknown_rejected_inline_no_restart(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend frobnicate",
            "user_id": "u1", "type": "",
        })

        # No restart; an inline rejection naming the known backends.
        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("unknown backend", joined)
        self.assertIn("claude", joined)

    async def test_dot_backend_refuses_while_run_active(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("run is active", joined)

    async def test_dot_backend_same_backend_is_noop(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend claude",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("already on", joined)

    async def test_bare_backend_shows_current_and_known(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "opus",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("claude", joined)
        # Lists the other known backends too.
        self.assertIn("codex", joined)

    async def test_dot_backend_no_session_replies_no_session(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })
        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("no session", joined)

    async def test_dot_backend_restart_is_quiet_no_greeting_run(self):
        from mm_bridge.bridge import INVITE_PLACEHOLDER
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        })

        # Session recreated on the new backend, but NO greeting/warming run
        # fired — the confirmation post is the only channel output.
        self.assertTrue(self.bridge.harness.created)
        sent_messages = [m for (_sid, m) in self.bridge.harness.sent]
        self.assertNotIn(INVITE_PLACEHOLDER, sent_messages)

    async def test_failed_backend_restart_replays_posts_to_restored_session(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        create_started = asyncio.Event()
        allow_failure = asyncio.Event()

        async def failing_create(**_kwargs):
            create_started.set()
            await allow_failure.wait()
            raise RuntimeError("harness down")

        self.bridge.harness.create_session = failing_create
        switch = asyncio.create_task(self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex",
            "user_id": "u1", "type": "",
        }))
        await create_started.wait()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "still here",
            "user_id": "u1", "type": "",
        })

        allow_failure.set()
        await switch

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s1")
        self.assertEqual(self.bridge.harness.sent, [("s1", "still here")])

    async def test_dot_backend_cold_cache_keeps_cwd_and_autorespond(self):
        # Same cold-cache state as `.model`: after a daemon restart every
        # mapped channel has `cfg is None`. The backend is meta-derived so the
        # swap itself is right, but rebuilding the rest of the config from
        # defaults relocates the session to `default_cwd`, resets autorespond,
        # and drops the `cwd=` token from the persisted Channel Purpose.
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"codex, gpt-5.4, autorespond, cwd={target}",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": target, "name": "repo"}, "origin": "harness",
        }]
        self.assertNotIn("c1", self.bridge.purpose_by_channel)  # cold cache

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend claude",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["backend"], "claude")
        # The backend swap drops the model, but not the directory.
        self.assertEqual(created["cwd"], target)
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn(f"cwd={target}", written)
        self.assertIn("autorespond", written)

    async def test_dot_backend_token_less_purpose_merges_against_live_backend(self):
        # Same token-less Purpose as `.model`, plus the production shape the
        # test double normally hides: the live harness returns an EMPTY model
        # catalog, so `purpose.parse` accepts the whole "agent-harness session
        # s1" string as a MODEL token. Merging against that Purpose's backend
        # (the default) instead of the live one makes `_merge_configs` see no
        # backend change — so it carries that garbage string into
        # `create_session` as the model.
        self.bridge.harness.models_by_backend = {"claude": [], "codex": []}
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "agent-harness session s1",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend claude",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["backend"], "claude")
        # The real backend DID change (codex → claude), so the model drops to
        # the new backend's default rather than carrying the Purpose's text.
        self.assertNotEqual(created["model"], "agent-harness session s1")
        self.assertNotIn(
            "agent-harness session", self.bridge.mm.channels["c1"]["purpose"],
        )

    async def test_dot_backend_unreadable_config_refuses_the_switch(self):
        # Unreadable Channel Purpose → the channel's cwd and autorespond flag
        # are unknown, and `_persist_purpose` couldn't write the result back
        # anyway. Refuse rather than restart on guessed values.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        def _boom(_channel_id):
            raise RuntimeError("mattermost unreachable")

        self.bridge.mm.get_channel = _boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend claude",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.mm.purposes, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("configuration", joined)
        self.assertNotIn("backend set", joined)


class CommandCwdTests(_BridgeTestCase):
    """`.cwd [<path>]` — the in-chat route to a channel's working directory.

    Mirrors `.backend`: bare reads (never restarts), a set validates the path
    up-front and then either persists it (dormant) or recreates the session
    there (active). Validation is stricter than the Channel Purpose parser
    because a bad value there degrades silently to `default_cwd`, whereas the
    command can say exactly what's wrong.
    """

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    def _joined(self) -> str:
        return "\n".join(self._posted_texts()).lower()

    def _active_channel(self, cwd: str = "/tmp/proj") -> None:
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False, cwd=cwd,
        )
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"claude, opus, cwd={cwd}",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "claude", "model": "opus",
            "project": {"path": cwd, "name": Path(cwd).name}, "origin": "harness",
        }]

    async def _dormant_channel(self, purpose_text: str = "") -> None:
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": purpose_text, "display_name": "Test channel",
        }
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

    # ── bare read ────────────────────────────────────────────────────────

    async def test_bare_cwd_reports_the_running_session_directory(self):
        self._active_channel(cwd="/tmp/proj")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".cwd", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("/tmp/proj", "\n".join(self._posted_texts()))

    async def test_bare_cwd_dormant_reports_the_next_sessions_directory(self):
        await self._dormant_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".cwd", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        # `default_cwd` from the fixture — what the first message would use.
        self.assertIn("/tmp/proj", "\n".join(self._posted_texts()))

    # ── set: dormant channel ─────────────────────────────────────────────

    async def test_dormant_set_persists_to_purpose_without_creating_a_session(self):
        from mm_bridge import purpose as purpose_mod
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        await self._dormant_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.bridge.purpose_by_channel["c1"].cwd, target)
        # Round-trip: what we wrote to the Purpose parses back to the same cwd.
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn(f"cwd={target}", written)
        reparsed = purpose_mod.parse(
            written, "claude", None, lambda _b: [],
        )
        self.assertEqual(reparsed.cwd, target)
        self.assertEqual(reparsed.warnings, [])

    async def test_dormant_set_then_first_message_starts_session_there(self):
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        await self._dormant_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "engage-1", "channel_id": "c1",
            "message": "@claude have a look", "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.harness.created), 1)
        self.assertEqual(self.bridge.harness.created[0]["cwd"], target)

    # ── set: active channel ──────────────────────────────────────────────

    async def test_active_set_recreates_the_session_in_the_new_directory(self):
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(self.bridge.harness.created)
        self.assertEqual(self.bridge.harness.created[-1]["cwd"], target)
        # Backend/model survive a cwd change — only the directory moves.
        self.assertEqual(self.bridge.harness.created[-1]["backend"], "claude")
        self.assertEqual(self.bridge.harness.created[-1]["model"], "opus")
        self.assertEqual(self.bridge.purpose_by_channel["c1"].cwd, target)
        self.assertIn(f"cwd={target}", self.bridge.mm.channels["c1"]["purpose"])

    async def test_cold_config_cache_keeps_backend_model_and_autorespond(self):
        # `purpose_by_channel` is EMPTY for every mapped channel after a daemon
        # restart — `_bootstrap_known_sessions` never populates it, and the
        # dot-command preload only runs for dormant channels. Rebuilding the
        # config from bare defaults here would silently turn a codex channel
        # into claude and reset the autorespond flag, then write that wrong
        # config back to the Channel Purpose while the confirmation claims
        # only the directory moved.
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "codex, gpt-5.4, mention-only",
        }
        self.assertNotIn("c1", self.bridge.purpose_by_channel)  # cold cache

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["backend"], "codex")
        self.assertEqual(created["model"], "gpt-5.4")
        self.assertEqual(created["cwd"], target)
        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)
        written = self.bridge.mm.channels["c1"]["purpose"]
        self.assertIn("codex", written)
        self.assertIn("gpt-5.4", written)
        self.assertIn("mention-only", written)
        self.assertIn(f"cwd={target}", written)
        self.assertNotIn("claude", written)

    async def test_token_less_purpose_keeps_the_live_backend(self):
        # A spawn/`.invite`-created channel's Purpose ("agent-harness session
        # <id>") carries no config tokens, so it parses to pure defaults —
        # moving the directory must not also move a codex session to claude.
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "agent-harness session s1",
        }
        self.bridge.harness.sessions_meta = [{
            "id": "s1", "backend": "codex", "model": "gpt-5.4",
            "project": {"path": "/tmp/proj", "name": "proj"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        created = self.bridge.harness.created[-1]
        self.assertEqual(created["backend"], "codex")
        self.assertEqual(created["cwd"], target)
        self.assertIn("codex", self.bridge.mm.channels["c1"]["purpose"])

    async def test_unreadable_config_refuses_the_switch(self):
        # If the Channel Purpose can't be read we can't know the channel's
        # backend/model — moving the session anyway would guess them. Refuse
        # instead (we couldn't persist the result either).
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self.bridge.mapping.link(Anchor("c1"), "s1")

        def _boom(_channel_id):
            raise RuntimeError("mattermost unreachable")

        self.bridge.mm.get_channel = _boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("configuration", self._joined())

    async def test_failed_restart_does_not_persist_the_new_directory(self):
        # Mirrors the `.backend` failed-restart contract: no success claim and
        # no Purpose write for a directory the channel isn't actually in.
        old = str(Path(self.tmp.name) / "old")
        Path(old).mkdir()
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self._active_channel(cwd=old)

        async def failing_create(**_kwargs):
            raise RuntimeError("harness down")

        self.bridge.harness.create_session = failing_create

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        # Channel keeps talking to its old, still-live session.
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s1")
        self.assertEqual(self.bridge.purpose_by_channel["c1"].cwd, old)
        self.assertNotIn(f"cwd={target}", self.bridge.mm.channels["c1"]["purpose"])
        joined = self._joined()
        self.assertNotIn("session restarted there", joined)

    async def test_comma_in_path_refused_because_purpose_is_comma_separated(self):
        # The Channel Purpose is a comma-separated config list, so a cwd
        # containing a comma reparses to a truncated path plus a stray-token
        # warning. Refuse rather than persist something that can't round-trip.
        target = Path(self.tmp.name) / "a,b"
        target.mkdir()
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = self._joined()
        self.assertIn("comma", joined)
        self.assertNotIn(f"cwd={target}", self.bridge.mm.channels["c1"]["purpose"])

    async def test_active_set_refuses_while_run_active(self):
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self._active_channel()
        self.bridge.current_run_id_by_session["s1"] = "run-s1"

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("run is active", self._joined())

    async def test_active_set_to_same_directory_is_a_noop(self):
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()
        self._active_channel(cwd=target)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("already", self._joined())

    # ── validation ───────────────────────────────────────────────────────

    async def test_relative_path_rejected_naming_the_absolute_requirement(self):
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".cwd projects/foo",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = self._joined()
        self.assertIn("absolute", joined)
        self.assertNotIn("does not exist", joined)

    async def test_missing_path_rejected_naming_existence(self):
        missing = str(Path(self.tmp.name) / "nope")
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {missing}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = self._joined()
        self.assertIn("does not exist", joined)
        self.assertNotIn("absolute", joined)

    async def test_file_path_rejected_as_not_a_directory(self):
        f = Path(self.tmp.name) / "a-file.txt"
        f.write_text("x")
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {f}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("not a directory", self._joined())

    async def test_tilde_is_expanded_and_the_absolute_path_persisted(self):
        import os
        from unittest import mock
        home = Path(self.tmp.name) / "home"
        (home / "work").mkdir(parents=True)
        self._active_channel()

        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            await self.bridge._on_mm_posted({
                "channel_id": "c1", "message": ".cwd ~/work",
                "user_id": "u1", "type": "",
            })

        expanded = str(home / "work")
        self.assertEqual(self.bridge.harness.created[-1]["cwd"], expanded)
        # The Purpose parser is strict (no `~` expansion) — persist absolute.
        self.assertIn(f"cwd={expanded}", self.bridge.mm.channels["c1"]["purpose"])

    async def test_path_outside_allowed_roots_rejected_naming_the_roots(self):
        # `_resolve_cwd` silently degrades an out-of-root cwd to `default_cwd`,
        # so the command must refuse rather than confirm a directory the
        # session would never get.
        from doubles import make_bridge
        allowed = Path(self.tmp.name) / "allowed"
        allowed.mkdir()
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        self.bridge = make_bridge(
            self.tmp.name, echoing=False,
            allowed_attachment_roots=[str(allowed)],
        )
        self._active_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {outside}",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("allowed", joined.lower())
        self.assertIn(str(allowed), joined)

    async def test_no_session_and_not_dormant_replies_no_session(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": ".cwd /tmp",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("no session", self._joined())

    # ── discoverability ──────────────────────────────────────────────────

    async def test_help_lists_cwd(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".help", "user_id": "u1", "type": "",
        })

        self.assertIn(".cwd", "\n".join(self._posted_texts()))


class ThreadConfigSwitchTests(_BridgeTestCase):
    """`.model <name>` / `.backend <name>` must be REFUSED inside a thread
    fork: `_restart_session_with_config` only relinks `Anchor(channel)`, so a
    switch inside a thread would replace the CHANNEL's session while the thread
    keeps its own — a silent mismatch with a false 'restarted' confirmation.
    Bare (read-only) `.model` / `.backend` still work in threads."""

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    def _setup_forked_thread(self) -> None:
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s-chan")
        self.bridge.mapping.link(Anchor("c1", "root1"), "s-fork")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

    async def test_dot_backend_switch_in_thread_refused(self):
        self._setup_forked_thread()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend codex", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        # No restart; both mappings untouched.
        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-chan")
        self.assertEqual(
            self.bridge.mapping.get_session(Anchor("c1", "root1")), "s-fork",
        )
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("thread", joined)
        self.assertIn("channel", joined)

    async def test_dot_model_switch_in_thread_refused(self):
        self._setup_forked_thread()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-chan")
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("thread", joined)

    async def test_dot_cwd_switch_in_thread_refused(self):
        self._setup_forked_thread()
        target = str(Path(self.tmp.name) / "repo")
        Path(target).mkdir()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": f".cwd {target}", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-chan")
        self.assertEqual(
            self.bridge.mapping.get_session(Anchor("c1", "root1")), "s-fork",
        )
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("thread", joined)

    async def test_bare_cwd_in_thread_still_reports_the_forks_directory(self):
        self._setup_forked_thread()
        self.bridge.harness.sessions_meta = [{
            "id": "s-fork", "backend": "codex", "model": "gpt-5.5",
            "project": {"path": "/fork-dir", "name": "fork-dir"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".cwd", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("/fork-dir", "\n".join(self._posted_texts()))

    async def test_bare_model_in_thread_still_reports(self):
        self._setup_forked_thread()  # channel config is claude/opus
        # Give the fork DISTINCT metadata so we can tell whether bare `.model`
        # reports the fork's model (correct) or leaked the channel's (opus).
        self.bridge.harness.sessions_meta = [{
            "id": "s-fork", "backend": "codex", "model": "gpt-5.5",
            "project": {"path": "/x", "name": "x"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        joined = "\n".join(self._posted_texts())
        self.assertIn("Current model: `gpt-5.5`", joined)  # the FORK's model
        self.assertNotIn("opus", joined)  # not the channel session's

    async def test_bare_backend_in_thread_still_reports(self):
        self._setup_forked_thread()  # channel config is claude/opus
        # Distinct fork backend so a leak of the channel session (claude) is
        # caught — bare `.backend` must report the fork's `codex`.
        self.bridge.harness.sessions_meta = [{
            "id": "s-fork", "backend": "codex", "model": "gpt-5.5",
            "project": {"path": "/x", "name": "x"}, "origin": "harness",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".backend", "root_id": "root1",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("Current backend: `codex`", "\n".join(self._posted_texts()))


class CommandPhase3Tests(_BridgeTestCase):
    """`.sessions`, `.invite` — phase 3 (channel creation + invite)."""

    def _posted_texts(self) -> list[str]:
        return [p.message for p in self.bridge.mm.posted]

    # ----- .sessions -----

    async def test_dot_sessions_lists_mapped_and_external(self):
        self.bridge.mapping.link(Anchor("c-mapped"), "s-mapped")
        self.bridge.mm.channels["c-mapped"] = {
            "id": "c-mapped", "display_name": "Mapped One",
        }
        self.bridge.harness.sessions_meta = [
            {"id": "s-mapped", "backend": "claude", "title": "Mapped One",
             "project": {"path": "/a", "name": "a"}, "origin": "harness",
             "updated_at": "2026-07-01T10:00:00Z"},
            {"id": "codex_ext", "backend": "codex", "title": None,
             "project": {"path": "/work/proj", "name": "proj"},
             "origin": "external", "updated_at": "2026-07-05T10:00:00Z"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".sessions",
            "user_id": "u1", "type": "",
        })

        joined = "\n".join(self._posted_texts())
        # External row is discoverable and carries an invite hint.
        self.assertIn("codex_ext", joined)
        self.assertIn(".invite codex_ext", joined)
        # External label falls back to project name (its title is null).
        self.assertIn("proj", joined)
        # Mapped row points at its channel, not an invite hint.
        self.assertIn("Mapped One", joined)

    async def test_dot_sessions_sorts_by_updated_at_desc(self):
        self.bridge.harness.sessions_meta = [
            {"id": "old", "backend": "claude", "title": "Old",
             "project": {"path": "/o", "name": "o"}, "origin": "harness",
             "updated_at": "2026-06-01T00:00:00Z"},
            {"id": "new", "backend": "claude", "title": "New",
             "project": {"path": "/n", "name": "n"}, "origin": "harness",
             "updated_at": "2026-07-06T00:00:00Z"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".sessions", "user_id": "u1", "type": "",
        })

        body = self._posted_texts()[-1]
        self.assertLess(body.index("New"), body.index("Old"))

    async def test_dot_sessions_respects_n_and_filters_suppressed(self):
        self.bridge.harness.sessions_meta = [
            {"id": f"s{i}", "backend": "claude", "title": f"S{i}",
             "project": {"path": "/x", "name": "x"}, "origin": "harness",
             "updated_at": f"2026-07-0{i}T00:00:00Z"}
            for i in range(1, 6)
        ] + [
            {"id": "claude_agent-hidden", "backend": "claude", "title": "Hidden",
             "project": {"path": "/x", "name": "x"}, "origin": "harness",
             "updated_at": "2026-07-09T00:00:00Z"},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".sessions 2", "user_id": "u1", "type": "",
        })

        body = self._posted_texts()[-1]
        # Suppressed agent session never shown.
        self.assertNotIn("Hidden", body)
        # Only 2 rows (the two most recent non-suppressed: S5, S4).
        self.assertIn("S5", body)
        self.assertIn("S4", body)
        self.assertNotIn("S3", body)

    async def test_dot_sessions_harness_unreachable(self):
        async def boom():
            raise RuntimeError("down")
        self.bridge.harness.list_sessions = boom

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".sessions", "user_id": "u1", "type": "",
        })

        self.assertIn("unreachable", "\n".join(self._posted_texts()).lower())

    # ----- .invite -----

    async def test_dot_invite_mapped_session_invites_requester(self):
        self.bridge.mapping.link(Anchor("c-target"), "s-target")
        self.bridge.mm.channels["c-target"] = {
            "id": "c-target", "display_name": "Target",
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".invite s-target",
            "user_id": "u42", "type": "",
        })

        self.assertEqual(self.bridge.mm.invited, [("c-target", "u42")])

    async def test_dot_invite_unmapped_external_creates_channel_and_warns(self):
        self.bridge.harness.sessions_meta = [{
            "id": "codex_ext", "backend": "codex", "title": "Ext",
            "project": {"path": "/work/proj", "name": "proj"},
            "origin": "external",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".invite codex_ext",
            "user_id": "u7", "type": "",
        })

        # A channel was created and mapped to the session.
        anchor = self.bridge.mapping.get_anchor("codex_ext")
        self.assertIsNotNone(anchor)
        # The requester was invited to it.
        self.assertEqual(self.bridge.mm.invited[-1][1], "u7")
        self.assertEqual(self.bridge.mm.invited[-1][0], anchor.channel_id)
        # A resume-fork warning was posted into the new channel.
        warned = [
            p for p in self.bridge.mm.posted
            if p.channel_id == anchor.channel_id and "fork" in p.message.lower()
        ]
        self.assertTrue(warned)

    async def test_dot_invite_pi_external_is_rejected(self):
        self.bridge.harness.sessions_meta = [{
            "id": "pi_ext", "backend": "pi", "title": "PiExt",
            "project": {"path": "/p", "name": "p"}, "origin": "external",
        }]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".invite pi_ext",
            "user_id": "u1", "type": "",
        })

        # No channel created, no invite.
        self.assertIsNone(self.bridge.mapping.get_anchor("pi_ext"))
        self.assertEqual(self.bridge.mm.invited, [])
        self.assertIn("can't be resumed", "\n".join(self._posted_texts()).lower())

    async def test_dot_invite_unknown_session_errors(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".invite nope",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.mm.invited, [])
        joined = "\n".join(self._posted_texts()).lower()
        self.assertIn("no session", joined)

    async def test_dot_invite_without_arg_shows_usage(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".invite", "user_id": "u1", "type": "",
        })

        self.assertIn("Usage", "\n".join(self._posted_texts()))


class FirstMessageNoConfigTests(_BridgeTestCase):
    """First-message backend/model selection was removed (2026-07-09).

    A first user message that *looks* like config tokens (`claude, sonnet`)
    is now forwarded to the agent verbatim — dot-commands and Channel
    Purpose are the only configuration paths. The first conversational message
    creates the dormant channel's session and carries the MM-context preamble.
    """

    async def _prime_channel(self, channel_id: str, purpose: str = "autorespond") -> None:
        """Invite the bot, leaving the channel dormant and configurable."""
        self.bridge.mm.channels[channel_id] = {"id": channel_id, "purpose": purpose}
        await self.bridge._on_mm_user_added(channel_id, self.bridge.mm.bot_user_id)
        assert self.bridge.mapping.get_session(Anchor(channel_id)) is None
        self.bridge.vd.sent.clear()
        self.bridge.vd.created.clear()
        self.bridge.mm.posted.clear()

    async def test_config_token_first_message_is_forwarded_not_intercepted(self):
        await self._prime_channel("c1")
        self.assertIn("c1", self.bridge._dormant_channels)

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "claude, sonnet",
            "user_id": "u1", "type": "",
        })

        # Forwarded to the agent — NOT swallowed as config.
        self.assertEqual(len(self.bridge.vd.sent), 1)
        session_id = self.bridge.mapping.get_session(Anchor("c1"))
        self.assertEqual(self.bridge.vd.sent[0][0], session_id)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("claude, sonnet"))
        # No session restart and no "Config applied" confirmation.
        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertFalse(
            any("Config applied" in p.message for p in self.bridge.mm.posted),
            "first message must not be consumed as config",
        )
        self.assertNotIn("c1", self.bridge._dormant_channels)

    async def test_second_config_looking_message_also_forwards(self):
        await self._prime_channel("c1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hello", "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "codex, gpt-5.5",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        session_id = self.bridge.mapping.get_session(Anchor("c1"))
        self.assertEqual(self.bridge.vd.sent[0][0], session_id)
        # The second message is forwarded verbatim — no preamble, and above
        # all not parsed as config. It carries a held-post stamp because the
        # first run was still tracked when it arrived (hold-and-coalesce);
        # the point of this test is that the CONTENT is untouched.
        self.assertTrue(
            self.bridge.vd.sent[1][1].endswith("codex, gpt-5.5"),
            self.bridge.vd.sent[1][1],
        )


class MergeConfigsTests(_BridgeTestCase):
    """`_merge_configs` layers a freshly-parsed Purpose on top of the
    channel's current Purpose. Per-backend default models live in
    ``Config.default_models`` and are resolved at session-create time —
    so when the operator changes ONLY the backend, the previous model
    token MUST be dropped (otherwise a claude model would leak into a
    codex session and crash `codex exec --model <claude-name>`)."""

    def test_backend_change_drops_carried_model(self):
        from mm_bridge.purpose import PurposeConfig
        current = PurposeConfig(backend="claude", model="sonnet", mention_only=False)
        new = PurposeConfig(backend="codex", model=None, mention_only=False)

        merged = self.bridge._merge_configs(current, new)

        self.assertEqual(merged.backend, "codex")
        # Critical: the carried "sonnet" must NOT survive a backend swap.
        self.assertIsNone(merged.model)

    def test_same_backend_carries_model(self):
        """When the backend doesn't change and the new parse omits the
        model, the current model is preserved (per-channel stickiness)."""
        from mm_bridge.purpose import PurposeConfig
        current = PurposeConfig(backend="claude", model="sonnet", mention_only=False)
        new = PurposeConfig(backend="claude", model=None, mention_only=False)

        merged = self.bridge._merge_configs(current, new)

        self.assertEqual(merged.backend, "claude")
        self.assertEqual(merged.model, "sonnet")

    def test_same_backend_explicit_model_overrides(self):
        """An explicitly-set new model still wins on same-backend updates."""
        from mm_bridge.purpose import PurposeConfig
        current = PurposeConfig(backend="claude", model="sonnet", mention_only=False)
        new = PurposeConfig(backend="claude", model="opus", mention_only=False)

        merged = self.bridge._merge_configs(current, new)

        self.assertEqual(merged.model, "opus")


class MessageContentNotConfigTests(_BridgeTestCase):
    """The bare `autorespond`/`noautorespond` message-content toggle was
    removed (2026-07-10): message content is never config — `.autorespond` is
    the only path. Such words are now forwarded to the agent verbatim and the
    mention flag is left untouched."""

    def _autorespond_channel(self) -> None:
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

    async def test_bare_autorespond_message_is_forwarded(self):
        self._autorespond_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "autorespond")])
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_bare_noautorespond_message_is_forwarded(self):
        self._autorespond_channel()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "noautorespond",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "noautorespond")])
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_autorespond_with_trailing_text_is_regular_message(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond now",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "autorespond now")])


class PurposeUpdateNoticeTests(_BridgeTestCase):
    """Self-written purpose changes must not spawn the "purpose changed"
    notice; genuine external (human) edits still do.

    These exercise the real WS round-trip via
    :class:`EventEchoingMattermostClient` — the double echoes every purpose
    write the bridge makes back as a ``channel_updated`` event, so the dedup
    is verified end-to-end (``deliver_ws_events``) rather than by the test
    author hand-scripting the event payloads.
    """

    async def asyncSetUp(self):  # type: ignore[override]
        await super().asyncSetUp()
        from mm_bridge.typing_indicator import TypingIndicator
        self.bridge.mm = EventEchoingMattermostClient()
        self.bridge.typing = TypingIndicator(self.bridge.mm, refresh_s=0.01)

    def _notices(self) -> list:
        return [
            p for p in self.bridge.mm.posted
            if "takes effect only for new sessions" in p.message
        ]

    async def test_self_triggered_purpose_change_suppresses_notice(self):
        """A bridge-initiated purpose write (here: an autorespond toggle)
        echoes back through the double as a channel_updated event and must
        NOT spawn a user notice."""
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }

        # Real self-write: the literal toggle persists the purpose.
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "noautorespond",
            "user_id": "u1", "type": "",
        })
        await self.bridge.mm.deliver_ws_events(self.bridge)

        self.assertEqual(
            self._notices(), [],
            "self-written purpose must not trigger the change notice",
        )

    async def test_external_purpose_change_still_posts_notice(self):
        """A human editing the Purpose in the MM UI is NOT a bridge self-write
        (the double never queued it), so the notice still fires. This one
        drives the WS handler directly — that's what an external edit is."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "", "purpose": "claude, opus, mention-only",
        })

        self.assertEqual(len(self._notices()), 1)

    async def test_channel_removal_clears_per_channel_config_state(self):
        """Removing the bot must drain the channel's pending self-write set and
        first-forward flag — otherwise a mid-restart removal leaks entries."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge._note_self_wrote_purpose("c1", "claude, opus")
        self.bridge._awaiting_first_forward.add("c1")

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertNotIn("c1", self.bridge._self_written_purpose)
        self.assertNotIn("c1", self.bridge._awaiting_first_forward)

    async def test_multiple_self_writes_all_suppressed(self):
        """Two self-writes in quick succession (as a `.model`/`.backend`
        restart makes: resume block + config) both echo back through the
        double and must both be suppressed — a single-slot tracker remembered
        only the last write and let the first one notify."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }
        first = "claude, opus\n\n---\n\nResume:\n```\ncd /x && claude --resume s1\n```"
        second = "claude, sonnet\n\n---\n\nResume:\n```\ncd /x && claude --resume s2\n```"
        for pur in (first, second):
            self.bridge._note_self_wrote_purpose("c1", pur)
            self.bridge.mm.set_channel_purpose("c1", pur)  # double queues a WS echo

        await self.bridge.mm.deliver_ws_events(self.bridge)

        self.assertEqual(
            self._notices(), [], "all self-written purpose writes must be silent",
        )

    async def test_dot_model_restart_emits_no_purpose_notice(self):
        """End-to-end: a `.model` switch's purpose writes echo back through the
        double as channel_updated events; none may spawn the change notice
        (the change already took effect via restart)."""
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ".model claude-sonnet",
            "user_id": "u1", "type": "",
        })
        # The double replays every purpose write the switch made, in order.
        self.assertGreaterEqual(
            len([p for (c, p) in self.bridge.mm.purposes if c == "c1"]), 1,
        )
        await self.bridge.mm.deliver_ws_events(self.bridge)

        self.assertEqual(
            self._notices(), [],
            "a self-initiated .model switch must not notify",
        )


class SessionAddedClaimTests(_BridgeTestCase):
    async def test_reconcile_resume_purposes_uses_vd_meta_for_cwd_and_backend(self):
        """Reconcile is the only path that runs after a daemon restart, so
        backend + cwd both come from VibeDeck's session metadata. The
        pre-existing resume block is replaced, the config section preserved."""
        self.bridge.mapping.link(Anchor("c1"), "sess-codex")
        self.bridge.mm.channels["c1"] = {
            "id": "c1",
            "purpose": (
                "codex\n\n---\n\nResume:\n```\ncd /old && codex resume old\n```"
            ),
            "header": "Parent: ~root~",
        }
        self.bridge.vd.sessions_meta = [
            {"id": "sess-codex", "projectPath": "/srv/new", "backend": "Codex"},
        ]

        await self.bridge._reconcile_resume_purposes()

        self.assertEqual(
            self.bridge.mm.channels["c1"]["purpose"],
            "codex\n"
            "\n"
            "---\n"
            "\n"
            "Resume:\n"
            "```\n"
            "cd /srv/new && codex resume sess-codex "
            "--yolo\n"
            "```",
        )
        # Header is NOT touched by reconcile.
        self.assertEqual(self.bridge.mm.channels["c1"]["header"], "Parent: ~root~")

    async def test_session_added_without_pending_creates_channel(self):
        await self.bridge._on_vd_event("session_added", {
            "id": "sess-cli", "projectPath": "/tmp/proj",
            "projectName": "my-project", "firstMessage": "hi from CLI",
        })

        self.assertTrue(self.bridge.mapping.get_anchor("sess-cli"))
        self.assertTrue(self.bridge.mm.channels)

    async def test_reconcile_falls_back_to_mm_purpose_when_vd_meta_missing(self):
        """If VibeDeck doesn't know about the session (stale mapping), the
        reconcile pass still emits a Resume block by re-parsing the MM
        Purpose for the backend. Cwd is omitted (no source to trust)."""
        self.bridge.mapping.link(Anchor("c1"), "sess-codex")
        self.bridge.mm.channels["c1"] = {
            "id": "c1",
            "purpose": "codex, gpt-5.4",
            "header": "",
        }
        self.bridge.vd.sessions_meta = []  # VD doesn't know this session.

        await self.bridge._reconcile_resume_purposes()

        self.assertEqual(
            self.bridge.mm.channels["c1"]["purpose"],
            "codex, gpt-5.4\n"
            "\n"
            "---\n"
            "\n"
            "Resume:\n"
            "```\n"
            "codex resume sess-codex "
            "--yolo\n"
            "```",
        )

    async def test_reconcile_unsupported_mm_purpose_skips_write(self):
        """A channel whose persisted MM Purpose names an unsupported backend
        (e.g. `pi`) must NOT have its Purpose touched during reconcile."""
        self.bridge.mapping.link(Anchor("c2"), "sess-pi")
        self.bridge.mm.channels["c2"] = {
            "id": "c2",
            "purpose": "pi",
            "header": "Operator note",
        }
        self.bridge.vd.sessions_meta = [
            {"id": "sess-pi", "projectPath": "/srv", "backend": "pi"},
        ]

        await self.bridge._reconcile_resume_purposes()

        self.assertEqual(self.bridge.mm.channels["c2"]["purpose"], "pi")
        self.assertEqual(self.bridge.mm.purposes, [])

    async def test_auto_created_channel_for_cli_session_gets_resume_block(self):
        """CLI-originated sessions are bound by creating a fresh MM channel
        in `_create_channel_for_session`. That path writes the Resume
        block straight into the newly-created Purpose using the SSE
        backend + projectPath, without waiting for a daemon restart."""
        await self.bridge._on_vd_event("session_added", {
            "id": "sess-cli",
            "projectPath": "/tmp/proj",
            "projectName": "my-project",
            "firstMessage": "hi from CLI",
            "backend": "Codex",
        })

        anchor = self.bridge.mapping.get_anchor("sess-cli")
        self.assertIsNotNone(anchor)
        new_channel = self.bridge.mm.channels[anchor.channel_id]
        self.assertEqual(
            new_channel.get("purpose"),
            "agent-harness session sess-cli\n"
            "\n"
            "---\n"
            "\n"
            "Resume:\n"
            "```\n"
            "cd /tmp/proj && codex resume sess-cli "
            "--yolo\n"
            "```",
        )

    async def test_persist_purpose_preserves_existing_resume_section(self):
        """`_persist_purpose` is the canonical-write path for config changes
        (autorespond toggle, model swap). It must NOT clobber an existing
        resume block — that block lives below the section separator and
        the bridge owns the bottom half independently of config edits."""
        from mm_bridge.purpose import PurposeConfig

        self.bridge.mm.channels["c1"] = {
            "id": "c1",
            "purpose": (
                "claude, opus\n"
                "\n"
                "---\n"
                "\n"
                "Resume:\n```\ncd /tmp && claude --resume s1\n```"
            ),
        }
        cfg = PurposeConfig(
            backend="claude", model="sonnet", mention_only=True,
        )
        self.bridge._persist_purpose("c1", cfg)

        new = self.bridge.mm.channels["c1"]["purpose"]
        # Config section is rewritten in canonical form...
        config_section, resume_section = new.split("\n\n---\n\n", 1)
        self.assertIn("sonnet", config_section)
        self.assertIn("mention-only", config_section)
        # ...and the resume section is preserved verbatim.
        self.assertEqual(
            resume_section,
            "Resume:\n```\ncd /tmp && claude --resume s1\n```",
        )


class ThreadForkTests(_BridgeTestCase):
    async def test_thread_post_in_mapped_channel_calls_fork(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.vd.sessions_meta = [
            {"id": "s1", "projectPath": "/tmp/proj", "backend": "claude"},
        ]
        self.bridge.mm.posts_by_id["r1"] = {"message": "root post body"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "r1", "message": "thread starter",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.forks), 1)
        fork_sid, fork_msg = self.bridge.vd.forks[0]
        self.assertEqual(fork_sid, "s1")
        self.assertIn("Mattermost thread context", fork_msg)
        self.assertIn("> root post body", fork_msg)
        self.assertTrue(fork_msg.endswith("thread starter"))
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1", "r1")), "fork-1")

    async def test_thread_fork_unavailable_marks_dead(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.vd.fork_response = {
            "status": "fork_unavailable", "reason": "opencode", "http_status": 501,
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "r1", "message": "thread starter",
            "user_id": "u1", "type": "",
        })

        self.assertIn(("c1", "r1"), self.bridge.dead_threads)


class AssistantMessageTests(_BridgeTestCase):
    async def test_plain_text_posts_to_channel(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [{"type": "text", "text": "hello there"}],
            },
        })

        self.assertTrue(any(p.message == "hello there" for p in self.bridge.mm.posted))

    async def test_leave_channel_directive_removes_bot(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "text", "text": 'goodbye <leaveChannel reason="done" />'},
                ],
            },
        })

        self.assertIn("c1", self.bridge.mm.removed)
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))


class DirectUserMessageMirrorTests(_BridgeTestCase):
    """User turns that arrive via the coding agent's UI/CLI (role=user from
    SSE) are mirrored back into the bound MM channel so MM watchers see the
    full conversation. Bridge-originated sends, tool results, and synthetic
    fork preambles are all suppressed.
    """

    USER_PREFIX = "_via coding agent:_"

    async def _direct_user_text(self, session_id: str, text: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "user",
                "blocks": [{"type": "text", "text": text}],
            },
        })

    async def test_direct_user_text_block_is_mirrored(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._direct_user_text("s1", "what's the build status?")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertIn("what's the build status?", mirrored[0].message)
        self.assertEqual(mirrored[0].channel_id, "c1")
        self.assertIsNone(mirrored[0].root_id)

    async def test_tool_result_role_user_is_not_mirrored(self):
        """Claude/Codex represent tool results as role=user with a
        tool_result block. These must not leak into MM."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "user",
                "blocks": [{
                    "type": "tool_result",
                    "content": "drwxr-xr-x  4 user  staff  128 ...",
                    "is_error": False,
                }],
            },
        })

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_recent_mm_forwarded_post_does_not_echo_back(self):
        """When MM forwards a user post to VD via send_message, VD's
        transcript echoes it back as role=user. The bridge must dedup
        against its recent-send window so the channel doesn't double up.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": None, "message": "hello agent",
            "user_id": "u1", "type": "", "id": "p-mm-1",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hello agent")])

        # VD echoes the same body back as role=user. Must not produce
        # an extra MM post.
        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s1", "hello agent")
        after = len(self.bridge.mm.posted)
        self.assertEqual(
            after, before,
            "MM-originated message must not be re-mirrored as a direct turn",
        )

    async def test_first_message_after_invite_claim_is_swallowed(self):
        """The first user-role event after an invite-claim is the
        firstMessage we shipped via create_session — not direct input."""
        self.bridge.mapping.link(Anchor("c1"), "s-inv")
        self.bridge._record_harness_send("s-inv", "kick-off prompt")

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-inv", "kick-off prompt")
        self.assertEqual(len(self.bridge.mm.posted), before)

    async def test_invite_first_message_truncated_in_session_added_still_dedups(self):
        """Regression: VD's `firstMessage` field in `session_added` is
        truncated to 200 chars by every backend's `get_first_user_message`.
        When the bridge ships a >200-char body via `create_session` (e.g.
        with `initial_catch_up_n` > 0 prepending a catch-up block), the
        role=user echo VD broadcasts later carries the full body — so
        recording the truncated `firstMessage` for dedup misses the echo
        and the catch-up block leaks into MM as `_via coding agent: …`.
        Fix: record `pending.initial_message` (the full body actually
        shipped) instead of `firstMessage`."""
        full_body = (
            "[catch-up: last 50 messages]\n"
            + ("\n".join(f"- u{i}: line {i} of context" for i in range(50)))
            + "\n\nkick-off prompt"
        )
        # Sanity: this is the regime the bug applies to.
        self.assertGreater(len(full_body), 200)
        truncated = full_body[:200]

        self.bridge.mapping.link(Anchor("c1"), "s-inv")
        self.bridge._record_harness_send("s-inv", full_body)

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-inv", full_body)
        self.assertEqual(
            len(self.bridge.mm.posted), before,
            "Truncated firstMessage in session_added must not defeat "
            "dedup of the full-body role=user echo.",
        )

    async def test_fork_message_echo_is_swallowed(self):
        """Symmetric to the invite truncation case. On Claude Code,
        `vd.fork_session(parent, fork_message)` ships `fork_message` via
        stdin; Claude writes it into the new session's transcript and
        VD broadcasts it as a role=user `message` event. The bridge must
        record `pending.initial_message` (= `fork_message`) so the echo
        is suppressed — otherwise the wrapped thread-context preamble
        plus the user's MM thread reply gets re-posted back into the
        same thread as `_via coding agent: …`.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")
        fork_message = (
            "[Mattermost thread context] You are continuing the parent "
            "conversation in a Mattermost thread. The user replied to "
            "this message:\n\n> root post body\n\n"
            "Their reply follows:\n\n"
            "thread starter"
        )
        self.bridge.mapping.link(Anchor("c1", "r1"), "s-fork")
        self.bridge._record_harness_send("s-fork", fork_message)

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-fork", fork_message)
        self.assertEqual(
            len(self.bridge.mm.posted), before,
            "fork_message echo from the new session must be deduplicated, "
            "not mirrored as a `_via coding agent: …` post.",
        )

    async def test_fork_continuation_preamble_is_swallowed(self):
        """Claude Code forks emit a synthetic continuation summary as
        firstMessage. It is NOT user-typed input — must be suppressed."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        synth = "This session is being continued from a previous conversation..."
        self.bridge.mapping.link(Anchor("c1", "r1"), "s-fork")
        self.bridge._record_harness_send("s-fork", synth)

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-fork", synth)
        self.assertEqual(len(self.bridge.mm.posted), before)

    async def test_autonomous_session_first_message_is_mirrored(self):
        """A session VD created on its own (no MM origin) gets a fresh
        channel and the firstMessage IS direct input — must surface in MM.
        """
        await self.bridge._on_vd_event("session_added", {
            "id": "s-cli", "projectPath": "/tmp/proj", "backend": "claude",
            "firstMessage": "started a session locally",
            "summaryTitle": "Local",
        })

        anchor = self.bridge.mapping.get_anchor("s-cli")
        self.assertIsNotNone(anchor)

        await self._direct_user_text("s-cli", "started a session locally")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertIn("started a session locally", mirrored[0].message)

    async def test_thread_anchor_routes_to_thread_root(self):
        self.bridge.mapping.link(Anchor("c1", "r1"), "s-thr")

        await self._direct_user_text("s-thr", "thread-only thought")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertEqual(mirrored[0].channel_id, "c1")
        self.assertEqual(mirrored[0].root_id, "r1")

    async def test_disabled_via_config(self):
        self.bridge.config.mirror_direct_user_messages = False
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._direct_user_text("s1", "should not show")

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_unbound_session_silently_dropped(self):
        await self._direct_user_text("s-orphan", "no anchor here")

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_dedup_window_expires(self):
        """An echo arriving long after the send window must be mirrored.
        We model this by directly draining the recent-send entry, then
        firing the same body as a direct turn."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": None, "message": "stale body",
            "user_id": "u1", "type": "", "id": "p-mm-1",
        })
        self.bridge.config.direct_user_message_dedup_window_seconds = 0.0
        # After zeroing the window, the recent-send entry is past — a
        # direct echo of the same body must now mirror.
        await self._direct_user_text("s1", "stale body")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)


class ToolUseCoalescingTests(_BridgeTestCase):
    """Verify tool-use blocks coalesce into a single per-turn placeholder
    post that gets hard-deleted when the turn ends.
    """

    async def _tool_use(self, session_id: str, tool: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "blocks": [{"type": "tool_use", "tool_name": tool}],
            },
        })

    async def _text(self, session_id: str, text: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "blocks": [{"type": "text", "text": text}],
            },
        })

    async def test_single_tool_use_creates_placeholder(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1)
        self.assertEqual(tool_posts[0].message, "_Using tool: Bash_")

    async def test_repeat_tool_bumps_counter_via_edit(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        for _ in range(3):
            await self._tool_use("s1", "Bash")

        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1, "only one placeholder post should be created")
        self.assertEqual(self.bridge.mm.edits[-1][1], "_Using tool: Bash (x3)_")

    async def test_different_tool_adds_new_line(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Read")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")

        final = self.bridge.mm.edits[-1][1]
        self.assertEqual(
            final,
            "_Using tool: Bash (x2)_\n_Using tool: Read_\n_Using tool: Bash (x2)_",
        )
        # Still one underlying post.
        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1)

    async def test_real_text_ends_run_without_deleting(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")

        await self._text("s1", "here is the answer")

        # Placeholder is NOT deleted — it stays in the channel as a
        # compact record of the tools used this turn.
        self.assertEqual(self.bridge.mm.deletes, [])
        # But state is dropped so the next turn starts fresh.
        self.assertNotIn("s1", self.bridge.tool_use_runs)
        self.assertTrue(
            any(p.message == "here is the answer" for p in self.bridge.mm.posted),
        )

    async def test_next_turn_creates_fresh_placeholder(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        first_placeholder_id = self.bridge.tool_use_runs["s1"].post_id
        await self._text("s1", "ok done")
        await self._tool_use("s1", "Read")

        # Different post id — a new placeholder, not an edit of the old.
        self.assertNotEqual(
            self.bridge.tool_use_runs["s1"].post_id, first_placeholder_id,
        )

    async def test_mixed_block_event_text_then_tool(self):
        """A single event with [text, tool_use] posts the text, then starts
        a fresh tool-use run for the trailing tool.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "text", "text": "planning next step"},
                    {"type": "tool_use", "tool_name": "Bash"},
                ],
            },
        })

        self.assertTrue(any(p.message == "planning next step" for p in self.bridge.mm.posted))
        self.assertIn("s1", self.bridge.tool_use_runs)
        self.assertEqual(
            self.bridge.tool_use_runs["s1"].lines, [["Bash", 1]],
        )

    async def test_tool_error_ends_run_without_deleting(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "tool_result", "is_error": True, "content": "boom"},
                ],
            },
        })

        self.assertEqual(self.bridge.mm.deletes, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)
        self.assertTrue(any("boom" in p.message for p in self.bridge.mm.posted))

    async def test_session_status_stopped_ends_run(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        await self.bridge._on_vd_event("session_status", {
            "session_id": "s1", "running": False,
        })

        self.assertEqual(self.bridge.mm.deletes, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)

    async def test_placeholder_is_never_deleted(self):
        """Safety net: regardless of turn end-reason, the placeholder post
        is preserved in the channel as a permanent record.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")
        for _ in range(3):
            await self._tool_use("s1", "Bash")
        await self._text("s1", "done")

        self.assertEqual(self.bridge.mm.deletes, [])

    async def test_show_tool_use_false_suppresses_placeholder(self):
        """When `show_tool_use=False`, tool_use blocks are silently
        dropped — no post, no edit, no run state. Real text still posts.
        """
        self.bridge.config.show_tool_use = False
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Read")

        self.assertFalse(
            any("Using tool" in p.message for p in self.bridge.mm.posted),
        )
        self.assertEqual(self.bridge.mm.edits, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)

        await self._text("s1", "here's the answer")
        self.assertTrue(
            any(p.message == "here's the answer" for p in self.bridge.mm.posted),
        )

    async def test_sessions_isolated(self):
        """Two live sessions must not share placeholder state."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mapping.link(Anchor("c2"), "s2")

        await self._tool_use("s1", "Bash")
        await self._tool_use("s2", "Read")
        await self._tool_use("s1", "Bash")

        self.assertEqual(self.bridge.tool_use_runs["s1"].lines, [["Bash", 2]])
        self.assertEqual(self.bridge.tool_use_runs["s2"].lines, [["Read", 1]])


class NameSyncTests(_BridgeTestCase):
    async def test_name_sync_prevents_ping_pong(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.name_sync.note_remote_update("mm", "c1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "old", "purpose": "",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "new-name", "purpose": "",
        })

        self.assertEqual(self.bridge.vd.titles, [])


class AutoJoinTests(_BridgeTestCase):
    """`auto_join_public_channels` opts the bot into team-wide presence.

    Joining is silent — no VD session until a user engages (by @mention
    under mention-only, or any message under autorespond). Self-joins must
    not be treated as external invites.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config.auto_join_public_channels = True
        self.bridge._dormant_channels.add("c1")

    async def test_channel_created_event_joins_when_enabled(self):
        await self.bridge._on_mm_channel_created("c-new")
        self.assertIn("c-new", self.bridge.mm.joined)
        # Session-less silent presence; the resulting user_added must not
        # spawn an invite flow.
        await self.bridge._on_mm_user_added("c-new", self.bridge.mm.bot_user_id)
        self.assertIsNone(self.bridge.mapping.get_session("c-new"))
        self.assertEqual(self.bridge.vd.created, [])
        self.assertIn("c-new", self.bridge._dormant_channels)

    async def test_channel_created_event_ignored_when_disabled(self):
        self.config.auto_join_public_channels = False
        await self.bridge._on_mm_channel_created("c-new")
        self.assertEqual(self.bridge.mm.joined, [])

    async def test_mention_triggers_engagement_session(self):
        # Channel exists, bot joined silently; no mapping yet.
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude hello there",
            "user_id": "u1", "type": "",
        })

        # VD session created with the engagement message (mention stripped).
        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("hello there"))
        # Synchronous harness create links immediately; no SSE claim remains.
        self.assertEqual(
            self.bridge.mapping.get_session(Anchor("c1")),
            self.bridge.vd.next_session_id,
        )
        self.assertNotIn("c1", self.bridge.warming_up_sessions)
        self.assertNotIn(
            "c1",
            self.bridge._awaiting_first_forward,
            "engagement sessions already consumed their first message",
        )
        # No welcome message for engagement — the response is the welcome.
        self.assertEqual(self.bridge.mm.posted, [])

    async def test_non_mention_ignored_under_mention_only_default(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "just chatting with my team",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.created, [])
        self.assertNotIn("c1", self.bridge.warming_up_sessions)

    async def test_autorespond_purpose_engages_on_any_message(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "no mention at all",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("no mention at all"))

    async def test_mention_with_config_tokens_starts_session_and_forwards(self):
        """Config-via-message on auto-join is gone (2026-07-09): `@claude
        autorespond` no longer configures the channel without a session — it
        starts one and is forwarded verbatim, exactly like the invite path.
        Configuration is dot-commands / Channel Purpose only."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude autorespond",
            "user_id": "u1", "type": "",
        })

        # A session was created and the message forwarded verbatim.
        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("autorespond"))
        # NOT consumed as config — no "Config applied" notice.
        self.assertFalse(
            any("Config applied" in p.message for p in self.bridge.mm.posted),
            "engagement message must not be swallowed as config",
        )

    async def test_mention_with_config_alias_also_forwards(self):
        """A config-alias word (`noautoresponse`) is likewise forwarded, not
        applied as config."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude noautoresponse",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("noautoresponse"))
        self.assertFalse(
            any("Config applied" in p.message for p in self.bridge.mm.posted),
        )

    async def test_mention_with_chat_still_engages(self):
        """Chat text with unknown tokens should still start a session."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude hello there",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("hello there"))

    async def test_engagement_chat_engages_with_empty_model_catalog(self):
        """The first engagement message always starts a session and is
        forwarded verbatim — never parsed as config. Regression for the
        2026-05-12 new-channel bug where message content was rewritten into
        the channel purpose instead of starting a session (the pre-session
        config parser that caused it was removed 2026-07-09)."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}
        # Mirror live agent-harness behaviour: empty model lists.
        self.bridge.harness.models_by_backend = {
            "claude": [], "codex": [], "pi": [], "opencode": [],
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "Hi Claude!",
            "user_id": "u1", "type": "",
        })

        # The message must start a session, not be swallowed as config.
        self.assertEqual(
            len(self.bridge.vd.created), 1,
            "expected an engagement session, but the message was eaten as config",
        )
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("Hi Claude!"))
        # And no config-applied notice should have gone to MM.
        applied_posts = [
            p for p in self.bridge.mm.posted
            if "Config applied" in (p.message or "")
        ]
        self.assertEqual(applied_posts, [])

    async def test_engagement_disabled_when_auto_join_disabled(self):
        self.config.auto_join_public_channels = False
        self.bridge._dormant_channels.discard("c1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "no mention at all",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.created, [])


class FirstMessagePreambleTests(_BridgeTestCase):
    """When a user posts the first message after invite, prepend a short
    MM-context preamble to the forwarded VD message."""

    async def _prime_channel_with_members(
        self,
        channel_id: str,
        *,
        display_name: str,
        members: list[dict],
    ) -> str:
        """Set up a mapped channel with `_awaiting_first_forward` set and the
        given member roster. Returns the session_id."""
        self.bridge.mm.channels[channel_id] = {
            "id": channel_id, "purpose": "", "display_name": display_name,
        }
        for m in members:
            uid = m["user_id"]
            self.bridge.mm.users[uid] = {
                "id": uid,
                "username": m.get("username") or f"u-{uid[:4]}",
                "is_bot": bool(m.get("is_bot")),
            }
        self.bridge.mm.channel_members[channel_id] = [
            {"user_id": m["user_id"]} for m in members
        ]
        session_id = "s-primed"
        self.bridge.mapping.link(Anchor(channel_id), session_id)
        self.bridge._awaiting_first_forward.add(channel_id)
        return session_id

    async def test_single_user_preamble_prepended(self) -> None:
        session_id = await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hi there",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent = self.bridge.vd.sent[0][1]
        self.assertTrue(
            sent.startswith(
                '[Running inside Mattermost channel "Bug Bash". '
                "You're talking to @alice. "
                "@-mention to keep their attention.]"
            ),
            f"preamble missing or malformed; got: {sent!r}",
        )
        self.assertIn("hi there", sent)

    async def test_multi_user_preamble_lists_humans_only(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="team-room",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
                {"user_id": "u-bob", "username": "bob"},
                {"user_id": "u-gpt", "username": "gpt", "is_bot": True},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "ping",
            "user_id": "u-alice", "type": "",
        })

        sent = self.bridge.vd.sent[0][1]
        self.assertIn(
            "Multiple users in this channel (`alice`, `bob`) "
            "— messages are prefixed with `username:`",
            sent,
        )
        self.assertNotIn("gpt", sent)  # bot excluded

    async def test_zero_humans_omits_user_sentence(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="bots-only",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
            ],
        )
        # A human posts even though they're not (yet) in the member list.
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stranger here",
            "user_id": "u-stranger", "type": "",
        })

        sent = self.bridge.vd.sent[0][1]
        self.assertTrue(sent.startswith(
            '[Running inside Mattermost channel "bots-only". '
            "@-mention to keep their attention.]"
        ))

    async def test_preamble_not_added_on_second_message(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "first",
            "user_id": "u-alice", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "second",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        self.assertIn("Running inside Mattermost channel", self.bridge.vd.sent[0][1])
        self.assertNotIn("Running inside Mattermost channel", self.bridge.vd.sent[1][1])

    async def test_config_word_first_message_forwards_with_preamble(self) -> None:
        """A bare `autorespond` first message is no longer special (the
        message-content toggle was removed) — it's forwarded verbatim and
        carries the MM-context preamble like any other first message."""
        await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[{"user_id": "u-alice", "username": "alice"}],
        )
        # Clear sent from any prior wiring.
        self.bridge.vd.sent.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent = self.bridge.vd.sent[0][1]
        self.assertIn("Running inside Mattermost channel", sent)
        self.assertTrue(sent.endswith("autorespond"))

    async def test_preamble_format_helper_single_user(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", ["alice"])
        self.assertEqual(
            out,
            '[Running inside Mattermost channel "My Chan". '
            "You're talking to @alice. "
            "@-mention to keep their attention.]",
        )

    async def test_preamble_format_helper_multi_user(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", ["alice", "bob", "carol"])
        self.assertIn("Multiple users in this channel (`alice`, `bob`, `carol`)", out)
        self.assertIn("messages are prefixed with `username:`", out)

    async def test_preamble_format_helper_empty(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", [])
        self.assertEqual(
            out,
            '[Running inside Mattermost channel "My Chan". '
            "@-mention to keep their attention.]",
        )


class MentionUserWhenDoneTests(_BridgeTestCase):
    """`mention_user_when_done` — post `@<username>` in the session's anchor
    when the harness run ends, targeted at the user whose MM post triggered it."""

    async def _complete_run(self, session_id: str) -> None:
        await self.bridge._on_harness_run_lifecycle(
            "run.completed",
            {"session_id": session_id},
        )

    async def _trigger_run(self, channel_id: str, user_id: str, session_id: str) -> None:
        self.bridge.mapping.link(Anchor(channel_id), session_id)
        await self.bridge._on_mm_posted({
            "channel_id": channel_id, "message": "do a thing",
            "user_id": user_id, "type": "",
        })

    async def test_posts_mention_to_channel_on_run_end(self) -> None:
        await self._trigger_run("c1", "u1", "s1")

        await self._complete_run("s1")

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].channel_id, "c1")
        self.assertEqual(mentions[0].message, "@u-u1")
        self.assertIsNone(mentions[0].root_id)

    async def test_posts_mention_into_thread_when_anchor_is_thread(self) -> None:
        self.bridge.mapping.link(Anchor("c1", "root-post"), "s-thread")
        # Feed a forwarded user post directly via the low-level path, since
        # `_on_mm_posted` has its own thread dispatch logic.
        await self.bridge._forward_user_post(
            "c1", "s-thread",
            {"user_id": "u2", "message": "hi", "type": ""},
            "hi", "root-post", first_message=False,
        )

        await self._complete_run("s-thread")

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].root_id, "root-post")
        self.assertEqual(mentions[0].message, "@u-u2")

    async def test_no_mention_when_no_triggerer_tracked(self) -> None:
        # Session exists but no user post was forwarded (e.g. autorespond loop).
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._complete_run("s1")

        self.assertFalse(any(p.message.startswith("@") for p in self.bridge.mm.posted))

    async def test_triggerer_consumed_on_use(self) -> None:
        await self._trigger_run("c1", "u1", "s1")

        # First completion event pings.
        await self._complete_run("s1")
        # A second running=false (e.g. spurious duplicate) must NOT re-ping.
        await self._complete_run("s1")

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)

    async def test_disabled_by_config(self) -> None:
        self.bridge.config.mention_user_when_done = False
        await self._trigger_run("c1", "u1", "s1")

        await self._complete_run("s1")

        self.assertFalse(any(p.message.startswith("@") for p in self.bridge.mm.posted))

    async def test_second_run_pings_most_recent_triggerer(self) -> None:
        # Alice triggers → run ends → ping @alice. Then Bob triggers → run
        # ends → ping @bob, not @alice.
        await self._trigger_run("c1", "u-alice", "s1")
        await self._complete_run("s1")
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "my turn",
            "user_id": "u-bob", "type": "",
        })
        await self._complete_run("s1")

        mentions = [p.message for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(mentions, ["@u-u-al", "@u-u-bo"])


class HarnessEventEnvelopeTests(_BridgeTestCase):
    """The live harness places ``session_id`` and ``run_id`` at the top
    level of the SSE Event envelope, NOT inside the inner ``data`` payload
    (see ``models.py: class Event`` in agent-harness). Bridge handlers
    must see them through ``_on_harness_event``'s dispatch.
    """

    async def test_message_event_with_top_level_session_id_dispatches(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        await self.bridge._on_harness_event(
            "message",
            {
                "sequence": 5,
                "event": "message",
                "data": {
                    "backend": "codex",
                    "origin": "harness",
                    "message": {
                        "role": "assistant",
                        "blocks": [{"type": "text", "text": "hello there"}],
                    },
                },
                "session_id": "codex_s1",
                "run_id": "run-1",
            },
        )

        self.assertTrue(
            any(p.message == "hello there" for p in self.bridge.mm.posted),
            f"assistant text was dropped; posts={[p.message for p in self.bridge.mm.posted]}",
        )

    async def test_run_completed_with_top_level_run_id_clears_state(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")
        self.bridge.current_run_id_by_session["codex_s1"] = "run-1"

        await self.bridge._on_harness_event(
            "run.completed",
            {
                "sequence": 10,
                "event": "run.completed",
                "data": {"stop_reason": "end_turn"},
                "session_id": "codex_s1",
                "run_id": "run-1",
            },
        )

        self.assertNotIn("codex_s1", self.bridge.current_run_id_by_session)

    async def test_tool_use_block_uses_name_field(self):
        """The harness ToolUseBlock serialises the tool identifier as
        ``name`` (per ``models.py: class ToolUseBlock``), not ``tool_name``.
        Bridge must read ``name`` so the placeholder shows the real tool.
        """
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        await self.bridge._on_harness_event(
            "message",
            {
                "sequence": 7,
                "event": "message",
                "data": {
                    "message": {
                        "role": "assistant",
                        "blocks": [{"type": "tool_use", "name": "Bash"}],
                    },
                },
                "session_id": "codex_s1",
                "run_id": "run-1",
            },
        )

        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1)
        self.assertIn("Bash", tool_posts[0].message)
        self.assertNotIn("unknown", tool_posts[0].message)


class ChannelJoinWelcomeTests(_BridgeTestCase):
    """Channel-join welcome — a manual-style post fired when the bot is
    added to a channel (auto-join OR /invite), before session creation.
    """

    def _welcomes(self) -> list:
        return [
            p for p in self.bridge.mm.posted
            if p.props and p.props.get("from_bridge") == "welcome"
        ]

    async def test_invite_posts_join_welcome_while_channel_is_dormant(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        welcomes = self._welcomes()
        self.assertEqual(len(welcomes), 1, "exactly one join welcome on /invite")
        # Body checkpoints — the elevator pitch (with the configured bot
        # username), the dot-command config hints, and the backend list.
        body = welcomes[0].message
        self.assertIn("@claude", body)  # bot_username from the fake
        self.assertIn("`.model", body)
        self.assertIn("`.backend", body)
        self.assertIn("Channel Purpose", body)
        self.assertIn("`claude`", body)  # backend list
        # The removed first-message selector must NOT be advertised anymore.
        self.assertNotIn("First message:", body)
        self.assertNotIn("Pick a backend", body)
        self.assertEqual(self.bridge.harness.created, [])
        self.assertIn("c1", self.bridge._dormant_channels)

    def test_session_start_welcome_points_to_dot_commands_not_first_message(self):
        from mm_bridge.purpose import PurposeConfig
        cfg = PurposeConfig(backend="claude", model="opus", mention_only=False)
        body = self.bridge._format_welcome(cfg, "/tmp/proj")
        # The removed first-message reconfig hint is gone...
        self.assertNotIn("First message", body)
        self.assertNotIn("reconfigure", body)
        # ...replaced by the dot-command config hints.
        self.assertIn(".model", body)
        self.assertIn(".backend", body)

    async def test_welcome_points_to_dot_help_and_dot_stop(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        body = self._welcomes()[0].message
        self.assertIn("`.help`", body)
        self.assertIn("`.stop`", body)

    async def test_auto_join_posts_join_welcome_with_silent_presence(self):
        self.config.auto_join_public_channels = True
        # Mark `c-new` as self-joined the same way the reconciler would,
        # then drive the user_added event.
        self.bridge._self_joined_channels.add("c-new")
        self.bridge.mm.channels["c-new"] = {"id": "c-new", "purpose": ""}

        await self.bridge._on_mm_user_added("c-new", self.bridge.mm.bot_user_id)

        # Silent presence — no session yet.
        self.assertEqual(self.bridge.vd.created, [])
        # But the welcome did go out.
        self.assertEqual(len(self._welcomes()), 1)

    async def test_re_invite_posts_a_second_welcome(self):
        """Idempotent at the call site, deliberate at the post site: a
        re-add IS the user reaching out again, so welcome them again."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)
        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self._welcomes()), 2)

    async def test_welcome_reflects_configured_default_models(self):
        """The backend list comes from ``Config.default_models`` — only
        configured backends appear, so unimplemented entries from
        ``KNOWN_BACKENDS`` (pi, opencode) never get advertised."""
        self.config.default_models = {"claude": "sonnet"}  # only one entry
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        body = self._welcomes()[0].message
        self.assertIn("`claude` (default `sonnet`)", body)
        # codex/pi/opencode all absent from the backend list — only
        # configured ones appear.
        self.assertNotIn("`codex`", body)
        self.assertNotIn("`pi`", body)
        self.assertNotIn("`opencode`", body)

    async def test_welcome_names_channel_effective_config_when_purpose_set(self):
        """When the Purpose already pins a backend/model, the welcome
        appends a 'this channel: …' summary so users see what they got."""
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, sonnet, mention-only",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        body = self._welcomes()[0].message
        self.assertIn("This channel:", body)
        self.assertIn("`claude`", body)
        self.assertIn("`sonnet`", body)
        self.assertIn("mention-only", body)

    async def test_welcome_carries_filter_marker_prop(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        welcomes = self._welcomes()
        self.assertEqual(welcomes[0].props, {"from_bridge": "welcome"})

    async def test_get_channel_failure_still_posts_welcome(self):
        """If we can't fetch the channel (network blip, racing delete),
        we still post a generic welcome rather than nothing."""
        def boom(_channel_id):
            raise RuntimeError("simulated mm.get_channel failure")
        self.config.auto_join_public_channels = True
        self.bridge._self_joined_channels.add("c-new")
        self.bridge.mm.get_channel = boom  # type: ignore[assignment]

        await self.bridge._on_mm_user_added("c-new", self.bridge.mm.bot_user_id)

        welcomes = self._welcomes()
        self.assertEqual(len(welcomes), 1)
        # No "This channel: …" line because we couldn't derive cfg.
        self.assertNotIn("This channel:", welcomes[0].message)


class HarnessWatchdogEventsTests(_BridgeTestCase):
    """Watchdog events from the harness ``RunProcess`` watchdogs:

    - ``run.terminated_after_end_turn`` (subprocess didn't exit within
      grace window after ``end_turn``) — render SILENT.
    - ``run.timed_out_idle`` (idle watchdog force-stopped a silent run) —
      VISIBLE warning post in the session anchor.

    agent-harness v0.1.1 (idle-watchdog spec, PR #36) added three more:
    - ``run.idle_warning`` (idle threshold crossed but the process tree is
      CPU-busy — kill DEFERRED) — reassure that work continues.
    - ``run.max_runtime_warning`` (emitted a lead window before the hard
      runtime cap) — warn the run will be stopped soon.
    - ``run.exceeded_max_runtime`` (the cap kill) — the session is fine.

    Every notice event posts AT MOST ONCE per run (no repeats if the
    harness re-emits). All events are supplemental; a normal terminal event
    still follows, so this handler must NOT touch typing/run-id state.
    """

    @staticmethod
    def _envelope(event: str, data: dict, *, session="codex_s1", run="run-1"):
        return {
            "sequence": 1,
            "event": event,
            "data": data,
            "session_id": session,
            "run_id": run,
        }

    async def test_terminated_after_end_turn_is_silent(self):
        """The LLM already finished its turn — no operator-facing post."""
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")
        posts_before = list(self.bridge.mm.posted)

        await self.bridge._on_harness_event(
            "run.terminated_after_end_turn",
            self._envelope(
                "run.terminated_after_end_turn",
                {
                    "grace_seconds": 20,
                    "hard_kill": False,
                    "returncode": -15,
                    "reason": "subprocess_did_not_exit_after_end_turn",
                },
            ),
        )

        # mm.posted unchanged — purely silent.
        self.assertEqual(self.bridge.mm.posted, posts_before)

    async def test_timed_out_idle_posts_warning_to_anchor(self):
        from mm_bridge.watchdog_notices import idle_timeout_notice
        self.bridge.mapping.link(Anchor("c1", "root-post"), "codex_s1")

        payload = {
            "idle_seconds": 600,
            "last_activity_event": "message.delta",
            "last_activity_at": "2026-05-17T12:00:00Z",
            "hard_kill": False,
            "reason": "no_activity_within_threshold",
        }
        await self.bridge._on_harness_event(
            "run.timed_out_idle", self._envelope("run.timed_out_idle", payload),
        )

        expected = idle_timeout_notice(payload)
        warnings = [p for p in self.bridge.mm.posted if p.message == expected]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].channel_id, "c1")
        self.assertEqual(warnings[0].root_id, "root-post")
        # Regression: the stale "30 minutes" copy is gone; the threshold now
        # comes from the payload (600s → 10 min).
        self.assertNotIn("30 minutes", warnings[0].message)
        self.assertIn("10 min", warnings[0].message)

    async def test_timed_out_idle_unmapped_session_does_not_post(self):
        """Without an anchor we have nowhere to put the warning — log and
        return, do not raise."""
        # No mapping.link — session is unknown.
        posts_before = list(self.bridge.mm.posted)

        await self.bridge._on_harness_event(
            "run.timed_out_idle",
            self._envelope(
                "run.timed_out_idle",
                {
                    "idle_seconds": 600,
                    "last_activity_event": "message.delta",
                    "last_activity_at": "2026-05-17T12:00:00Z",
                    "hard_kill": False,
                    "reason": "no_activity_within_threshold",
                },
                session="unknown_session",
            ),
        )

        self.assertEqual(self.bridge.mm.posted, posts_before)

    async def test_timed_out_idle_swallows_mm_post_failure(self):
        """If MM rejects the warning post, log and move on — don't bubble
        the exception up through the SSE dispatcher (matches the pattern
        used by ``_mention_triggerer_on_done``)."""
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        def boom(*args, **kwargs):
            raise RuntimeError("simulated mm.post failure")

        self.bridge.mm.post = boom  # type: ignore[assignment]

        # Must not raise.
        await self.bridge._on_harness_event(
            "run.timed_out_idle",
            self._envelope(
                "run.timed_out_idle",
                {
                    "idle_seconds": 600,
                    "last_activity_event": "tool_use",
                    "last_activity_at": "2026-05-17T12:00:00Z",
                    "hard_kill": True,
                    "reason": "no_activity_within_threshold",
                },
            ),
        )

    # ---- new v0.1.1 watchdog warning events ----

    async def test_idle_warning_posts_notice_to_anchor(self):
        from mm_bridge.watchdog_notices import idle_warning_notice
        self.bridge.mapping.link(Anchor("c1", "root-post"), "codex_s1")

        payload = {
            "idle_seconds": 605,
            "idle_timeout_seconds": 600,
            "cpu_seconds_delta": 3.2,
            "reason": "process_tree_busy_deferring_kill",
        }
        await self.bridge._on_harness_event(
            "run.idle_warning", self._envelope("run.idle_warning", payload),
        )

        notes = [
            p for p in self.bridge.mm.posted
            if p.message == idle_warning_notice(payload)
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].channel_id, "c1")
        self.assertEqual(notes[0].root_id, "root-post")

    async def test_max_runtime_warning_posts_notice_to_anchor(self):
        from mm_bridge.watchdog_notices import max_runtime_warning_notice
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        payload = {
            "max_run_seconds": 3600,
            "seconds_until_kill": 600,
            "reason": "approaching_max_runtime",
        }
        await self.bridge._on_harness_event(
            "run.max_runtime_warning",
            self._envelope("run.max_runtime_warning", payload),
        )

        notes = [
            p for p in self.bridge.mm.posted
            if p.message == max_runtime_warning_notice(payload)
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].channel_id, "c1")

    async def test_exceeded_max_runtime_posts_notice_to_anchor(self):
        from mm_bridge.watchdog_notices import exceeded_max_runtime_notice
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        payload = {
            "max_run_seconds": 3600,
            "hard_kill": False,
            "reason": "max_runtime_exceeded",
        }
        await self.bridge._on_harness_event(
            "run.exceeded_max_runtime",
            self._envelope("run.exceeded_max_runtime", payload),
        )

        notes = [
            p for p in self.bridge.mm.posted
            if p.message == exceeded_max_runtime_notice(payload)
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].channel_id, "c1")

    async def test_idle_warning_unmapped_session_does_not_post(self):
        posts_before = list(self.bridge.mm.posted)

        await self.bridge._on_harness_event(
            "run.idle_warning",
            self._envelope(
                "run.idle_warning",
                {"idle_seconds": 605, "idle_timeout_seconds": 600},
                session="unknown_session",
            ),
        )

        self.assertEqual(self.bridge.mm.posted, posts_before)

    async def test_idle_warning_swallows_mm_post_failure(self):
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        def boom(*args, **kwargs):
            raise RuntimeError("simulated mm.post failure")

        self.bridge.mm.post = boom  # type: ignore[assignment]

        # Must not raise.
        await self.bridge._on_harness_event(
            "run.idle_warning",
            self._envelope(
                "run.idle_warning",
                {"idle_seconds": 605, "idle_timeout_seconds": 600},
            ),
        )

    async def test_watchdog_notice_deduped_per_run(self):
        """A re-emitted warning for the same run posts only once."""
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")
        payload = {"idle_seconds": 605, "idle_timeout_seconds": 600}

        for _ in range(3):
            await self.bridge._on_harness_event(
                "run.idle_warning", self._envelope("run.idle_warning", payload),
            )

        from mm_bridge.watchdog_notices import idle_warning_notice
        notes = [
            p for p in self.bridge.mm.posted
            if p.message == idle_warning_notice(payload)
        ]
        self.assertEqual(len(notes), 1)

    async def test_distinct_events_same_run_each_post_once(self):
        """Dedup is per (run, event-type): different warning events for the
        same run each get their own single post."""
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")

        await self.bridge._on_harness_event(
            "run.idle_warning",
            self._envelope("run.idle_warning", {"idle_seconds": 605}),
        )
        await self.bridge._on_harness_event(
            "run.max_runtime_warning",
            self._envelope(
                "run.max_runtime_warning",
                {"max_run_seconds": 3600, "seconds_until_kill": 600},
            ),
        )

        self.assertEqual(len(self.bridge.mm.posted), 2)

    async def test_watchdog_notice_reposts_after_run_terminal(self):
        """The per-run dedup is cleared when the run ends, so a later run in
        the same session can warn again."""
        self.bridge.mapping.link(Anchor("c1"), "codex_s1")
        payload = {"idle_seconds": 605, "idle_timeout_seconds": 600}

        await self.bridge._on_harness_event(
            "run.idle_warning",
            self._envelope("run.idle_warning", payload, run="run-1"),
        )
        # Run ends (terminal event clears the session's watchdog dedup state).
        await self.bridge._on_harness_event(
            "run.completed",
            self._envelope("run.completed", {"returncode": 0}, run="run-1"),
        )
        await self.bridge._on_harness_event(
            "run.idle_warning",
            self._envelope("run.idle_warning", payload, run="run-2"),
        )

        from mm_bridge.watchdog_notices import idle_warning_notice
        notes = [
            p for p in self.bridge.mm.posted
            if p.message == idle_warning_notice(payload)
        ]
        self.assertEqual(len(notes), 2)


class TypingIndicatorActivityTests(_BridgeTestCase):
    """Typing indicator must follow the session's *status*, not merely the
    SSE event TYPE.

    The agent-harness freshness fix (harness main) emits a
    ``session.updated`` SSE event carrying ``data.session.status == "idle"``
    specifically to signal the session went QUIET. A ``session.updated``
    carrying ``status == "running"`` (and every real activity event:
    ``message`` / ``message.delta`` / ``tool.*``) means the session is busy.

    Contract: ``data.session.status`` is the canonical location of the flip
    payload (agent-harness ``observer._maybe_publish_status_flip``); a
    top-level ``data.status`` is accepted as a fallback. An idle-flip must
    NOT count as activity and must STOP typing; a running-flip / activity
    event keeps typing alive.
    """

    async def _settle(self) -> None:
        # Let the TypingIndicator refresh loop (refresh_s=0.01) tick at least
        # once so a started loop has published into FakeMmClient.typing.
        await asyncio.sleep(0.05)

    async def test_session_updated_idle_does_not_start_typing(self):
        """RED on pre-fix code: an idle-flip is treated as activity and
        starts typing. After the fix, an idle ``session.updated`` is NOT
        activity — no typing loop, no published indicator."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x",
                      "session": {"id": "ses_x", "status": "idle"}}},
        )
        await self._settle()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertEqual(self.bridge.mm.typing, [])
        self.assertNotIn("ses_x", self.bridge.last_activity_ts)

    async def test_message_event_starts_typing(self):
        """Positive guard against over-correction: a real ``message`` event
        is activity and starts typing."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "message",
            {"data": {"session_id": "ses_x",
                      "message": {"role": "assistant", "content": "hi"}}},
        )
        await self._settle()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        self.assertIn(("c1", None), self.bridge.mm.typing)
        self.assertIn("ses_x", self.bridge.last_activity_ts)

    async def test_session_updated_running_starts_typing(self):
        """A ``session.updated`` carrying ``status == "running"`` is a real
        activity signal and keeps current behavior (typing on)."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x",
                      "session": {"id": "ses_x", "status": "running"}}},
        )
        await self._settle()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        self.assertIn(("c1", None), self.bridge.mm.typing)
        self.assertIn("ses_x", self.bridge.last_activity_ts)

    async def test_idle_flip_stops_already_running_typing(self):
        """The explicit "went quiet" signal: a message starts typing, then
        an idle ``session.updated`` must STOP it (external/observer sessions
        have no run-terminal event to do this cleanup)."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "message",
            {"data": {"session_id": "ses_x",
                      "message": {"role": "assistant", "content": "working"}}},
        )
        await self._settle()
        self.assertIn("ses_x", self.bridge.typing.running_sessions())

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x",
                      "session": {"id": "ses_x", "status": "idle"}}},
        )
        await self._settle()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.last_activity_ts)

    async def test_idle_status_at_top_level_data_is_honored(self):
        """Defensive payload-shape: status may arrive at ``data.status``
        instead of ``data.session.status``. Still treated as idle → no
        typing."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x", "status": "idle"}},
        )
        await self._settle()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertEqual(self.bridge.mm.typing, [])

    async def test_session_updated_unknown_status_is_not_activity(self):
        """SAFE fallback: a ``session.updated`` with missing/unknown status
        must NOT start typing. Genuine output always also emits
        ``message`` / ``message.delta`` / ``tool.*`` events that keep typing
        alive, so a status-less freshness tick should not.

        Use a session NOT in ``_known_sessions`` would trigger channel
        creation; here it is mapped+known so the only effect under test is
        the activity decision."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x", "session": {"id": "ses_x"}}},
        )
        await self._settle()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertEqual(self.bridge.mm.typing, [])


class TypingRunLifecycleTests(_BridgeTestCase):
    """Typing must follow the harness RUN lifecycle: ON ⇔ a run is active.

    During long tool calls / async subagent waits the harness emits NO
    ``message``/``tool.*`` events for minutes, and its observer flips
    ``session.status`` to "idle" on pure rollout-file silence — both fire
    MID-RUN. So neither event-stream silence (the watchdog) nor a quiet
    ``session.updated`` flip may kill the indicator while a run is active.
    The Run row (``GET /v1/sessions/{sid}/runs/{run_id}``: status
    queued/running vs terminal) is the authoritative liveness signal; the
    watchdog reconciles against it instead of blindly stopping. Sessions
    WITHOUT a tracked run (external/observer) keep the PR #22 behavior.
    """

    async def _settle(self) -> None:
        await asyncio.sleep(0.05)

    async def _start_run(self, run_id: str | None = "run-1") -> None:
        """Drive a ``run.started`` SSE event for ses_x (anchor pre-linked).
        The harness puts ``run_id`` in the event ENVELOPE (data={});
        ``run_id=None`` simulates a payload that omitted it."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")
        envelope: dict = {"session_id": "ses_x", "data": {}}
        if run_id is not None:
            envelope["run_id"] = run_id
        await self.bridge._on_harness_event("run.started", envelope)

    def _silence(self) -> None:
        """Age ses_x's last activity far past typing_stop_after_silence_seconds."""
        self.bridge.last_activity_ts["ses_x"] = time.monotonic() - 1000.0

    async def test_run_started_tracks_active_run_and_starts_typing(self):
        await self._start_run()
        await self._settle()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        self.assertIn(("c1", None), self.bridge.mm.typing)
        self.assertEqual(self.bridge.active_run_by_session.get("ses_x"), "run-1")

    async def test_watchdog_keeps_typing_while_run_alive(self):
        """RED on pre-fix code: 15s of event silence stopped typing even
        though the run was mid-tool-call. Now the watchdog asks the harness
        for the Run row and keeps typing while it is queued/running."""
        await self._start_run()
        await self._settle()
        self.bridge.harness.runs_meta[("ses_x", "run-1")] = {
            "id": "run-1", "status": "running",
        }
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        # The successful probe counts as activity, so the next reconcile
        # only happens after another full silence window.
        self.assertGreater(
            self.bridge.last_activity_ts["ses_x"], time.monotonic() - 5.0,
        )

    async def test_quiet_flip_ignored_while_run_active(self):
        """RED on pre-fix code: the observer's freshness-based idle-flip
        (rollout-file silence during a long tool call) stopped typing
        mid-run. With a run active, the flip is noise — ignore it."""
        await self._start_run()
        await self._settle()
        self.assertIn("ses_x", self.bridge.typing.running_sessions())

        await self.bridge._on_harness_event(
            "session.updated",
            {"data": {"session_id": "ses_x",
                      "session": {"id": "ses_x", "status": "idle"}}},
        )
        await self._settle()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())

    async def test_watchdog_stops_typing_on_missed_terminal_event(self):
        """Missed-terminal-event recovery: run tracked as active but the
        harness says it completed → stop typing and clear all tracking."""
        await self._start_run()
        await self._settle()
        self.bridge.harness.runs_meta[("ses_x", "run-1")] = {
            "id": "run-1", "status": "completed",
        }
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.active_run_by_session)
        self.assertNotIn("ses_x", self.bridge.last_activity_ts)

    async def test_watchdog_stops_typing_when_run_unknown(self):
        """get_run → None (404): can't confirm liveness → stop (conservative)."""
        await self._start_run()
        await self._settle()
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.active_run_by_session)

    async def test_watchdog_stops_typing_when_harness_probe_raises(self):
        """A dead harness must not leave typing stuck ON: any probe error
        counts as NOT alive."""
        await self._start_run()
        await self._settle()
        self.bridge.harness.run_probe_error = RuntimeError("harness down")
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.active_run_by_session)

    async def test_watchdog_run_id_missing_falls_back_to_runs_list(self):
        """run.started without a run_id: reconcile via the session's runs
        list — ANY queued/running row keeps typing alive."""
        await self._start_run(run_id=None)
        await self._settle()
        self.bridge.harness.session_runs_meta["ses_x"] = [
            {"id": "r1", "status": "completed"},
            {"id": "r2", "status": "running"},
        ]
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())

    async def test_watchdog_run_id_missing_all_runs_terminal_stops(self):
        await self._start_run(run_id=None)
        await self._settle()
        self.bridge.harness.session_runs_meta["ses_x"] = [
            {"id": "r1", "status": "failed"},
        ]
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.active_run_by_session)

    async def test_terminal_run_event_stops_typing_and_clears_active_run(self):
        """Regression guard: the normal terminal event still does the
        cleanup, now including the active-run entry."""
        await self._start_run()
        await self._settle()

        await self.bridge._on_harness_event(
            "run.completed",
            {"session_id": "ses_x", "run_id": "run-1", "data": {}},
        )
        await self._settle()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.active_run_by_session)

    async def test_watchdog_silence_without_active_run_stops_typing(self):
        """Regression guard (external sessions): no tracked run → silence
        still stops typing, no harness probe involved."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")
        await self.bridge._on_harness_event(
            "message",
            {"data": {"session_id": "ses_x",
                      "message": {"role": "assistant", "content": "hi"}}},
        )
        await self._settle()
        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        self._silence()

        await self.bridge._typing_watchdog_tick()

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())
        self.assertNotIn("ses_x", self.bridge.last_activity_ts)

    async def test_watchdog_leaves_recently_active_sessions_alone(self):
        """Within the silence window nothing is stopped or probed."""
        await self._start_run()
        await self._settle()

        await self.bridge._typing_watchdog_tick()

        self.assertIn("ses_x", self.bridge.typing.running_sessions())

    async def test_watchdog_loop_runs_ticks(self):
        """Wiring guard: the long-lived watchdog task actually executes
        ticks (silent no-run session gets stopped by the loop itself)."""
        self.bridge.mapping.link(Anchor("c1"), "ses_x")
        self.bridge._known_sessions.add("ses_x")
        await self.bridge._on_harness_event(
            "message",
            {"data": {"session_id": "ses_x",
                      "message": {"role": "assistant", "content": "hi"}}},
        )
        await self._settle()
        self.assertIn("ses_x", self.bridge.typing.running_sessions())
        self._silence()
        self.bridge.config.typing_refresh_seconds = 0.01

        task = asyncio.create_task(self.bridge._run_typing_watchdog())
        try:
            await asyncio.sleep(0.1)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertNotIn("ses_x", self.bridge.typing.running_sessions())


if __name__ == "__main__":
    unittest.main()
