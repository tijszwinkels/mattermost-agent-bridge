"""Round F3 — truthful provider errors.

Two recorded incidents (Meshenger V2, 2026-08-31 and 2026-09-01), both on the
**pi** backend, surfaced a provider quota wall as an anonymous
``CLI exited with a non-zero status (1)`` attributed to the ``claude``
backend. Three lies in one line: wrong backend, no failure class, and — the
expensive one — no statement that the message was never processed and would
never be retried. The lead read the silence as "busy" and the lane starved.

These tests pin all three truths. See
``specs/20260901-truthful-provider-errors/``.
"""
from __future__ import annotations

import unittest

import httpx

from mm_bridge.backend_errors import (
    PATH_CLI_EXIT,
    PATH_HARNESS_PROCESS,
    PATH_NOT_ACCEPTED,
    classify_failure,
    failure_state_note,
    format_provider_failure,
)
from mm_bridge.config import Anchor
from mm_bridge.purpose import PurposeConfig

from test_bridge import _BridgeTestCase
from test_failure_classifier import INCIDENT_OLLAMA_429, INCIDENT_OPENROUTER_403


def _warnings(bridge) -> list[str]:
    return [p.message for p in bridge.mm.posted if ":warning:" in p.message]


# ─────────────────── 1. Message shapes (pure formatter) ────────────────────


class MessageShapeTests(unittest.TestCase):
    def test_incident_message_reads_exactly_as_specified(self):
        """The load-bearing sentence, verbatim from the round brief."""
        msg = format_provider_failure(
            action="run your message",
            backend="pi",
            failure=classify_failure(INCIDENT_OPENROUTER_403),
            path=PATH_NOT_ACCEPTED,
        )
        self.assertIn("Provider limit hit", msg)
        self.assertIn("OpenRouter", msg)
        self.assertIn("daily quota", msg)
        self.assertIn("HTTP 403", msg)
        self.assertIn("`pi` backend", msg)
        self.assertIn(
            "Your message was NOT processed and won't retry by itself — "
            "repost it once the limit resets, or `.model` / `.backend` to switch.",
            msg,
        )

    def test_cli_exit_path_says_the_run_started_and_died(self):
        """Lead's FLAG-2 edit: never claim the message reached the model."""
        msg = format_provider_failure(
            action="run your message",
            backend="pi",
            failure=classify_failure(INCIDENT_OPENROUTER_403),
            path=PATH_CLI_EXIT,
        )
        self.assertIn("The run started but died before finishing", msg)
        self.assertIn("anything already posted above is all there is", msg)
        self.assertIn("Nothing will retry it", msg)
        self.assertNotIn("reached the model", msg)
        self.assertNotIn("NOT processed", msg)

    def test_harness_process_path_is_its_own_truth(self):
        """orchestrator.py:579/:603 — the harness could not start/drive the CLI."""
        failure = classify_failure("FileNotFoundError: No such file: pi")
        msg = format_provider_failure(
            action="run your message",
            backend="pi",
            failure=failure,
            path=PATH_HARNESS_PROCESS,
        )
        self.assertIn("couldn't start or keep the CLI running", msg)
        self.assertIn("Nothing will retry it", msg)

    def test_rate_limited_headline(self):
        msg = format_provider_failure(
            action="run your message",
            backend="pi",
            failure=classify_failure(INCIDENT_OLLAMA_429),
            path=PATH_CLI_EXIT,
        )
        self.assertIn("rate limit", msg.lower())
        self.assertIn("ollama", msg.lower())
        self.assertIn("HTTP 429", msg)

    def test_unknown_class_keeps_todays_wording_plus_the_retry_sentence(self):
        """R6: unknown errors keep today's detail text, and gain the truth."""
        msg = format_provider_failure(
            action="run your message",
            backend="claude",
            failure=classify_failure("something exploded"),
            path=PATH_NOT_ACCEPTED,
        )
        self.assertIn("I tried to run your message with the `claude` backend", msg)
        self.assertIn("something exploded", msg)
        self.assertIn("mm-bridge doctor", msg)
        self.assertIn("NOT processed", msg)

    def test_unknown_backend_never_becomes_a_guess(self):
        msg = format_provider_failure(
            action="run your message",
            backend=None,
            failure=classify_failure(INCIDENT_OPENROUTER_403),
            path=PATH_CLI_EXIT,
        )
        self.assertIn("the backend", msg)
        self.assertNotIn("`claude`", msg)

    def test_every_path_states_the_retry_truth(self):
        """R3, structurally: no path may omit it."""
        for path in (PATH_NOT_ACCEPTED, PATH_CLI_EXIT, PATH_HARNESS_PROCESS):
            for detail in (INCIDENT_OPENROUTER_403, "who knows"):
                with self.subTest(path=path, detail=detail[:20]):
                    msg = format_provider_failure(
                        action="run your message",
                        backend="pi",
                        failure=classify_failure(detail),
                        path=path,
                    )
                    self.assertIn("retry", msg.lower())


