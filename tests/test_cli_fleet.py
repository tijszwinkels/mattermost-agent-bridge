"""Tests for `mm-bridge fleet` — the CLI half of the fleet view.

Same rows as the `.fleet` dot-command (both call `render_fleet`), but read
from disk instead of from a live daemon: the state file for the declared
states, the holds file for the held counts, one Mattermost listing for the
titles, and a best-effort harness probe per row.

Staleness contract, as for `mm-bridge inbox`: the daemon owns the files and
writes them atomically, so the CLI reads a snapshot that may be one tick
old — never a torn one. Every failure mode renders as an empty fleet, rc 0.

Spec: specs/20260901-turn-state-fleet/requirements.md §6.4.1 / §6.5.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config
from mm_bridge.held_posts import HELD_POSTS_SCHEMA_VERSION


class _FakeMM:
    def __init__(self, channels):
        self._channels = channels
        self.logged_in = False

    def login(self):
        self.logged_in = True

    def list_bot_channels(self):
        return [dict(c) for c in self._channels]

    def get_channel(self, channel_id):
        for c in self._channels:
            if c["id"] == channel_id:
                return dict(c)
        return {"id": channel_id}


class _FakeHarness:
    """Stands in for `AgentHarnessClient` in the CLI's probe pass."""

    live: set[str] = set()
    fail: bool = False

    def __init__(self, url):
        self.url = url

    async def list_session_runs(self, session_id):
        if type(self).fail:
            raise RuntimeError("harness unreachable")
        return ([{"id": "r1", "status": "running"}]
                if session_id in type(self).live else [])

    async def close(self):
        return None


class FleetCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.holds = self.root / "held_posts.json"
        self.sidecar_dir = self.root / "sidecar"
        self.cfg = Config(
            mm_bot_token="t",
            state_file=str(self.state),
            sidecar_dir=str(self.sidecar_dir),
            held_posts_file=str(self.holds),
        )
        sidecar.write(self.sidecar_dir, "ses_lead", "c1", None)
        _FakeHarness.live = set()
        _FakeHarness.fail = False
        self.channels = [
            {"id": "c1", "name": "lead", "display_name": "lead"},
            {"id": "c-kestrel", "name": "kestrel", "display_name": "kestrel",
             "header": "Parent: ~lead~"},
            {"id": "c-stranger", "name": "stranger", "display_name": "stranger",
             "header": "Parent: ~elsewhere~"},
        ]

    # ----- fixtures -----

    def write_state(self, *, states=True) -> None:
        entry = {"channel_id": "c-kestrel", "root_id": None,
                 "session_id": "ses_kestrel"}
        if states:
            entry["state"] = {
                "kind": "awaiting", "on": "lead", "note": "M0 gate",
                "set_at": time.time() - 47 * 60, "source": "agent",
            }
        self.state.write_text(json.dumps({
            "version": 6,
            "entries": [
                {"channel_id": "c1", "root_id": None, "session_id": "ses_lead"},
                entry,
                {"channel_id": "c-stranger", "root_id": None,
                 "session_id": "ses_stranger"},
            ],
        }))

    def write_holds(self, n=1) -> None:
        self.holds.write_text(json.dumps({
            "version": HELD_POSTS_SCHEMA_VERSION,
            "anchors": [{
                "channel_id": "c-kestrel", "root_id": None,
                "session_id": "ses_kestrel",
                "posts": [
                    {"held_at_ms": 1_788_000_000_000, "username": "tijs",
                     "post": {"id": f"p{i}", "message": "hi"}}
                    for i in range(n)
                ],
            }],
        }))

    def run_fleet(self, **kwargs) -> tuple[int, str, str]:
        args = argparse.Namespace(
            **{"channel": None, "all": False, "json": False, **kwargs}
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Config, "load", staticmethod(lambda: self.cfg)), \
                patch("mm_bridge.cli._make_mm_client",
                      return_value=_FakeMM(self.channels)), \
                patch("mm_bridge.cli.AgentHarnessClient", _FakeHarness), \
                patch("sys.stdout", out), patch("sys.stderr", err), \
                patch.dict("os.environ", {"CLAUDE_SESSION_ID": "ses_lead"},
                           clear=False):
            rc = cli.cmd_fleet(args)
        return rc, out.getvalue(), err.getvalue()

    # ----- tests -----

    def test_renders_children_from_disk(self):
        self.write_state()
        rc, out, _ = self.run_fleet()
        self.assertEqual(rc, 0)
        self.assertIn("kestrel", out)
        self.assertIn("awaiting → lead", out)
        self.assertIn("47 min", out)
        self.assertIn("note: M0 gate", out)
        self.assertNotIn("stranger", out)

    def test_held_counts_come_from_the_holds_file(self):
        self.write_state()
        self.write_holds(n=2)
        _, out, _ = self.run_fleet()
        self.assertIn("held: 2", out)

    def test_all_widens_to_every_mapped_session(self):
        self.write_state()
        _, out, _ = self.run_fleet(all=True)
        self.assertIn("stranger", out)

    def test_a_live_run_is_shown_as_working(self):
        self.write_state()
        _FakeHarness.live = {"ses_kestrel"}
        _, out, _ = self.run_fleet()
        self.assertIn("working", out)

    def test_an_unreachable_harness_marks_rows_uncertain(self):
        self.write_state()
        _FakeHarness.fail = True
        rc, out, _ = self.run_fleet()
        self.assertEqual(rc, 0)
        self.assertIn("?", out)

    def test_a_missing_state_file_is_an_empty_fleet_not_a_traceback(self):
        rc, out, _ = self.run_fleet()
        self.assertEqual(rc, 0)
        self.assertIn("kestrel", out)   # the channel exists, dormant

    def test_a_corrupt_state_file_is_an_empty_fleet(self):
        self.state.write_text("{not json")
        rc, out, _ = self.run_fleet()
        self.assertEqual(rc, 0)
        self.assertNotIn("Traceback", out)

    def test_json_output_carries_the_rows(self):
        self.write_state()
        rc, out, _ = self.run_fleet(json=True)
        payload = json.loads(out)
        self.assertEqual(rc, 0)
        titles = [r["title"] for r in payload]
        self.assertIn("kestrel", titles)
        row = next(r for r in payload if r["title"] == "kestrel")
        self.assertEqual(row["state"]["kind"], "awaiting")
        self.assertEqual(row["held"], 0)

    def test_the_cli_never_reconciles_sidecars(self):
        """The daemon owns the sidecar dir; a read-only view must not touch it."""
        self.write_state()
        (self.sidecar_dir / "ses_ghost.json").write_text('{"channel_id": "cX"}')
        self.run_fleet()
        self.assertTrue((self.sidecar_dir / "ses_ghost.json").exists())


if __name__ == "__main__":
    unittest.main()
