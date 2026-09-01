"""Turn-end session state — the pure model, and the fleet renderer.

`session_state.py` is deliberately I/O-free (modelled on `purpose.py` /
`commands.py`): the bridge, the CLI and the persistence layer all read it,
and three of those live outside `bridge.py`. Keeping the rendering rule in
one place is what lets `.fleet` and `mm-bridge fleet` be asserted once.

Spec: specs/20260901-turn-state-fleet/design.md §1 and §5.3.
"""

from __future__ import annotations

import unittest

from mm_bridge.session_state import (
    KINDS,
    FleetRow,
    SessionState,
    humanise_age,
    render_fleet,
)


class SessionStateModelTests(unittest.TestCase):
    def test_the_four_kinds(self) -> None:
        self.assertEqual(KINDS, ("idle", "awaiting", "parked", "blocked"))

    def test_describe_bare_kind(self) -> None:
        self.assertEqual(SessionState("idle").describe(), "idle")

    def test_describe_names_who_is_waited_on(self) -> None:
        self.assertEqual(
            SessionState("awaiting", on="lead").describe(), "awaiting → lead",
        )

    def test_note_is_not_part_of_the_state_column(self) -> None:
        """One rule: the note always lives in its own column."""
        self.assertEqual(
            SessionState("blocked", note="quota").describe(), "blocked",
        )

    def test_a_bridge_classified_block_names_its_class(self) -> None:
        """F3 contract: `note` starts with the failure class, and the state
        column surfaces it so a lead sees WHY without reading the note."""
        s = SessionState("blocked", note="quota_exhausted (anthropic, HTTP 529)",
                         source="bridge")
        self.assertEqual(s.describe(), "blocked (quota_exhausted)")

    def test_an_agent_declared_block_stays_bare(self) -> None:
        """An agent's prose note is not a class name, so it is not promoted."""
        s = SessionState("blocked", note="waiting on the DB migration",
                         source="agent")
        self.assertEqual(s.describe(), "blocked")

    def test_a_bridge_block_with_no_note_stays_bare(self) -> None:
        self.assertEqual(
            SessionState("blocked", source="bridge").describe(), "blocked",
        )

    def test_round_trip_through_json(self) -> None:
        s = SessionState("awaiting", on="lead", note="M0 gate",
                         set_at=1756704000.0, source="agent")
        self.assertEqual(SessionState.from_json(s.to_json()), s)

    def test_from_json_rejects_junk_rather_than_raising(self) -> None:
        """A corrupt state must cost its own value, never the whole entry."""
        for junk in (None, [], "awaiting", {}, {"kind": "nonsense"},
                     {"kind": 7}, {"kind": "idle", "set_at": "soon"}):
            with self.subTest(junk=junk):
                self.assertIsNone(SessionState.from_json(junk))

    def test_from_json_tolerates_missing_optionals(self) -> None:
        s = SessionState.from_json({"kind": "parked"})
        assert s is not None
        self.assertEqual(s.kind, "parked")
        self.assertIsNone(s.on)
        self.assertEqual(s.source, "agent")


class AgeRenderingTests(unittest.TestCase):
    def test_seconds_and_minutes(self) -> None:
        self.assertEqual(humanise_age(0), "just now")
        self.assertEqual(humanise_age(59), "just now")
        self.assertEqual(humanise_age(60), "1 min")
        self.assertEqual(humanise_age(47 * 60), "47 min")

    def test_hours_carry_minutes(self) -> None:
        self.assertEqual(humanise_age(3 * 3600 + 12 * 60), "3h 12m")

    def test_negative_age_is_clamped(self) -> None:
        """A clock step backwards must not render `-3h`."""
        self.assertEqual(humanise_age(-500), "just now")


class FleetRenderTests(unittest.TestCase):
    def _rows(self) -> list[FleetRow]:
        return [
            FleetRow(
                title="kestrel",
                state=SessionState("awaiting", on="lead", note="C5 gate"),
                age_s=47 * 60, run_live=False, held=0,
            ),
            FleetRow(
                title="grebe", state=SessionState("idle"),
                age_s=12 * 60, run_live=True, held=2,
            ),
            FleetRow(
                title="wagtail", state=SessionState("blocked", note="quota"),
                age_s=3 * 3600 + 12 * 60, run_live=False, held=1,
            ),
        ]

    def test_renders_one_line_per_row_in_a_fence(self) -> None:
        out = render_fleet(self._rows())
        self.assertTrue(out.startswith("```"))
        self.assertTrue(out.rstrip().endswith("```"))
        body = [ln for ln in out.splitlines() if ln and not ln.startswith("```")]
        self.assertEqual(len(body), 3)

    def test_awaiting_row_shows_target_age_note_and_held(self) -> None:
        line = render_fleet(self._rows()).splitlines()[1]
        self.assertIn("kestrel", line)
        self.assertIn("awaiting → lead", line)
        self.assertIn("47 min", line)
        self.assertIn("note: C5 gate", line)
        self.assertIn("held: 0", line)

    def test_live_run_row_shows_working_and_the_run_age(self) -> None:
        line = render_fleet(self._rows()).splitlines()[2]
        self.assertIn("working", line)
        self.assertIn("run 12m", line)
        self.assertIn("held: 2", line)

    def test_failure_column_is_a_dash_until_f3(self) -> None:
        for line in render_fleet(self._rows()).splitlines()[1:-1]:
            self.assertIn("err: -", line)

    def test_uncertain_row_is_marked(self) -> None:
        rows = [FleetRow(title="pipit", state=SessionState("idle"),
                         age_s=10, run_live=False, held=0, uncertain=True)]
        self.assertIn("?", render_fleet(rows).splitlines()[1])

    def test_dormant_row_renders_a_dash_not_a_gap(self) -> None:
        rows = [FleetRow(title="empty-chan", state=None, age_s=None,
                         run_live=False, held=0)]
        line = render_fleet(rows).splitlines()[1]
        self.assertIn("empty-chan", line)
        self.assertIn("-", line)

    def test_columns_line_up(self) -> None:
        lines = render_fleet(self._rows()).splitlines()[1:-1]
        cols = [ln.index("held:") for ln in lines]
        self.assertEqual(len(set(cols)), 1, "held column must be aligned")

    def test_empty_fleet_says_so_without_a_fence(self) -> None:
        self.assertIn("no child channels", render_fleet([]).lower())


if __name__ == "__main__":
    unittest.main()
