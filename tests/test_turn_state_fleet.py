"""Turn-end state, the fleet view, and the awaiting-nag (round F2).

Spec: specs/20260901-turn-state-fleet/. Each test names the ruling it
covers (R1-R11 from the round brief, C1 from the M0 gate).

Every asynchronous wait here is bounded — a red test that hangs is not a
red test (R10).
"""

from __future__ import annotations

import tempfile
import time
import unittest

from mm_bridge.bridge import Bridge
from mm_bridge.config import Anchor, Config
from mm_bridge.session_state import SessionState

from doubles import FakeAgentHarnessClient, FakeMattermostClient


class _F2TestCase(unittest.IsolatedAsyncioTestCase):
    """A bridge with a mapped channel session `s1` in channel `c1`."""

    async def asyncSetUp(self):  # type: ignore[override]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            mm_bot_token="t",
            default_cwd="/tmp/proj",
            state_file=f"{self.tmp.name}/state.json",
            sidecar_dir=f"{self.tmp.name}/sidecar",
            held_posts_file=f"{self.tmp.name}/held_posts.json",
            default_backend="claude",
            initial_catch_up_n=0,
        )
        self.bridge = self.make_bridge()

    def make_bridge(self, **overrides) -> Bridge:
        cfg = Config(**{**self.config.__dict__, **overrides})
        bridge = Bridge(cfg)
        bridge.mm = FakeMattermostClient()
        bridge.harness = FakeAgentHarnessClient()
        bridge.mapping.link(Anchor("c1"), "s1")
        return bridge

    # ----- fixtures -----

    async def reply(self, text: str, *, session_id="s1", channel_id="c1",
                    thread_root=None, bridge=None) -> None:
        """One assistant text block — the seam a turn's reply arrives on."""
        b = bridge or self.bridge
        await b._handle_assistant_text_block(
            session_id, channel_id, thread_root, text,
        )

    def messages(self, bridge=None, channel_id=None) -> list[str]:
        b = bridge or self.bridge
        return [
            p.message for p in b.mm.posted
            if channel_id is None or p.channel_id == channel_id
        ]

    def state(self, session_id="s1", bridge=None) -> SessionState | None:
        return (bridge or self.bridge).mapping.get_state(session_id)

    def run_live(self, bridge=None, session_id="s1", run_id="run-1") -> None:
        b = bridge or self.bridge
        b.active_run_by_session[session_id] = run_id
        b.harness.session_runs_meta[session_id] = [
            {"id": run_id, "status": "running", "origin": "harness"},
        ]
        b.harness.runs_meta[(session_id, run_id)] = {
            "id": run_id, "status": "running", "origin": "harness",
        }


# ─────────────────── A. the <state/> directive (R2, R3) ───────────────────


class StateDirectiveTests(_F2TestCase):
    async def test_tag_sets_the_state_and_never_reaches_the_channel(self):
        await self.reply('M0 done.\n<state kind="awaiting" on="lead" note="M0 gate" />')
        s = self.state()
        assert s is not None
        self.assertEqual((s.kind, s.on, s.note), ("awaiting", "lead", "M0 gate"))
        self.assertEqual(self.messages(), ["M0 done."])

    async def test_a_reply_that_is_only_a_tag_posts_nothing(self):
        await self.reply('<state kind="parked" />')
        self.assertEqual(self.messages(), [])
        assert self.state() is not None
        self.assertEqual(self.state().kind, "parked")

    async def test_a_reply_with_no_tag_ends_idle(self):
        """Zero migration (R3): silence means idle, exactly as today."""
        await self.reply('<state kind="awaiting" on="lead" />')
        await self.reply("here is the answer")
        assert self.state() is not None
        self.assertEqual(self.state().kind, "idle")

    async def test_last_tag_wins(self):
        await self.reply(
            'was <state kind="awaiting" on="lead"/> now <state kind="parked"/>'
        )
        self.assertEqual(self.state().kind, "parked")

    async def test_unknown_kind_leaves_the_state_and_teaches_the_vocabulary(self):
        await self.reply('<state kind="awaiting" on="lead" note="gate" />')
        await self.reply('done\n<state kind="snoozing" />')
        s = self.state()
        assert s is not None
        self.assertEqual(s.kind, "awaiting", "unknown kind must not change state")
        joined = "\n".join(self.messages())
        self.assertNotIn("<state", joined, "the tag must still be stripped")
        self.assertIn("snoozing", joined)
        for kind in ("idle", "awaiting", "parked", "blocked"):
            self.assertIn(kind, joined)

    async def test_fenced_tag_is_documentation_not_a_declaration(self):
        text = 'Use:\n```\n<state kind="awaiting" on="lead" />\n```'
        await self.reply(text)
        self.assertEqual(self.state().kind, "idle")
        self.assertIn("<state", self.messages()[0])

    async def test_state_is_per_session_so_a_thread_fork_has_its_own(self):
        """R9: a thread-fork session is a session, with its own row."""
        self.bridge.mapping.link(Anchor("c1", "root-1"), "s2")
        await self.reply('<state kind="awaiting" on="lead"/>')
        await self.reply('<state kind="blocked" note="quota"/>',
                         session_id="s2", thread_root="root-1")
        self.assertEqual(self.state("s1").kind, "awaiting")
        self.assertEqual(self.state("s2").kind, "blocked")


