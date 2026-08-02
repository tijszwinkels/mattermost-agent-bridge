"""Unit tests for mm_bridge.commands — the pure dot-command parser/registry.

Mirrors the pure-unit-test style of tests/test_purpose.py: no I/O, no bridge.

Spec: implementation plan (ig ...135aeb87), "Parser rules".
"""

from __future__ import annotations

from mm_bridge.commands import (
    REGISTRY,
    CommandSpec,
    ParsedCommand,
    dormant_help_note,
    help_text,
    parse,
)


# ---------------------------------------------------------------------------
# Non-commands → None (forwarded to the agent)
# ---------------------------------------------------------------------------


def test_plain_message_is_not_a_command():
    assert parse("hello there") is None


def test_message_with_leading_dot_word_inside_is_not_a_command():
    # The dot must lead the (mention-stripped) message.
    assert parse("update the .gitignore please") is None


def test_bare_dot_is_not_a_command():
    assert parse(".") is None


def test_dot_space_is_not_a_command():
    assert parse(". hello") is None


def test_natural_language_stop_is_not_a_dot_command():
    # Existing natural-language commands keep flowing through untouched.
    assert parse("stop") is None
    assert parse("@claude catch up") is None
    assert parse("autorespond") is None


# ---------------------------------------------------------------------------
# Known commands
# ---------------------------------------------------------------------------


def test_known_command_parses():
    cmd = parse(".stop")
    assert isinstance(cmd, ParsedCommand)
    assert cmd.name == "stop"
    assert cmd.arg is None
    assert cmd.spec is REGISTRY["stop"]
    assert cmd.known is True


def test_command_is_case_insensitive():
    cmd = parse(".HELP")
    assert cmd.name == "help"
    assert cmd.spec is REGISTRY["help"]


def test_command_with_arg():
    cmd = parse(".autorespond on")
    assert cmd.name == "autorespond"
    assert cmd.arg == "on"


def test_command_arg_preserves_case_and_inner_spaces():
    cmd = parse(".status  Some Thing ")
    assert cmd.name == "status"
    assert cmd.arg == "Some Thing"


def test_trailing_whitespace_after_bare_command_yields_no_arg():
    cmd = parse(".stop   ")
    assert cmd.name == "stop"
    assert cmd.arg is None


# ---------------------------------------------------------------------------
# Unknown dot-words → intercepted (spec is None), never forwarded
# ---------------------------------------------------------------------------


def test_unknown_dot_word_is_a_command_with_no_spec():
    cmd = parse(".frobnicate now")
    assert isinstance(cmd, ParsedCommand)
    assert cmd.name == "frobnicate"
    assert cmd.arg == "now"
    assert cmd.spec is None
    assert cmd.known is False


# ---------------------------------------------------------------------------
# Mention stripping
# ---------------------------------------------------------------------------


def test_leading_claude_mention_is_stripped():
    cmd = parse("@claude .stop")
    assert cmd is not None
    assert cmd.name == "stop"


def test_leading_claude_mention_stripped_case_insensitively():
    cmd = parse("@Claude   .status")
    assert cmd is not None
    assert cmd.name == "status"


def test_configured_bot_mention_is_stripped():
    cmd = parse("@mybot .help", mentions=("mybot",))
    assert cmd is not None
    assert cmd.name == "help"


def test_foreign_mention_is_not_stripped_so_not_a_command():
    # A mention that isn't ours leaves the text starting with '@', not '.'.
    assert parse("@someoneelse .stop") is None


# ---------------------------------------------------------------------------
# Registry + help
# ---------------------------------------------------------------------------


def test_registry_entries_are_command_specs():
    assert REGISTRY
    for name, spec in REGISTRY.items():
        assert isinstance(spec, CommandSpec)
        assert spec.name == name
        assert spec.usage.startswith(".")
        assert spec.summary


def test_help_lists_every_registered_command():
    text = help_text()
    for spec in REGISTRY.values():
        assert spec.usage in text
        assert spec.summary in text


def test_pr1_commands_are_registered():
    for name in ("help", "stop", "autorespond", "status"):
        assert name in REGISTRY


def test_phase2_commands_are_registered():
    for name in ("model", "models", "running"):
        assert name in REGISTRY


def test_model_command_captures_free_text_arg():
    cmd = parse(".model claude-sonnet-thinking")
    assert cmd.name == "model"
    assert cmd.arg == "claude-sonnet-thinking"


def test_bare_model_has_no_arg():
    cmd = parse(".model")
    assert cmd.name == "model"
    assert cmd.arg is None


def test_backend_command_is_registered_and_parses():
    assert "backend" in REGISTRY
    cmd = parse(".backend codex")
    assert cmd.name == "backend"
    assert cmd.arg == "codex"


def test_bare_backend_has_no_arg():
    cmd = parse(".backend")
    assert cmd.name == "backend"
    assert cmd.arg is None


def test_cwd_command_is_registered_and_parses():
    assert "cwd" in REGISTRY
    cmd = parse(".cwd /home/me/projects/foo")
    assert cmd.name == "cwd"
    assert cmd.arg == "/home/me/projects/foo"


def test_bare_cwd_has_no_arg():
    cmd = parse(".cwd")
    assert cmd.name == "cwd"
    assert cmd.arg is None


def test_cwd_arg_preserves_case_and_tilde():
    # Paths are case-sensitive and `~` must survive to the handler, which
    # expands it (the Channel Purpose parser is strict and does not).
    cmd = parse(".cwd ~/Projects/MM-Bridge")
    assert cmd.arg == "~/Projects/MM-Bridge"


# ---------------------------------------------------------------------------
# Command capability metadata — the single source of truth the bridge's
# pre-session (dormant) gate reads. `global_scope` marks operator-wide
# commands that reveal/act on state spanning channels; those need an explicit
# @mention in a dormant channel. Everything else is channel-local and safe.
# ---------------------------------------------------------------------------


def test_global_scope_flags_only_operator_wide_commands():
    # These span all of the operator's sessions — privacy-sensitive.
    for name in ("sessions", "running", "invite"):
        assert REGISTRY[name].global_scope is True, name
    # These are channel-local: they only read/change THIS channel.
    for name in (
        "help", "status", "stop", "model", "backend", "cwd", "models", "autorespond",
    ):
        assert REGISTRY[name].global_scope is False, name


def test_cwd_is_session_scoped_but_channel_local():
    # `.cwd` acts on the channel's own session (a set recreates it), exactly
    # like `.model` / `.backend` — and never reveals cross-channel state.
    assert REGISTRY["cwd"].session_scoped is True
    assert REGISTRY["cwd"].global_scope is False


def test_status_and_stop_are_session_scoped_but_channel_local():
    # `.status`/`.stop` act on the channel's own session — never global.
    for name in ("status", "stop"):
        assert REGISTRY[name].session_scoped is True, name
        assert REGISTRY[name].global_scope is False, name


def test_dormant_help_note_is_registry_derived():
    note = dormant_help_note()
    # The privacy carve-out lists exactly the global-scope commands.
    for name in ("sessions", "running", "invite"):
        assert f"`.{name}`" in note, name
    # Channel-local commands (incl. the previously-broken `.status`) are
    # advertised as usable before the first session.
    assert "`.status`" in note
    assert "@claude" in note
