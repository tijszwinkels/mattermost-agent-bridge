"""Bounded per-session stderr tail (lead condition C3, round F3).

The harness publishes CLI stderr as ``process.stderr`` events; the bridge
already receives them and needs the last few lines to classify a
``run.failed`` that carries only a returncode. The buffer must stay small and
bounded in BOTH lines and bytes — one dict entry per live session, never a
transcript.
"""
from __future__ import annotations

import unittest

from mm_bridge.stderr_tail import SessionStderrTails


class BoundsTests(unittest.TestCase):
    def test_keeps_only_the_last_max_lines(self):
        tails = SessionStderrTails(max_lines=3)
        for i in range(10):
            tails.append("s1", f"line-{i}")
        self.assertEqual(tails.lines("s1"), ("line-7", "line-8", "line-9"))

    def test_total_bytes_are_capped(self):
        tails = SessionStderrTails(max_lines=100, max_bytes=100)
        for i in range(50):
            tails.append("s1", "x" * 40)
        joined = "".join(tails.lines("s1"))
        self.assertLessEqual(len(joined.encode()), 100)
        self.assertTrue(tails.lines("s1"), "must not empty itself entirely")

    def test_one_oversized_line_is_truncated_not_dropped(self):
        tails = SessionStderrTails(max_lines=5, max_bytes=4096)
        tails.append("s1", "E" * 5000)
        (only,) = tails.lines("s1")
        self.assertLess(len(only), 5000)
        self.assertTrue(only.startswith("EEE"))

    def test_multiline_chunk_splits_into_lines(self):
        tails = SessionStderrTails(max_lines=5)
        tails.append("s1", "first\nsecond\n\nthird\n")
        self.assertEqual(tails.lines("s1"), ("first", "second", "third"))


class IsolationTests(unittest.TestCase):
    def test_sessions_do_not_bleed_into_each_other(self):
        tails = SessionStderrTails()
        tails.append("s1", "one")
        tails.append("s2", "two")
        self.assertEqual(tails.lines("s1"), ("one",))
        self.assertEqual(tails.lines("s2"), ("two",))

    def test_clear_drops_the_session_entry(self):
        tails = SessionStderrTails()
        tails.append("s1", "one")
        tails.clear("s1")
        self.assertEqual(tails.lines("s1"), ())

    def test_unknown_session_reads_empty(self):
        self.assertEqual(SessionStderrTails().lines("nope"), ())

    def test_clear_of_unknown_session_is_a_no_op(self):
        SessionStderrTails().clear("nope")  # must not raise


if __name__ == "__main__":
    unittest.main()
