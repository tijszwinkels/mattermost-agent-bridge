"""CLI subcommand tests — invite / channel / serve / spawn dispatch."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Anchor, Config


@dataclass
class FakeMM:
    users_by_username: dict = field(default_factory=dict)
    channels_by_id: dict = field(default_factory=dict)
    invited: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    renames: list = field(default_factory=list)
    headers: list = field(default_factory=list)
    logged_in: bool = False
    missing_users: set = field(default_factory=set)
    invite_failures: set = field(default_factory=set)

    def login(self) -> None:
        self.logged_in = True

    def get_user_by_username(self, username: str) -> dict:
        if username in self.missing_users:
            raise RuntimeError(f"no such user: {username}")
        return self.users_by_username[username]

    def invite_user(self, channel_id: str, user_id: str) -> None:
        if user_id in self.invite_failures:
            raise RuntimeError(f"invite failed for {user_id}")
        self.invited.append((channel_id, user_id))

    def get_channel(self, channel_id: str) -> dict:
        return self.channels_by_id[channel_id]

    def post_message(self, channel_id: str, message: str) -> dict:
        self.posts.append(
            {"channel_id": channel_id, "message": message, "root_id": None},
        )
        return {"id": f"post-{len(self.posts)}"}

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list | None = None,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        self.posts.append(
            {
                "channel_id": channel_id, "message": message,
                "root_id": root_id, "props": props,
            },
        )
        return {"id": f"post-{len(self.posts)}"}

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self.renames.append((channel_id, display_name))

    def set_channel_header(self, channel_id: str, header: str) -> None:
        self.headers.append((channel_id, header))


class InviteHelperTests(unittest.TestCase):
    """`cli._invite_to_channel` — mockable core of the invite subcommand."""

    def test_resolves_username_and_calls_invite(self) -> None:
        mm = FakeMM(users_by_username={"alice": {"id": "u-alice"}})
        cli._invite_to_channel(mm, "c1", "alice")
        self.assertEqual(mm.invited, [("c1", "u-alice")])

    def test_strips_at_prefix(self) -> None:
        mm = FakeMM(users_by_username={"alice": {"id": "u-alice"}})
        cli._invite_to_channel(mm, "c1", "@alice")
        self.assertEqual(mm.invited, [("c1", "u-alice")])

    def test_unknown_user_raises(self) -> None:
        mm = FakeMM(missing_users={"nobody"})
        with self.assertRaises(RuntimeError):
            cli._invite_to_channel(mm, "c1", "nobody")


class AnchorLookupTests(unittest.TestCase):
    """`cli._resolve_anchor_from_session` — session_id → Anchor."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_single_line_sidecar_resolves_to_channel_anchor(self) -> None:
        sidecar.write(self.sdir, "sess-1", "chan-42")
        self.assertEqual(
            cli._resolve_anchor_from_session(self.sdir, "sess-1"),
            Anchor("chan-42"),
        )

    def test_two_line_sidecar_resolves_to_thread_anchor(self) -> None:
        sidecar.write(self.sdir, "sess-fork", "chan-42", "root-7")
        self.assertEqual(
            cli._resolve_anchor_from_session(self.sdir, "sess-fork"),
            Anchor("chan-42", "root-7"),
        )

    def test_raises_when_sidecar_missing(self) -> None:
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_anchor_from_session(self.sdir, "sess-unknown")

    def test_raises_when_sidecar_empty(self) -> None:
        self.sdir.mkdir(parents=True)
        (self.sdir / "sess-empty").write_text("")
        with self.assertRaises(cli.NotInMattermostChannel):
            cli._resolve_anchor_from_session(self.sdir, "sess-empty")


