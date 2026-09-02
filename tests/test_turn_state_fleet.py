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


class TaglessBlockWriteTests(_F2TestCase):
    """C2: a tagless text block must not rewrite the state file.

    `_apply_state_directive` runs per assistant text block, and a chatty run
    emits one per tool narration. Stamping `set_at=now` every time would make
    every block a full atomic rewrite of the state file, on a path with no
    throttle — the SSE cursor throttles its own writes at 2s for exactly this
    reason.
    """

    def count_saves(self, bridge=None):
        b = bridge or self.bridge
        saves = []
        real = b.mapping.save

        def counting_save():
            saves.append(1)
            real()

        b.mapping.save = counting_save
        return saves

    async def test_five_tagless_blocks_write_at_most_once(self):
        saves = self.count_saves()
        for i in range(5):
            await self.reply(f"narrating step {i}")
        self.assertLessEqual(len(saves), 1, "one write per text block")
        self.assertEqual(self.state().kind, "idle")

    async def test_a_tagless_block_after_awaiting_still_transitions_once(self):
        await self.reply('<state kind="awaiting" on="lead" note="M0"/>')
        saves = self.count_saves()
        await self.reply("here is the answer")
        self.assertEqual(len(saves), 1)
        self.assertEqual(self.state().kind, "idle")
        self.assertIsNone(self.state().on)

    async def test_a_no_op_tagless_block_leaves_the_age_alone(self):
        """`set_at` stays where `run.started` re-stamped it — the documented
        age semantics."""
        await self.reply("first")
        stamped = self.state().set_at
        await self.reply("second")
        self.assertEqual(self.state().set_at, stamped)

    async def test_an_explicit_tag_always_writes_even_when_identical(self):
        """A re-declared `awaiting` must start a NEW episode."""
        await self.reply('<state kind="awaiting" on="lead"/>')
        first = self.state().set_at
        self.bridge._nagged_levels["s1"] = {1}
        saves = self.count_saves()
        await self.reply('<state kind="awaiting" on="lead"/>')
        self.assertEqual(len(saves), 1)
        self.assertGreaterEqual(self.state().set_at, first)
        self.assertNotIn("s1", self.bridge._nagged_levels)

    async def test_a_tagless_block_for_a_stateless_session_writes_once(self):
        self.bridge.mapping.set_state("s1", None)
        saves = self.count_saves()
        await self.reply("hello")
        self.assertEqual(len(saves), 1)
        self.assertEqual(self.state().kind, "idle")


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

    async def test_f3_can_block_a_session_that_has_no_state_yet(self):
        """F3 calls this from run-failure surfacing, possibly before the
        session has ever declared anything."""
        prev = self.bridge.set_session_state(
            "s1", "blocked", note="quota_exhausted (anthropic, HTTP 529)",
            source="bridge",
        )
        self.assertIsNone(prev)
        s = self.state()
        assert s is not None
        self.assertEqual((s.kind, s.source), ("blocked", "bridge"))

    async def test_the_state_api_never_raises_into_its_caller(self):
        """F3 asserts that a raising API must not swallow its warning post."""
        def boom():
            raise OSError("disk full")

        self.bridge.mapping.save = boom
        try:
            self.bridge.set_session_state("s1", "blocked", source="bridge")
        except Exception as exc:  # pragma: no cover - the assertion is the point
            self.fail(f"set_session_state raised {exc!r}")

    async def test_an_unmapped_session_is_a_no_op_not_a_crash(self):
        self.assertIsNone(self.bridge.set_session_state("ghost", "blocked"))

    async def test_a_bridge_block_is_replaced_by_the_next_turns_tag(self):
        """Normal rules apply afterwards (lead ruling)."""
        self.bridge.set_session_state(
            "s1", "blocked", note="rate_limited (anthropic, HTTP 429)",
            source="bridge",
        )
        await self.reply("recovered, carrying on")
        self.assertEqual(self.state().kind, "idle")

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

    async def test_a_bridge_classified_block_names_its_class_in_the_row(self):
        self.bridge.set_session_state(
            "s-kestrel", "blocked",
            note="quota_exhausted (anthropic, HTTP 529)", source="bridge",
        )
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "kestrel" in ln)
        self.assertIn("blocked (quota_exhausted)", line)
        self.assertIn("note: quota_exhausted (anthropic, HTTP 529)", line)

    async def test_an_agent_declared_block_shows_its_note_verbatim(self):
        self.bridge.set_session_state(
            "s-kestrel", "blocked", note="waiting on the DB migration",
        )
        line = next(ln for ln in (await self.fleet()).splitlines()
                    if "kestrel" in ln)
        self.assertIn("blocked ", line)
        self.assertNotIn("blocked (", line)
        self.assertIn("note: waiting on the DB migration", line)

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


