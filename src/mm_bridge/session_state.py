"""Turn-end session state — the model, and the fleet renderer.

A bridged agent can end a reply with one directive:

    <state kind="awaiting" on="lead" note="M0 gate" />

and the bridge remembers that claim, per session, with the moment it was
made. That is the whole of the model. Everything downstream — the `.fleet`
view, `mm-bridge fleet`, the awaiting-nag — reads it.

Why this is a module and not four fields on the Bridge:

* **Three readers outside `bridge.py`.** The persistence layer (`config.py`),
  the CLI (`cli.py`) and the bridge all need the value and its rendering. A
  typed, I/O-free value keeps one rule in one place — the same reason
  `purpose.py` and `commands.py` are pure.
* **`render_fleet` is asserted once.** The dot-command and the CLI produce
  byte-identical output because they call the same function.

`set_at` is WALL CLOCK (epoch seconds), unlike every other elapsed-time
measurement in the bridge (`last_activity_ts`, `_held_probe_ts`), which use
`time.monotonic()` deliberately. It has to be: it is the one field that must
survive a daemon restart and be renderable by an out-of-process CLI. The cost
is that a large clock step can make one nag early or late; against a
30-minute threshold that is noise.

Spec: specs/20260901-turn-state-fleet/design.md §1 and §5.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The declarable kinds. `working` is deliberately NOT here: it is derived
# from the run tracker for display and never overwrites what an agent said
# about itself (design.md §4.1).
KINDS: tuple[str, ...] = ("idle", "awaiting", "parked", "blocked")

# The kind that an agent's silence means. A reply with no <state/> tag ends
# `idle`, which is exactly today's unmodelled behaviour made explicit — the
# zero-migration property of the whole feature.
DEFAULT_KIND = "idle"


@dataclass(frozen=True)
class SessionState:
    """One session's last claim about itself.

    Frozen so it is safe to hand to the renderer, the CLI and the persistence
    layer without any of them being able to mutate the bridge's copy.
    """

    kind: str
    on: str | None = None
    note: str | None = None
    set_at: float = 0.0
    source: str = "agent"  # "agent" | "bridge" (F3 sets `blocked`)

    def describe(self) -> str:
        """The state column: ``awaiting → lead``, ``idle``, ``blocked``.

        The note is deliberately NOT folded in here. One rule — the note
        always lives in its own column — beats a rule that depends on
        whether `on` happens to be set.
        """
        return f"{self.kind} → {self.on}" if self.on else self.kind

    def to_json(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "set_at": self.set_at,
                               "source": self.source}
        if self.on:
            out["on"] = self.on
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_json(cls, raw: Any) -> "SessionState | None":
        """Parse a persisted state, or return None on anything unexpected.

        Never raises: a corrupt state must cost its own value, never the
        channel↔session entry it rides on (design.md §2).
        """
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        if kind not in KINDS:
            logger.debug("Dropping session state with unknown kind %r", kind)
            return None
        set_at = raw.get("set_at", 0.0)
        if not isinstance(set_at, (int, float)) or isinstance(set_at, bool):
            logger.debug("Dropping session state with bad set_at %r", set_at)
            return None
        on = raw.get("on")
        note = raw.get("note")
        source = raw.get("source")
        return cls(
            kind=kind,
            on=on if isinstance(on, str) and on else None,
            note=note if isinstance(note, str) and note else None,
            set_at=float(set_at),
            source=source if isinstance(source, str) and source else "agent",
        )


def humanise_age(seconds: float | None) -> str:
    """``just now`` / ``47 min`` / ``3h 12m`` — the fleet's age column.

    Clamps negatives to "just now": a clock step backwards must render as
    uninformative, not as `-3h`.
    """
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def humanise_run(seconds: float | None) -> str:
    """``run 12m`` / ``run 1h 05m`` / bare ``run`` when the age is unknown."""
    if seconds is None:
        return "run"
    s = max(0, int(seconds))
    if s < 3600:
        return f"run {s // 60}m"
    return f"run {s // 3600}h {(s % 3600) // 60:02d}m"


@dataclass(frozen=True)
class FleetRow:
    """One rendered line: a child channel and what is true of it.

    Claims (`state`) sit next to observations (`run_live`, `held`) on
    purpose — a stale claim is then visible AS a stale claim, which is the
    only honest thing a view like this can offer.
    """

    title: str
    state: SessionState | None
    age_s: float | None
    run_live: bool
    held: int
    uncertain: bool = False   # the run probe fell back to the tracker
    failure: str = "-"        # F3 seam: last failure class

    @property
    def note_text(self) -> str:
        return (self.state.note or "") if self.state else ""


EMPTY_FLEET = (
    "_No child channels here — `mm-bridge spawn` sets the "
    "`Parent: ~this-channel~` header that this view reads._"
)


def render_fleet(rows: list[FleetRow]) -> str:
    """Render `rows` as an aligned, fenced table.

    Fenced because Mattermost renders a code block in a monospaced font on
    every client including mobile; without it the columns collapse.
    """
    if not rows:
        return EMPTY_FLEET

    cells: list[tuple[str, ...]] = []
    for r in rows:
        if r.state is None:
            state = "-"
        else:
            state = "working" if r.run_live else r.state.describe()
        if r.uncertain:
            state += " ?"
        when = humanise_run(r.age_s) if r.run_live else humanise_age(r.age_s)
        note = f"note: {r.note_text}" if r.note_text else ""
        cells.append((r.title, state, when, note,
                      f"held: {r.held}", f"err: {r.failure}"))

    widths = [max(len(c[i]) for c in cells) for i in range(len(cells[0]))]
    lines = [
        "   ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip()
        for row in cells
    ]
    return "```\n" + "\n".join(lines) + "\n```"
