"""Anchor model tests — the unified `(channel_id, Optional[root_id])` key.

Covers the `Anchor` value type and the rewritten `ChannelMapping` API built
around it (one forward map, one reverse map), plus the v2 → v3 JSON schema
migration path that reads legacy `channel_to_session` + `thread_mapping`
fields and re-emits them under the new `entries` key on first save.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mm_bridge.config import Anchor, ChannelMapping
from mm_bridge.session_state import SessionState


class AnchorTypeTests(unittest.TestCase):
    """`Anchor` — a frozen, hashable `(channel_id, Optional[root_id])` tuple."""

    def test_channel_anchor_has_no_root(self) -> None:
        a = Anchor("chan-1")
        self.assertEqual(a.channel_id, "chan-1")
        self.assertIsNone(a.root_id)
        self.assertFalse(a.is_thread)

    def test_thread_anchor_carries_root(self) -> None:
        a = Anchor("chan-1", "root-42")
        self.assertEqual(a.channel_id, "chan-1")
        self.assertEqual(a.root_id, "root-42")
        self.assertTrue(a.is_thread)

    def test_anchors_are_hashable_and_usable_as_dict_keys(self) -> None:
        d = {Anchor("c1"): "s1", Anchor("c1", "r1"): "s2"}
        self.assertEqual(d[Anchor("c1")], "s1")
        self.assertEqual(d[Anchor("c1", "r1")], "s2")

    def test_channel_and_thread_anchors_with_same_channel_are_distinct(self) -> None:
        self.assertNotEqual(Anchor("c1"), Anchor("c1", "r1"))

    def test_equal_anchors_hash_identically(self) -> None:
        self.assertEqual(hash(Anchor("c1", "r1")), hash(Anchor("c1", "r1")))

    def test_empty_string_root_normalizes_to_none(self) -> None:
        """Passing `""` as root_id is treated as a channel anchor — no empty-string roots."""
        self.assertEqual(Anchor("c1", ""), Anchor("c1"))


class ChannelMappingAnchorAPITests(unittest.TestCase):
    """`ChannelMapping` exposes `link / unlink / get_session / get_anchor` over `Anchor`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_link_channel_anchor_roundtrips(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))

    def test_link_thread_anchor_roundtrips(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_channel_and_thread_anchors_coexist(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1", "r1"), "s-fork")
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_unlink_returns_and_removes_session(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1", "r1"), "s-fork")
        removed = m.unlink(Anchor("c1", "r1"))
        self.assertEqual(removed, "s-fork")
        self.assertIsNone(m.get_session(Anchor("c1", "r1")))
        self.assertIsNone(m.get_anchor("s-fork"))

    def test_unlink_missing_returns_none(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.unlink(Anchor("nope")))

    def test_link_overwrites_existing_session_for_same_anchor(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1"), "s2")
        self.assertEqual(m.get_session(Anchor("c1")), "s2")
        # Old reverse entry is gone (no dangling session_to_anchor row).
        self.assertIsNone(m.get_anchor("s1"))
        self.assertEqual(m.get_anchor("s2"), Anchor("c1"))

    def test_persistence_survives_reload(self) -> None:
        m1 = ChannelMapping.load(self.state, self.sdir)
        m1.link(Anchor("c1"), "s1")
        m1.link(Anchor("c1", "r1"), "s-fork")

        m2 = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m2.get_session(Anchor("c1")), "s1")
        self.assertEqual(m2.get_session(Anchor("c1", "r1")), "s-fork")


class ChannelMappingMigrationTests(unittest.TestCase):
    """Legacy v2 JSON state files are transparently upgraded to v3 on load."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_v2(self, data: dict) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps(data))

    def test_reads_legacy_v2_channel_only(self) -> None:
        self._write_v2({"channel_to_session": {"c1": "s1"}})
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_anchor("s1"), Anchor("c1"))

    def test_reads_legacy_v2_with_thread_mapping(self) -> None:
        self._write_v2({
            "channel_to_session": {"c1": "s1"},
            "thread_mapping": {"c1:r1": "s-fork", "c2:r2": "s-fork-2"},
        })
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")
        self.assertEqual(m.get_session(Anchor("c1", "r1")), "s-fork")
        self.assertEqual(m.get_session(Anchor("c2", "r2")), "s-fork-2")
        self.assertEqual(m.get_anchor("s-fork"), Anchor("c1", "r1"))

    def test_save_emits_current_schema(self) -> None:
        from mm_bridge.config import STATE_SCHEMA_VERSION
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "s1")
        m.link(Anchor("c1", "r1"), "s-fork")

        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("version"), STATE_SCHEMA_VERSION)
        self.assertIn("entries", data)
        entries = data["entries"]
        self.assertIsInstance(entries, list)
        # Each entry is a dict with channel_id, root_id, session_id.
        by_session = {e["session_id"]: e for e in entries}
        self.assertEqual(by_session["s1"]["channel_id"], "c1")
        self.assertIsNone(by_session["s1"].get("root_id"))
        self.assertEqual(by_session["s-fork"]["channel_id"], "c1")
        self.assertEqual(by_session["s-fork"]["root_id"], "r1")

    def test_legacy_v2_file_is_rewritten_as_current_schema_on_first_save(self) -> None:
        from mm_bridge.config import STATE_SCHEMA_VERSION
        self._write_v2({
            "channel_to_session": {"c1": "s1"},
            "thread_mapping": {"c1:r1": "s-fork"},
        })
        m = ChannelMapping.load(self.state, self.sdir)
        # Trigger a save (link is a no-op write if nothing changes, so just
        # re-link the same pair — save() is called unconditionally).
        m.link(Anchor("c1"), "s1")
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("version"), STATE_SCHEMA_VERSION)
        self.assertNotIn("channel_to_session", data)
        self.assertNotIn("thread_mapping", data)


class LastEventSeqTests(unittest.TestCase):
    """``last_event_seq`` is the SSE cursor checkpoint for restart safety."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_none_for_fresh_file(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.last_event_seq)

    def test_save_emits_last_event_seq_field(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.set_event_seq(42)
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data.get("last_event_seq"), 42)

    def test_set_event_seq_is_monotonic(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.set_event_seq(100)
        m.set_event_seq(50)  # stale event on reconnect — must not rewind
        self.assertEqual(m.last_event_seq, 100)

    def test_v3_state_loads_with_seq_none(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 3,
            "entries": [
                {"channel_id": "c1", "root_id": None, "session_id": "s1"},
            ],
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(m.last_event_seq)
        self.assertEqual(m.get_session(Anchor("c1")), "s1")

    def test_v4_state_roundtrips_seq(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 4,
            "entries": [],
            "last_event_seq": 7259,
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.last_event_seq, 7259)


class AdoptedSessionIdsTests(unittest.TestCase):
    """``adopted_session_ids`` is the persisted set of external session
    ids whose channel mapping was replaced by ``_replace_external_session``.
    The bootstrap must consult it before auto-spawning recovery channels."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = Path(self.tmp.name) / "sidecar"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_empty_on_fresh_load(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.adopted_session_ids, set())

    def test_mark_adopted_persists(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.mark_adopted("claude_aa4bf742")
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(
            data.get("adopted_session_ids"), ["claude_aa4bf742"],
        )

    def test_mark_adopted_is_idempotent(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.mark_adopted("claude_abc")
        m.mark_adopted("claude_abc")
        self.assertEqual(m.adopted_session_ids, {"claude_abc"})

    def test_v4_state_loads_with_empty_adopted_set(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 4,
            "entries": [],
            "last_event_seq": 100,
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.adopted_session_ids, set())

    def test_v5_state_roundtrips_adopted_set(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 5,
            "entries": [],
            "last_event_seq": None,
            "adopted_session_ids": ["claude_x", "claude_y"],
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.adopted_session_ids, {"claude_x", "claude_y"})
        # Round-trip back to disk.
        m.save()
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(
            sorted(data["adopted_session_ids"]), ["claude_x", "claude_y"],
        )


class SessionStateSchemaTests(unittest.TestCase):
    """v5 → v6: one small `state` object per entry (F2).

    Colocated with the mapping rather than kept in a sibling file because a
    state's lifetime IS its session's: `unlink()` and session replacement
    must drop it, and colocation gets that for free.

    Spec: specs/20260901-turn-state-fleet/design.md §2.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = f"{self.tmp.name}/sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _mapping(self) -> ChannelMapping:
        return ChannelMapping.load(self.state, self.sdir)

    def test_state_round_trips_through_disk(self) -> None:
        m = self._mapping()
        m.link(Anchor("c1"), "ses_a")
        m.set_state("ses_a", SessionState("awaiting", on="lead",
                                          note="M0 gate", set_at=1756704000.0))
        reloaded = ChannelMapping.load(self.state, self.sdir)
        got = reloaded.get_state("ses_a")
        assert got is not None
        self.assertEqual(got.kind, "awaiting")
        self.assertEqual(got.on, "lead")
        self.assertEqual(got.note, "M0 gate")
        self.assertEqual(got.set_at, 1756704000.0)

    def test_saved_file_declares_version_6(self) -> None:
        m = self._mapping()
        m.link(Anchor("c1"), "ses_a")
        data = json.loads(Path(self.state).read_text())
        self.assertEqual(data["version"], 6)

    def test_v5_file_loads_with_no_states(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 5,
            "entries": [{"channel_id": "c1", "root_id": None,
                         "session_id": "ses_a"}],
            "last_event_seq": 7,
            "adopted_session_ids": [],
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "ses_a")
        self.assertIsNone(m.get_state("ses_a"))

    def test_corrupt_state_object_costs_the_state_not_the_entry(self) -> None:
        Path(self.state).parent.mkdir(parents=True, exist_ok=True)
        Path(self.state).write_text(json.dumps({
            "version": 6,
            "entries": [{"channel_id": "c1", "root_id": None,
                         "session_id": "ses_a", "state": {"kind": "nonsense"}}],
        }))
        m = ChannelMapping.load(self.state, self.sdir)
        self.assertEqual(m.get_session(Anchor("c1")), "ses_a")
        self.assertIsNone(m.get_state("ses_a"))

    def test_unlink_drops_the_state_with_the_entry(self) -> None:
        m = self._mapping()
        m.link(Anchor("c1"), "ses_a")
        m.set_state("ses_a", SessionState("awaiting"))
        m.unlink(Anchor("c1"))
        self.assertIsNone(m.get_state("ses_a"))
        reloaded = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(reloaded.get_state("ses_a"))

    def test_replacing_an_anchors_session_drops_the_old_state(self) -> None:
        """`.model` / `.backend` swaps relink the anchor to a new session."""
        m = self._mapping()
        m.link(Anchor("c1"), "ses_old")
        m.set_state("ses_old", SessionState("awaiting"))
        m.link(Anchor("c1"), "ses_new")
        self.assertIsNone(m.get_state("ses_old"))

    def test_set_state_none_clears(self) -> None:
        m = self._mapping()
        m.link(Anchor("c1"), "ses_a")
        m.set_state("ses_a", SessionState("awaiting"))
        m.set_state("ses_a", None)
        self.assertIsNone(m.get_state("ses_a"))

    def test_state_for_an_unmapped_session_is_not_persisted(self) -> None:
        """States ride entries; a session with no anchor has no row to ride."""
        m = self._mapping()
        m.set_state("ses_ghost", SessionState("awaiting"))
        reloaded = ChannelMapping.load(self.state, self.sdir)
        self.assertIsNone(reloaded.get_state("ses_ghost"))


class AtomicSaveTests(unittest.TestCase):
    """`save()` is atomic — the CLI (`mm-bridge fleet`) now reads this file
    out of process while the daemon writes it, so a torn read must be
    impossible. Mirrors `HeldPostStore._save`.

    Lead condition on the v6 ruling: shared-path code gets its own reds.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = f"{self.tmp.name}/state.json"
        self.sdir = f"{self.tmp.name}/sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_lands_through_os_replace(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        with patch("mm_bridge.config.os.replace", wraps=os.replace) as repl:
            m.link(Anchor("c1"), "ses_a")
        self.assertTrue(repl.called, "save() must publish via os.replace")
        self.assertEqual(json.loads(Path(self.state).read_text())["version"], 6)

    def test_a_failed_write_leaves_no_temp_file_behind(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "ses_a")
        before = set(Path(self.tmp.name).iterdir())
        with patch("mm_bridge.config.os.replace",
                   side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.save()
        self.assertEqual(set(Path(self.tmp.name).iterdir()), before)

    def test_a_failed_write_leaves_the_previous_file_intact(self) -> None:
        m = ChannelMapping.load(self.state, self.sdir)
        m.link(Anchor("c1"), "ses_a")
        good = Path(self.state).read_text()
        # Mutate in memory only — `link()` would save successfully first.
        m.anchor_to_session[Anchor("c2")] = "ses_b"
        with patch("mm_bridge.config.os.replace",
                   side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.save()
        self.assertEqual(Path(self.state).read_text(), good)


if __name__ == "__main__":
    unittest.main()