# ──────────────────────────── C. the nag (R5-R7) ────────────────────────────


class _NagTestCase(_F2TestCase):
    """A lead channel `c1` (session `s-lead`) with one spawned child."""

    THRESHOLD = 1800.0   # the delay these waits REQUEST via `nag="30m"`

    async def asyncSetUp(self):  # type: ignore[override]
        await super().asyncSetUp()
        # Who an escalation mentions. The no-config fallback (most recent
        # non-bot poster) has its own test below.
        self.config.operator_username = "tijs"
        self.bridge = self.make_bridge()
        self.bridge.mapping.unlink(Anchor("c1"))
        self.bridge.mapping.link(Anchor("c1"), "s-lead")
        self.bridge.mapping.link(Anchor("c-kestrel"), "s-kestrel")
        self.bridge.mm.channels.update({
            "c1": {"id": "c1", "name": "lead", "display_name": "lead"},
            "c-kestrel": {"id": "c-kestrel", "name": "kestrel",
                          "display_name": "kestrel",
                          "header": "Parent: ~lead~"},
        })
        self.bridge.mm.usernames = {"tijs"}

    def awaiting(self, session_id="s-kestrel", *, ago: float, on="lead",
                 note="M0 gate", nag_after: float | None = -1) -> None:
        """An `awaiting` state. `nag_after=None` is a PASSIVE wait (C3): a
        fleet row and nothing else."""
        if nag_after == -1:
            nag_after = self.THRESHOLD
        self.bridge.mapping.set_state(session_id, SessionState(
            "awaiting", on=on, note=note, set_at=time.time() - ago,
            nag_after=nag_after,
        ))

    async def sweep(self) -> None:
        await self.bridge._sweep_awaiting_nags()

    def nags(self, channel_id=None) -> list[str]:
        return [
            p.message for p in self.bridge.mm.posted
            if "bridge nag" in p.message
            and (channel_id is None or p.channel_id == channel_id)
        ]

    def delivered(self) -> list[tuple[str, str]]:
        return list(self.bridge.harness.sent)


