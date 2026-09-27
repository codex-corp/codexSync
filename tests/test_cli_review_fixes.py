"""Exit codes and input handling from the 2026-09-27 review (CS-315, CS-317, CS-325).

`AI_RULES` gives each exit code one meaning, and a scheduled task or a script
acts on nothing else: 2 is "both sides changed, decide", 4 is "your input is
wrong". Each test here is a case where the code said the other one, or said
nothing and exited 1 with a traceback.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import _observe_global_state, move_chats
from codexsync.cli import build_parser, main
from codexsync.config import load_config
from codexsync.exceptions import ConfigError, FailSafeError
from codexsync.semantic_transfer import TransferAction
from codexsync.stable_reader import SourceTooLargeError, SourceUnstableError, StableReader

try:
    from tests.test_recovery import _write_config
except ImportError:  # collected with tests/ itself on sys.path
    from test_recovery import _write_config


def _exit_code(argv: list[str]) -> int:
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        try:
            return main(argv)
        except SystemExit as exc:
            return exc.code  # type: ignore[return-value]


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"cli-review-{uuid.uuid4().hex}"
        (self.root / "local-state").mkdir(parents=True)
        (self.root / "cloud").mkdir(parents=True)
        self.config_path = _write_config(self.root)
        self.addCleanup(shutil.rmtree, self.root, True)


class ArgumentErrorTests(unittest.TestCase):
    """CS-315: argparse's own status 2 means "conflict" here."""

    def test_a_malformed_command_line_exits_four(self) -> None:
        for argv in (["sync", "--bogus"], ["no-such-command"], ["recover", "rollback"], []):
            with self.subTest(argv=argv):
                self.assertEqual(_exit_code(argv), 4)

    def test_help_still_exits_zero(self) -> None:
        self.assertEqual(_exit_code(["--help"]), 0)
        self.assertEqual(_exit_code(["sync", "--help"]), 0)

    def test_a_negative_chat_limit_is_bad_input(self) -> None:
        """`--limit -3` sliced `[:-3]` and silently dropped the last rows."""
        self.assertEqual(_exit_code(["chats", "list", "--limit", "-3"]), 4)
        self.assertEqual(_exit_code(["chats", "list", "--limit", "many"]), 4)

    def test_config_help_names_the_real_search_order(self) -> None:
        text = build_parser().format_help()
        self.assertIn("last opened", " ".join(text.split()))
        self.assertNotIn("per-user location", " ".join(text.split()))


class ValidateTests(_Sandbox):
    def test_a_config_every_mutation_refuses_is_not_valid(self) -> None:
        self.assertEqual(_exit_code(["-c", str(self.config_path), "validate"]), 0)
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            text.replace("backup_before_overwrite = true", "backup_before_overwrite = false"), encoding="utf-8"
        )
        self.assertEqual(_exit_code(["-c", str(self.config_path), "validate"]), 4)

    def test_a_config_without_include_roots_is_valid_but_says_sync_has_nothing(self) -> None:
        """CS-319: a Guardian-only config loads; `validate` says what `sync` will do with it."""
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn('include_roots = ["data"]', text)
        self.config_path.write_text(text.replace('include_roots = ["data"]', ""), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            self.assertEqual(main(["-c", str(self.config_path), "validate"]), 0)
        self.assertIn("targets.include_roots is not set", out.getvalue())
        self.assertIn("Config is valid.", out.getvalue())


def _plan(*actions: TransferAction):
    items = tuple(
        SimpleNamespace(action=action, codes=(), conflict_id=None) for action in actions
    )
    return SimpleNamespace(
        plan_id="p" * 64, volatile=False, layout_id="l", mirror_layout_id="m", canonical_version=1,
        items=items, codes=(), blocked_items=tuple(item for item in items if item.action.is_blocked),
    )


class SessionsScanTests(_Sandbox):
    def _scan(self, *extra: str, plan=None) -> int:
        with patch("codexsync.cli.scan_session_transfer", return_value=plan or _plan()) as scan:
            code = _exit_code([
                "-c", str(self.config_path), "sessions", "scan",
                "--source-machine", "a", "--target-machine", "b", *extra,
            ])
        self.scanned = scan.called
        return code

    def test_only_a_decision_the_apply_refuses_is_a_conflict(self) -> None:
        """Layout- and catalogue-blocked branches are left alone by `sessions apply`."""
        for action in (TransferAction.BLOCKED_UNPROVEN_LAYOUT, TransferAction.BLOCKED_UNSUPPORTED_BACKEND):
            with self.subTest(action=action):
                self.assertEqual(self._scan(plan=_plan(TransferAction.FAST_FORWARD_LOCAL, action)), 0)
        for action in (TransferAction.BLOCKED_CONFLICT, TransferAction.BLOCKED_TARGET_COLLISION):
            with self.subTest(action=action):
                self.assertEqual(self._scan(plan=_plan(TransferAction.BLOCKED_UNPROVEN_LAYOUT, action)), 2)

    def test_a_scope_file_that_is_not_there_refuses_instead_of_dropping_the_set(self) -> None:
        """CS-317: a typo meant an empty working set, and the apply wrote everything."""
        self.assertEqual(self._scan("--scope-file", str(self.root / "no-such-scope.json")), 4)
        self.assertFalse(self.scanned)

    def test_a_scope_file_that_is_not_a_working_set_is_bad_input(self) -> None:
        for name, payload in (("list.json", "[]"), ("text.json", "not json"), ("other.json", '{"format": "x"}')):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(payload, encoding="utf-8")
                self.assertEqual(self._scan("--scope-file", str(path)), 4)
                self.assertFalse(self.scanned)


class GlobalStateReadTests(_Sandbox):
    """CS-325: a missing or unstable global state was a traceback and exit 1."""

    def test_a_chat_move_without_a_global_state_is_a_config_error(self) -> None:
        with patch("codexsync.app._make_safety_gate") as gate:
            gate.return_value.check.return_value = SimpleNamespace(process_state=None)
            with self.assertRaises(ConfigError):
                move_chats(self.config_path, chat_refs=["x"], to_project="p")

    def test_an_unstable_or_oversized_state_maps_to_its_exit_code(self) -> None:
        cfg = load_config(self.config_path)
        local = self.root / "local-state"
        (local / ".codex-global-state.json").write_text(json.dumps({}), encoding="utf-8")
        with patch.object(StableReader, "read_once", side_effect=SourceUnstableError("moving")):
            with self.assertRaises(FailSafeError):
                _observe_global_state(cfg, self.config_path, local)
        with patch.object(StableReader, "read_once", side_effect=SourceTooLargeError("big")):
            with self.assertRaises(ConfigError):
                _observe_global_state(cfg, self.config_path, local)


if __name__ == "__main__":
    unittest.main()
