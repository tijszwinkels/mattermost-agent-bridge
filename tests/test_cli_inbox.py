"""Tests for `mm-bridge inbox` — the agent-facing view of the hold buffer.

The whole point of this command is that an agent can ask "is anything
waiting for me?" before it freezes a FINAL. It reads only the local holds
file, so these tests drive it with no Mattermost at all.

Spec: specs/20260901-hold-and-coalesce/design.md §7 (R8).
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config
from mm_bridge.held_posts import HELD_POSTS_SCHEMA_VERSION


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.holds = self.root / "held_posts.json"
        self.sidecar_dir = self.root / "sidecar"
        self.cfg = Config(
            mm_bot_token="t",
            state_file=str(self.root / "state.json"),
            sidecar_dir=str(self.sidecar_dir),
            held_posts_file=str(self.holds),
        )
        sidecar.write(self.sidecar_dir, "ses_1", "c1", None)

    # ----- helpers -----

    def write_holds(self, *, channel_id="c1", root_id=None, posts=None) -> None:
        self.holds.write_text(json.dumps({
            "version": HELD_POSTS_SCHEMA_VERSION,
            "anchors": [{
                "channel_id": channel_id,
                "root_id": root_id,
                "session_id": "ses_1",
                "posts": posts if posts is not None else [
                    {
                        "held_at_ms": 1_788_000_000_000,
                        "username": "tijs",
                        "post": {
                            "id": "p1", "channel_id": channel_id,
                            "user_id": "u1", "message": "check the logs",
                            "create_at": 1_788_000_000_000,
                        },
                    },
                    {
                        "held_at_ms": 1_788_000_060_000,
                        "username": "bittern",
                        "post": {
                            "id": "p2", "channel_id": channel_id,
                            "user_id": "u2", "message": "R6 changed",
                            "create_at": 1_788_000_060_000,
                        },
                    },
                ],
            }],
        }))

    def run_inbox(self, **kwargs) -> tuple[int, str, str]:
        args = argparse.Namespace(channel=None, json=False, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stdout", out), patch("sys.stderr", err), \
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "ses_1"},
                           clear=False):
            rc = cli.cmd_inbox(args)
        return rc, out.getvalue(), err.getvalue()

    # ----- tests -----

    def test_lists_held_posts_oldest_first(self):
        self.write_holds()
        rc, out, _ = self.run_inbox()

        self.assertEqual(rc, 0)
        self.assertIn("tijs", out)
        self.assertIn("check the logs", out)
        self.assertIn("bittern", out)
        self.assertIn("R6 changed", out)
        self.assertLess(out.index("check the logs"), out.index("R6 changed"))

    def test_shows_the_count(self):
        self.write_holds()
        _, out, _ = self.run_inbox()
        self.assertIn("2", out)

    def test_empty_when_no_holds_file_exists(self):
        rc, out, _ = self.run_inbox()
        self.assertEqual(rc, 0)
        self.assertIn("(empty)", out)

    def test_empty_when_this_anchor_holds_nothing(self):
        self.write_holds(channel_id="other-channel")
        rc, out, _ = self.run_inbox()
        self.assertEqual(rc, 0)
        self.assertIn("(empty)", out)

    def test_a_thread_forked_session_reads_its_own_thread(self):
        sidecar.write(self.sidecar_dir, "ses_thread", "c1", "root1")
        self.write_holds(root_id="root1")
        args = argparse.Namespace(channel=None, json=False)
        out = io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stdout", out), \
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "ses_thread"},
                           clear=False):
            rc = cli.cmd_inbox(args)
        self.assertEqual(rc, 0)
        self.assertIn("check the logs", out.getvalue())

    def test_json_output_carries_the_envelopes(self):
        self.write_holds()
        args = argparse.Namespace(channel=None, json=True)
        out = io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stdout", out), \
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "ses_1"},
                           clear=False):
            rc = cli.cmd_inbox(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual([p["post"]["id"] for p in payload], ["p1", "p2"])
        self.assertEqual(payload[0]["username"], "tijs")

    def test_json_output_of_an_empty_inbox_is_an_empty_list(self):
        args = argparse.Namespace(channel=None, json=True)
        out = io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stdout", out), \
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "ses_1"},
                           clear=False):
            rc = cli.cmd_inbox(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), [])

    def test_a_corrupt_holds_file_reports_empty_rather_than_crashing(self):
        # Best-effort by contract: the daemon owns this file, and a reader
        # must never be the thing that breaks.
        self.holds.write_text("{not json")
        rc, out, _ = self.run_inbox()
        self.assertEqual(rc, 0)
        self.assertIn("(empty)", out)

    def test_no_bot_token_needed_on_the_sidecar_path(self):
        # The username is stored in the envelope precisely so that an agent
        # can check its inbox without any Mattermost credentials.
        self.cfg.mm_bot_token = ""
        self.write_holds()
        rc, out, _ = self.run_inbox()
        self.assertEqual(rc, 0)
        self.assertIn("check the logs", out)

    def test_outside_a_session_it_explains_itself(self):
        args = argparse.Namespace(channel=None, json=False)
        err = io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stderr", err), \
                patch.dict("os.environ", {}, clear=True):
            rc = cli.cmd_inbox(args)
        self.assertEqual(rc, 2)
        self.assertIn("Mattermost channel", err.getvalue())

    def test_explicit_channel_id_bypasses_the_sidecar(self):
        # A raw 26-char MM id resolves locally — no login, no bot token.
        other = "abcdefghijklmnopqrstuvwxyz"
        self.write_holds(channel_id=other)
        args = argparse.Namespace(channel=other, json=False)
        out = io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("sys.stdout", out), \
                patch.dict("os.environ", {}, clear=True):
            rc = cli.cmd_inbox(args)
        self.assertEqual(rc, 0)
        self.assertIn("check the logs", out.getvalue())

    def test_inbox_is_registered_as_a_subcommand(self):
        parser = cli._build_parser()
        args = parser.parse_args(["inbox"])
        self.assertIs(args.func, cli.cmd_inbox)


if __name__ == "__main__":
    unittest.main()