class NagThresholdTests(_NagTestCase):
    async def test_no_nag_before_the_threshold(self):
        self.awaiting(ago=self.THRESHOLD - 60)
        await self.sweep()
        self.assertEqual(self.nags(), [])

    async def test_nag_at_the_threshold_lands_in_the_parent_channel(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        nags = self.nags(channel_id="c1")
        self.assertEqual(len(nags), 1, self.bridge.mm.posted)
        self.assertIn("kestrel", nags[0])
        self.assertIn("30 min", nags[0])
        self.assertIn("M0 gate", nags[0])

    async def test_the_nag_says_it_is_from_the_bridge(self):
        """It is flushed later as `HH:MM <bot>: ...` — it must not read as
        an agent's own post."""
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertTrue(self.nags()[0].startswith("⏰ bridge nag:"))

    async def test_the_nag_becomes_a_turn_for_the_parent_session(self):
        """R5: a post the bridge authors is dropped by `is_own_post`, so the
        turn has to be submitted explicitly or the nag wakes nobody."""
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(len(self.delivered()), 1)
        session_id, body = self.delivered()[0]
        self.assertEqual(session_id, "s-lead")
        self.assertIn("bridge nag", body)

    async def test_a_note_free_wait_nags_without_an_empty_quote(self):
        self.awaiting(ago=self.THRESHOLD + 10, note=None)
        await self.sweep()
        self.assertNotIn('("', self.nags()[0])

    async def test_only_awaiting_sessions_are_nagged(self):
        for kind in ("idle", "parked", "blocked"):
            with self.subTest(kind=kind):
                self.bridge.mapping.set_state("s-kestrel", SessionState(
                    kind, set_at=time.time() - self.THRESHOLD * 3,
                    nag_after=self.THRESHOLD,
                ))
                self.bridge._nagged_levels.clear()
                await self.sweep()
                self.assertEqual(self.nags(), [])

    async def test_a_running_session_is_never_nagged(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        self.run_live(session_id="s-kestrel")
        await self.sweep()
        self.assertEqual(self.nags(), [])


class NagEpisodeTests(_NagTestCase):
    async def test_single_shot_per_episode(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        await self.sweep()
        self.assertEqual(len(self.nags()), 1)

    async def test_escalation_at_twice_the_threshold_mentions_the_operator(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.awaiting(ago=self.THRESHOLD * 2 + 10)
        self.bridge._nagged_levels["s-kestrel"] = {1}
        await self.sweep()
        nags = self.nags()
        self.assertEqual(len(nags), 2, nags)
        self.assertIn("@tijs", nags[1])

    async def test_the_escalation_fires_once_too(self):
        self.awaiting(ago=self.THRESHOLD * 2 + 10)
        await self.sweep()
        await self.sweep()
        await self.sweep()
        self.assertEqual(len(self.nags()), 1)

    async def test_a_state_change_starts_a_fresh_episode(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.bridge.set_session_state("s-kestrel", "awaiting", on="lead")
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(len(self.nags()), 2)

    async def test_run_started_ends_the_episode(self):
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        await self.bridge._on_harness_event(
            "run.started", {"data": {"session_id": "s-kestrel", "run_id": "r1"}},
        )
        self.assertNotIn("s-kestrel", self.bridge._nagged_levels)
        # ...and the wait is re-stamped, so nothing is due again immediately.
        await self.sweep()
        self.assertEqual(len(self.nags()), 1)

    async def test_a_restart_re_rings_an_overdue_episode_once(self):
        """R4: bookkeeping is in-memory, so this is the accepted cost."""
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        revived = self.make_bridge()
        revived.mm.channels.update(self.bridge.mm.channels)
        revived.mapping.link(Anchor("c1"), "s-lead")
        await revived._sweep_awaiting_nags()
        again = [p.message for p in revived.mm.posted if "bridge nag" in p.message]
        self.assertEqual(len(again), 1)
        await revived._sweep_awaiting_nags()
        self.assertEqual(
            len([p for p in revived.mm.posted if "bridge nag" in p.message]), 1,
        )


class NagPlacementTests(_NagTestCase):
    async def test_a_named_human_is_nagged_in_the_childs_own_channel(self):
        """Lead ruling on Q2: `is_own_post` means such a post cannot wake the
        child, and the human being summoned is where the context is."""
        self.awaiting(ago=self.THRESHOLD + 10, on="tijs")
        await self.sweep()
        self.assertEqual(self.nags(channel_id="c1"), [])
        own = self.nags(channel_id="c-kestrel")
        self.assertEqual(len(own), 1)
        self.assertIn("@tijs", own[0])
        self.assertEqual(self.delivered(), [], "must not wake anyone as a turn")

    async def test_the_escalation_for_a_named_human_goes_to_the_parent(self):
        self.awaiting(ago=self.THRESHOLD * 2 + 10, on="tijs")
        self.bridge._nagged_levels["s-kestrel"] = {1}
        await self.sweep()
        parent = self.nags(channel_id="c1")
        self.assertEqual(len(parent), 1)
        self.assertIn("@tijs", parent[0])
        self.assertEqual(len(self.delivered()), 1)

    async def test_an_unknown_username_is_treated_as_the_lead_and_says_so(self):
        self.awaiting(ago=self.THRESHOLD + 10, on="ghost")
        await self.sweep()
        nags = self.nags(channel_id="c1")
        self.assertEqual(len(nags), 1)
        self.assertIn("ghost", nags[0])
        self.assertEqual(len(self.delivered()), 1)

    async def test_no_parent_header_nags_the_session_own_channel(self):
        self.bridge.mm.channels["c-kestrel"].pop("header")
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        own = self.nags(channel_id="c-kestrel")
        self.assertEqual(len(own), 1)
        self.assertIn("@", own[0], "an orphan nag must mention someone")

    async def test_a_thread_fork_nags_its_channels_own_session(self):
        """R9: the thread's overseer is the channel session it lives in."""
        self.bridge.mapping.link(Anchor("c-kestrel", "root-1"), "s-thread")
        self.awaiting("s-thread", ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(len(self.nags(channel_id="c-kestrel")), 1)
        self.assertEqual(self.delivered()[0][0], "s-kestrel")

    async def test_a_busy_parent_holds_the_nag_instead_of_queueing_a_turn(self):
        """F1 coalescing folds a nag that lands on a lead mid-crunch."""
        self.run_live(session_id="s-lead")
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(len(self.nags(channel_id="c1")), 1, "still visible")
        self.assertEqual(self.delivered(), [], "must not jump the live run")
        self.assertEqual(len(self.bridge._held.peek(Anchor("c1"))), 1)


class NagBoundingTests(_NagTestCase):
    async def test_at_most_one_nag_per_sweep_across_the_fleet(self):
        self.bridge.mapping.link(Anchor("c-grebe"), "s-grebe")
        self.bridge.mm.channels["c-grebe"] = {
            "id": "c-grebe", "name": "grebe", "display_name": "grebe",
            "header": "Parent: ~lead~",
        }
        self.awaiting("s-kestrel", ago=self.THRESHOLD + 600)
        self.awaiting("s-grebe", ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(len(self.nags()), 1)
        self.assertIn("kestrel", self.nags()[0], "oldest wait rings first")
        await self.sweep()
        self.assertEqual(len(self.nags()), 2)

    async def test_the_kill_switch_stops_c_without_touching_a_or_b(self):
        bridge = self.make_bridge(nag_enabled=False)
        bridge.mm.channels.update(self.bridge.mm.channels)
        bridge.mapping.link(Anchor("c1"), "s-lead")
        bridge.mapping.link(Anchor("c-kestrel"), "s-kestrel")
        bridge.mapping.set_state("s-kestrel", SessionState(
            "awaiting", on="lead", set_at=time.time() - self.THRESHOLD * 5,
            nag_after=self.THRESHOLD,   # asked to ring; the kill switch wins
        ))
        await bridge._sweep_awaiting_nags()
        self.assertEqual(
            [p for p in bridge.mm.posted if "bridge nag" in p.message], [],
        )
        # A and B still work.
        await bridge._handle_assistant_text_block(
            "s-kestrel", "c-kestrel", None, '<state kind="parked"/>',
        )
        self.assertEqual(bridge.mapping.get_state("s-kestrel").kind, "parked")

    async def test_each_wait_rings_at_its_own_requested_delay(self):
        """C3: there is no global threshold any more — the wait asks."""
        self.awaiting("s-kestrel", ago=1000, nag_after=900)
        await self.sweep()
        self.assertEqual(len(self.nags()), 1)

    async def test_a_wait_that_asked_for_longer_stays_quiet(self):
        self.awaiting("s-kestrel", ago=1000, nag_after=3600)
        await self.sweep()
        self.assertEqual(self.nags(), [])

    async def test_without_an_operator_the_last_human_in_the_channel_is_rung(self):
        """An escalation that mentions nobody notifies nobody."""
        bridge = self.make_bridge(operator_username="")
        bridge.mm.channels.update(self.bridge.mm.channels)
        bridge.mm.users["u-real"] = {"id": "u-real", "username": "wren"}
        bridge.mm.posts_by_channel["c-kestrel"] = [
            {"id": "p1", "user_id": "u-real", "message": "any news?"},
            {"id": "p2", "user_id": bridge.mm.bot_user_id, "message": "working"},
        ]
        bridge.mapping.link(Anchor("c1"), "s-lead")
        bridge.mapping.link(Anchor("c-kestrel"), "s-kestrel")
        bridge.mapping.set_state("s-kestrel", SessionState(
            "awaiting", on="lead", set_at=time.time() - self.THRESHOLD * 2 - 10,
            nag_after=self.THRESHOLD,
        ))
        await bridge._sweep_awaiting_nags()
        nags = [p.message for p in bridge.mm.posted if "bridge nag" in p.message]
        self.assertEqual(len(nags), 1)
        self.assertIn("@wren", nags[0])

    async def test_a_failed_nag_is_not_retried_within_the_episode(self):
        """A doorbell that rings twice on a transient error is worse than one
        that is missed; the sweep still escalates later."""
        def boom(*a, **k):
            raise RuntimeError("mattermost down")

        self.bridge.mm.post = boom
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(self.bridge._nagged_levels.get("s-kestrel"), {1})


class NagSweepWiringTests(_NagTestCase):
    """R7: the sweep is a third phase of the EXISTING watchdog task."""

    async def test_the_watchdog_runs_the_nag_sweep(self):
        import asyncio as _asyncio
        rang = _asyncio.Event()

        async def fake_sweep():
            rang.set()

        self.bridge.config.typing_refresh_seconds = 0.01
        self.bridge._sweep_awaiting_nags = fake_sweep
        task = _asyncio.create_task(self.bridge._run_typing_watchdog())
        try:
            await _asyncio.wait_for(rang.wait(), timeout=5)
        finally:
            task.cancel()

    async def test_a_failing_nag_sweep_does_not_kill_the_watchdog(self):
        import asyncio as _asyncio
        calls = []

        async def exploding_sweep():
            calls.append(1)
            raise RuntimeError("nag sweep broke")

        self.bridge.config.typing_refresh_seconds = 0.01
        self.bridge._sweep_awaiting_nags = exploding_sweep
        task = _asyncio.create_task(self.bridge._run_typing_watchdog())
        try:
            for _ in range(200):
                await _asyncio.sleep(0.01)
                if len(calls) >= 2:
                    break
            self.assertGreaterEqual(len(calls), 2, "watchdog died on a raise")
            self.assertFalse(task.done())
        finally:
            task.cancel()


# ─────────────── C3: the nag is opt-in, per wait (Tijs's ruling) ─────────────


class OptInNagTests(_NagTestCase):
    """The conservative default: `awaiting` is PASSIVE.

    A wait rings only if it asked to, with `nag="<duration>"`. Nothing else
    in the system may cause an agent to be woken.
    """

    async def test_an_awaiting_with_no_nag_attribute_never_rings(self):
        for ago in (self.THRESHOLD + 10, self.THRESHOLD * 4, 86_400):
            with self.subTest(ago=ago):
                self.awaiting(ago=ago, nag_after=None)
                self.bridge._nagged_levels.clear()
                await self.sweep()
                self.assertEqual(self.nags(), [], "a passive wait must not ring")

    async def test_a_passive_wait_still_shows_in_the_fleet(self):
        """Passive means "no post", not "invisible"."""
        self.bridge.mm.channels["c-kestrel"]["header"] = "Parent: ~lead~"
        self.awaiting(ago=self.THRESHOLD + 10, nag_after=None)
        await self.bridge._cmd_fleet("c1", None, None)
        out = self.bridge.mm.posted[-1].message
        self.assertIn("awaiting → lead", out)

    async def test_the_tag_requests_a_nag(self):
        await self.reply(
            '<state kind="awaiting" on="lead" note="M0" nag="45m" />',
            session_id="s-kestrel", channel_id="c-kestrel",
        )
        s = self.bridge.mapping.get_state("s-kestrel")
        assert s is not None
        self.assertEqual(s.nag_after, 45 * 60)

    async def test_duration_grammar(self):
        cases = {"90": 900, "1200": 1200, "45m": 2700, "2h": 7200,
                 "3600s": 3600, "  30m  ": 1800}
        for text, expected in cases.items():
            with self.subTest(text=text):
                await self.reply(
                    f'<state kind="awaiting" on="lead" nag="{text}" />',
                    session_id="s-kestrel", channel_id="c-kestrel",
                )
                self.assertEqual(
                    self.bridge.mapping.get_state("s-kestrel").nag_after, expected,
                )

    async def test_the_delay_is_clamped_both_ways(self):
        bridge = self.make_bridge(nag_min_seconds=900, nag_max_seconds=14400)
        bridge.mapping.link(Anchor("c-kestrel"), "s-kestrel")
        for text, expected in (("30s", 900), ("99h", 14400)):
            with self.subTest(text=text):
                await bridge._handle_assistant_text_block(
                    "s-kestrel", "c-kestrel", None,
                    f'<state kind="awaiting" on="lead" nag="{text}" />',
                )
                self.assertEqual(
                    bridge.mapping.get_state("s-kestrel").nag_after, expected,
                )

    async def test_an_invalid_duration_keeps_the_state_and_says_so(self):
        await self.reply(
            'M0 done\n<state kind="awaiting" on="lead" nag="soonish" />',
            session_id="s-kestrel", channel_id="c-kestrel",
        )
        s = self.bridge.mapping.get_state("s-kestrel")
        assert s is not None
        self.assertEqual(s.kind, "awaiting", "the state still applies")
        self.assertIsNone(s.nag_after, "but the nag is ignored")
        joined = "\n".join(self.messages(channel_id="c-kestrel"))
        self.assertIn("soonish", joined)
        self.assertIn("M0 done", joined)

    async def test_a_non_positive_duration_is_refused(self):
        for text in ("0", "-5m"):
            with self.subTest(text=text):
                await self.reply(
                    f'<state kind="awaiting" on="lead" nag="{text}" />',
                    session_id="s-kestrel", channel_id="c-kestrel",
                )
                self.assertIsNone(
                    self.bridge.mapping.get_state("s-kestrel").nag_after,
                )

    async def test_nag_on_a_non_awaiting_kind_is_ignored_quietly(self):
        await self.reply(
            '<state kind="parked" nag="30m" />',
            session_id="s-kestrel", channel_id="c-kestrel",
        )
        s = self.bridge.mapping.get_state("s-kestrel")
        assert s is not None
        self.assertEqual(s.kind, "parked")
        self.assertIsNone(s.nag_after)
        self.assertEqual(self.messages(channel_id="c-kestrel"), [],
                         "a debug log, not a channel notice")

    async def test_escalation_is_twice_the_REQUESTED_delay(self):
        self.awaiting(ago=1000, nag_after=900)      # level 1 due, not level 2
        await self.sweep()
        self.assertNotIn("@tijs", self.nags()[0])
        self.awaiting(ago=1900, nag_after=900)      # now past 2x
        self.bridge._nagged_levels["s-kestrel"] = {1}
        await self.sweep()
        self.assertIn("@tijs", self.nags()[1])

    async def test_the_requested_delay_survives_a_restart(self):
        self.awaiting(ago=10, nag_after=1200)
        revived = self.make_bridge()
        s = revived.mapping.get_state("s-kestrel")
        assert s is not None
        self.assertEqual(s.nag_after, 1200)

    async def test_a_run_restamp_keeps_the_request(self):
        self.awaiting(ago=self.THRESHOLD, nag_after=1200)
        await self.bridge._on_harness_event(
            "run.started", {"data": {"session_id": "s-kestrel", "run_id": "r1"}},
        )
        self.assertEqual(
            self.bridge.mapping.get_state("s-kestrel").nag_after, 1200,
        )


class NagOptOutTests(_NagTestCase):
    """`no-nag` in a channel's Purpose: nothing may be placed there."""

    async def test_a_no_nag_parent_receives_neither_post_nor_turn(self):
        self.bridge.mm.channels["c1"]["purpose"] = "claude, autorespond, no-nag"
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(self.nags(), [])
        self.assertEqual(self.delivered(), [])

    async def test_a_no_nag_child_channel_blocks_a_named_humans_level_1(self):
        self.bridge.mm.channels["c-kestrel"]["purpose"] = "claude, no-nag"
        self.awaiting(ago=self.THRESHOLD + 10, on="tijs")
        await self.sweep()
        self.assertEqual(self.nags(), [])

    async def test_opting_the_child_out_does_not_silence_its_parent(self):
        """The token protects the channel it is in, not the wait."""
        self.bridge.mm.channels["c-kestrel"]["purpose"] = "claude, no-nag"
        self.awaiting(ago=self.THRESHOLD + 10, on="lead")
        await self.sweep()
        self.assertEqual(len(self.nags(channel_id="c1")), 1)

    async def test_the_level_is_still_marked_so_it_escalates_normally(self):
        self.bridge.mm.channels["c1"]["purpose"] = "no-nag"
        self.awaiting(ago=self.THRESHOLD + 10)
        await self.sweep()
        self.assertEqual(self.bridge._nagged_levels.get("s-kestrel"), {1})