class CurrentSessionIdTests(unittest.TestCase):
    """`cli._current_session_id` — env-var-first resolver chain.

    Order: ``MM_BRIDGE_SESSION_ID`` (with valid sidecar) → ``CLAUDE_SESSION_ID``
    → ``MM_BRIDGE_SESSION_ID`` (orphan) → live-codex PPid tie-breaker →
    cwd-mtime walk over codex rollouts. The sidecar-gated form of
    ``MM_BRIDGE_SESSION_ID`` takes precedence so an explicit pin from
    ``mm-bridge spawn`` overrides a leaked ``CLAUDE_SESSION_ID`` from
    the parent shell (RC1). The fallback steps require a *sidecar_dir*
    and additionally gate every candidate id on whether a sidecar
    exists for it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sdir = Path(self.tmp.name) / "sessions"
        self.sdir.mkdir(parents=True)
        # Stub the PPid tie-breaker to None by default so tests don't
        # accidentally read the test-runner's real /proc state. Tests
        # that exercise the tie-breaker override this with their own
        # patch.
        ppid_patcher = patch(
            "mm_bridge.cli.find_active_codex_rollout_uuid",
            return_value=None,
        )
        self.mock_ppid = ppid_patcher.start()
        self.addCleanup(ppid_patcher.stop)

    def _empty_env(self) -> dict:
        # patch.dict baseline that strips both session-id env vars.
        return {"CLAUDE_SESSION_ID": "", "MM_BRIDGE_SESSION_ID": ""}

    def test_claude_session_id_wins(self) -> None:
        with patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "from-claude",
             "MM_BRIDGE_SESSION_ID": "from-bridge"},
        ):
            self.assertEqual(cli._current_session_id(), "from-claude")

    def test_mm_bridge_session_id_used_when_claude_absent(self) -> None:
        with patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "", "MM_BRIDGE_SESSION_ID": "from-bridge"},
        ):
            self.assertEqual(cli._current_session_id(), "from-bridge")

    def test_mm_bridge_session_id_with_sidecar_beats_claude_session_id(
        self,
    ) -> None:
        """RC1 fix: an explicit ``MM_BRIDGE_SESSION_ID`` whose sidecar
        resolves overrides a leaked ``CLAUDE_SESSION_ID`` from the
        parent shell. Without the sidecar gate, the parent's claude id
        would win and the child would resolve to the parent's
        channel."""
        sidecar.write(self.sdir, "child-sid", "child-chan")
        sidecar.write(self.sdir, "parent-claude-sid", "parent-chan")
        with patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "parent-claude-sid",
             "MM_BRIDGE_SESSION_ID": "child-sid"},
        ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "child-sid",
            )

    def test_claude_session_id_used_when_mm_bridge_has_no_sidecar(self) -> None:
        """Backward compat: an orphan ``MM_BRIDGE_SESSION_ID`` (no
        sidecar on disk) must not block the legacy claude path. The
        sidecar gate filters orphan env values, then ``CLAUDE_SESSION_ID``
        takes over with its existing semantics (no sidecar gate)."""
        sidecar.write(self.sdir, "from-claude", "claude-chan")
        with patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "from-claude",
             "MM_BRIDGE_SESSION_ID": "orphan-no-sidecar"},
        ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "from-claude",
            )

    def test_mm_bridge_session_id_returned_as_fallback_when_no_sidecar(
        self,
    ) -> None:
        """Orphan ``MM_BRIDGE_SESSION_ID`` with no ``CLAUDE_SESSION_ID``
        is still returned verbatim — preserves the original "trust the
        env" behaviour for consumers that pre-date the sidecar gate."""
        with patch.dict(
            "os.environ",
            {"CLAUDE_SESSION_ID": "", "MM_BRIDGE_SESSION_ID": "orphan"},
        ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "orphan",
            )

    def test_rollout_fallback_returns_id_when_sidecar_present(self) -> None:
        sidecar.write(self.sdir, "codex-sid-007", "chan-007")
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-sid-007"]),
             ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "codex-sid-007",
            )

    def test_rollout_fallback_skipped_when_sidecar_absent(self) -> None:
        # Resolver finds a candidate UUID but no sidecar → not adopted.
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-sid-no-sidecar"]),
             ):
            with self.assertRaises(cli.NotInMattermostChannel):
                cli._current_session_id(self.sdir)

    def test_rollout_fallback_walks_until_sidecar_found(self) -> None:
        """Newest cwd-match without a sidecar must not short-circuit.

        Realistic scenario: a non-bridge codex session ran in the same
        cwd recently and now has the newest rollout, but the actual
        bridge-linked session is older. The resolver must keep looking.
        """
        sidecar.write(self.sdir, "codex-sid-old-but-linked", "chan-old")
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([
                     "codex-sid-newest-no-sidecar",
                     "codex-sid-old-but-linked",
                 ]),
             ):
            self.assertEqual(
                cli._current_session_id(self.sdir),
                "codex-sid-old-but-linked",
            )

    def test_rollout_fallback_skips_empty_sidecar(self) -> None:
        """An empty (corrupt) sidecar file must not satisfy the gate.

        Use ``sidecar.read()`` for the gate, not ``Path.exists()`` — an
        empty file would otherwise be adopted and then crash downstream
        with an opaque error.
        """
        # Write a truly empty file at <sdir>/<sid> — ``Path.exists()`` is
        # True but ``sidecar.read()`` returns None (no channel id).
        self.sdir.mkdir(parents=True, exist_ok=True)
        (self.sdir / "codex-sid-empty-sidecar").write_text("")
        sidecar.write(self.sdir, "codex-sid-valid-but-older", "chan-real")
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([
                     "codex-sid-empty-sidecar",
                     "codex-sid-valid-but-older",
                 ]),
             ):
            self.assertEqual(
                cli._current_session_id(self.sdir),
                "codex-sid-valid-but-older",
            )

    def test_ppid_tiebreaker_wins_over_mtime_walk(self) -> None:
        """When a live codex parent is in our chain, the UUID it has
        open beats whatever the cwd-mtime walk would have picked.

        Realistic scenario from PR review: an idle same-cwd codex
        session's rollout was just touched by an unrelated tool call,
        so its mtime is newer than the active session's. Without the
        tie-breaker, mm-bridge would route to the idle session's
        channel; with it, the active session wins.
        """
        sidecar.write(self.sdir, "codex-from-ppid", "chan-active")
        sidecar.write(self.sdir, "codex-from-mtime", "chan-stale")
        self.mock_ppid.return_value = "codex-from-ppid"
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-from-mtime"]),
             ) as mock_iter:
            self.assertEqual(
                cli._current_session_id(self.sdir), "codex-from-ppid",
            )
            # Tie-breaker shortcut → mtime walk never consulted.
            mock_iter.assert_not_called()

    def test_ppid_tiebreaker_falls_through_when_no_sidecar(self) -> None:
        """A codex parent UUID that has no sidecar (race window between
        codex start and daemon writing the sidecar) must not block the
        mtime walk — older candidates with sidecars stay reachable."""
        sidecar.write(self.sdir, "codex-from-mtime", "chan-mtime")
        self.mock_ppid.return_value = "codex-without-sidecar"
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-from-mtime"]),
             ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "codex-from-mtime",
            )

    def test_ppid_tiebreaker_none_uses_mtime_walk(self) -> None:
        """No live codex ancestor (background task, macOS, etc.) — the
        mtime walk takes over with its existing semantics."""
        sidecar.write(self.sdir, "codex-from-mtime", "chan-mtime")
        # Default-mocked tie-breaker already returns None.
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-from-mtime"]),
             ):
            self.assertEqual(
                cli._current_session_id(self.sdir), "codex-from-mtime",
            )

    def test_rollout_fallback_skipped_when_sidecar_dir_not_passed(self) -> None:
        # Without a sidecar_dir the resolver chain never reaches the
        # rollout fallback — preserves the original error path for callers
        # that haven't been migrated yet.
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-sid-anything"]),
             ) as mock_iter:
            with self.assertRaises(cli.NotInMattermostChannel):
                cli._current_session_id()
            mock_iter.assert_not_called()

    def test_raises_when_no_resolver_succeeds(self) -> None:
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([]),
             ):
            with self.assertRaises(cli.NotInMattermostChannel):
                cli._current_session_id(self.sdir)

    def test_error_mentions_all_resolvers(self) -> None:
        with patch.dict("os.environ", self._empty_env()), \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([]),
             ):
            try:
                cli._current_session_id(self.sdir)
            except cli.NotInMattermostChannel as exc:
                msg = str(exc)
            else:
                self.fail("expected NotInMattermostChannel")
        self.assertIn("CLAUDE_SESSION_ID", msg)
        self.assertIn("MM_BRIDGE_SESSION_ID", msg)
        self.assertIn("rollout", msg)