# ──────────────── 2. Backend attribution (the reproduction) ────────────────


class BackendAttributionTests(_BridgeTestCase):
    """R2 — `_backend_for_channel` was defined twice; the resume resolver's
    `default_backend` guess shadowed the error resolver's honest `None`."""

    def _pi_session_with_cold_purpose_cache(self):
        # No `purpose_by_channel` entry: exactly what a channel created by
        # `mm-bridge spawn --backend pi` looks like after a daemon restart.
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "backend": "pi", "project": {"path": "/tmp/proj"}},
        ]

    async def test_pi_session_is_never_labelled_claude(self):
        """THE reproduction. Red before the split, green after."""
        self._pi_session_with_cold_purpose_cache()

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        msg = _warnings(self.bridge)[-1]
        self.assertIn("`pi` backend", msg)
        self.assertNotIn("`claude` backend", msg)

    async def test_harness_wire_name_is_normalised(self):
        """The harness says `claude-code`; the channel speaks `claude`."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "backend": "claude-code", "project": {"path": "/tmp/proj"}},
        ]

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        msg = _warnings(self.bridge)[-1]
        self.assertIn("`claude` backend", msg)
        self.assertNotIn("claude-code", msg)

    async def test_no_evidence_anywhere_says_the_backend(self):
        """R2/F1: unknown must degrade to 'the backend', never default_backend."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.harness.sessions_meta = []

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        msg = _warnings(self.bridge)[-1]
        self.assertIn("the backend", msg)
        self.assertNotIn("`claude` backend", msg)

    async def test_live_meta_lookup_is_best_effort(self):
        """Lead condition: Path A is often 'harness unreachable'. A raising
        `get_session` must fall through to the Purpose backend, not blow up
        or delay the warning."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="codex", model=None, mention_only=False,
        )

        async def boom(_session_id):
            raise httpx.ConnectError("harness unreachable")

        self.bridge.harness.get_session = boom  # type: ignore[assignment]

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        msg = _warnings(self.bridge)[-1]
        self.assertIn("`codex` backend", msg)

    async def test_resume_resolver_keeps_its_default_backend_fallback(self):
        """The split must not regress the Resume line: a resume command has to
        name SOMETHING runnable, so guessing the default is right there."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.assertEqual(self.bridge._backend_for_resume("c1"), "claude")


# ─────────────── 3. The stderr tail, end to end (S1 / C1 / C2) ─────────────


