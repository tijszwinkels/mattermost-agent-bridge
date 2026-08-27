"""Parser tests for ``mm_bridge.channel_ref`` — pure, no I/O.

The parser classifies whatever a human pastes into ``--channel`` /
``--thread``:

* ``^[a-z0-9]{26}$``            → channel/post id (pass through, no lookup)
* bare slug (no ``/``, not an id) → slug, resolved later vs the cfg team
* ``https://host/<team>/channels/<slug>`` → slug + team from the URL
* ``https://host/<team>/pl/<post_id>``    → permalink (post id + team + host)

Anything else is a parse error the CLI turns into a friendly exit-2.
"""

from __future__ import annotations

import unittest

from mm_bridge.channel_ref import ChannelRefError, parse_channel_ref


class IdRefTests(unittest.TestCase):
    def test_26_char_lowercase_alnum_is_an_id_ref(self) -> None:
        value = "aaaaaaaaaabbbbbbbbbbcccccc"
        self.assertEqual(len(value), 26)
        ref = parse_channel_ref(value)
        self.assertEqual(ref.kind, "id")
        self.assertEqual(ref.value, value)
        self.assertIsNone(ref.team)
        self.assertIsNone(ref.host)

    def test_id_like_strings_are_ids_not_slugs(self) -> None:
        for v in ("82d9f869040f7b92924e849e0f8e6207"[:26], "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p"[:26]):
            self.assertEqual(parse_channel_ref(v).kind, "id")


class SlugRefTests(unittest.TestCase):
    def test_bare_slug_is_a_slug_ref_with_no_team_or_host(self) -> None:
        ref = parse_channel_ref("general")
        self.assertEqual(ref.kind, "slug")
        self.assertEqual(ref.slug, "general")
        self.assertIsNone(ref.team)
        self.assertIsNone(ref.host)

    def test_bare_slug_with_dashes_and_underscores(self) -> None:
        ref = parse_channel_ref("my-channel_2")
        self.assertEqual(ref.kind, "slug")
        self.assertEqual(ref.slug, "my-channel_2")

    def test_channel_url_parses_team_slug_and_host(self) -> None:
        ref = parse_channel_ref(
            "https://mm.example/tinkertank/channels/general"
        )
        self.assertEqual(ref.kind, "slug")
        self.assertEqual(ref.team, "tinkertank")
        self.assertEqual(ref.slug, "general")
        self.assertEqual(ref.host, "mm.example")

    def test_channel_url_query_string_is_stripped(self) -> None:
        ref = parse_channel_ref(
            "https://mm.example/tinkertank/channels/general?jump=12"
        )
        self.assertEqual(ref.kind, "slug")
        self.assertEqual(ref.slug, "general")
        self.assertEqual(ref.team, "tinkertank")

    def test_channel_url_trailing_slash_is_ok(self) -> None:
        ref = parse_channel_ref("https://mm.example/t/channels/general/")
        self.assertEqual(ref.kind, "slug")
        self.assertEqual(ref.slug, "general")

    def test_channel_url_host_is_case_insensitive(self) -> None:
        ref = parse_channel_ref("https://MM.Example/t/channels/general")
        self.assertEqual(ref.host, "mm.example")

    def test_slug_that_looks_url_like_but_lacks_a_scheme_is_rejected(self):
        with self.assertRaises(ChannelRefError):
            parse_channel_ref("mm.example/tinkertank/channels/general")

    def test_non_http_scheme_is_rejected(self) -> None:
        with self.assertRaises(ChannelRefError):
            parse_channel_ref("ftp://mm.example/t/channels/general")


class PermalinkRefTests(unittest.TestCase):
    POST_ID = "aaaaaaaaaaaa1111111111111111"

    def test_permalink_parses_post_id_team_and_host(self) -> None:
        ref = parse_channel_ref(
            f"https://mm.example/tinkertank/pl/{self.POST_ID}"
        )
        self.assertEqual(ref.kind, "permalink")
        self.assertEqual(ref.post_id, self.POST_ID)
        self.assertEqual(ref.team, "tinkertank")
        self.assertEqual(ref.host, "mm.example")

    def test_permalink_host_is_lowercased(self) -> None:
        ref = parse_channel_ref(f"https://MM.Example/t/pl/{self.POST_ID}")
        self.assertEqual(ref.host, "mm.example")


class ParseErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.post_id = "b" * 10 + "c" * 10 + "d" * 6

    def _err(self, *args: object):
        return self.assertRaises(ChannelRefError)

    def test_empty_string_is_rejected(self):
        with self._err():
            parse_channel_ref("")

    def test_url_with_no_path_is_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/")

    def test_url_with_single_path_segment_is_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/tinkertank")

    def test_channel_url_missing_team_is_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/channels/general")

    def test_channel_url_too_deep_is_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/t/channels/a/b")

    def test_pl_segment_with_missing_post_id_is_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/t/pl/")

    def test_unrelated_path_segments_are_rejected(self):
        with self._err():
            parse_channel_ref("https://mm.example/t/other/general")

    def test_bare_string_with_slash_but_no_url_is_rejected(self):
        with self._err():
            parse_channel_ref("tinkertank/channels/general")


if __name__ == "__main__":
    unittest.main()
