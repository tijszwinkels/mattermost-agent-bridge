"""Tests for `mm-bridge post`."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from mm_bridge import cli, sidecar
from mm_bridge.config import Config


@dataclass
class FakeMM:
    posted: list = field(default_factory=list)
    uploaded: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    logged_in: bool = False
    login_raises: Exception | None = None
    upload_raises: Exception | None = None
    post_raises: Exception | None = None
    get_channel_raises: Exception | None = None
    max_file_size: int = 50 * 1024 * 1024
    next_file_id_counter: int = 0
    next_post_id_counter: int = 0
    # slug → channel record, for ``--channel`` slug/URL resolution.
    named_channels: dict = field(default_factory=dict)
    # post id → post record, for permalink resolution (``--channel``/``--thread``).
    post_by_id: dict = field(default_factory=dict)
    # Simulate a 404 from the two lookups above.
    lookup_404: bool = False
    lookups: list = field(default_factory=list)

    def login(self) -> None:
        if self.login_raises:
            raise self.login_raises
        self.logged_in = True

    def upload_file(self, channel_id: str, path: Path) -> str:
        if self.upload_raises:
            raise self.upload_raises
        self.next_file_id_counter += 1
        fid = f"f-{self.next_file_id_counter}"
        self.uploaded.append((channel_id, str(path), fid))
        return fid

    def get_max_file_size(self) -> int:
        return self.max_file_size

    def get_channel(self, channel_id: str) -> dict:
        if self.get_channel_raises:
            raise self.get_channel_raises
        return self.channels.get(channel_id, {"id": channel_id, "name": ""})

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list | None = None,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        if self.post_raises:
            raise self.post_raises
        self.next_post_id_counter += 1
        pid = f"post-{self.next_post_id_counter}"
        self.posted.append({
            "channel_id": channel_id,
            "message": message,
            "file_ids": list(file_ids) if file_ids else [],
            "root_id": root_id,
            "props": dict(props) if props else None,
        })
        return {"id": pid}

    def get_channel_by_name(self, team_name: str, channel_name: str) -> dict:
        self.lookups.append(("get_channel_by_name", team_name, channel_name))
        if self.lookup_404:
            raise RuntimeError(
                f"404 NOT FOUND: channel '{channel_name}' in team '{team_name}'"
            )
        if channel_name in self.named_channels:
            return self.named_channels[channel_name]
        # Identity fallback keeps legacy bare-slug tests green: the slug IS
        # the resolved id.
        return {"id": channel_name, "name": channel_name}

    def get_post(self, post_id: str) -> dict:
        self.lookups.append(("get_post", post_id))
        if self.lookup_404 or post_id not in self.post_by_id:
            raise RuntimeError(f"404 NOT FOUND: post '{post_id}'")
        return self.post_by_id[post_id]


class PostCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            allowed_attachment_roots=[self.tmp.name],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(
        self,
        fake_mm: FakeMM,
        argv: list[str],
        *,
        session_id: str | None = "my-sess",
        stdin: str = "",
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {}
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("sys.stdin", io.StringIO(stdin)), \
             patch.dict("os.environ", env, clear=False) as osenv:
            if session_id is None:
                osenv.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    # ---------- channel resolution ----------

    def test_explicit_channel_wins(self) -> None:
        mm = FakeMM()
        rc, out, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "explicit-chan", "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "post-1")
        self.assertEqual(mm.posted[0]["channel_id"], "explicit-chan")
        self.assertEqual(mm.posted[0]["message"], "hello")
        self.assertIsNone(mm.posted[0]["root_id"])

    def test_falls_back_to_session_sidecar(self) -> None:
        sidecar.write(self.sdir, "my-sess", "sidecar-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "sidecar-chan")
        self.assertIsNone(mm.posted[0]["root_id"])
        # ``mm-bridge post`` stamps ``from_bridge_cli="post"`` plus the
        # sender's channel/session ids and the caller's INTENT. With no
        # ``--channel``/``--thread`` this is the default post-into-my-own-
        # channel path → ``from_bridge_cli_target="self"``. The dispatcher
        # drops such a post only when it loops back to its own author session
        # (a status update); explicit-target posts always forward.
        self.assertEqual(
            mm.posted[0]["props"],
            {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "self",
                "from_bridge_cli_channel": "sidecar-chan",
                "from_bridge_cli_session": "my-sess",
            },
        )

    def test_default_post_stamps_harness_id_through_dashed_alias(self) -> None:
        """The production shape (2026-07-10 regression): the harness wrote the
        canonical ``ses_<32hex>`` sidecar plus a dashed-UUID alias symlink, and
        the claude session looks itself up by ``CLAUDE_SESSION_ID`` (the dashed
        UUID). The stamped ``from_bridge_cli_session`` MUST be the ``ses_`` id
        (what the bridge maps), resolved through the alias — not the dashed
        UUID, which would compare across namespaces and never suppress."""
        harness_id = "ses_00112233445566778899aabbccddeeff"
        dashed = "00112233-4455-6677-8899-aabbccddeeff"
        sidecar.write(self.sdir, harness_id, "self-chan")
        self.assertTrue((self.sdir / dashed).is_symlink(), "alias fixture missing")

        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"], session_id=dashed)
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "self-chan")
        self.assertEqual(
            mm.posted[0]["props"]["from_bridge_cli_session"], harness_id,
        )
        self.assertEqual(mm.posted[0]["props"]["from_bridge_cli_target"], "self")

    def test_default_post_stamps_real_file_id_verbatim(self) -> None:
        """Codex / spawned shape: the sidecar is a real file at the session id
        (no dashed alias). ``canonical_id`` returns it verbatim — it already IS
        the harness id the bridge maps."""
        sidecar.write(self.sdir, "codex_abc123def456", "self-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "hi"], session_id="codex_abc123def456",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            mm.posted[0]["props"]["from_bridge_cli_session"], "codex_abc123def456",
        )

    def test_no_channel_and_no_sidecar_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "hi"], session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("channel", err.lower())

    # ---------- thread resolution ----------

    def test_thread_forked_sidecar_posts_to_thread_by_default(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["root_id"], "root-9")

    def test_no_thread_overrides_sidecar_root(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--no-thread", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(mm.posted[0]["root_id"])

    def test_thread_arg_overrides_sidecar(self) -> None:
        sidecar.write(self.sdir, "my-sess", "fc", "root-9")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--thread", "other", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["root_id"], "other")
        # An explicit --thread is a directed target → "explicit" (always
        # forwarded), even without --channel.
        self.assertEqual(mm.posted[0]["props"]["from_bridge_cli_target"], "explicit")

    def test_thread_arg_with_channel_arg(self) -> None:
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "--thread", "r1", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "c1")
        self.assertEqual(mm.posted[0]["root_id"], "r1")

    # ---------- message / stdin ----------

    def test_stdin_message_with_dash(self) -> None:
        # From inside a session, a default (no --channel) stdin-body post is the
        # milestone shape. Confirm the body is read AND the intent tag is
        # stamped "self" regardless of the message coming via stdin vs argv.
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "-"], stdin="piped body\n",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "self-chan")
        self.assertEqual(mm.posted[0]["message"], "piped body")
        self.assertEqual(mm.posted[0]["props"]["from_bridge_cli_target"], "self")

    def test_stdin_dash_empty_no_file_exits_2(self) -> None:
        # `post -` with empty stdin and no attachment is an empty body →
        # exit 2 (preserved behaviour, now routed through the shared helper).
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "-"], stdin="",
        )
        self.assertEqual(rc, 2)
        self.assertIn("empty", err.lower())
        self.assertEqual(mm.posted, [])

    def test_stdin_dash_tty_exits_2_rather_than_hanging(self) -> None:
        # `post -` from an interactive terminal must error, not block on read().
        class _FakeTTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM()
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["mm-bridge", "post", "-"]), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("sys.stdin", _FakeTTY("")), \
             patch.dict("os.environ", {"CLAUDE_SESSION_ID": "my-sess"}, clear=False):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("terminal", err.getvalue().lower())
        self.assertEqual(mm.posted, [])

    def test_empty_body_no_file_exits_2(self) -> None:
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "   "],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("empty", err.lower())

    def test_empty_body_with_file_ok(self) -> None:
        f = Path(self.tmp.name) / "attach.txt"
        f.write_text("hello")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", "--file", str(f), ""],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["message"], "")
        self.assertEqual(mm.posted[0]["file_ids"], ["f-1"])

    # ---------- attachments ----------

    def test_file_uploaded_and_post_gets_file_id(self) -> None:
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.uploaded), 1)
        self.assertEqual(mm.uploaded[0][0], "c1")
        self.assertEqual(mm.posted[0]["file_ids"], ["f-1"])

    def test_relative_file_resolves_from_current_working_directory(self) -> None:
        f = Path(self.tmp.name) / "relative.txt"
        f.write_text("x")
        mm = FakeMM()
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            rc, _, _ = self._invoke(
                mm,
                ["mm-bridge", "post", "--channel", "c1",
                 "--file", "relative.txt", "hi"],
                session_id=None,
            )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.uploaded), 1)
        self.assertEqual(Path(mm.uploaded[0][1]), f)

    def test_more_than_10_files_exits_2_without_upload(self) -> None:
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        files_args = []
        for _ in range(11):
            files_args += ["--file", str(f)]
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1", *files_args, "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(mm.uploaded, [])
        self.assertEqual(mm.posted, [])
        self.assertIn("10", err)

    def test_file_outside_allowed_roots_exits_2(self) -> None:
        # Force a root that excludes the temp dir.
        self.cfg.allowed_attachment_roots = ["/nonexistent/root"]
        f = Path(self.tmp.name) / "a.txt"
        f.write_text("x")
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("allowed_attachment_roots", err)
        self.assertEqual(mm.uploaded, [])
        self.assertEqual(mm.posted, [])

    def test_missing_file_exits_3(self) -> None:
        missing = Path(self.tmp.name) / "does-not-exist.txt"
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(missing), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 3)
        self.assertEqual(mm.posted, [])

    def test_file_too_big_exits_3(self) -> None:
        f = Path(self.tmp.name) / "big.bin"
        f.write_bytes(b"0" * 100)
        mm = FakeMM(max_file_size=50)
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "c1",
             "--file", str(f), "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 3)
        self.assertEqual(mm.posted, [])

    # ---------- errors ----------

    def test_missing_bot_token_exits_1(self) -> None:
        self.cfg.mm_bot_token = ""
        mm = FakeMM()
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "c1", "hi"], session_id=None,
        )
        self.assertEqual(rc, 1)
        self.assertIn("MM_BOT_TOKEN", err)

    def test_login_failure_exits_3(self) -> None:
        mm = FakeMM(login_raises=RuntimeError("boom"))
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "c1", "hi"], session_id=None,
        )
        self.assertEqual(rc, 3)

    def test_mutually_exclusive_thread_flags(self) -> None:
        mm = FakeMM()
        # argparse itself enforces this; exit code from argparse is 2.
        with patch("sys.argv", [
            "mm-bridge", "post", "--channel", "c1",
            "--thread", "r", "--no-thread", "hi",
        ]), patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=mm), \
             patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()


class CrossChannelMirrorTests(unittest.TestCase):
    """`mm-bridge post --channel <other>` mirrors body in sender's channel."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self.tmp.name) / "sessions"
        self.cfg = Config(
            mm_bot_token="t",
            sidecar_dir=str(self.sdir),
            state_file=f"{self.tmp.name}/state.json",
            allowed_attachment_roots=[self.tmp.name],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _invoke(
        self,
        fake_mm: FakeMM,
        argv: list[str],
        *,
        session_id: str | None = "my-sess",
        stdin: str = "",
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {}
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id
        with patch("sys.argv", argv), \
             patch("mm_bridge.cli.Config.load", return_value=self.cfg), \
             patch("mm_bridge.cli._make_mm_client", return_value=fake_mm), \
             patch("sys.stdout", out), patch("sys.stderr", err), \
             patch("sys.stdin", io.StringIO(stdin)), \
             patch.dict("os.environ", env, clear=False) as osenv:
            if session_id is None:
                osenv.pop("CLAUDE_SESSION_ID", None)
            with self.assertRaises(SystemExit) as cm:
                cli.main()
            return cm.exception.code, out.getvalue(), err.getvalue()

    def test_cross_channel_post_creates_mirror_in_self_channel(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"other-chan": {"id": "other-chan",
                                              "name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)

        original, mirror = mm.posted[0], mm.posted[1]
        self.assertEqual(original["channel_id"], "other-chan")
        self.assertEqual(original["message"], "hello")

        self.assertEqual(mirror["channel_id"], "self-chan")
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-slug~_",
        )
        # The mirror is the sender's own-channel echo: marker + the
        # sender's channel id (which equals the channel the mirror lands
        # in) → daemon drops it on the sender's own session. The session
        # id rides along for telemetry symmetry with the explicit-
        # agentcom marker.
        self.assertEqual(
            mirror["props"],
            {
                "from_bridge_cli": "cross-post-mirror",
                "from_bridge_cli_channel": "self-chan",
                "from_bridge_cli_session": "my-sess",
            },
        )
        self.assertEqual(mirror["file_ids"], [])
        self.assertIsNone(mirror["root_id"])

    def test_cross_channel_original_carries_sender_channel_id(self) -> None:
        """Cross-channel agentcom: when ``--channel <other>`` is given from
        inside a bridge session, the original post carries the ``"post"``
        marker, ``from_bridge_cli_target="explicit"``, and the SENDER's own
        channel and session ids. The dispatcher ALWAYS forwards explicit-target
        posts — this is the agentcom path. Today's bug (post id
        ``tc8ssq5j7jdr3y18qgu9t5nmuw``) was the bridge predicate dropping these
        via channel equality after RC1 leaked the parent's session id; keying
        on the explicit intent makes that class of silent drop impossible."""
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "other-chan")
        self.assertEqual(
            mm.posted[0]["props"],
            {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "explicit",
                "from_bridge_cli_channel": "self-chan",
                "from_bridge_cli_session": "my-sess",
            },
        )

    def test_cli_post_stamps_sender_session_id(self) -> None:
        """Spec test 6: every CLI-authored post made from inside a
        bridge session carries ``from_bridge_cli_session`` alongside
        ``from_bridge_cli_channel``. The session id is the bridge's
        canonical identifier; sender-side resolvers can confuse the
        channel (RC1) but never the sidecar-keyed session id."""
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"target-chan": {"name": "target-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "target-chan", "hi"],
        )
        self.assertEqual(rc, 0)
        # Original post props — outbound to recipient channel (explicit
        # ``--channel`` → target="explicit", always forwarded).
        self.assertEqual(
            mm.posted[0]["props"],
            {
                "from_bridge_cli": "post",
                "from_bridge_cli_target": "explicit",
                "from_bridge_cli_channel": "self-chan",
                "from_bridge_cli_session": "my-sess",
            },
        )
        # Mirror props — sender's own-channel transcript record.
        self.assertEqual(
            mm.posted[1]["props"],
            {
                "from_bridge_cli": "cross-post-mirror",
                "from_bridge_cli_channel": "self-chan",
                "from_bridge_cli_session": "my-sess",
            },
        )

    def test_post_without_session_omits_marker(self) -> None:
        """A `mm-bridge post --channel <X>` call from a shell that is
        NOT inside a bridge session has no own-channel echo to
        suppress, so the CLI must omit the marker entirely. The daemon
        then forwards the post normally to whatever session is linked
        to <X>."""
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "explicit-chan", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(mm.posted[0]["props"])

    def test_mirror_falls_back_to_channel_id_when_get_channel_raises(
        self,
    ) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(get_channel_raises=RuntimeError("boom"))
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        mirror = mm.posted[1]
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-chan~_",
        )

    def test_mirror_includes_attachment_count_but_no_file_ids(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        f1 = Path(self.tmp.name) / "a.txt"
        f1.write_text("x")
        f2 = Path(self.tmp.name) / "b.txt"
        f2.write_text("y")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm,
            [
                "mm-bridge", "post", "--channel", "other-chan",
                "--file", str(f1), "--file", str(f2), "hello",
            ],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        original, mirror = mm.posted[0], mm.posted[1]
        self.assertEqual(original["file_ids"], ["f-1", "f-2"])
        self.assertEqual(
            mirror["message"],
            "hello\n\n_→ also sent to ~other-slug~ with 2 attachment(s)_",
        )
        self.assertEqual(mirror["file_ids"], [])
        # No re-upload: still only 2 uploads total.
        self.assertEqual(len(mm.uploaded), 2)

    def test_no_mirror_when_channel_equals_self_id(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM(channels={"self-chan": {"name": "self-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "self-chan", "hi"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)
        # Explicit `--channel <own channel>` is the accepted gap: still tagged
        # "explicit" (→ the bridge forwards it), NOT "self".
        self.assertEqual(mm.posted[0]["props"]["from_bridge_cli_target"], "explicit")

    def test_no_mirror_when_no_channel_flag(self) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan")
        mm = FakeMM()
        rc, _, _ = self._invoke(mm, ["mm-bridge", "post", "hi"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)
        self.assertEqual(mm.posted[0]["channel_id"], "self-chan")

    def test_mirror_lands_in_senders_thread_when_session_is_thread_forked(
        self,
    ) -> None:
        sidecar.write(self.sdir, "my-sess", "self-chan", "self-root")
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "other-chan", "hello"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 2)
        original, mirror = mm.posted[0], mm.posted[1]
        # The cross-channel original is unaffected by the sender's own
        # thread — it goes to other-chan at channel level (no --thread).
        self.assertEqual(original["channel_id"], "other-chan")
        self.assertIsNone(original["root_id"])
        # The mirror lands inside the sender's own thread so it shows up
        # in the same scrollback the human is watching.
        self.assertEqual(mirror["channel_id"], "self-chan")
        self.assertEqual(mirror["root_id"], "self-root")

    def test_no_mirror_when_no_sidecar(self) -> None:
        mm = FakeMM(channels={"other-chan": {"name": "other-slug"}})
        rc, _, _ = self._invoke(
            mm,
            ["mm-bridge", "post", "--channel", "other-chan", "hi"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(mm.posted), 1)
        self.assertEqual(mm.posted[0]["channel_id"], "other-chan")


class ChannelAndPermalinkRefPostTests(PostCommandTests):
    """``mm-bridge post --channel`` / ``--thread`` accept a slug, channel
    URL, or permalink — the post must land in the RESOLVED channel id (and,
    for permalinks, the post's thread root), never in the raw slug/URL."""

    HOST = "mm.example"
    ID26 = "a1b2c3a1b2c3a1b2c3a1b2c3a1"  # 26 × [a-z0-9] (not 22/27/32)

    def setUp(self) -> None:
        super().setUp()
        self.cfg.mm_url = self.HOST

    # -- slug -----------------------------------------------------------

    def test_slug_posts_to_resolved_id(self) -> None:
        mm = FakeMM(named_channels={"general": {"id": "chan-general"}})
        rc, out, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "general", "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-general")
        self.assertEqual(mm.posted[0]["message"], "hello")
        self.assertFalse(mm.posted[0]["root_id"])
        self.assertIn(("get_channel_by_name", "workspace", "general"), mm.lookups)

    def test_channel_url_posts_to_resolved_id(self) -> None:
        mm = FakeMM(named_channels={"general": {"id": "chan-general"}})
        url = f"https://{self.HOST}/tinkertank/channels/general"
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-general")
        self.assertIn(("get_channel_by_name", "tinkertank", "general"), mm.lookups)

    def test_url_with_trailing_slash_and_query_resolves(self) -> None:
        mm = FakeMM(named_channels={"general": {"id": "chan-general"}})
        url = f"https://{self.HOST}/team/channels/general?x=1#anchor"
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-general")

    # -- id fast path ----------------------------------------------------

    def test_26_char_id_used_verbatim_without_lookup(self) -> None:
        mm = FakeMM()
        rc, _, _ = self._invoke(
            mm, ["mm-bridge", "post", "--channel", self.ID26, "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], self.ID26)
        for kind, *rest in mm.lookups:
            self.assertNotIn(kind, ("get_channel_by_name", "get_post"))

    # -- permalink -------------------------------------------------------

    def test_permalink_posts_to_post_channel_and_root(self) -> None:
        mm = FakeMM(post_by_id={"p1": {
            "id": "p1", "channel_id": "chan-link", "root_id": "root1",
        }})
        url = f"https://{self.HOST}/team/pl/p1"
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "in the thread"],
            session_id=None,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-link")
        self.assertEqual(mm.posted[0]["root_id"], "root1")

    def test_permalink_on_a_root_post_replies_in_its_thread(self) -> None:
        mm = FakeMM(post_by_id={"rootx": {
            "id": "rootx", "channel_id": "chan-link", "root_id": "",
        }})
        url = f"https://{self.HOST}/team/pl/rootx"
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "level"],
            session_id=None,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-link")
        # A root post's permalink names the thread it anchors, so the post
        # replies INSIDE that thread (root_id = the post itself).
        self.assertEqual(mm.posted[0]["root_id"], "rootx")
        self.assertIn(("get_post", "rootx"), mm.lookups)

    def test_permalink_with_explicit_thread_prefers_thread(self) -> None:
        mm = FakeMM(post_by_id={"p1": {
            "id": "p1", "channel_id": "chan-link", "root_id": "root1",
        }})
        url = f"https://{self.HOST}/team/pl/p1"
        rc, _, _ = self._invoke(
            mm, [
                "mm-bridge", "post", "--channel", url,
                "--thread", "explicit-root", "hi",
            ],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "chan-link")
        self.assertEqual(mm.posted[0]["root_id"], "explicit-root")

    # -- --thread permalinks ---------------------------------------------

    def test_thread_permalink_resolves_root(self) -> None:
        mm = FakeMM(post_by_id={"p1": {
            "id": "p1", "channel_id": "chan-x", "root_id": "thread-root",
        }})
        url = f"https://{self.HOST}/team/pl/p1"
        rc, _, _ = self._invoke(
            mm, [
                "mm-bridge", "post", "--channel", "c1", "--thread", url, "hi",
            ],
            session_id=None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(mm.posted[0]["channel_id"], "c1")
        self.assertEqual(mm.posted[0]["root_id"], "thread-root")

    def test_thread_permalink_foreign_host_refused(self) -> None:
        mm = FakeMM(post_by_id={"p1": {
            "id": "p1", "channel_id": "chan-x", "root_id": "thread-root",
        }})
        url = f"https://other.example/team/pl/p1"
        rc, _, err = self._invoke(
            mm, [
                "mm-bridge", "post", "--channel", "c1", "--thread", url, "hi",
            ],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("other.example", err)
        self.assertEqual(mm.posted, [])

    # -- error paths -----------------------------------------------------

    def test_channel_url_foreign_host_refused_no_post(self) -> None:
        mm = FakeMM(named_channels={"general": {"id": "chan-general"}})
        url = f"https://other.example/team/channels/general"
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("other.example", err)
        self.assertIn(self.HOST, err)
        self.assertEqual(mm.posted, [])

    def test_unknown_slug_nothing_posted_membership_hint(self) -> None:
        mm = FakeMM(lookup_404=True)
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", "ghost-chan", "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("ghost-chan", err)
        self.assertIn("member", err.lower())
        self.assertEqual(mm.posted, [])

    def test_unknown_permalink_nothing_posted(self) -> None:
        mm = FakeMM(lookup_404=True)
        url = f"https://{self.HOST}/team/pl/p1"
        rc, _, err = self._invoke(
            mm, ["mm-bridge", "post", "--channel", url, "hello"],
            session_id=None,
        )
        self.assertEqual(rc, 2)
        self.assertIn("p1", err)
        self.assertEqual(mm.posted, [])


if __name__ == "__main__":
    unittest.main()