class StateApiTests(_F2TestCase):
    async def test_set_session_state_returns_the_previous_state(self):
        """R3: one entry point, and F3 needs exactly one call."""
        self.assertIsNone(
            self.bridge.set_session_state("s1", "awaiting", on="lead")
        )
        prev = self.bridge.set_session_state(
            "s1", "blocked", note="anthropic 529", source="bridge",
        )
        assert prev is not None
        self.assertEqual(prev.kind, "awaiting")
        self.assertEqual(self.state().kind, "blocked")
        self.assertEqual(self.state().source, "bridge")

    async def test_set_session_state_stamps_the_moment(self):
        before = time.time()
        self.bridge.set_session_state("s1", "awaiting")
        self.assertGreaterEqual(self.state().set_at, before)

    async def test_an_unknown_kind_is_refused_by_the_api_too(self):
        self.bridge.set_session_state("s1", "awaiting")
        self.bridge.set_session_state("s1", "nonsense")
        self.assertEqual(self.state().kind, "awaiting")

    async def test_state_survives_a_restart(self):
        """R4: the declared state is durable; only nag bookkeeping is not."""
        self.bridge.set_session_state("s1", "awaiting", on="lead", note="M0")
        revived = self.make_bridge()
        s = revived.mapping.get_state("s1")
        assert s is not None
        self.assertEqual((s.kind, s.on, s.note), ("awaiting", "lead", "M0"))


class RunLifecycleStateTests(_F2TestCase):
    async def _run_started(self, session_id="s1", run_id="run-1"):
        await self.bridge._on_harness_event(
            "run.started", {"data": {"session_id": session_id, "run_id": run_id}},
        )

    async def test_run_started_keeps_the_declared_kind(self):
        """R3: `working` is display-only and never overwrites a claim."""
        self.bridge.set_session_state("s1", "awaiting", on="lead")
        await self._run_started()
        self.assertEqual(self.state().kind, "awaiting")

    async def test_run_started_restamps_the_wait(self):
        """The age column means 'since the state was set or the run started'."""
        self.bridge.set_session_state("s1", "awaiting", on="lead")
        self.bridge.mapping.set_state(
            "s1", SessionState("awaiting", on="lead", set_at=time.time() - 9999),
        )
        await self._run_started()
        self.assertGreater(self.state().set_at, time.time() - 5)

    async def test_a_session_with_no_state_starts_idle_when_it_runs(self):
        await self._run_started()
        s = self.state()
        assert s is not None
        self.assertEqual(s.kind, "idle")

    async def test_a_run_that_dies_with_no_text_block_keeps_the_state(self):
        self.bridge.set_session_state("s1", "awaiting", on="lead", note="M0")
        await self._run_started()
        await self.bridge._on_harness_event(
            "run.interrupted", {"data": {"session_id": "s1", "run_id": "run-1"}},
        )
        s = self.state()
        assert s is not None
        self.assertEqual((s.kind, s.note), ("awaiting", "M0"))


if __name__ == "__main__":
    unittest.main()


# ────────────────────────── B. the fleet view (R8, C1) ──────────────────────


class _FleetTestCase(_F2TestCase):
    """A lead channel `c1` with two spawned children and one stranger."""

    async def asyncSetUp(self):  # type: ignore[override]
        await super().asyncSetUp()
        mm = self.bridge.mm
        mm.channels.update({
            "c1": {"id": "c1", "name": "lead", "display_name": "lead"},
            "c-kestrel": {"id": "c-kestrel", "name": "kestrel",
                          "display_name": "kestrel",
                          "header": "Parent: ~lead~"},
            "c-grebe": {"id": "c-grebe", "name": "grebe",
                        "display_name": "grebe",
                        "header": "Parent: ~lead~ ([thread](http://x/pl/p1))"},
            "c-stranger": {"id": "c-stranger", "name": "stranger",
                           "display_name": "stranger",
                           "header": "Parent: ~someone-else~"},
        })
        self.bridge.mapping.link(Anchor("c-kestrel"), "s-kestrel")
        self.bridge.mapping.link(Anchor("c-grebe"), "s-grebe")
        self.bridge.mapping.link(Anchor("c-stranger"), "s-stranger")

    async def fleet(self, channel_id="c1", arg=None) -> str:
        await self.bridge._cmd_fleet(channel_id, arg, None)
        return self.bridge.mm.posted[-1].message


