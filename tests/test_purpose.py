"""Unit tests for mm_bridge.purpose.

Spec: specs/20260417-mattermost-bridge-v2/requirements.md §3
"""

from __future__ import annotations

import pytest

from mm_bridge.purpose import (
    KNOWN_BACKENDS,
    PurposeConfig,
    SECTION_SEPARATOR,
    canonical_backend,
    join_sections,
    parse,
    split_config_section,
    has_no_nag,
    to_purpose_string,
)


# ---------------------------------------------------------------------------
# canonical_backend
# ---------------------------------------------------------------------------


def test_canonical_backend_maps_wire_aliases():
    assert canonical_backend("claude-code") == "claude"
    assert canonical_backend("claudecode") == "claude"


def test_canonical_backend_lowercases_and_passes_through_known():
    assert canonical_backend("CODEX") == "codex"
    assert canonical_backend("claude") == "claude"


def test_canonical_backend_none_and_empty():
    assert canonical_backend(None) is None
    assert canonical_backend("") == ""


# A fixed model catalogue used by most tests — mirrors what VibeDeck would return.
_MODELS: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": ["gpt-5.4"],
    "pi": ["pi-v1"],
    "opencode": [],
}


def _models_for(backend: str) -> list[str]:
    return _MODELS[backend]


# ---------------------------------------------------------------------------
# Defaults / empty input
# ---------------------------------------------------------------------------


def test_empty_purpose_returns_defaults():
    cfg = parse("", "claude", "opus", _models_for)
    assert cfg == PurposeConfig(backend="claude", model="opus", mention_only=False, warnings=[])


