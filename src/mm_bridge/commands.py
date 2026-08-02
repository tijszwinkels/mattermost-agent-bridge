"""Dot-command parser + registry.

A *pure* module (no I/O, modelled on ``purpose.py``): it maps a Mattermost
message to a typed :class:`ParsedCommand`, and describes the known commands in
a registry the bridge dispatches from and ``.help`` renders.

Contract:
    * ``parse()`` returns ``None`` when the message is **not** a dot-command
      (after stripping an optional leading ``@claude`` / bot mention). The
      bridge forwards those to the agent as normal user turns.
    * ``parse()`` returns a :class:`ParsedCommand` when the (mention-stripped)
      message matches ``^\\.(\\w+)(\\s+(.*))?$`` case-insensitively. Known words
      carry their :class:`CommandSpec`; unknown dot-words carry ``spec=None``
      so the bridge can intercept them with an "unknown command — try `.help`"
      reply instead of forwarding them.

Execution lives in the bridge (it needs ``self.mm`` / ``self.harness`` /
``self.mapping``), exactly like the existing ``_run_stop_command`` handlers.

Spec: implementation plan (ig ...135aeb87), "Parser rules" / "Command semantics".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CommandSpec:
    """A known dot-command's metadata — the single source of truth the bridge
    reads to decide pre-session (dormant) behaviour, so a new command's
    capabilities alone determine how it's routed before a session exists.

    Two orthogonal flags drive that routing:

    * ``session_scoped`` — the command acts on the channel's own harness
      session. Before a session exists these still respond (``.status``
      reports the pre-session config; ``.stop`` says "no session"); they
      never create one.
    * ``global_scope`` — the command reveals or acts on operator-wide state
      spanning channels (``.sessions``/``.running``/``.invite``). In a
      *dormant* channel these are honored only with an explicit ``@mention``,
      so a bare dot-word in a shared room can't leak cross-channel state. In
      an active (mapped) channel they run normally.

    The two are independent: ``.status``/``.stop`` are ``session_scoped`` but
    channel-local (never ``global_scope``); ``.sessions`` is ``global_scope``
    but not tied to this channel's session.
    """

    name: str
    usage: str
    summary: str
    session_scoped: bool = False
    global_scope: bool = False


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed dot-command. ``spec is None`` marks an unknown dot-word."""

    name: str
    arg: str | None
    spec: CommandSpec | None

    @property
    def known(self) -> bool:
        return self.spec is not None


# Ordered registry — insertion order drives ``.help`` output. Phase 2/3
# commands (`.model`, `.models`, `.running`, `.sessions`, `.invite`) are
# appended in the follow-up PR.
_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("help", ".help", "Show this list of commands."),
    CommandSpec(
        "stop", ".stop", "Interrupt the running turn in this channel.",
        session_scoped=True,
    ),
    CommandSpec(
        "autorespond", ".autorespond [on|off]",
        "Reply to every message, or only when @mentioned (bare = toggle).",
    ),
    CommandSpec(
        "status", ".status", "Show the session, model and run status here.",
        session_scoped=True,
    ),
    CommandSpec(
        "model", ".model [<name>]",
        "Show/select the model (configures the next session if dormant).",
        session_scoped=True,
    ),
    CommandSpec(
        "backend", ".backend [<name>]",
        "Show/select the backend (resets the model; configures if dormant).",
        session_scoped=True,
    ),
    CommandSpec(
        "cwd", ".cwd [<path>]",
        "Show/set the working directory (configures the next session if dormant).",
        session_scoped=True,
    ),
    CommandSpec(
        "models", ".models",
        "List the available models for this channel's backend.",
    ),
    CommandSpec(
        "running", ".running",
        "List sessions with a run in flight right now.",
        global_scope=True,
    ),
    CommandSpec(
        "sessions", ".sessions [N]",
        "List recent sessions across all agents (incl. terminal ones).",
        global_scope=True,
    ),
    CommandSpec(
        "invite", ".invite <session-id>",
        "Get invited to a session's Mattermost channel (creating it if needed).",
        global_scope=True,
    ),
)

REGISTRY: dict[str, CommandSpec] = {s.name: s for s in _SPECS}


# ``^\.(\w+)`` — a leading dot immediately followed by a word. The optional
# ``(?:\s+(.*))?`` captures a trailing argument. Not DOTALL: an argument stays
# on one logical line, matching how these are typed.
_COMMAND_RE = re.compile(r"^\.(\w+)(?:\s+(.*\S))?\s*$", re.IGNORECASE)

# A leading ``@name`` mention we may strip. ``[\w.\-]+`` covers usernames with
# dots/hyphens as Mattermost allows.
_MENTION_RE = re.compile(r"^@([\w.\-]+)\s+(.*)$", re.DOTALL)


def _strip_leading_mention(message: str, mentions: Iterable[str]) -> str:
    """Drop a single leading ``@<ours>`` mention, else return text unchanged.

    ``@claude`` is always recognised (the canonical bot handle used across the
    codebase); any configured bot username is added on top. A leading mention
    that isn't ours is left in place — the message then won't start with ``.``
    and ``parse`` returns ``None`` (forwarded), which is the safe default.
    """
    text = message.strip()
    names = {"claude"}
    names.update(m.lstrip("@").lower() for m in mentions if m)
    m = _MENTION_RE.match(text)
    if m and m.group(1).lower() in names:
        return m.group(2).strip()
    return text


def parse(message: str, *, mentions: Iterable[str] = ()) -> ParsedCommand | None:
    """Parse ``message`` into a :class:`ParsedCommand`, or ``None``.

    ``mentions`` is an optional iterable of extra bot usernames (with or
    without a leading ``@``) whose leading mention should be stripped in
    addition to ``@claude``.
    """
    text = _strip_leading_mention(message or "", mentions)
    m = _COMMAND_RE.match(text)
    if not m:
        return None
    name = m.group(1).lower()
    arg = m.group(2)
    arg = arg.strip() if arg else None
    return ParsedCommand(name=name, arg=arg or None, spec=REGISTRY.get(name))


def help_text() -> str:
    """Render the registry as a Mattermost-friendly command list."""
    lines = ["**Commands** — type these in-channel:"]
    for spec in REGISTRY.values():
        lines.append(f"• `{spec.usage}` — {spec.summary}")
    return "\n".join(lines)


def dormant_help_note() -> str:
    """Explain pre-session command availability, derived from the registry.

    Channel-local commands work without a mention before the first session;
    ``global_scope`` (operator-wide) commands require an explicit ``@claude``
    so a bare dot-word in a shared room can't leak cross-channel state. Both
    lists come straight from :data:`REGISTRY` so a new command's ``global_scope``
    flag is the only thing that decides where it appears.
    """
    channel_local = [
        f"`.{s.name}`" for s in REGISTRY.values() if not s.global_scope
    ]
    operator_wide = [
        f"`.{s.name}`" for s in REGISTRY.values() if s.global_scope
    ]
    note = (
        "_Before the first session, channel commands ("
        + ", ".join(channel_local)
        + ") work without a mention."
    )
    if operator_wide:
        note += (
            " For privacy, " + ", ".join(operator_wide) + " require `@claude`."
        )
    return note + "_"
