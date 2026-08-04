"""Tests for `Config._apply_env` MM_URL parsing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mm_bridge.config import Config


class ApplyEnvMmUrlTests(unittest.TestCase):
    """MM_URL may be a bare hostname or a full URL; both must work."""

    def _apply(self, env: dict[str, str]) -> Config:
        cfg = Config()
        with patch.dict("os.environ", env, clear=True):
            cfg._apply_env()
        return cfg

    def test_bare_hostname_leaves_port_and_scheme_unchanged(self) -> None:
        cfg = self._apply({"MM_URL": "localhost"})
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 8065)  # default
        self.assertEqual(cfg.mm_scheme, "http")  # default

    def test_full_http_url_with_explicit_port(self) -> None:
        cfg = self._apply({"MM_URL": "http://localhost:8065"})
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 8065)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_full_https_url_without_port_defaults_to_443(self) -> None:
        cfg = self._apply({"MM_URL": "https://mm.example.com"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 443)
        self.assertEqual(cfg.mm_scheme, "https")

    def test_full_http_url_without_port_defaults_to_80(self) -> None:
        cfg = self._apply({"MM_URL": "http://mm.example.com"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 80)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_full_https_url_with_explicit_port(self) -> None:
        cfg = self._apply({"MM_URL": "https://mm.example.com:8443"})
        self.assertEqual(cfg.mm_url, "mm.example.com")
        self.assertEqual(cfg.mm_port, 8443)
        self.assertEqual(cfg.mm_scheme, "https")

    def test_explicit_mm_port_env_overrides_url_port(self) -> None:
        """Explicit MM_PORT wins even when MM_URL embeds a port."""
        cfg = self._apply({
            "MM_URL": "http://localhost:8065",
            "MM_PORT": "9000",
        })
        self.assertEqual(cfg.mm_url, "localhost")
        self.assertEqual(cfg.mm_port, 9000)
        self.assertEqual(cfg.mm_scheme, "http")

    def test_explicit_mm_scheme_env_overrides_url_scheme(self) -> None:
        cfg = self._apply({
            "MM_URL": "http://localhost:8065",
            "MM_SCHEME": "https",
        })
        self.assertEqual(cfg.mm_scheme, "https")


class AgentHarnessConfigTests(unittest.TestCase):
    """agent-harness backend URL and session-default config."""

    def _apply(self, env: dict[str, str]) -> Config:
        cfg = Config()
        with patch.dict("os.environ", env, clear=True):
            cfg._apply_env()
        return cfg

    def test_defaults_point_at_agent_harness(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.agent_harness_url, "http://localhost:8877")
        self.assertEqual(cfg.typing_stop_after_silence_seconds, 15.0)

    def test_toml_agent_harness_url_sets_field(self) -> None:
        cfg = Config()
        cfg._apply_toml({"agent_harness": {"url": "http://pillar:8877"}})
        self.assertEqual(cfg.agent_harness_url, "http://pillar:8877")

    def test_env_renamed_backend_knobs(self) -> None:
        cfg = self._apply({
            "AH_URL": "http://harness:8877",
            "MM_BRIDGE_DEFAULT_CWD": "/work",
            "MM_BRIDGE_DEFAULT_BACKEND": "codex",
            "MM_BRIDGE_DEFAULT_AUTORESPOND": "yes",
        })

        self.assertEqual(cfg.agent_harness_url, "http://harness:8877")
        self.assertEqual(cfg.default_cwd, "/work")
        self.assertEqual(cfg.default_backend, "codex")
        self.assertTrue(cfg.default_autorespond)

    def test_old_vd_env_and_toml_are_ignored(self) -> None:
        cfg = self._apply({
            "VD_URL": "http://vd.invalid",
            "VD_DEFAULT_CWD": "/old",
            "VD_DEFAULT_BACKEND": "pi",
            "VD_DEFAULT_MODEL": "old-model",
            "VD_DEFAULT_AUTORESPOND": "true",
        })
        cfg._apply_toml({"vibedeck": {"url": "http://vd.toml"}})

        self.assertEqual(cfg.agent_harness_url, "http://localhost:8877")
        self.assertNotEqual(cfg.default_cwd, "/old")
        self.assertEqual(cfg.default_backend, "claude")
        self.assertEqual(cfg.default_model_for("claude"), "opus")
        self.assertFalse(cfg.default_autorespond)


class DefaultModelsTests(unittest.TestCase):
    """Per-backend default model resolution.

    A single ``default_model`` scalar previously caused codex sessions to
    crash on startup (``codex exec --model opus`` exits 1). The bridge now
    keeps a per-backend table so each backend gets the right default.
    """

    def _apply(self, env: dict[str, str]) -> Config:
        cfg = Config()
        with patch.dict("os.environ", env, clear=True):
            cfg._apply_env()
        return cfg

    def test_built_in_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.default_model_for("claude"), "opus")
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_default_model_for_unknown_backend_is_none(self) -> None:
        cfg = Config()
        # No baked-in default for pi/opencode — harness picks.
        self.assertIsNone(cfg.default_model_for("pi"))
        self.assertIsNone(cfg.default_model_for("opencode"))
        self.assertIsNone(cfg.default_model_for(None))
        self.assertIsNone(cfg.default_model_for(""))

    def test_default_model_for_is_case_insensitive(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.default_model_for("Claude"), "opus")
        self.assertEqual(cfg.default_model_for("CODEX"), "gpt-5.5")

    def test_per_backend_env_overrides_built_in(self) -> None:
        cfg = self._apply({
            "MM_BRIDGE_DEFAULT_MODEL_CLAUDE": "sonnet",
            "MM_BRIDGE_DEFAULT_MODEL_CODEX": "gpt-5.4-mini",
        })
        self.assertEqual(cfg.default_model_for("claude"), "sonnet")
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.4-mini")

    def test_per_backend_env_can_add_new_backend(self) -> None:
        cfg = self._apply({"MM_BRIDGE_DEFAULT_MODEL_PI": "some-pi-model"})
        self.assertEqual(cfg.default_model_for("pi"), "some-pi-model")

    def test_per_backend_env_empty_string_unsets(self) -> None:
        """Empty value lets the harness pick rather than baking in a guess."""
        cfg = self._apply({"MM_BRIDGE_DEFAULT_MODEL_CLAUDE": ""})
        self.assertIsNone(cfg.default_model_for("claude"))
        # Other backends untouched.
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_legacy_env_applies_to_claude_only(self) -> None:
        cfg = self._apply({"MM_BRIDGE_DEFAULT_MODEL": "haiku"})
        self.assertEqual(cfg.default_model_for("claude"), "haiku")
        # Codex default must not be overwritten by the legacy scalar.
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_legacy_env_empty_string_unsets_claude_only(self) -> None:
        cfg = self._apply({"MM_BRIDGE_DEFAULT_MODEL": ""})
        self.assertIsNone(cfg.default_model_for("claude"))
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_per_backend_env_wins_over_legacy_for_claude(self) -> None:
        """Operator opt-in to per-backend env defaults must not be silently
        undone by an older legacy ``MM_BRIDGE_DEFAULT_MODEL`` scalar still
        sitting in the same environment."""
        cfg = self._apply({
            "MM_BRIDGE_DEFAULT_MODEL_CLAUDE": "sonnet",
            "MM_BRIDGE_DEFAULT_MODEL": "opus",
        })
        self.assertEqual(cfg.default_model_for("claude"), "sonnet")

    def test_toml_table_replaces_built_in(self) -> None:
        cfg = Config()
        cfg._apply_toml({
            "default_models": {"claude": "sonnet", "codex": "gpt-5.4"},
        })
        self.assertEqual(cfg.default_model_for("claude"), "sonnet")
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.4")

    def test_toml_partial_table_preserves_built_in_for_other_backends(self) -> None:
        """Operator who writes ``[default_models] claude = "sonnet"`` must
        not silently lose the codex=gpt-5.5 built-in — that's what kept
        codex sessions alive in the first place."""
        cfg = Config()
        cfg._apply_toml({"default_models": {"claude": "sonnet"}})
        self.assertEqual(cfg.default_model_for("claude"), "sonnet")
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_toml_legacy_scalar_applies_to_claude_only(self) -> None:
        cfg = Config()
        cfg._apply_toml({"default_model": "haiku"})
        self.assertEqual(cfg.default_model_for("claude"), "haiku")
        # Codex default must not be overwritten by the legacy scalar.
        self.assertEqual(cfg.default_model_for("codex"), "gpt-5.5")

    def test_toml_per_backend_table_wins_over_legacy_scalar(self) -> None:
        """If a TOML file carries both the deprecated scalar and the new
        per-backend table, the table (operator's explicit migration) wins."""
        cfg = Config()
        cfg._apply_toml({
            "default_model": "opus",
            "default_models": {"claude": "sonnet"},
        })
        self.assertEqual(cfg.default_model_for("claude"), "sonnet")


class ModelCatalogTests(unittest.TestCase):
    """`[models]` TOML table — operator-maintained per-backend model lists
    surfaced by the `.models` command (the harness catalog is empty today)."""

    def test_no_models_table_yields_empty_list(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.configured_models_for("claude"), [])
        self.assertEqual(cfg.configured_models_for("codex"), [])

    def test_toml_models_table_populates_catalog(self) -> None:
        cfg = Config()
        cfg._apply_toml({
            "models": {
                "claude": ["opus", "sonnet", "haiku"],
                "codex": ["gpt-5.5", "gpt-5.4-mini"],
            },
        })
        self.assertEqual(
            cfg.configured_models_for("claude"), ["opus", "sonnet", "haiku"],
        )
        self.assertEqual(
            cfg.configured_models_for("codex"), ["gpt-5.5", "gpt-5.4-mini"],
        )

    def test_configured_models_for_is_case_insensitive(self) -> None:
        cfg = Config()
        cfg._apply_toml({"models": {"claude": ["opus"]}})
        self.assertEqual(cfg.configured_models_for("Claude"), ["opus"])

    def test_unknown_backend_and_none_yield_empty(self) -> None:
        cfg = Config()
        cfg._apply_toml({"models": {"claude": ["opus"]}})
        self.assertEqual(cfg.configured_models_for("pi"), [])
        self.assertEqual(cfg.configured_models_for(None), [])
        self.assertEqual(cfg.configured_models_for(""), [])

    def test_values_are_coerced_to_strings(self) -> None:
        cfg = Config()
        cfg._apply_toml({"models": {"claude": ["opus", 5]}})
        self.assertEqual(cfg.configured_models_for("claude"), ["opus", "5"])


class PublicUrlTests(unittest.TestCase):
    """``mm_public_url`` — optional user-facing base URL for permalinks.

    The daemon talks to MM over ``mm_url`` (often ``localhost``); permalinks
    rendered in channel headers must point at a URL humans can reach from
    their browsers. ``MM_PUBLIC_URL`` / ``[mattermost].public_url`` decouples
    the two. Falls back to empty (callers construct from ``mm_url``).
    """

    def test_default_is_empty(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.mm_public_url, "")

    def test_env_var_sets_public_url(self) -> None:
        cfg = Config()
        with patch.dict(
            "os.environ",
            {"MM_PUBLIC_URL": "http://mm.example.com:8065"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertEqual(
            cfg.mm_public_url, "http://mm.example.com:8065",
        )

    def test_toml_mattermost_public_url_sets_field(self) -> None:
        cfg = Config()
        cfg._apply_toml({"mattermost": {"public_url": "https://mm.example.com"}})
        self.assertEqual(cfg.mm_public_url, "https://mm.example.com")


class DangerousPermissionsTests(unittest.TestCase):
    """Bridge-owned dangerous-permissions config mirrors VibeDeck operator mode."""

    def test_default_is_true(self) -> None:
        """Default assumes the bridge is paired with a VibeDeck daemon started
        with --dangerously-skip-permissions, which is the typical operator
        setup. Constrained deployments opt out via env or TOML."""
        cfg = Config()
        self.assertTrue(cfg.dangerous_permissions)

    def test_env_zero_disables_dangerous_permissions(self) -> None:
        cfg = Config()
        with patch.dict(
            "os.environ",
            {"MM_BRIDGE_DANGEROUS_PERMISSIONS": "0"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertFalse(cfg.dangerous_permissions)

    def test_env_true_keeps_dangerous_permissions(self) -> None:
        cfg = Config(dangerous_permissions=False)
        with patch.dict(
            "os.environ",
            {"MM_BRIDGE_DANGEROUS_PERMISSIONS": "true"},
            clear=True,
        ):
            cfg._apply_env()
        self.assertTrue(cfg.dangerous_permissions)

    def test_toml_can_disable_dangerous_permissions(self) -> None:
        cfg = Config()
        cfg._apply_toml({"dangerous_permissions": False})
        self.assertFalse(cfg.dangerous_permissions)


class MirrorExternalSessionsTests(unittest.TestCase):
    """``mirror_external_sessions`` — channel auto-creation for CLI/terminal
    ("external") sessions the observer discovers. Defaults to true (the
    single-tenant behavior); shared-server operators set it to false so
    terminal work never surfaces as public channels."""

    def test_default_is_true(self) -> None:
        self.assertTrue(Config().mirror_external_sessions)

    def test_toml_key_disables(self) -> None:
        cfg = Config()
        cfg._apply_toml({"mirror_external_sessions": False})
        self.assertFalse(cfg.mirror_external_sessions)

    def test_env_disables(self) -> None:
        cfg = Config()
        with patch.dict(
            "os.environ", {"MM_MIRROR_EXTERNAL_SESSIONS": "0"}, clear=True
        ):
            cfg._apply_env()
        self.assertFalse(cfg.mirror_external_sessions)

    def test_env_overrides_toml(self) -> None:
        cfg = Config()
        cfg._apply_toml({"mirror_external_sessions": False})
        with patch.dict(
            "os.environ", {"MM_MIRROR_EXTERNAL_SESSIONS": "true"}, clear=True
        ):
            cfg._apply_env()
        self.assertTrue(cfg.mirror_external_sessions)


if __name__ == "__main__":
    unittest.main()
