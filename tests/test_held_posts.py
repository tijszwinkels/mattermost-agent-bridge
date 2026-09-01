"""Unit tests for the hold buffer + its on-disk persistence.

Spec: specs/20260901-hold-and-coalesce/{requirements,design}.md §5 / §1.
The store owns ONLY the buffer and the file — rendering, attachment
download and ``create_run`` live in the Bridge, so everything here runs
without Mattermost, without the harness, and without asyncio.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from mm_bridge.config import Anchor
from mm_bridge.held_posts import (
    HELD_POSTS_SCHEMA_VERSION,
    HeldPost,
    HeldPostStore,
)


def make_post(post_id: str, *, message: str = "hi", user_id: str = "u1",
              create_at: int = 1_788_000_000_000, **extra) -> dict:
    return {
        "id": post_id,
        "channel_id": "c1",
        "user_id": user_id,
        "message": message,
        "create_at": create_at,
        **extra,
    }


def held(post_id: str, *, username: str = "tijs", **kw) -> HeldPost:
    post = make_post(post_id, **kw)
    return HeldPost(
        post=post, username=username, held_at_ms=post["create_at"],
    )


class HeldPostTest(unittest.TestCase):
    def test_accessors_read_through_to_the_post_dict(self):
        h = held("p1", message="  spaced  ", user_id="u9")
        self.assertEqual(h.post_id, "p1")
        self.assertEqual(h.user_id, "u9")
        self.assertEqual(h.message, "spaced")

    def test_timestamp_is_local_time_hh_mm(self):
        # Requirements §3.5: daemon-LOCAL time, not UTC. Building the epoch
        # from a local datetime and asserting the same wall clock back is
        # what pins that down.
        local = datetime(2026, 9, 1, 14, 7, 33)
        h = HeldPost(
            post=make_post("p1", create_at=int(local.timestamp() * 1000)),
            username="tijs",
            held_at_ms=int(local.timestamp() * 1000),
        )
        self.assertEqual(h.timestamp(), "14:07")

    def test_timestamp_tolerates_a_missing_create_at(self):
        h = HeldPost(post={"id": "p1"}, username="tijs", held_at_ms=0)
        self.assertEqual(h.timestamp(), "??:??")

    def test_render_includes_attachment_notes_above_the_message(self):
        h = held("p1", message="see attached")
        line = h.render(["[User attached file: /tmp/x/notes.txt]"])
        self.assertIn("notes.txt", line)
        self.assertLess(line.index("notes.txt"), line.index("see attached"))

    def test_render_of_an_empty_post_is_empty(self):
        # A post that was only a bot mention renders to nothing, so the
        # flush can skip it rather than send a stamped blank line.
        self.assertEqual(held("p1", message="").render(), "")

    def test_summary_is_the_first_line_only(self):
        h = held("p1", message="first\nsecond")
        self.assertTrue(h.summary().endswith("first…"))
        self.assertNotIn("second", h.summary())

    def test_summary_of_a_single_line_post_has_no_ellipsis(self):
        self.assertFalse(held("p1", message="just one").summary().endswith("…"))

    def test_json_round_trip(self):
        h = held("p1", message="hello", username="bittern")
        again = HeldPost.from_json(json.loads(json.dumps(h.to_json())))
        self.assertEqual(again, h)

    def test_from_json_rejects_an_entry_with_no_post_id(self):
        self.assertIsNone(
            HeldPost.from_json({"post": {"message": "x"}, "username": "u"}),
        )


class HeldPostStoreBufferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "held_posts.json"
        self.store = HeldPostStore(self.path, cap=50)
        self.anchor = Anchor("c1")

    def test_add_then_peek_preserves_arrival_order(self):
        for pid in ("p1", "p2", "p3"):
            self.assertTrue(self.store.add(self.anchor, held(pid)))
        self.assertEqual(
            [h.post_id for h in self.store.peek(self.anchor)],
            ["p1", "p2", "p3"],
        )
        self.assertEqual(len(self.store), 3)

    def test_peek_of_an_unknown_anchor_is_empty(self):
        self.assertEqual(self.store.peek(Anchor("nope")), [])

    def test_duplicate_post_id_is_accepted_but_not_appended_twice(self):
        # R2 §2.4 — the warming-queue replay path re-dispatches post dicts.
        # A duplicate must report "held" (so the caller does NOT eagerly
        # submit it) while leaving the buffer alone.
        self.assertTrue(self.store.add(self.anchor, held("p1")))
        self.assertTrue(self.store.add(self.anchor, held("p1")))
        self.assertEqual(len(self.store.peek(self.anchor)), 1)

    def test_add_at_cap_is_refused(self):
        store = HeldPostStore(self.path, cap=2)
        self.assertTrue(store.add(self.anchor, held("p1")))
        self.assertTrue(store.add(self.anchor, held("p2")))
        self.assertFalse(store.add(self.anchor, held("p3")))
        self.assertEqual(
            [h.post_id for h in store.peek(self.anchor)], ["p1", "p2"],
        )

    def test_cap_is_per_anchor_not_global(self):
        store = HeldPostStore(self.path, cap=1)
        self.assertTrue(store.add(Anchor("c1"), held("p1")))
        self.assertTrue(store.add(Anchor("c2"), held("p2")))
        self.assertFalse(store.add(Anchor("c1"), held("p3")))

    def test_non_positive_cap_disables_holding(self):
        store = HeldPostStore(self.path, cap=0)
        self.assertFalse(store.add(self.anchor, held("p1")))
        self.assertEqual(len(store), 0)

    def test_discard_removes_only_the_named_ids(self):
        for pid in ("p1", "p2", "p3"):
            self.store.add(self.anchor, held(pid))
        self.store.discard(self.anchor, {"p1", "p3"})
        self.assertEqual(
            [h.post_id for h in self.store.peek(self.anchor)], ["p2"],
        )

    def test_discarding_every_id_drops_the_anchor_entirely(self):
        self.store.add(self.anchor, held("p1"))
        self.store.discard(self.anchor, {"p1"})
        self.assertEqual(self.store.anchors(), [])

    def test_discard_with_no_ids_is_a_no_op(self):
        self.store.add(self.anchor, held("p1"))
        self.store.discard(self.anchor, set())
        self.assertEqual(len(self.store.peek(self.anchor)), 1)

    def test_clear_returns_what_it_dropped(self):
        self.store.add(self.anchor, held("p1"))
        self.store.add(self.anchor, held("p2"))
        dropped = self.store.clear(self.anchor)
        self.assertEqual([h.post_id for h in dropped], ["p1", "p2"])
        self.assertEqual(self.store.peek(self.anchor), [])

    def test_forget_channel_drops_the_channel_and_its_threads(self):
        self.store.add(Anchor("c1"), held("p1"))
        self.store.add(Anchor("c1", "root1"), held("p2"))
        self.store.add(Anchor("c2"), held("p3"))
        self.store.forget_channel("c1")
        self.assertEqual(self.store.anchors(), [Anchor("c2")])

    def test_forget_anchor_drops_only_that_thread(self):
        self.store.add(Anchor("c1"), held("p1"))
        self.store.add(Anchor("c1", "root1"), held("p2"))
        self.store.forget_anchor(Anchor("c1", "root1"))
        self.assertEqual(self.store.anchors(), [Anchor("c1")])

    def test_thread_anchors_are_kept_apart_from_the_channel(self):
        # R2 — a thread fork holds independently of its parent channel.
        self.store.add(Anchor("c1"), held("p1"))
        self.store.add(Anchor("c1", "root1"), held("p2"))
        self.assertEqual(
            [h.post_id for h in self.store.peek(Anchor("c1"))], ["p1"],
        )
        self.assertEqual(
            [h.post_id for h in self.store.peek(Anchor("c1", "root1"))], ["p2"],
        )


class HeldPostStorePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "held_posts.json"

    def reload(self) -> HeldPostStore:
        store = HeldPostStore(self.path, cap=50)
        store.load()
        return store

    def test_holds_survive_a_restart(self):
        # R4 — the whole point: a daemon restart must not lose held posts.
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1"), held("p1", message="first"))
        store.add(Anchor("c1", "root1"), held("p2", message="threaded"))

        restored = self.reload()
        self.assertEqual(
            [h.post_id for h in restored.peek(Anchor("c1"))], ["p1"],
        )
        self.assertEqual(
            restored.peek(Anchor("c1", "root1"))[0].message, "threaded",
        )

    def test_session_id_is_recorded_per_anchor(self):
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1"), held("p1"), session_id="ses_abc")
        self.assertEqual(self.reload().session_id(Anchor("c1")), "ses_abc")

    def test_discard_and_clear_are_persisted(self):
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1"), held("p1"))
        store.add(Anchor("c1"), held("p2"))
        store.discard(Anchor("c1"), {"p1"})
        self.assertEqual(
            [h.post_id for h in self.reload().peek(Anchor("c1"))], ["p2"],
        )
        store.clear(Anchor("c1"))
        self.assertEqual(self.reload().anchors(), [])

    def test_written_file_matches_the_documented_schema(self):
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1", "root1"), held("p1"), session_id="ses_abc")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], HELD_POSTS_SCHEMA_VERSION)
        entry = data["anchors"][0]
        self.assertEqual(entry["channel_id"], "c1")
        self.assertEqual(entry["root_id"], "root1")
        self.assertEqual(entry["session_id"], "ses_abc")
        self.assertEqual(entry["posts"][0]["username"], "tijs")
        self.assertEqual(entry["posts"][0]["post"]["id"], "p1")

    def test_save_leaves_no_temp_file_behind(self):
        # Atomicity is tmp + os.replace; a leftover .tmp would mean the
        # rename never happened and a reader could see a partial file.
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1"), held("p1"))
        siblings = {p.name for p in self.path.parent.iterdir()}
        self.assertEqual(siblings, {"held_posts.json"})

    def test_load_of_a_missing_file_is_empty(self):
        self.assertEqual(self.reload().anchors(), [])

    def test_load_tolerates_invalid_json(self):
        # A corrupt hold file must never stop the daemon from booting.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json")
        self.assertEqual(self.reload().anchors(), [])

    def test_load_tolerates_an_unknown_schema_version(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 99, "anchors": []}))
        self.assertEqual(self.reload().anchors(), [])

    def test_load_skips_malformed_entries_but_keeps_the_rest(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "version": HELD_POSTS_SCHEMA_VERSION,
            "anchors": [
                {"root_id": None, "posts": []},                 # no channel_id
                {"channel_id": "c2", "posts": [{"post": {}}]},  # no post id
                {"channel_id": "c3", "root_id": None, "posts": [
                    {"post": make_post("p9"), "username": "tijs",
                     "held_at_ms": 1},
                ]},
            ],
        }))
        restored = self.reload()
        self.assertEqual(restored.anchors(), [Anchor("c3")])
        self.assertEqual(restored.peek(Anchor("c3"))[0].post_id, "p9")

    def test_load_is_idempotent_and_replaces_in_memory_state(self):
        store = HeldPostStore(self.path, cap=50)
        store.add(Anchor("c1"), held("p1"))
        store.load()
        store.load()
        self.assertEqual(len(store.peek(Anchor("c1"))), 1)


if __name__ == "__main__":
    unittest.main()