class StderrClassificationTests(_BridgeTestCase):
    def _pi_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "backend": "pi", "project": {"path": "/tmp/proj"}},
        ]

    async def _stderr(self, text: str, session_id: str = "s1"):
        await self.bridge._on_harness_event(
            "process.stderr",
            {"event": "process.stderr", "data": {"text": text},
             "session_id": session_id, "run_id": "run-1"},
        )

    async def _run_started(self, run_id: str, session_id: str = "s1"):
        await self.bridge._on_harness_event(
            "run.started",
            {"event": "run.started", "data": {}, "session_id": session_id,
             "run_id": run_id},
        )

    async def _run_failed(self, run_id: str, data: dict, session_id: str = "s1"):
        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": data, "session_id": session_id,
             "run_id": run_id},
        )

    async def test_the_incident_end_to_end(self):
        """A returncode-only run.failed classifies off the buffered stderr."""
        self._pi_session()
        await self._run_started("run-1")
        await self._stderr("pi: connecting to openrouter\n" + INCIDENT_OPENROUTER_403)
        await self._run_failed("run-1", {"returncode": 1})

        msg = _warnings(self.bridge)[-1]
        self.assertIn("Provider limit hit", msg)
        self.assertIn("OpenRouter", msg)
        self.assertIn("HTTP 403", msg)
        self.assertIn("`pi` backend", msg)
        self.assertIn("Nothing will retry it", msg)

    async def test_c1_the_raw_tail_is_never_quoted(self):
        """C1: at most the single matched line, and never a credential."""
        self._pi_session()
        await self._run_started("run-1")
        await self._stderr("pi: booting with a very chatty banner")
        await self._stderr("pi: loading tools from /home/claude/tools")
        await self._stderr(
            "403 daily limit reached for key sk-or-v1-deadbeefcafebabe01234567"
            " at https://openrouter.ai/api/v1"
        )
        await self._run_failed("run-1", {"returncode": 1})

        msg = _warnings(self.bridge)[-1]
        self.assertIn("Provider limit hit", msg)
        self.assertNotIn("deadbeefcafebabe", msg)
        self.assertNotIn("sk-or-v1-deadbeef", msg)
        self.assertNotIn("chatty banner", msg)
        self.assertNotIn("loading tools", msg)

    async def test_c2_a_new_run_never_classifies_on_the_previous_runs_stderr(self):
        self._pi_session()
        await self._run_started("run-1")
        await self._stderr(INCIDENT_OPENROUTER_403)
        await self._run_failed("run-1", {"returncode": 1})
        self.assertIn("Provider limit hit", _warnings(self.bridge)[-1])

        await self._run_started("run-2")
        await self._run_failed("run-2", {"returncode": 1})

        msg = _warnings(self.bridge)[-1]
        self.assertNotIn("Provider limit hit", msg)
        self.assertIn("non-zero status", msg)

    async def test_stderr_for_an_unmapped_session_does_not_raise(self):
        await self._stderr("noise", session_id="unmapped")

    async def test_exception_shaped_run_failed_uses_the_harness_process_truth(self):
        self._pi_session()
        await self._run_started("run-1")
        await self._run_failed(
            "run-1",
            {"error": "No such file or directory: 'pi'",
             "error_type": "FileNotFoundError"},
        )

        msg = _warnings(self.bridge)[-1]
        self.assertIn("couldn't start or keep the CLI running", msg)
        self.assertIn("No such file or directory", msg)


# ───────────────────── 4. Delivery-path truth (Path A) ─────────────────────


class DeliveryFailureTests(_BridgeTestCase):
    async def test_create_run_failure_states_not_processed_and_no_retry(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="pi", model=None, mention_only=True,
        )

        async def failing(session_id, message):
            raise RuntimeError(INCIDENT_OPENROUTER_403)

        self.bridge.harness.create_run = failing  # type: ignore[assignment]

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "@claude do the thing",
            "user_id": "u1", "type": "",
        })

        msg = _warnings(self.bridge)[-1]
        self.assertIn("`pi` backend", msg)
        self.assertIn("Provider limit hit", msg)
        self.assertIn("Your message was NOT processed", msg)
        self.assertIn("won't retry by itself", msg)

    async def test_catch_up_failure_also_states_the_retry_truth(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="pi", model=None, mention_only=False,
        )
        self.bridge.mm.posts_by_channel["c1"] = [
            {"user_id": "u1", "message": "m1", "type": ""},
        ]

        async def failing(session_id, message):
            raise RuntimeError("catch-up run blew up")

        self.bridge.harness.create_run = failing  # type: ignore[assignment]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude catch up 50",
            "user_id": "u1", "type": "",
        })

        msg = _warnings(self.bridge)[-1]
        self.assertIn("NOT processed", msg)