def test_whitespace_only_purpose_returns_defaults():
    cfg = parse("   ,  ,\t, ", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.mention_only is False
    assert cfg.warnings == []


def test_none_default_model_stays_none_when_no_model_specified():
    cfg = parse("claude", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model is None
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Backend-only and backend+model
# ---------------------------------------------------------------------------


def test_backend_only_uses_default_model():
    cfg = parse("claude", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_backend_and_model():
    cfg = parse("claude, opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_case_insensitive():
    cfg = parse("Claude, Opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_codex_with_model():
    cfg = parse("codex, gpt-5.4", "claude", "opus", _models_for)
    assert cfg.backend == "codex"
    assert cfg.model == "gpt-5.4"
    assert cfg.warnings == []


def test_pi_backend_only_uses_default_model():
    cfg = parse("pi", "claude", "opus", _models_for)
    assert cfg.backend == "pi"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_all_known_backends_recognised():
    for backend in KNOWN_BACKENDS:
        cfg = parse(backend, "claude", None, _models_for)
        assert cfg.backend == backend
        assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Model-first (no backend token)
# ---------------------------------------------------------------------------


def test_model_only_uses_default_backend():
    cfg = parse("opus", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_model_only_case_insensitive():
    cfg = parse("OPUS", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Unknown tokens → warnings, fall back to defaults
# ---------------------------------------------------------------------------


def test_typo_first_token_warns_and_uses_defaults():
    cfg = parse("opusz", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert len(cfg.warnings) == 1
    assert "opusz" in cfg.warnings[0]


def test_unknown_first_token_falls_back_to_defaults():
    cfg = parse("nonsense, sonnet", "claude", "opus", _models_for)
    # First token doesn't resolve → default backend. Then "sonnet" *does* match
    # a claude model, so it should be set.
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert len(cfg.warnings) == 1
    assert "nonsense" in cfg.warnings[0]


def test_unknown_later_token_warns():
    cfg = parse("claude, bogus", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"  # default_model since nothing else set it
    assert len(cfg.warnings) == 1
    assert "bogus" in cfg.warnings[0]


# ---------------------------------------------------------------------------
# mention-only flag
# ---------------------------------------------------------------------------


def test_mention_only_flag():
    cfg = parse("claude, mention-only", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_backend_model_and_mention_only():
    cfg = parse("claude, sonnet, mention-only", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_mention_only_case_insensitive():
    cfg = parse("Claude, Sonnet, MENTION-ONLY", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.model == "sonnet"


# ---------------------------------------------------------------------------
# cwd= token
# ---------------------------------------------------------------------------


def test_cwd_defaults_to_none():
    cfg = parse("claude, opus", "claude", "opus", _models_for)
    assert cfg.cwd is None


def test_cwd_absolute_path():
    cfg = parse("claude, cwd=/home/claude/projects/foo", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.cwd == "/home/claude/projects/foo"
    assert cfg.warnings == []


def test_cwd_preserves_case():
    cfg = parse("cwd=/Users/alice/Foo", "claude", "opus", _models_for)
    assert cfg.cwd == "/Users/alice/Foo"
    assert cfg.warnings == []


def test_cwd_with_backend_model_and_mention_only():
    cfg = parse(
        "claude, sonnet, cwd=/home/claude/projects/foo, mention-only",
        "claude", "opus", _models_for,
    )
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert cfg.mention_only is True
    assert cfg.cwd == "/home/claude/projects/foo"
    assert cfg.warnings == []


def test_cwd_relative_path_warns():
    cfg = parse("claude, cwd=./foo", "claude", "opus", _models_for)
    assert cfg.cwd is None
    assert len(cfg.warnings) == 1
    assert "cwd" in cfg.warnings[0].lower()
    assert "./foo" in cfg.warnings[0]


def test_cwd_empty_value_warns():
    cfg = parse("claude, cwd=", "claude", "opus", _models_for)
    assert cfg.cwd is None
    assert len(cfg.warnings) == 1
    assert "cwd" in cfg.warnings[0].lower()


def test_cwd_prefix_case_insensitive():
    cfg = parse("claude, CWD=/home/claude/foo", "claude", "opus", _models_for)
    assert cfg.cwd == "/home/claude/foo"


def test_cwd_surrounding_whitespace_tolerated():
    cfg = parse("claude, cwd = /home/claude/foo  ", "claude", "opus", _models_for)
    assert cfg.cwd == "/home/claude/foo"


# ---------------------------------------------------------------------------
# Whitespace tolerance
# ---------------------------------------------------------------------------


def test_extra_whitespace_tolerated():
    cfg = parse("  CLAUDE ,  OPUS  ", "claude", "sonnet", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# Two model tokens — last wins, first warns
# ---------------------------------------------------------------------------


def test_two_model_tokens_last_wins_warn_on_first():
    cfg = parse("claude, opus, sonnet", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert len(cfg.warnings) == 1
    assert "opus" in cfg.warnings[0]


def test_three_model_tokens_warn_on_each_earlier():
    cfg = parse("claude, opus, sonnet, haiku", "claude", None, _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "haiku"
    # Two warnings: opus was displaced by sonnet, sonnet was displaced by haiku.
    assert len(cfg.warnings) == 2


# ---------------------------------------------------------------------------
# Robustness — never raises
# ---------------------------------------------------------------------------


def test_never_raises_when_models_callable_fails():
    """When the model-list callback raises, parsing still completes. Under
    US-5.3, a failing or empty catalog means "no model verification
    available" — the raw token is recorded verbatim and passed to
    ``POST /v1/sessions``, so a transient harness hiccup doesn't block the
    operator from naming the model they want."""
    def broken(backend: str) -> list[str]:
        raise RuntimeError("harness unreachable")

    cfg = parse("claude, opus", "claude", "sonnet", broken)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_never_raises_on_garbage_input():
    # Weird punctuation shouldn't explode the parser.
    cfg = parse(",,,,", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"


# ---------------------------------------------------------------------------
# autorespond / noautorespond tokens + default_autorespond
# ---------------------------------------------------------------------------


def test_noautorespond_is_synonym_for_mention_only():
    cfg = parse("claude, noautorespond", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_autorespond_token_turns_mention_only_off():
    cfg = parse("claude, mention-only, autorespond", "claude", "opus", _models_for)
    # Last-write wins within Step 2a — autorespond clears the flag.
    assert cfg.mention_only is False


def test_default_autorespond_false_means_mention_only_by_default():
    cfg = parse("", "claude", "opus", _models_for, default_autorespond=False)
    assert cfg.mention_only is True


def test_default_autorespond_false_still_overridable_by_autorespond_token():
    cfg = parse(
        "claude, autorespond", "claude", "opus", _models_for,
        default_autorespond=False,
    )
    assert cfg.mention_only is False


def test_default_autorespond_true_defaults_to_responding():
    cfg = parse("", "claude", "opus", _models_for, default_autorespond=True)
    assert cfg.mention_only is False


def test_autoresponse_spelling_alias():
    """Users naturally type `autoresponse` / `noautoresponse` (with `e`)."""
    cfg = parse("claude, autoresponse", "claude", "opus", _models_for)
    assert cfg.mention_only is False
    assert cfg.warnings == []


def test_noautoresponse_spelling_alias():
    cfg = parse("claude, noautoresponse", "claude", "opus", _models_for)
    assert cfg.mention_only is True
    assert cfg.warnings == []


def test_autorespond_case_insensitive():
    cfg = parse("claude, AUTORESPOND", "claude", "opus", _models_for,
                default_autorespond=False)
    assert cfg.mention_only is False


# ---------------------------------------------------------------------------
# to_purpose_string — canonical serialization for persistence
# ---------------------------------------------------------------------------


def test_to_purpose_string_always_emits_flag():
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=False)
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, autorespond"
    )


def test_to_purpose_string_emits_mention_only_when_set():
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=True)
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, mention-only"
    )


def test_to_purpose_string_flag_independent_of_default():
    """Same config, different default_autorespond → same output."""
    cfg = PurposeConfig(backend="claude", model="opus", mention_only=False)
    assert (
        to_purpose_string(cfg, default_autorespond=True)
        == to_purpose_string(cfg, default_autorespond=False)
    )


def test_to_purpose_string_with_cwd():
    cfg = PurposeConfig(
        backend="claude", model="opus", cwd="/home/claude/foo",
    )
    assert to_purpose_string(cfg, default_autorespond=True) == (
        "claude, opus, autorespond, cwd=/home/claude/foo"
    )


def test_to_purpose_string_drops_model_when_none():
    cfg = PurposeConfig(backend="claude", model=None)
    assert to_purpose_string(cfg, default_autorespond=True) == "claude, autorespond"


def test_to_purpose_string_parseable_again():
    """Round-trip: serialize → parse → same effective config."""
    models = {"claude": ["opus", "haiku"], "codex": [], "pi": [], "opencode": []}
    orig = PurposeConfig(
        backend="claude", model="haiku", mention_only=True, cwd="/home/foo",
    )
    s = to_purpose_string(orig, default_autorespond=True)
    reparsed = parse(
        s, "claude", "opus", lambda b: models.get(b, []),
        default_autorespond=True,
    )
    assert reparsed.backend == orig.backend
    assert reparsed.model == orig.model
    assert reparsed.mention_only == orig.mention_only
    assert reparsed.cwd == orig.cwd
    assert reparsed.warnings == []


# ---------------------------------------------------------------------------
# Section split / join (config vs. resume block)
# ---------------------------------------------------------------------------


def test_section_separator_is_three_dashes():
    assert SECTION_SEPARATOR == "---"


@pytest.mark.parametrize(
    ("text", "config", "rest"),
    [
        ("", "", ""),
        ("claude, opus", "claude, opus", ""),
        (
            "claude, opus\n---\nResume:\n```cd /p && claude --resume s1```",
            "claude, opus",
            "Resume:\n```cd /p && claude --resume s1```",
        ),
        # Separator alone at the top → empty config, full body in rest.
        ("---\nResume: ...", "", "Resume: ..."),
        # Multiple separators — only the first one splits.
        ("a\n---\nb\n---\nc", "a", "b\n---\nc"),
        # Lines with trailing whitespace around the separator are tolerated.
        ("config  \n  ---  \nresume", "config  ", "resume"),
        # A line containing dashes but not just `---` doesn't trigger a split.
        ("claude, opus\n--- not really ---\nstill config", "claude, opus\n--- not really ---\nstill config", ""),
    ],
)
def test_split_config_section_pulls_config_before_separator(
    text: str, config: str, rest: str,
) -> None:
    assert split_config_section(text) == (config, rest)


@pytest.mark.parametrize(
    ("config", "rest", "expected"),
    [
        ("", "", ""),
        ("claude, opus", "", "claude, opus"),
        ("", "Resume: …", "---\nResume: …"),
        ("claude, opus", "Resume: …", "claude, opus\n\n---\n\nResume: …"),
        # Strips leading/trailing whitespace per section.
        ("  claude  ", "  Resume  ", "claude\n\n---\n\nResume"),
    ],
)
def test_join_sections_canonical_layout(
    config: str, rest: str, expected: str,
) -> None:
    assert join_sections(config, rest) == expected


def test_section_split_join_roundtrip_preserves_payload():
    """`split → join → split` is a no-op on the canonical layout."""
    config, rest = "claude, opus, autorespond", "Resume:\n```cmd```"
    rebuilt = join_sections(config, rest)
    again = split_config_section(rebuilt)
    assert again == (config, rest)


def test_parse_ignores_resume_section():
    """Tokens after the separator must not produce warnings or override config."""
    text = (
        "claude, opus, mention-only\n"
        "---\n"
        "Resume:\n"
        "```\ncd /home/foo && claude --resume sess-123\n```\n"
    )
    cfg = parse(text, "claude", "opus", lambda b: ["opus", "haiku"])
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.mention_only is True
    assert cfg.warnings == []  # critical: no warnings from the resume body


# ---------------------------------------------------------------------------
# US-5.3: empty `available_models_for(...)` means "no catalog", not "no models"
# ---------------------------------------------------------------------------


def test_parse_passes_unknown_model_through_when_catalog_empty():
    """The live harness returns 200 ``{"data": []}`` for known backends until
    it has an authoritative model catalog (see US-5.3). The parser MUST
    record the raw token as the model and pass it verbatim to
    ``POST /v1/sessions`` so operators can use models the harness hasn't
    enumerated."""
    cfg = parse("claude, claude-opus-4-7", "claude", "opus", lambda _b: [])
    assert cfg.backend == "claude"
    assert cfg.model == "claude-opus-4-7"
    assert cfg.warnings == []


def test_parse_passes_unknown_model_through_with_known_default_backend():
    """Same as above but the user typed only a model token; we still
    canonicalise the default backend and pass the model through."""
    cfg = parse("custom-model-x", "claude", "opus", lambda _b: [])
    # When the catalog is empty for the default backend, a single
    # unrecognised token is treated as a model under the default backend.
    assert cfg.backend == "claude"
    assert cfg.model == "custom-model-x"
    assert cfg.warnings == []


def test_parse_accepts_claude_code_backend_alias():
    """US-5.2: the bridge accepts both ``claude`` (legacy purpose token)
    and ``claude-code`` (display/harness wire) at parse time and
    canonicalises internally."""
    cfg = parse("claude-code, opus", "claude", "opus", _models_for)
    assert cfg.backend == "claude"
    assert cfg.model == "opus"
    assert cfg.warnings == []


def test_parse_accepts_claude_code_with_space_alias():
    """The natural display string ``Claude Code`` (with space, as the
    product is branded) must canonicalise to the ``claude`` backend.
    Without this, the empty-catalog ``accept_unknown`` fallback silently
    treats ``claude code`` as a *model* under whatever the default
    backend is, with no warning — wrong backend, no operator signal."""
    cfg = parse(
        "Claude Code, sonnet", "codex", None, lambda _b: [],
    )
    assert cfg.backend == "claude"
    assert cfg.model == "sonnet"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# strict_catalog: with an empty catalog, don't silently accept plain words as
# model names. (A parser capability — the bridge no longer parses chat content
# as config, so nothing passes strict_catalog=True in production; these tests
# exercise the option directly. See purpose.parse's `strict_catalog` docstring.)
# ---------------------------------------------------------------------------


def test_strict_catalog_rejects_unknown_first_token_when_catalog_empty():
    """With ``strict_catalog=True`` and an empty catalog, an unknown first
    token like ``Hi!`` is NOT accepted as a model name — the "empty catalog →
    accept any token" fallback is disabled and the token bubbles out as a
    warning instead of silently overwriting the model."""
    cfg = parse(
        "Hi!", "claude", "opus", lambda _b: [], strict_catalog=True,
    )
    assert cfg.warnings, "expected an unknown-token warning under strict mode"
    # Model stays at the caller's default — no silent overwrite.
    assert cfg.model == "opus"


def test_strict_catalog_still_accepts_explicit_backend_alias():
    """Strict mode only tightens the catalog-empty fallback. Explicit
    backend keywords like ``claude`` still parse cleanly, so a genuine
    config string (``claude, autorespond``) is still recognised."""
    cfg = parse(
        "claude", "claude", "opus", lambda _b: [], strict_catalog=True,
    )
    assert cfg.backend == "claude"
    assert cfg.warnings == []


def test_strict_catalog_rejects_unknown_later_token_too():
    """A known backend followed by an unknown token should warn under
    strict mode even when the catalog is empty (no silent model
    assignment from an unrecognised token)."""
    cfg = parse(
        "claude, banana", "claude", "opus", lambda _b: [],
        strict_catalog=True,
    )
    assert any("banana" in w for w in cfg.warnings)


def test_strict_catalog_off_keeps_permissive_purpose_parsing():
    """Default (``strict_catalog=False``) keeps US-5.3 behavior so
    operators can set unenumerated models via the Channel Purpose
    field."""
    cfg = parse(
        "claude, claude-opus-4-7", "claude", "opus", lambda _b: [],
    )
    assert cfg.model == "claude-opus-4-7"
    assert cfg.warnings == []


# ---------------------------------------------------------------------------
# `no-nag` — opting a CHANNEL out of receiving awaiting-nags (F2 / C3)


def test_no_nag_token_is_parsed():
    cfg = parse("claude, autorespond, no-nag", "claude", None, _models_for)
    assert cfg.no_nag is True
    assert cfg.backend == "claude"
    assert cfg.warnings == []


def test_no_nag_spelling_variants():
    for token in ("no-nag", "nonag", "no_nag", "NO-NAG"):
        assert parse(f"claude, {token}", "claude", None, _models_for).no_nag is True


def test_absent_no_nag_defaults_to_false():
    assert parse("claude, autorespond", "claude", None, _models_for).no_nag is False
    assert parse("", "claude", None, _models_for).no_nag is False


def test_no_nag_alone_still_yields_the_default_backend():
    cfg = parse("no-nag", "claude", None, _models_for)
    assert cfg.no_nag is True
    assert cfg.backend == "claude"
    assert cfg.warnings == []


def test_no_nag_survives_a_round_trip():
    """A `.cwd` / `.autorespond` rewrite must never silently drop the opt-out."""
    cfg = parse("claude, mention-only, cwd=/tmp/x, no-nag", "claude", None, _models_for)
    again = parse(to_purpose_string(cfg, default_autorespond=True),
                  "claude", None, _models_for)
    assert again.no_nag is True
    assert again.cwd == "/tmp/x"
    assert again.mention_only is True


def test_has_no_nag_scans_raw_text_without_a_catalog():
    assert has_no_nag("claude, no-nag") is True
    assert has_no_nag("claude, autorespond") is False
    assert has_no_nag("") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
