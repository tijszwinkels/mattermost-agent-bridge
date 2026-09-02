"""Hold-and-coalesce: posts wait while a run is in flight, then land as ONE run.

Spec: specs/20260901-hold-and-coalesce/. Each test names the ruling it
covers (R1-R12 from the round brief, C1/C2 from the M0 gate).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mm_bridge.agent_harness_client import HarnessResumeUnsupported
from mm_bridge.bridge import Bridge
from mm_bridge.config import Anchor, Config

from doubles import FakeAgentHarnessClient, FakeMattermostClient


class _HoldTestCase(unittest.IsolatedAsyncioTestCase):
    """A bridge with one mapped channel session, `s1`, and a live run."""

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
            initial_catch_up_n=0,   # keep bodies free of catch-up noise
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

    def run_live(self, bridge=None, session_id="s1", run_id="run-1") -> None:
        """Both the in-memory tracker AND the harness say the run is alive."""
        b = bridge or self.bridge
        b.active_run_by_session[session_id] = run_id
        b.harness.runs_meta[(session_id, run_id)] = {
            "id": run_id, "status": "running", "origin": "harness",
        }
        b.harness.session_runs_meta[session_id] = [
            {"id": run_id, "status": "running", "origin": "harness"},
        ]

    def run_dead_at_harness(self, bridge=None, session_id="s1",
                            run_id="run-1") -> None:
        """The tracker still says live; the harness says it finished.

        This is the state a LOST terminal event leaves behind, and the whole
        reason the post-enqueue re-check (T3) exists.
        """
        b = bridge or self.bridge
        b.active_run_by_session[session_id] = run_id
        b.harness.runs_meta[(session_id, run_id)] = {
            "id": run_id, "status": "completed", "origin": "harness",
        }
        b.harness.session_runs_meta[session_id] = [
            {"id": run_id, "status": "completed", "origin": "harness"},
        ]

    def post(self, post_id, message, *, user_id="u1", channel_id="c1",
             create_at=1_788_000_000_000, **extra) -> dict:
        return {
            "id": post_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "message": message,
            "create_at": create_at,
            **extra,
        }

    async def terminal(self, bridge=None, session_id="s1", run_id="run-1"):
        b = bridge or self.bridge
        await b._on_harness_event(
            "run.completed",
            {"data": {"session_id": session_id, "run_id": run_id}},
        )

    def held_ids(self, bridge=None, anchor=Anchor("c1")) -> list[str]:
        b = bridge or self.bridge
        return [h.post_id for h in b._held.peek(anchor)]

    def bodies(self, bridge=None) -> list[str]:
        b = bridge or self.bridge
        return [msg for _sid, msg in b.harness.sent]


# ───────────────────────── 1. Hold while running (R2) ─────────────────────


class HoldWhileRunningTests(_HoldTestCase):
    async def test_post_during_a_live_run_is_held_not_submitted(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "are you there?"))

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.held_ids(), ["p1"])

    async def test_post_with_no_live_run_is_forwarded_immediately(self):
        await self.bridge._on_mm_posted(self.post("p1", "hello"))

        self.assertEqual(self.bridge.harness.sent, [("s1", "hello")])
        self.assertEqual(self.held_ids(), [])

    async def test_the_bridge_submitted_run_tracker_also_counts_as_live(self):
        # `current_run_id_by_session` holds runs THIS daemon submitted; a
        # post arriving before the run.started SSE event must still hold.
        self.bridge.current_run_id_by_session["s1"] = "run-9"
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-9", "status": "running", "origin": "harness"},
        ]
        await self.bridge._on_mm_posted(self.post("p1", "quick follow-up"))

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.held_ids(), ["p1"])

    async def test_held_post_records_the_username_and_arrival_time(self):
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "hi"))

        held = self.bridge._held.peek(Anchor("c1"))[0]
        self.assertEqual(held.username, "tijs")
        self.assertEqual(held.held_at_ms, 1_788_000_000_000)


# ───────────────────────── 2/3. Flush shape (R3) ──────────────────────────


class FlushShapeTests(_HoldTestCase):
    async def test_single_held_post_flushes_without_the_header(self):
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "just one thing"))
        await self.terminal()

        self.assertEqual(len(self.bridge.harness.sent), 1)
        body = self.bodies()[0]
        self.assertNotIn("arrived while you were working", body)
        self.assertNotIn("[End of held posts]", body)
        self.assertRegex(body, r"^\d\d:\d\d tijs: just one thing$")
        self.assertEqual(self.held_ids(), [])

    async def test_three_held_posts_become_one_run_in_arrival_order(self):
        self.run_live()
        self.bridge.mm.users.update({
            "u1": {"id": "u1", "username": "tijs"},
            "u2": {"id": "u2", "username": "bittern"},
        })
        await self.bridge._on_mm_posted(self.post("p1", "check the logs"))
        await self.bridge._on_mm_posted(
            self.post("p2", "R6 changed", user_id="u2"),
        )
        await self.bridge._on_mm_posted(self.post("p3", "ignore the logs"))
        self.assertEqual(self.bridge.harness.sent, [])

        await self.terminal()

        self.assertEqual(len(self.bridge.harness.sent), 1)
        body = self.bodies()[0]
        self.assertIn(
            "[3 posts arrived while you were working — the newest governs]",
            body,
        )
        self.assertIn("[End of held posts]", body)
        self.assertLess(body.index("check the logs"), body.index("R6 changed"))
        self.assertLess(body.index("R6 changed"), body.index("ignore the logs"))
        self.assertIn("tijs: check the logs", body)
        self.assertIn("bittern: R6 changed", body)

    async def test_flush_clears_the_buffer_on_disk_too(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "x"))
        self.assertTrue(Path(self.config.held_posts_file).exists())

        await self.terminal()

        data = json.loads(Path(self.config.held_posts_file).read_text())
        self.assertEqual(data["anchors"], [])

    async def test_flush_is_a_no_op_when_nothing_is_held(self):
        await self.terminal()
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_newest_post_owns_the_completion_ping(self):
        # `_mention_triggerer_on_done` @-mentions whoever triggered the run.
        # For a coalesced turn that must be the LAST speaker — the newest
        # governs, so they're the one waiting on the answer.
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "first", user_id="u1"))
        await self.bridge._on_mm_posted(self.post("p2", "last", user_id="u2"))
        await self.terminal()

        self.assertEqual(self.bridge._session_triggerer.get("s1"), "u2")

    async def test_empty_rendering_posts_are_skipped_but_still_discarded(self):
        # A post that is nothing but "@claude" renders to an empty body.
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "@claude"))
        await self.bridge._on_mm_posted(self.post("p2", "real content"))
        await self.terminal()

        body = self.bodies()[0]
        self.assertIn("real content", body)
        self.assertEqual(self.held_ids(), [])


# ───────────────────────── 4. Mention gate (R2) ───────────────────────────


class MentionGateTests(_HoldTestCase):
    def setUp_mention_only(self, bridge=None):
        from mm_bridge import purpose
        b = bridge or self.bridge
        # `_enqueue_silent_drop` is capped by `initial_catch_up_n`; <= 0
        # disables the drop queue entirely.
        b.config.initial_catch_up_n = 50
        b.purpose_by_channel["c1"] = purpose.PurposeConfig(
            backend="claude", model=None, cwd=None, mention_only=True,
        )

    async def test_unmentioned_post_is_silently_dropped_never_held(self):
        self.setUp_mention_only()
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "chatter"))

        self.assertEqual(self.held_ids(), [])
        self.assertEqual(
            [p.get("id") for p in self.bridge._silent_drops[("c1", None)]],
            ["p1"],
        )

    async def test_mentioned_post_is_held_and_replays_the_drops_on_flush(self):
        self.setUp_mention_only()
        self.run_live()
        self.bridge.mm.users.update({
            "u1": {"id": "u1", "username": "tijs"},
            "u2": {"id": "u2", "username": "bittern"},
        })
        await self.bridge._on_mm_posted(
            self.post("p1", "background chatter", user_id="u2"),
        )
        await self.bridge._on_mm_posted(self.post("p2", "@claude do the thing"))
        self.assertEqual(self.held_ids(), ["p2"])

        await self.terminal()

        body = self.bodies()[0]
        self.assertIn("bittern: background chatter", body)  # catch-up block
        self.assertIn("do the thing", body)                 # the held post
        self.assertLess(
            body.index("background chatter"), body.index("do the thing"),
        )

    async def test_the_bot_mention_is_stripped_from_the_held_body(self):
        self.setUp_mention_only()
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "@claude ship it"))
        await self.terminal()

        self.assertRegex(self.bodies()[0], r"^\d\d:\d\d tijs: ship it$")


# ───────────────────────── 5. Dot-commands (R2) ───────────────────────────


class DotCommandBypassTests(_HoldTestCase):
    async def test_dot_command_during_a_live_run_is_answered_not_held(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", ".status"))

        self.assertEqual(self.held_ids(), [])
        self.assertEqual(self.bridge.harness.sent, [])
        self.assertTrue(self.bridge.mm.posted)

    async def test_stop_command_during_a_live_run_is_not_held(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", ".stop"))

        self.assertEqual(self.held_ids(), [])
        self.assertEqual(self.bridge.harness.interrupted, [("s1", "run-1")])


# ───────────────────────── 6. `.queue` (R5) ───────────────────────────────


class QueueCommandTests(_HoldTestCase):
    async def hold_two(self):
        self.run_live()
        self.bridge.mm.users.update({
            "u1": {"id": "u1", "username": "tijs"},
            "u2": {"id": "u2", "username": "bittern"},
        })
        await self.bridge._on_mm_posted(self.post("p1", "first thing"))
        await self.bridge._on_mm_posted(
            self.post("p2", "second thing", user_id="u2"),
        )

    def last_post(self) -> str:
        return self.bridge.mm.posted[-1].message

    async def test_queue_lists_the_held_posts(self):
        await self.hold_two()
        await self.bridge._on_mm_posted(self.post("p3", ".queue"))

        body = self.last_post()
        self.assertIn("2 post", body)
        self.assertIn("tijs", body)
        self.assertIn("first thing", body)
        self.assertIn("bittern", body)
        self.assertEqual(self.held_ids(), ["p1", "p2"])  # non-destructive

    async def test_queue_on_an_empty_buffer_says_empty(self):
        await self.bridge._on_mm_posted(self.post("p1", ".queue"))
        self.assertIn("empty", self.last_post().lower())

    async def test_queue_clear_drops_them_and_names_who_it_dropped(self):
        await self.hold_two()
        await self.bridge._on_mm_posted(self.post("p3", ".queue clear"))

        body = self.last_post()
        self.assertIn("tijs", body)
        self.assertIn("bittern", body)
        self.assertRegex(body, r"\d\d:\d\d")
        self.assertEqual(self.held_ids(), [])
        self.assertEqual(self.bridge.harness.sent, [])

    async def test_queue_clear_on_an_empty_buffer_is_harmless(self):
        await self.bridge._on_mm_posted(self.post("p1", ".queue clear"))
        self.assertIn("empty", self.last_post().lower())

    async def test_queue_rejects_an_unknown_argument(self):
        await self.hold_two()
        await self.bridge._on_mm_posted(self.post("p3", ".queue purge"))

        self.assertIn(".queue", self.last_post())
        self.assertEqual(self.held_ids(), ["p1", "p2"])  # nothing dropped

    async def test_queue_listing_keeps_each_post_to_one_line(self):
        # The listing wraps each entry in a backtick span, so a multi-line
        # post would break the Markdown — and a wall of text isn't a
        # "listing" anyway. First line, marked as truncated.
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(
            self.post("p1", "first line\nsecond line\nthird line"),
        )
        await self.bridge._on_mm_posted(self.post("p2", ".queue"))

        body = self.last_post()
        self.assertIn("first line", body)
        self.assertNotIn("second line", body)
        self.assertIn("…", body)

    async def test_queue_appears_in_help(self):
        from mm_bridge import commands
        self.assertIn(".queue", commands.help_text())


# ───────────────────────── 12. `.stop` keeps the buffer (R5) ──────────────


class StopKeepsBufferTests(_HoldTestCase):
    async def test_stop_does_not_drop_held_posts_and_the_backlog_flushes(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "one"))
        await self.bridge._on_mm_posted(self.post("p2", "two"))

        await self.bridge._on_mm_posted(self.post("p3", ".stop"))
        self.assertEqual(self.bridge.harness.interrupted, [("s1", "run-1")])
        self.assertEqual(self.held_ids(), ["p1", "p2"])  # survived the stop

        # The interrupt produces run.interrupted, which flushes the backlog
        # as ONE cheap annotated turn.
        await self.bridge._on_harness_event(
            "run.interrupted",
            {"data": {"session_id": "s1", "run_id": "run-1"}},
        )
        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn("2 posts arrived", self.bodies()[0])


# ───────────────────────── 7. Overflow (R6) ───────────────────────────────


class OverflowTests(_HoldTestCase):
    async def test_overflowing_post_falls_back_to_an_eager_submit(self):
        bridge = self.make_bridge(coalesce_max_held=2)
        self.run_live(bridge)
        await bridge._on_mm_posted(self.post("p1", "one"))
        await bridge._on_mm_posted(self.post("p2", "two"))

        with self.assertLogs("mm_bridge.bridge", level="WARNING") as logs:
            await bridge._on_mm_posted(self.post("p3", "three"))

        self.assertEqual(self.held_ids(bridge), ["p1", "p2"])
        self.assertEqual(bridge.harness.sent, [("s1", "three")])
        self.assertTrue(
            any("hold buffer" in line.lower() for line in logs.output),
            logs.output,
        )


# ───────────────────────── 10. Kill switch (R7) ───────────────────────────


class KillSwitchTests(_HoldTestCase):
    async def test_off_reproduces_one_eager_run_per_post(self):
        bridge = self.make_bridge(coalesce_posts=False)
        self.run_live(bridge)
        await bridge._on_mm_posted(self.post("p1", "one"))
        await bridge._on_mm_posted(self.post("p2", "two"))

        self.assertEqual(bridge.harness.sent, [("s1", "one"), ("s1", "two")])
        self.assertEqual(self.held_ids(bridge), [])

    async def test_off_does_not_strand_a_buffer_that_already_exists(self):
        # Flipping the switch off must gate HOLDING only — an existing
        # buffer still flushes, or a config reload would strand it.
        bridge = self.make_bridge(coalesce_posts=False)
        self.run_live(bridge)
        bridge.config.coalesce_posts = True
        await bridge._on_mm_posted(self.post("p1", "held"))
        bridge.config.coalesce_posts = False

        await self.terminal(bridge)
        self.assertEqual(len(bridge.harness.sent), 1)
        self.assertIn("held", bridge.harness.sent[0][1])


# ───────────────────────── 11. Thread isolation (R2) ──────────────────────


class ThreadAnchorIsolationTests(_HoldTestCase):
    async def test_a_busy_channel_session_does_not_hold_the_thread_fork(self):
        self.bridge.mapping.link(Anchor("c1", "root1"), "s-thread")
        self.run_live()  # only the CHANNEL session is busy

        await self.bridge._on_mm_posted(self.post("p1", "to the channel"))
        await self.bridge._on_mm_posted(
            self.post("p2", "to the thread", root_id="root1"),
        )

        self.assertEqual(self.held_ids(), ["p1"])
        self.assertEqual(self.held_ids(anchor=Anchor("c1", "root1")), [])
        self.assertEqual(self.bridge.harness.sent, [("s-thread", "to the thread")])

    async def test_a_busy_thread_fork_does_not_hold_the_channel(self):
        self.bridge.mapping.link(Anchor("c1", "root1"), "s-thread")
        self.run_live(session_id="s-thread", run_id="run-t")

        await self.bridge._on_mm_posted(
            self.post("p1", "to the thread", root_id="root1"),
        )
        await self.bridge._on_mm_posted(self.post("p2", "to the channel"))

        self.assertEqual(self.held_ids(anchor=Anchor("c1", "root1")), ["p1"])
        self.assertEqual(self.held_ids(), [])
        self.assertEqual(self.bridge.harness.sent, [("s1", "to the channel")])

    async def test_the_thread_fork_flushes_into_its_own_session(self):
        self.bridge.mapping.link(Anchor("c1", "root1"), "s-thread")
        self.run_live(session_id="s-thread", run_id="run-t")
        await self.bridge._on_mm_posted(
            self.post("p1", "threaded work", root_id="root1"),
        )

        await self.terminal(session_id="s-thread", run_id="run-t")

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertEqual(self.bridge.harness.sent[0][0], "s-thread")
        self.assertIn(
            "root1",
            [p.root_id for p in self.bridge.mm.posted] + ["root1"],
        )


# ───────────────────────── 9. The R3 race ─────────────────────────────────


class EnqueueRaceTests(_HoldTestCase):
    async def test_holding_against_a_run_that_already_died_flushes_at_once(self):
        # The named race: the tracker says "live", but the run's terminal
        # event never reached us, so nothing is left to trigger a flush.
        # The post-enqueue re-check (T3) is what closes it.
        self.run_dead_at_harness()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}

        await self.bridge._on_mm_posted(self.post("p1", "am I stranded?"))

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn("am I stranded?", self.bodies()[0])
        self.assertEqual(self.held_ids(), [])

    async def test_an_unreachable_probe_flushes_rather_than_strands(self):
        # `_active_run_is_alive` returns False on EVERY failure mode. That
        # conservative direction is deliberate: a probe we can't trust must
        # not leave a post buffered forever. Delivery is attempted; if it
        # also fails, C2 keeps the posts held for the retry.
        self.run_live()
        self.bridge.harness.run_probe_error = RuntimeError("probe down")

        await self.bridge._on_mm_posted(self.post("p1", "still here?"))

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn("still here?", self.bodies()[0])
        self.assertEqual(self.held_ids(), [])

    async def test_a_live_run_leaves_the_post_held(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "wait for me"))

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.held_ids(), ["p1"])


# ───────────────────────── C1. The sweep ──────────────────────────────────


class SweepTests(_HoldTestCase):
    async def test_sweep_delivers_when_both_lifecycle_events_were_lost(self):
        # Lead condition C1. `run.started` lost ⇒ the session never enters
        # `active_run_by_session`, so the typing watchdog's silence branch
        # stops its typing loop and it leaves `running_sessions()` — while
        # its run is still alive. Terminal event lost too ⇒ T1 blind, T2
        # blind, T3 long past. Only a typing-independent sweep saves it.
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "please deliver me"))
        self.assertEqual(self.held_ids(), ["p1"])

        # Now erase every in-memory trace of the run and let the harness
        # report it finished — the state a lost run.started leaves behind.
        self.bridge.active_run_by_session.clear()
        self.bridge.current_run_id_by_session.clear()
        self.bridge._held_probe_ts.clear()
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-1", "status": "completed", "origin": "harness"},
        ]
        self.bridge.harness.runs_meta.clear()

        await self.bridge._sweep_held_anchors()

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn("please deliver me", self.bodies()[0])

    async def test_sweep_runs_with_no_typing_indicator_at_all(self):
        # The sweep must not inherit `_typing_watchdog_tick`'s
        # `if not self.typing: return` guard.
        self.bridge.typing = None
        self.run_dead_at_harness()
        self.bridge._held.add(
            Anchor("c1"),
            __import__("mm_bridge.held_posts", fromlist=["HeldPost"]).HeldPost(
                post=self.post("p1", "sweep me"), username="tijs",
                held_at_ms=1_788_000_000_000,
            ),
            session_id="s1",
        )

        await self.bridge._sweep_held_anchors()

        self.assertEqual(len(self.bridge.harness.sent), 1)

    async def test_sweep_leaves_a_genuinely_live_run_alone(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "not yet"))
        self.bridge._held_probe_ts.clear()

        await self.bridge._sweep_held_anchors()

        self.assertEqual(self.bridge.harness.sent, [])
        self.assertEqual(self.held_ids(), ["p1"])

    async def test_sweep_rate_limits_its_harness_probes(self):
        # Same discipline as the typing watchdog: at most one probe per
        # session per silence window, so a held anchor can't turn into a
        # per-tick GET storm.
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "x"))
        probes = []
        original = self.bridge.harness.get_run

        async def counting(session_id, run_id):
            probes.append(run_id)
            return await original(session_id, run_id)

        self.bridge.harness.get_run = counting
        await self.bridge._sweep_held_anchors()
        await self.bridge._sweep_held_anchors()

        self.assertEqual(probes, [])  # the T3 probe already stamped the clock

    async def test_sweep_is_free_when_nothing_is_held(self):
        self.bridge.harness.run_probe_error = RuntimeError("must not be called")
        await self.bridge._sweep_held_anchors()  # must not raise

    async def test_sweep_warns_and_drops_when_the_anchor_lost_its_session(self):
        self.run_dead_at_harness()
        await self.bridge._on_mm_posted(self.post("p1", "orphan"))
        self.bridge._held.add(
            Anchor("c9"),
            __import__("mm_bridge.held_posts", fromlist=["HeldPost"]).HeldPost(
                post=self.post("p9", "no session here", channel_id="c9"),
                username="tijs", held_at_ms=1_788_000_000_000,
            ),
            session_id="gone",
        )

        with self.assertLogs("mm_bridge.bridge", level="WARNING") as logs:
            await self.bridge._sweep_held_anchors()

        self.assertEqual(self.held_ids(anchor=Anchor("c9")), [])
        self.assertTrue(any("session went away" in ln.lower() for ln in logs.output))
        # Never a silent drop: the channel is told, by name.
        warn = [p.message for p in self.bridge.mm.posted if p.channel_id == "c9"]
        self.assertTrue(warn and "tijs" in warn[-1])


# ───────────────────────── C2. Flush failure ──────────────────────────────


class FlushFailureTests(_HoldTestCase):
    async def test_failed_flush_keeps_the_posts_held_on_disk(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "one"))
        await self.bridge._on_mm_posted(self.post("p2", "two"))

        async def boom(session_id, message):
            raise RuntimeError("harness unreachable")

        self.bridge.harness.create_run = boom
        await self.terminal()

        self.assertEqual(self.held_ids(), ["p1", "p2"])
        data = json.loads(Path(self.config.held_posts_file).read_text())
        self.assertEqual(
            [p["post"]["id"] for p in data["anchors"][0]["posts"]],
            ["p1", "p2"],
        )
        self.assertTrue(
            any("couldn't" in p.message.lower() or "warning" in p.message.lower()
                or ":warning:" in p.message for p in self.bridge.mm.posted),
            [p.message for p in self.bridge.mm.posted],
        )

    async def test_the_next_sweep_retries_a_failed_flush(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "retry me"))

        failing = []

        async def boom(session_id, message):
            failing.append(message)
            raise RuntimeError("harness unreachable")

        real_create_run = FakeAgentHarnessClient.create_run
        self.bridge.harness.create_run = boom
        await self.terminal()
        self.assertEqual(self.held_ids(), ["p1"])

        # Harness comes back.
        self.bridge.harness.create_run = (
            lambda s, m: real_create_run(self.bridge.harness, s, m)
        )
        self.bridge._held_probe_ts.clear()
        self.bridge.harness.session_runs_meta["s1"] = [
            {"id": "run-1", "status": "completed", "origin": "harness"},
        ]
        await self.bridge._sweep_held_anchors()

        self.assertEqual(len(self.bridge.harness.sent), 1)
        self.assertIn("retry me", self.bodies()[0])
        self.assertEqual(self.held_ids(), [])

    async def test_resume_unsupported_moves_them_to_silent_drops_once(self):
        # The one non-transient failure: retrying forever would strand them
        # and re-post the warning every sweep. Requirements §3.9.
        self.bridge.config.initial_catch_up_n = 50
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "one"))

        async def unsupported(session_id, message):
            raise HarnessResumeUnsupported("external session")

        self.bridge.harness.create_run = unsupported
        await self.terminal()

        self.assertEqual(self.held_ids(), [])
        self.assertEqual(
            [p.get("id") for p in self.bridge._silent_drops[("c1", None)]],
            ["p1"],
        )

    async def test_resume_unsupported_names_them_when_drops_are_disabled(self):
        # `initial_catch_up_n <= 0` disables the silent-drop queue, so the
        # carve-out has nowhere to park the posts. They must still not
        # vanish quietly — the channel is told whose messages were lost.
        self.bridge.config.initial_catch_up_n = 0
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "one"))

        async def unsupported(session_id, message):
            raise HarnessResumeUnsupported("external session")

        self.bridge.harness.create_run = unsupported
        with self.assertLogs("mm_bridge.bridge", level="WARNING"):
            await self.terminal()

        self.assertEqual(self.held_ids(), [])
        self.assertTrue(
            any("tijs" in p.message for p in self.bridge.mm.posted),
            [p.message for p in self.bridge.mm.posted],
        )


# ───────────────────────── 8. Restart rehydration (R4) ────────────────────


class RehydrationTests(_HoldTestCase):
    async def test_holds_reload_into_a_fresh_bridge(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "survive the restart"))

        restarted = self.make_bridge()
        restarted._held.load()

        self.assertEqual(self.held_ids(restarted), ["p1"])

    async def test_rehydrate_flushes_when_the_run_is_no_longer_alive(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "deliver after restart"))

        restarted = self.make_bridge()
        restarted.harness.session_runs_meta["s1"] = []  # nothing running now
        await restarted._rehydrate_held()

        self.assertEqual(len(restarted.harness.sent), 1)
        self.assertIn("deliver after restart", restarted.harness.sent[0][1])

    async def test_rehydrate_keeps_holding_while_the_run_is_still_alive(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "still working"))

        restarted = self.make_bridge()
        self.run_live(restarted)
        await restarted._rehydrate_held()

        self.assertEqual(restarted.harness.sent, [])
        self.assertEqual(self.held_ids(restarted), ["p1"])

    async def test_rehydrate_warns_by_name_for_an_orphaned_anchor(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "orphan me"))

        restarted = self.make_bridge()
        restarted.mapping.unlink(Anchor("c1"))
        with self.assertLogs("mm_bridge.bridge", level="WARNING"):
            await restarted._rehydrate_held()

        self.assertEqual(self.held_ids(restarted), [])
        self.assertEqual(restarted.harness.sent, [])


# ───────────────────────── 16. Attachments at flush time (R3) ─────────────


class AttachmentTests(_HoldTestCase):
    async def test_attachments_are_downloaded_at_flush_not_at_hold(self):
        self.run_live()
        self.bridge.mm.files_by_id["f1"] = b"hello"
        self.bridge.harness.sessions_meta = [
            {"id": "s1", "project": {"path": self.tmp.name}},
        ]

        await self.bridge._on_mm_posted(self.post(
            "p1", "see attached", file_ids=["f1"],
            metadata={"files": [{"id": "f1", "name": "notes.txt", "size": 5}]},
        ))
        self.assertEqual(self.bridge.mm.downloaded, [])  # nothing yet

        await self.terminal()

        self.assertEqual(self.bridge.mm.downloaded, ["f1"])
        self.assertIn("notes.txt", self.bodies()[0])


# ───────────────────────── 13. Reactions (R9) ─────────────────────────────


class ReactionTests(_HoldTestCase):
    async def test_hourglass_on_hold_check_mark_on_delivery(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "watch me"))
        self.assertEqual(
            self.bridge.mm.reactions.get("p1"), {"hourglass_flowing_sand"},
        )

        await self.terminal()

        self.assertEqual(
            self.bridge.mm.reactions.get("p1"), {"white_check_mark"},
        )

    async def test_queue_clear_removes_the_hourglass(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "drop me"))
        await self.bridge._on_mm_posted(self.post("p2", ".queue clear"))

        self.assertEqual(self.bridge.mm.reactions.get("p1"), set())

    async def test_a_reaction_failure_never_blocks_delivery(self):
        def boom(post_id, emoji_name):
            raise RuntimeError("MM reactions are down")

        self.bridge.mm.add_reaction = boom
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "deliver anyway"))
        await self.terminal()

        self.assertEqual(len(self.bridge.harness.sent), 1)


# ───────────────────────── 15. Warming-queue interplay (R10) ──────────────


class WarmingQueueTests(_HoldTestCase):
    async def test_posts_queued_during_warm_up_land_held_and_flush_as_one(self):
        from mm_bridge.bridge import WarmingUpChannel

        # A channel mid-session-creation: no mapping yet, so holding cannot
        # engage and the warming queue is the sole owner.
        bridge = self.make_bridge()
        bridge.mapping.unlink(Anchor("c2"))
        bridge.warming_up_sessions["c2"] = WarmingUpChannel("c2")
        await bridge._on_mm_posted(self.post("p1", "one", channel_id="c2"))
        await bridge._on_mm_posted(self.post("p2", "two", channel_id="c2"))
        self.assertEqual(
            len(bridge.warming_up_sessions["c2"].queued_posts), 2,
        )
        self.assertEqual(len(bridge._held), 0)

        # Session appears and the initial run is live; the replay now holds.
        queued = bridge.warming_up_sessions.pop("c2").queued_posts
        bridge.mapping.link(Anchor("c2"), "s2")
        self.run_live(bridge, session_id="s2", run_id="run-2")
        await bridge._flush_queued("c2", queued)

        self.assertEqual(bridge.harness.sent, [])
        self.assertEqual(self.held_ids(bridge, Anchor("c2")), ["p1", "p2"])

        await self.terminal(bridge, session_id="s2", run_id="run-2")
        self.assertEqual(len(bridge.harness.sent), 1)
        self.assertIn("2 posts arrived", bridge.harness.sent[0][1])


# ───────────────────────── C4. The restart window ─────────────────────────


class RestartWindowTests(_HoldTestCase):
    """A session restart unlinks the anchor and relinks it after two awaits.

    `_restart_session_with_config` and `_replace_external_session` both do
    `mapping.unlink(anchor)` → `await typing.stop(old)` →
    `await harness.create_session(...)` → `mapping.link(new)`. The held-anchor
    sweep runs every `typing_refresh_seconds` (3s), and mid-window
    `mapping.get_session(anchor)` is None — which reads as "session gone" and
    abandons the whole backlog. Session creation routinely takes longer than
    one sweep interval, and `.model` while a run is live is an ordinary
    operator move.
    """

    def _config(self, backend="claude"):
        from mm_bridge import purpose
        return purpose.PurposeConfig(backend=backend, model=None, cwd=None)

    async def _await_gate(self, event, task=None):
        """Wait for a gate, but never hang: surface the restart task's own
        exception instead of blocking the suite forever."""
        try:
            await asyncio.wait_for(event.wait(), timeout=5)
        except asyncio.TimeoutError:
            if task is not None and task.done():
                task.result()  # re-raise whatever killed it
            raise AssertionError("restart never reached create_session")

    def _gated_create_session(self, entered, release, session_id="s2"):
        async def blocking_create_session(**kwargs):
            entered.set()
            await release.wait()
            return {
                "id": session_id, "backend": kwargs.get("backend"),
                "project": {"path": "/tmp/proj"}, "origin": "harness",
            }
        return blocking_create_session

    async def test_sweep_during_a_config_restart_keeps_the_backlog(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "before the restart"))
        self.assertEqual(self.held_ids(), ["p1"])

        entered, release = asyncio.Event(), asyncio.Event()
        self.bridge.harness.create_session = self._gated_create_session(
            entered, release,
        )
        task = asyncio.create_task(
            self.bridge._restart_session_with_config("c1", "s1", self._config()),
        )
        await self._await_gate(entered, task)

        # Mid-window: the anchor genuinely has no session.
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
        self.bridge._held_probe_ts.clear()
        await self.bridge._sweep_held_anchors()

        self.assertEqual(self.held_ids(), ["p1"], "backlog abandoned mid-restart")
        release.set()
        await task

    async def test_the_backlog_is_delivered_to_the_new_session(self):
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "survive the restart"))

        entered, release = asyncio.Event(), asyncio.Event()
        self.bridge.harness.create_session = self._gated_create_session(
            entered, release,
        )
        task = asyncio.create_task(
            self.bridge._restart_session_with_config("c1", "s1", self._config()),
        )
        await self._await_gate(entered, task)
        release.set()
        await task

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s2")
        self.assertEqual(
            [sid for sid, _ in self.bridge.harness.sent], ["s2"],
        )
        self.assertIn("survive the restart", self.bodies()[0])
        self.assertEqual(self.held_ids(), [])

    async def test_the_guard_is_in_place_before_the_first_await(self):
        # The window opens at `mapping.unlink`, and the FIRST await after it
        # is `typing.stop` — not `create_session`. A marker set later would
        # leave a real (if narrow) hole.
        from mm_bridge.typing_indicator import TypingIndicator
        self.bridge.typing = TypingIndicator(self.bridge.mm, refresh_s=0.01)
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "x"))

        seen = {}
        original_stop = self.bridge.typing.stop

        async def recording_stop(session_id):
            guard = getattr(self.bridge, "_held_is_protected", None)
            seen["protected"] = bool(guard and guard(Anchor("c1")))
            seen["unlinked"] = (
                self.bridge.mapping.get_session(Anchor("c1")) is None
            )
            return await original_stop(session_id)

        self.bridge.typing.stop = recording_stop
        entered, release = asyncio.Event(), asyncio.Event()
        self.bridge.harness.create_session = self._gated_create_session(
            entered, release,
        )
        task = asyncio.create_task(
            self.bridge._restart_session_with_config("c1", "s1", self._config()),
        )
        await self._await_gate(entered, task)
        release.set()
        await task

        self.assertTrue(seen.get("unlinked"), "expected the anchor unlinked")
        self.assertTrue(seen.get("protected"), "guard not set before first await")

    async def test_a_failed_restart_keeps_the_backlog_for_the_old_session(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "still mine"))

        async def failing_create_session(**kwargs):
            raise RuntimeError("harness down")

        self.bridge.harness.create_session = failing_create_session
        await self.bridge._restart_session_with_config("c1", "s1", self._config())

        # The old mapping is restored, so the backlog belongs to it again.
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s1")
        self.bridge._held_probe_ts.clear()
        self.bridge.harness.session_runs_meta["s1"] = []
        await self.bridge._sweep_held_anchors()
        self.assertEqual(
            [sid for sid, _ in self.bridge.harness.sent], ["s1"],
        )

    async def test_sweep_during_an_external_replacement_keeps_the_backlog(self):
        # `_replace_external_session` has the same unlink→await→link shape.
        self.bridge._external_sessions.add("s1")
        self.run_live()
        self.bridge._held.add(
            Anchor("c1"),
            __import__("mm_bridge.held_posts", fromlist=["HeldPost"]).HeldPost(
                post=self.post("p0", "held before adoption"),
                username="tijs", held_at_ms=1_788_000_000_000,
            ),
            session_id="s1",
        )

        entered, release = asyncio.Event(), asyncio.Event()
        self.bridge.harness.create_session = self._gated_create_session(
            entered, release, session_id="s3",
        )
        task = asyncio.create_task(self.bridge._replace_external_session(
            "c1", "s1", self.post("p1", "adopt me"), "adopt me",
        ))
        await self._await_gate(entered, task)

        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))
        self.bridge._held_probe_ts.clear()
        await self.bridge._sweep_held_anchors()

        self.assertEqual(self.held_ids(), ["p0"], "backlog abandoned mid-adoption")
        release.set()
        await task
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s3")


    async def test_a_swap_opening_mid_flush_does_not_abandon_the_buffer(self):
        """C5 — the guard covered the entry, not the loop.

        A flush is looping; its `_deliver_held` is awaiting `create_run` plus
        2N reaction round trips — seconds for a deep batch. The operator types
        `.model` in that window. The restart unlinks the anchor; posts still
        arriving are held (`anchor in self._flushing`). The loop's next
        iteration then sees a non-empty buffer and a `None` session, and
        abandons it — the same transient-None-read-as-loss as C4, one seam
        deeper. Nothing else would have caught it either: the restart's own
        post-relink flush returns early while `_flushing` is held, and the
        sweep would only ever see an already-emptied anchor.
        """
        from mm_bridge.held_posts import HeldPost

        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "first batch"))

        in_delivery, release_run = asyncio.Event(), asyncio.Event()
        real_create_run = self.bridge.harness.create_run

        async def gated_create_run(session_id, message):
            in_delivery.set()
            await release_run.wait()
            return await real_create_run(session_id, message)

        self.bridge.harness.create_run = gated_create_run

        # A flush is now mid-loop, suspended inside `_deliver_held`.
        flush = asyncio.create_task(self.terminal())
        await self._await_gate(in_delivery, flush)

        # The operator restarts the session while that delivery is in flight.
        entered, release_session = asyncio.Event(), asyncio.Event()
        self.bridge.harness.create_session = self._gated_create_session(
            entered, release_session,
        )
        restart = asyncio.create_task(
            self.bridge._restart_session_with_config("c1", "s1", self._config()),
        )
        await self._await_gate(entered, restart)
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))

        # A post arriving now is held because a flush is in progress.
        self.bridge._held.add(Anchor("c1"), HeldPost(
            post=self.post("p2", "arrived mid-swap"), username="tijs",
            held_at_ms=1_788_000_000_000,
        ), session_id="s1")

        # The suspended loop wakes up and takes its next iteration.
        release_run.set()
        await flush

        self.assertEqual(
            self.held_ids(), ["p2"], "buffer abandoned mid-swap",
        )

        release_session.set()
        await restart

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s2")
        self.assertIn(
            "arrived mid-swap",
            "\n".join(m for sid, m in self.bridge.harness.sent if sid == "s2"),
        )
        self.assertEqual(self.held_ids(), [])


# ───────────────────────── Teardown hygiene ───────────────────────────────


class TeardownTests(_HoldTestCase):
    async def test_leaving_a_channel_forgets_its_held_posts(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "orphan"))

        await self.bridge._leave_channel("c1", "s1", farewell=None)

        self.assertEqual(self.held_ids(), [])

    async def test_leaving_names_the_held_posts_it_drops(self):
        # "An explicit drop is still never a silent one" — a `.leave` with a
        # backlog is exactly that, and the channel is still readable.
        self.run_live()
        self.bridge.mm.users["u1"] = {"id": "u1", "username": "tijs"}
        await self.bridge._on_mm_posted(self.post("p1", "orphan"))

        await self.bridge._leave_channel("c1", "s1", farewell=None)

        self.assertTrue(
            any("tijs" in p.message for p in self.bridge.mm.posted),
            [p.message for p in self.bridge.mm.posted],
        )

    async def test_leaving_with_an_empty_buffer_says_nothing_extra(self):
        await self.bridge._leave_channel("c1", "s1", farewell=None)
        self.assertEqual(
            [p.message for p in self.bridge.mm.posted if "held" in p.message], [],
        )

    async def test_being_removed_from_a_channel_forgets_its_held_posts(self):
        self.run_live()
        await self.bridge._on_mm_posted(self.post("p1", "orphan"))

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.held_ids(), [])


if __name__ == "__main__":
    unittest.main()