# ───────────────────────── 5. The F2 state seam ────────────────────────────


class StateNoteTests(unittest.TestCase):
    """`note` is F2's rendering input: "<class> (<provider>, HTTP <status>)",
    with provider and status omitted when unknown."""

    def test_provider_and_status_both_known(self):
        self.assertEqual(
            failure_state_note(classify_failure(INCIDENT_OPENROUTER_403), PATH_CLI_EXIT),
            "quota_exhausted (openrouter, HTTP 403)")

    def test_provider_only(self):
        self.assertEqual(
            failure_state_note(classify_failure("ollama is unhappy about rate limit"),
                               PATH_CLI_EXIT),
            "rate_limited (ollama)")

    def test_status_only(self):
        self.assertEqual(
            failure_state_note(classify_failure("401 Unauthorized"), PATH_CLI_EXIT),
            "auth (HTTP 401)")

    def test_neither_known(self):
        self.assertEqual(
            failure_state_note(classify_failure("mystery"), PATH_CLI_EXIT), "unknown")

    def test_harness_process_is_its_own_class_token(self):
        """F2's vocabulary includes `harness_process`; an unclassifiable
        harness-process death is more useful as that than as `unknown`."""
        self.assertEqual(
            failure_state_note(classify_failure("No such file: pi"),
                               PATH_HARNESS_PROCESS),
            "harness_process")

    def test_a_classified_harness_process_failure_keeps_the_real_class(self):
        self.assertEqual(
            failure_state_note(classify_failure(INCIDENT_OPENROUTER_403),
                               PATH_HARNESS_PROCESS),
            "quota_exhausted (openrouter, HTTP 403)")


class StateSeamTests(_BridgeTestCase):
    """F2 (`feat/turn-state-fleet`) owns `set_session_state`. This branch must
    stand alone on `main`, where that API does not exist yet."""

    async def test_seam_is_a_no_op_when_f2s_api_is_absent(self):
        self.assertFalse(hasattr(self.bridge, "set_session_state"))
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="pi", model=None, mention_only=False,
        )

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        self.assertTrue(_warnings(self.bridge), "the warning must still post")

    async def test_seam_calls_f2s_api_when_present(self):
        """The contract F2 (Nuthatch) pinned at M1: `on=None`, `source="bridge"`,
        and a note whose FIRST TOKEN is the class, because F2 renders
        `blocked (<first token>)`."""
        calls: list[tuple] = []

        def set_session_state(session_id, kind, *, on, note, source):
            calls.append((session_id, kind, on, note, source))

        self.bridge.set_session_state = set_session_state  # type: ignore[attr-defined]
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "backend": "pi", "project": {"path": "/tmp/proj"}},
        ]

        await self.bridge._on_harness_event(
            "run.started",
            {"event": "run.started", "data": {}, "session_id": "s1", "run_id": "r1"},
        )
        await self.bridge._on_harness_event(
            "process.stderr",
            {"event": "process.stderr", "data": {"text": INCIDENT_OPENROUTER_403},
             "session_id": "s1", "run_id": "r1"},
        )
        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "r1"},
        )

        self.assertEqual(len(calls), 1)
        session_id, kind, on, note, source = calls[0]
        self.assertEqual(session_id, "s1")
        self.assertEqual(kind, "blocked")
        self.assertIsNone(on)
        self.assertEqual(source, "bridge")
        self.assertEqual(note, "quota_exhausted (openrouter, HTTP 403)")
        self.assertTrue(note.startswith("quota_exhausted"), "F2 renders the first token")

    async def test_a_raising_f2_api_never_swallows_the_warning(self):
        def boom(*_a, **_kw):
            raise RuntimeError("F2 is having a day")

        self.bridge.set_session_state = boom  # type: ignore[attr-defined]
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="pi", model=None, mention_only=False,
        )

        await self.bridge._on_harness_event(
            "run.failed",
            {"event": "run.failed", "data": {"returncode": 1},
             "session_id": "s1", "run_id": "run-1"},
        )

        self.assertTrue(_warnings(self.bridge))


if __name__ == "__main__":
    unittest.main()