class BareInvocationTests(unittest.TestCase):
    """`mm-bridge` with no subcommand prints help and exits 1."""

    def test_bare_invocation_exits_1(self) -> None:
        with patch("sys.argv", ["mm-bridge"]):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 1)


class InviteCommandTests(unittest.TestCase):
    """End-to-end dispatch of `mm-bridge invite <username>` with mocked MM."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        sidecar.write(self.sdir, "my-session", "my-channel")

        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
        )
        self.fake_mm = FakeMM(users_by_username={"alice": {"id": "u-alice"}})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_invite_dispatch_calls_mm_with_resolved_ids(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "alice"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "my-session"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertTrue(self.fake_mm.logged_in)
        self.assertEqual(self.fake_mm.invited, [("my-channel", "u-alice")])

    def test_invite_without_session_env_exits_nonzero(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "alice"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {}, clear=False) as env, \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([]),
             ):
            env.pop("CLAUDE_SESSION_ID", None)
            env.pop("MM_BRIDGE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_invite_without_sidecar_exits_nonzero(self) -> None:
        with patch("sys.argv", ["mm-bridge", "invite", "alice"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "unknown-sess"}):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_invite_from_thread_fork_session_invites_to_channel(self) -> None:
        """The bug the anchor refactor fixes: inviting from a thread-fork
        session must succeed and invite to the fork's channel."""
        sidecar.write(self.sdir, "fork-sess", "fork-chan", "root-9")
        with patch("sys.argv", ["mm-bridge", "invite", "alice"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "fork-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.fake_mm.invited, [("fork-chan", "u-alice")])


class ChannelCommandTests(unittest.TestCase):
    """`mm-bridge channel` prints the channel_id for the current session."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        sidecar.write(self.sdir, "my-session", "my-channel")
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_channel_prints_channel_id(self) -> None:
        import io
        buf = io.StringIO()
        with patch("sys.argv", ["mm-bridge", "channel"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "my-session"}), \
             patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "my-channel")

    def test_channel_falls_back_to_codex_rollout_when_env_absent(self) -> None:
        """Codex tool shells with no session-id env can still resolve.

        Mirrors the post-`mm-bridge spawn --backend codex` situation: env
        vars unset, but the daemon has written a sidecar keyed by the
        codex session id, and the resolver finds it via the cwd-matched
        rollout file.
        """
        import io
        sidecar.write(self.sdir, "codex-sess-xyz", "codex-chan-xyz")
        buf = io.StringIO()
        with patch("sys.argv", ["mm-bridge", "channel"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {}, clear=False) as env, \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter(["codex-sess-xyz"]),
             ), \
             patch("sys.stdout", buf):
            env.pop("CLAUDE_SESSION_ID", None)
            env.pop("MM_BRIDGE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "codex-chan-xyz")

    def test_channel_from_thread_fork_session_prints_channel_id(self) -> None:
        """Thread-fork sessions must self-identify too (was broken before)."""
        import io
        sidecar.write(self.sdir, "fork-sess", "fork-chan", "root-9")
        buf = io.StringIO()
        with patch("sys.argv", ["mm-bridge", "channel"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "fork-sess"}), \
             patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), "fork-chan")


class WaitForNewSidecarTests(unittest.TestCase):
    """`_wait_for_new_sidecar` — the polling helper used by `cmd_spawn`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.sdir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_clock(self, ticks: list[float]):
        """Return a clock() that yields successive values from `ticks`."""
        it = iter(ticks)
        return lambda: next(it)

    def test_returns_new_sidecar_on_first_poll(self) -> None:
        sidecar.write(self.sdir, "new-sess", "new-chan")
        sid, cid = cli._wait_for_new_sidecar(
            self.sdir, before=set(),
            timeout=1.0, interval=0.01,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        )
        self.assertEqual(sid, "new-sess")
        self.assertEqual(cid, "new-chan")

    def test_ignores_sidecars_present_in_before(self) -> None:
        sidecar.write(self.sdir, "existing", "existing-chan")
        sidecar.write(self.sdir, "fresh", "fresh-chan")
        sid, cid = cli._wait_for_new_sidecar(
            self.sdir, before={"existing"},
            timeout=1.0, interval=0.01,
            clock=lambda: 0.0,
            sleep=lambda _: None,
        )
        self.assertEqual(sid, "fresh")
        self.assertEqual(cid, "fresh-chan")

    def test_timeout_when_no_new_sidecar(self) -> None:
        # Clock advances past deadline immediately after the first poll.
        clock = self._fake_clock([0.0, 0.0, 2.0])  # start, loop 1, loop 2
        with self.assertRaises(TimeoutError):
            cli._wait_for_new_sidecar(
                self.sdir, before=set(),
                timeout=1.0, interval=0.01,
                clock=clock,
                sleep=lambda _: None,
            )

    def test_empty_sidecar_raises(self) -> None:
        (self.sdir / "bad-sess").write_text("")
        with self.assertRaises(RuntimeError):
            cli._wait_for_new_sidecar(
                self.sdir, before=set(),
                timeout=1.0, interval=0.01,
                clock=lambda: 0.0,
                sleep=lambda _: None,
            )

    def test_missing_dir_times_out(self) -> None:
        missing = Path(self.tmp.name) / "does-not-exist"
        clock = self._fake_clock([0.0, 0.0, 2.0])
        with self.assertRaises(TimeoutError):
            cli._wait_for_new_sidecar(
                missing, before=set(),
                timeout=1.0, interval=0.01,
                clock=clock,
                sleep=lambda _: None,
            )


class SpawnCommandTests(unittest.TestCase):
    """End-to-end dispatch of `mm-bridge spawn` with mocked MM/harness/sidecar."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        # Parent session's sidecar.
        sidecar.write(self.sdir, "parent-sess", "parent-chan")

        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            agent_harness_url="http://harness.invalid",
            default_cwd="/tmp",
            default_backend="claude",
        )

        self.fake_mm = FakeMM(
            users_by_username={"alice": {"id": "u-alice"}},
            channels_by_id={
                "parent-chan": {"id": "parent-chan", "name": "parent-slug"},
                "new-chan": {
                    "id": "new-chan", "name": "s-abc",
                    "display_name": "Auto-derived Title",
                },
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _simulate_daemon_creates_sidecar(
        self,
        sess_id: str = "new-sess",
        chan_id: str = "new-chan",
        expected_model: str | None = "opus",
    ):
        """Return an async stub that mimics harness → daemon → sidecar appearance.

        ``expected_model`` is the per-backend default that the spawn CLI is
        expected to compute via ``Config.default_model_for(backend)``.
        Defaults to ``opus`` since the test fixture's ``default_backend``
        is claude.
        """
        async def _stub(harness_url, message, cwd, backend, model, title=None, effort=None):
            self.assertEqual(model, expected_model)
            sidecar.write(self.sdir, sess_id, chan_id)
            return {"status": "started"}
        return _stub

    def _invoke(self, argv, *, stdin: str = ""):
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "parent-sess"},
             ), \
             patch("sys.stdin", io.StringIO(stdin)), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch(
                 "mm_bridge.cli._harness_create_session",
                 side_effect=self._simulate_daemon_creates_sidecar(),
             ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code

    def test_spawn_happy_path(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "fix the bug",
            "--title", "Bug Fix", "--cwd", "/repo", "--backend", "claude",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(self.fake_mm.logged_in)
        # Rename + header applied to the new channel.
        self.assertEqual(
            self.fake_mm.renames, [("new-chan", "Bug Fix")],
        )
        self.assertEqual(
            self.fake_mm.headers, [("new-chan", "Parent: ~parent-slug~")],
        )
        # Two posts: kickoff in sub-channel, announcement in parent.
        self.assertEqual(len(self.fake_mm.posts), 2)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("new-chan", posts_by_chan)
        self.assertIn("parent-chan", posts_by_chan)
        sub_body = posts_by_chan["new-chan"]["message"]
        parent_body = posts_by_chan["parent-chan"]["message"]
        self.assertIn("Spawned from ~parent-slug~", sub_body)
        self.assertIn("> fix the bug", sub_body)
        self.assertIn("Spawned **Bug Fix** in ~s-abc~", parent_body)
        self.assertIn("> fix the bug", parent_body)
        # Spawning from a channel-level session: announcement is not threaded.
        self.assertIsNone(posts_by_chan["parent-chan"]["root_id"])

    def test_spawn_prompt_from_stdin_forwarded_to_harness_and_quoted(
        self,
    ) -> None:
        """``mm-bridge spawn -`` reads the prompt from stdin (the heredoc
        dispatch path). The piped body must reach ``create_session``
        verbatim AND be quoted into the kickoff/announcement posts — the
        2026-07-14 bug was the literal ``"-"`` arriving as the whole brief.
        """
        captured: dict = {}

        async def _stub(harness_url, message, cwd, backend, model, title=None, effort=None):
            captured["message"] = message
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", ["mm-bridge", "spawn", "-"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("sys.stdin", io.StringIO("do the big multi-line brief\nstep 2\n")), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        self.assertEqual(captured["message"], "do the big multi-line brief\nstep 2")
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("> do the big multi-line brief", posts_by_chan["new-chan"]["message"])
        self.assertIn("> step 2", posts_by_chan["parent-chan"]["message"])

    def test_spawn_dash_empty_stdin_exits_2_without_harness_call(self) -> None:
        """``spawn -`` with empty stdin is an explicit error — never an
        empty brief that leaves the sub-session idle. Fails fast: no
        harness call, no MM login, no posts."""
        called = {"harness": False}

        async def _stub(*a, **k):
            called["harness"] = True
            return {"status": "started"}

        with patch("sys.argv", ["mm-bridge", "spawn", "-"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("sys.stdin", io.StringIO("")), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(called["harness"])
        self.assertEqual(self.fake_mm.posts, [])

    def test_spawn_dash_tty_stdin_exits_2_rather_than_hanging(self) -> None:
        """``spawn -`` with an interactive TTY stdin errors immediately
        instead of blocking forever on ``read()``."""

        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        async def _stub(*a, **k):
            raise AssertionError("harness must not be called on TTY stdin")

        with patch("sys.argv", ["mm-bridge", "spawn", "-"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("sys.stdin", _FakeTTY("")), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(cm.exception.code, 2)

    def test_spawn_literal_prompt_still_works(self) -> None:
        """A normal positional prompt (not ``-``) is unaffected — stdin
        is never consulted."""
        rc = self._invoke(["mm-bridge", "spawn", "plain brief"])
        self.assertEqual(rc, 0)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("> plain brief", posts_by_chan["new-chan"]["message"])

    def test_spawn_posts_carry_bridge_cli_marker_prop(self) -> None:
        """Both spawn-authored posts (parent announcement and child kickoff)
        are created by the CLI — the daemon's per-process own-post tracker
        only sees IDs from its own MattermostClient, so without the marker
        the WS echo would be forwarded to the linked session as a user turn.
        VD already received ``args.prompt`` via ``create_session`` and
        delivered it as the new session's first turn; the kickoff post is
        a visual record for the channel, not a duplicate user input.

        Both posts also carry ``from_bridge_cli_channel`` set to the
        channel they land in — the daemon's channel-scoped predicate
        treats that as own-channel echo and drops them. (The kickoff is
        also delivered via VD, so it must not arrive twice as a user
        turn in the new session.)"""
        rc = self._invoke([
            "mm-bridge", "spawn", "fix the bug", "--title", "Bug Fix",
        ])
        self.assertEqual(rc, 0)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}

        announcement = posts_by_chan["parent-chan"]
        self.assertEqual(
            announcement.get("props"),
            {
                "from_bridge_cli": "spawn-announcement",
                "from_bridge_cli_channel": "parent-chan",
            },
        )

        kickoff = posts_by_chan["new-chan"]
        self.assertEqual(
            kickoff.get("props"),
            {
                "from_bridge_cli": "spawn-kickoff",
                "from_bridge_cli_channel": "new-chan",
            },
        )

    def test_spawn_without_title_does_not_rename(self) -> None:
        rc = self._invoke(["mm-bridge", "spawn", "ad hoc"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.renames, [])
        # Header still set.
        self.assertEqual(len(self.fake_mm.headers), 1)
        # Parent post uses daemon-derived display_name.
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn(
            "**Auto-derived Title**", posts_by_chan["parent-chan"]["message"],
        )

    def test_spawn_no_forward_prompt_skips_all_posts(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "quiet one", "--no-forward-prompt",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.posts, [])

    def test_spawn_with_invite_adds_user_to_new_channel(self) -> None:
        rc = self._invoke([
            "mm-bridge", "spawn", "need help", "--invite", "alice",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake_mm.invited, [("new-chan", "u-alice")])

    def test_spawn_codex_backend_uses_codex_default_model(self) -> None:
        """``mm-bridge spawn --backend codex`` must not inherit claude's
        default model. The single ``default_model = "opus"`` scalar that
        used to live on Config caused ``codex exec --model opus`` to exit
        1 immediately — the whole point of the per-backend table.
        """
        captured: dict = {}

        async def _stub(harness_url, message, cwd, backend, model, title=None, effort=None):
            captured["backend"] = backend
            captured["model"] = model
            captured["title"] = title
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", [
            "mm-bridge", "spawn", "hi", "--backend", "codex",
        ]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        self.assertEqual(captured["backend"], "codex")
        self.assertEqual(captured["model"], "gpt-5.5")
        # No --title flag passed: title arrives as None.
        self.assertIsNone(captured["title"])

    def test_spawn_model_flag_overrides_default_model(self) -> None:
        """``mm-bridge spawn --model claude-fable-5`` must surface the
        explicit model to ``create_session`` verbatim, overriding the
        per-backend config default. This is the supported way for an
        agent to spawn a sub-session on a non-default model (e.g. Fable)
        without mutating the daemon's config."""
        captured: dict = {}

        async def _stub(harness_url, message, cwd, backend, model, title=None, effort=None):
            captured["backend"] = backend
            captured["model"] = model
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", [
            "mm-bridge", "spawn", "hi", "--model", "claude-fable-5",
        ]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        # Explicit --model wins over the claude default ("opus").
        self.assertEqual(captured["backend"], "claude")
        self.assertEqual(captured["model"], "claude-fable-5")

    def test_spawn_effort_flag_reaches_create_session(self) -> None:
        """``mm-bridge spawn --effort xhigh`` sets the sub-session's
        reasoning level at create time — the spawn-path equivalent of an
        `effort=` Channel Purpose token."""
        captured: dict = {}

        async def _stub(
            harness_url, message, cwd, backend, model, title=None, effort=None,
        ):
            captured["effort"] = effort
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", [
            "mm-bridge", "spawn", "hi", "--effort", "XHigh",
        ]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        # Normalised to the canonical lowercase level.
        self.assertEqual(captured["effort"], "xhigh")

    def test_spawn_without_effort_sends_none(self) -> None:
        """Unset stays unset — the backend CLI's own default applies."""
        captured: dict = {}

        async def _stub(
            harness_url, message, cwd, backend, model, title=None, effort=None,
        ):
            captured["effort"] = effort
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", ["mm-bridge", "spawn", "hi"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        self.assertIsNone(captured["effort"])

    def test_spawn_rejects_unknown_effort_level_before_dispatching(self) -> None:
        """codex doesn't validate the level locally — it forwards it and the
        API 400s mid-run. Fail here instead, before anything is spawned."""
        called: list = []

        async def _stub(*a, **kw):
            called.append(kw)
            return {"status": "started"}

        stderr = io.StringIO()
        with patch("sys.argv", [
            "mm-bridge", "spawn", "hi", "--effort", "turbo",
        ]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub), \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)

        self.assertEqual(called, [])
        self.assertIn("xhigh", stderr.getvalue())

    def test_spawn_passes_title_to_harness_create_session(self) -> None:
        """``mm-bridge spawn --title "Foo"`` must surface "Foo" as the
        harness Session.title so consumers (e.g. command-bridge lane
        headers) display the human-readable name instead of falling
        back to project name or session id."""
        captured: dict = {}

        async def _stub(harness_url, message, cwd, backend, model, title=None, effort=None):
            captured["title"] = title
            sidecar.write(self.sdir, "new-sess", "new-chan")
            return {"status": "started"}

        with patch("sys.argv", [
            "mm-bridge", "spawn", "hi", "--title", "Yellow Pages",
        ]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "parent-sess"}), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_stub):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)

        self.assertEqual(captured["title"], "Yellow Pages")

    def test_spawn_without_session_env_exits_2(self) -> None:
        with patch("sys.argv", ["mm-bridge", "spawn", "x"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict("os.environ", {}, clear=False) as env, \
             patch(
                 "mm_bridge.cli.iter_session_ids_by_cwd",
                 return_value=iter([]),
             ):
            env.pop("CLAUDE_SESSION_ID", None)
            env.pop("MM_BRIDGE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 2)

    def test_spawn_from_thread_fork_uses_public_url_for_header_permalink(
        self,
    ) -> None:
        """When ``mm_public_url`` is set, the Parent: header permalink
        uses that base URL — not the daemon-internal ``mm_url``."""
        self.cfg.mm_public_url = "http://mm.example.com:8065"
        self.cfg.mm_team = "workspace"
        sidecar.write(self.sdir, "fork-sess", "parent-chan", "root-9")
        with patch("sys.argv", ["mm-bridge", "spawn", "carry on"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "fork-sess"},
             ), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch(
                 "mm_bridge.cli._harness_create_session",
                 side_effect=self._simulate_daemon_creates_sidecar(),
             ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        header_text = self.fake_mm.headers[0][1]
        self.assertIn(
            "http://mm.example.com:8065/workspace/pl/root-9",
            header_text,
        )

    def test_spawn_from_thread_fork_creates_sibling_channel(self) -> None:
        """Spawning from a thread-fork session must succeed. The sub-session
        lives in a fresh sibling channel (not nested thread) and the parent
        announcement goes into the fork's thread (not the channel root)."""
        sidecar.write(self.sdir, "fork-sess", "parent-chan", "root-9")
        with patch("sys.argv", ["mm-bridge", "spawn", "carry on"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "fork-sess"},
             ), \
             patch("mm_bridge.cli._make_mm_client", return_value=self.fake_mm), \
             patch(
                 "mm_bridge.cli._harness_create_session",
                 side_effect=self._simulate_daemon_creates_sidecar(),
             ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 0)
        posts_by_chan = {p["channel_id"]: p for p in self.fake_mm.posts}
        self.assertIn("new-chan", posts_by_chan)
        self.assertIn("parent-chan", posts_by_chan)
        # Kickoff in the sub-channel is never threaded.
        self.assertIsNone(posts_by_chan["new-chan"]["root_id"])
        # Announcement lands in the parent thread, not at the channel root.
        self.assertEqual(posts_by_chan["parent-chan"]["root_id"], "root-9")
        # Child channel's Parent: header carries a permalink back into
        # the parent thread, not just the channel mention.
        self.assertEqual(len(self.fake_mm.headers), 1)
        header_text = self.fake_mm.headers[0][1]
        self.assertIn("Parent: ~parent-slug~", header_text)
        self.assertIn("[thread](", header_text)
        self.assertIn("/pl/root-9", header_text)

    def test_spawn_harness_failure_exits_3(self) -> None:
        async def _boom(harness_url, message, cwd, backend, model, effort=None):
            raise RuntimeError("harness down")
        with patch("sys.argv", ["mm-bridge", "spawn", "x"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch.dict(
                "os.environ", {"CLAUDE_SESSION_ID": "parent-sess"},
             ), \
             patch(
                 "mm_bridge.cli._make_mm_client", return_value=self.fake_mm,
             ), \
             patch("mm_bridge.cli._harness_create_session", side_effect=_boom):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            self.assertEqual(cm.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