class FleetViewTests(_FleetTestCase):
    async def test_renders_children_only(self):
        out = await self.fleet()
        self.assertIn("kestrel", out)
        self.assertIn("grebe", out)
        self.assertNotIn("stranger", out)

    async def test_a_child_row_carries_state_target_and_note(self):
        self.bridge.set_session_state(
            "s-kestrel", "awaiting", on="lead", note="C5 gate",
        )
        out = await self.fleet()
        line = next(ln for ln in out.splitlines() if "kestrel" in ln)
        self.assertIn("awaiting → lead", line)
        self.assertIn("note: C5 gate", line)

    async def test_held_count_comes_from_the_f1_buffer(self):
        from mm_bridge import held_posts
        self.bridge._held.add(
            Anchor("c-grebe"),
            held_posts.HeldPost(post={"id": "p9"}, username="tijs",
                                held_at_ms=0),
            session_id="s-grebe",
        )
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "grebe" in ln)
        self.assertIn("held: 1", line)

    async def test_a_dormant_child_is_shown_not_hidden(self):
        self.bridge.mapping.unlink(Anchor("c-kestrel"))
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "kestrel" in ln)
        self.assertIn("-", line)

    async def test_thread_forks_get_their_own_row(self):
        """R9: state is per session, so a thread fork is its own row."""
        self.bridge.mapping.link(Anchor("c-kestrel", "root-1"), "s-thread")
        self.bridge.set_session_state("s-thread", "blocked", note="quota")
        out = await self.fleet()
        thread_lines = [ln for ln in out.splitlines() if "thread" in ln]
        self.assertEqual(len(thread_lines), 1, out)
        self.assertIn("blocked", thread_lines[0])

    async def test_all_widens_to_every_mapped_session(self):
        out = await self.fleet(arg="all")
        self.assertIn("stranger", out)

    async def test_a_channel_with_no_children_says_so(self):
        out = await self.fleet(channel_id="c-stranger")
        self.assertIn("no child channels", out.lower())

    async def test_the_listing_is_fetched_once_not_once_per_row(self):
        await self.fleet()
        self.assertEqual(self.bridge.mm.list_bot_channels_calls, 1)


class FleetProbeTests(_FleetTestCase):
    """C1: the run tracker is stale in both directions, so the fleet probes.

    `_typing_watchdog_tick` early-returns with no typing indicator and
    otherwise only sweeps `typing.running_sessions()`, so a session whose
    `run.started` was lost is never reconciled. A fleet that renders a
    working builder as `idle` is the lie this feature exists to remove.
    """

    async def test_probe_beats_a_stale_tracker(self):
        self.bridge.harness.session_runs_meta["s-kestrel"] = [
            {"id": "r1", "status": "running", "origin": "harness"},
        ]
        self.assertNotIn("s-kestrel", self.bridge.active_run_by_session)
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "kestrel" in ln)
        self.assertIn("working", line)

    async def test_a_failed_probe_falls_back_to_the_tracker_and_is_marked(self):
        self.run_live(session_id="s-kestrel", run_id="r1")

        async def boom(session_id):
            raise RuntimeError("harness down")

        self.bridge.harness.list_session_runs = boom
        self.bridge.harness.get_run = boom
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "kestrel" in ln)
        self.assertIn("working", line)
        self.assertIn("?", line)

    async def test_a_slow_probe_does_not_hang_the_view(self):
        import asyncio as _asyncio

        async def never(session_id):
            await _asyncio.sleep(30)

        self.bridge.harness.list_session_runs = never
        self.bridge.harness.get_run = never
        self.bridge.FLEET_PROBE_TIMEOUT_S = 0.05
        out = await _asyncio.wait_for(self.fleet(), timeout=5)
        self.assertIn("?", out)

    async def test_probes_run_in_parallel_not_serially(self):
        import asyncio as _asyncio

        async def slow(session_id):
            await _asyncio.sleep(0.1)
            return []

        self.bridge.harness.list_session_runs = slow
        started = time.monotonic()
        await self.fleet()   # two child sessions: 0.1s parallel, 0.2s serial
        self.assertLess(time.monotonic() - started, 0.15,
                        "probes must fan out, not run one after another")
