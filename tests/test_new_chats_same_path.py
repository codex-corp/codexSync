"""D-020: `[semantic] new_chats = "same_path"` puts a chat this machine never
held into `.codex` at the path it has on its own machine, as 0.1 did.

It is opt-in, every item it places says so (`NEW_CHAT_SAME_PATH`), a
catalogue that places the thread elsewhere or cannot be read still refuses,
and `doctor` counts the chats Codex's catalogue does not list, so a chat Codex
never took up is a warning rather than a silence.
"""
from __future__ import annotations

import json
import lzma
from pathlib import Path
import shutil
import sqlite3
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.config import load_config
from codexsync.exceptions import ConfigError
from codexsync.models import NEW_CHATS_VALUES
from codexsync.preflight import _check_session_visibility
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_transfer import (
    IN_PLACE,
    NEW_CHAT_SAME_PATH,
    NEW_CHATS_LAYOUTS,
    PROVEN_LAYOUTS,
    SAME_PATH_LAYOUT_ID,
    TransferAction,
    build_transfer_plan,
    layout_for_new_chats,
    save_transfer_plan,
)
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState, scan_sessions
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements


def _catalogue(by_session, archived=()):
    return ThreadPlacements(PlacementStatus.AVAILABLE, dict(by_session), frozenset(archived))


class SettingTests(unittest.TestCase):
    def test_every_setting_value_has_a_layout(self) -> None:
        self.assertEqual(tuple(NEW_CHATS_LAYOUTS), NEW_CHATS_VALUES)

    def test_the_default_keeps_the_id_plans_had_before(self) -> None:
        self.assertEqual(layout_for_new_chats("keep_in_cloud"), "unproven")
        self.assertEqual(layout_for_new_chats("same_path"), SAME_PATH_LAYOUT_ID)

    def test_the_settings_screen_offers_exactly_these_values(self) -> None:
        # The screen keeps its choices as a literal (the i18n test reads them
        # without Qt), so the two lists are compared here.
        source = (
            Path(__file__).resolve().parents[1] / "src" / "codexsync" / "gui" / "screens" / "settings.py"
        ).read_text(encoding="utf-8")
        literal = "(" + ", ".join(f'"{value}"' for value in NEW_CHATS_VALUES) + ")"
        self.assertIn(f'Field("semantic", "new_chats", "choice", "{NEW_CHATS_VALUES[0]}", {literal})', source)

    def test_same_path_is_not_a_proven_layout(self) -> None:
        # It was seen working under 0.1, not in the controlled run.
        self.assertNotIn(SAME_PATH_LAYOUT_ID, PROVEN_LAYOUTS)


class SamePathPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"same-path-{uuid.uuid4().hex}"
        self.local_root = self.root / "local"
        self.remote_root = self.root / "remote"
        self.local_root.mkdir(parents=True)
        self.remote_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _branch(self, root: Path, relative: str, lines: list[str]) -> None:
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"".join(json.dumps({"r": line}).encode("utf-8") + b"\n" for line in lines)
        if relative.endswith(".xz"):
            payload = lzma.compress(payload)
        path.write_bytes(payload)

    def _descriptor(self, relative: str, lines: int, *, state=SessionState.ACTIVE, session_id="s1"):
        return SessionDescriptor(session_id, state, relative, "0" * 64, 0, lines)

    def _plan(self, local, remote, *, layout_id=SAME_PATH_LAYOUT_ID, **kwargs):
        return build_transfer_plan(
            SessionCatalog(list(local), {}), SessionCatalog(list(remote), {}),
            local_root=self.local_root, remote_root=self.remote_root,
            source_machine="desktop", target_machine="laptop", layout_id=layout_id, **kwargs,
        )

    def _new_chat(self, remote_path="sessions/2026/09/27/a.jsonl.xz", *, state=SessionState.ACTIVE, **kwargs):
        self._branch(self.remote_root, remote_path, ["1", "2"])
        return self._plan([], [self._descriptor(remote_path, 2, state=state)], **kwargs).items[0]

    def test_a_new_chat_goes_to_its_source_path_in_plain_jsonl(self) -> None:
        item = self._new_chat(placements=_catalogue({}))
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)
        self.assertEqual(item.target_relative_path, "sessions/2026/09/27/a.jsonl")
        self.assertIn(NEW_CHAT_SAME_PATH, item.codes)

    def test_an_archived_chat_goes_to_the_archive(self) -> None:
        item = self._new_chat(
            "archived_sessions/a.jsonl.xz", state=SessionState.ARCHIVED, placements=_catalogue({})
        )
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)
        self.assertEqual(item.target_relative_path, "archived_sessions/a.jsonl")

    def test_a_machine_without_a_catalogue_takes_it(self) -> None:
        # A Codex that never ran here builds its catalogue from the files.
        item = self._new_chat(placements=ThreadPlacements(PlacementStatus.ABSENT, {}))
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)

    def test_an_unreadable_catalogue_refuses(self) -> None:
        item = self._new_chat(
            placements=ThreadPlacements(PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",))
        )
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND)
        self.assertIn("CATALOG_UNREADABLE", item.codes)
        self.assertNotIn(NEW_CHAT_SAME_PATH, item.codes)

    def test_a_catalogue_placing_the_thread_elsewhere_refuses(self) -> None:
        item = self._new_chat(placements=_catalogue({"s1": "sessions/2026/01/01/a.jsonl"}))
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND)
        self.assertIn("CATALOG_PLACES_ELSEWHERE", item.codes)

    def test_a_file_already_at_the_target_is_not_overwritten(self) -> None:
        self._branch(self.local_root, "sessions/2026/09/27/a.jsonl", ["someone else's"])
        item = self._new_chat(placements=_catalogue({}))
        self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn("DESTINATION_OCCUPIED", item.codes)

    def test_without_the_setting_a_new_chat_stays_in_the_cloud(self) -> None:
        item = self._new_chat(layout_id="unproven", placements=_catalogue({}))
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIsNone(item.target_relative_path)

    def test_a_chat_both_machines_hold_is_still_written_in_place(self) -> None:
        # The rendered path would be the mirror's date folder; in-place keeps
        # the file this machine and its catalogue already use.
        self._branch(self.local_root, "sessions/2026/09/27/a.jsonl", ["1"])
        self._branch(self.remote_root, "sessions/2026/09/26/a.jsonl.xz", ["1", "2"])
        item = self._plan(
            [self._descriptor("sessions/2026/09/27/a.jsonl", 1)],
            [self._descriptor("sessions/2026/09/26/a.jsonl.xz", 2)],
            placements=_catalogue({"s1": "sessions/2026/09/27/a.jsonl"}),
        ).items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "sessions/2026/09/27/a.jsonl")
        self.assertIn(IN_PLACE, item.codes)
        self.assertNotIn(NEW_CHAT_SAME_PATH, item.codes)

    def test_the_setting_is_part_of_the_plan_id(self) -> None:
        self._branch(self.remote_root, "sessions/a.jsonl", ["1"])
        remote = [self._descriptor("sessions/a.jsonl", 1)]
        same = self._plan([], remote, placements=_catalogue({})).plan_id
        kept = self._plan([], remote, layout_id="unproven", placements=_catalogue({})).plan_id
        self.assertNotEqual(same, kept)


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class SamePathApplyTests(unittest.TestCase):
    """The whole envelope: config setting, scan, apply, and doctor's count."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"same-path-apply-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        self.local_dir.mkdir(parents=True)
        self.cloud_dir.mkdir(parents=True)
        self.plan_path = self.root / "plan.json"
        self.config_path = self.root / "config.toml"
        self._write_config('new_chats = "same_path"')

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_config(self, semantic_line: str) -> None:
        self.config_path.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-b"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            local_state_dir = "{self.local_dir.as_posix()}"
            cloud_root_dir = "{self.cloud_dir.as_posix()}"
            backup_dir = "{(self.root / 'backups').as_posix()}"
            temp_dir = "{(self.root / '.tmp').as_posix()}"

            [guardian]
            root_dir = "{(self.root / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(self.root / 'semantic').as_posix()}"
            {semantic_line}

            [targets]
            include_roots = ["sessions"]

            [backup]
            backup_before_overwrite = true
            compression = "none"

            [state]
            manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
            """).strip() + "\n", encoding="utf-8")

    def _mirror_branch(self, relative: str, session_id: str) -> bytes:
        rows = [{"type": "session_meta", "payload": {"id": session_id}}, {"type": "event", "record": "one"}]
        payload = b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows)
        path = self.cloud_dir / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(payload))
        return payload

    def _thread_catalogue(self, rows: dict[str, Path]) -> None:
        connection = sqlite3.connect(self.local_dir / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, 0)", [(thread, str(path)) for thread, path in rows.items()]
            )
            connection.commit()
        finally:
            connection.close()

    def _scan(self):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            plan = scan_session_transfer(self.config_path, source_machine="desktop", target_machine="laptop")
        save_transfer_plan(plan, self.plan_path)
        return plan

    def _apply(self, plan) -> int:
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return apply_session_transfer(self.config_path, plan_path=self.plan_path, confirm_plan=plan.plan_id)

    def test_a_new_chat_lands_in_codex_and_doctor_counts_it_until_codex_lists_it(self) -> None:
        relative = "sessions/2026/09/27/rollout-s2.jsonl"
        payload = self._mirror_branch(relative + ".xz", "s2")
        self._thread_catalogue({})

        plan = self._scan()
        self.assertEqual(plan.layout_id, SAME_PATH_LAYOUT_ID)
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)
        self.assertIn(NEW_CHAT_SAME_PATH, item.codes)

        self.assertEqual(self._apply(plan), 1)
        written = self.local_dir / Path(*relative.split("/"))
        self.assertEqual(written.read_bytes(), payload, "plain JSONL, never the mirror's container")
        self.assertEqual(sorted(path.name for path in written.parent.iterdir()), ["rollout-s2.jsonl"])

        catalog = scan_sessions(self.local_dir, volatile=True)
        before = _check_session_visibility(self.local_dir, catalog)
        self.assertEqual(before.status, "WARN")
        self.assertIn("not_listed=1", before.details)

        # What Codex does when it takes the file up.
        self._thread_catalogue({"s2": written})
        after = _check_session_visibility(self.local_dir, catalog)
        self.assertEqual(after.status, "PASS", after.details)

        # The next scan agrees with what is on disk: nothing left to write.
        rescan = self._scan()
        self.assertEqual(rescan.items[0].action, TransferAction.NOOP)

    def test_the_default_leaves_it_in_the_cloud(self) -> None:
        self._write_config("")
        self._mirror_branch("sessions/2026/09/27/rollout-s2.jsonl.xz", "s2")
        self._thread_catalogue({})
        plan = self._scan()
        self.assertEqual(plan.items[0].action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertEqual(self._apply(plan), 0)
        self.assertFalse((self.local_dir / "sessions").exists())

    def test_an_unknown_value_is_a_config_error(self) -> None:
        self._write_config('new_chats = "everywhere"')
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_no_catalogue_is_not_a_warning(self) -> None:
        catalog = scan_sessions(self.local_dir, volatile=True)
        self.assertEqual(_check_session_visibility(self.local_dir, catalog).status, "PASS")


if __name__ == "__main__":
    unittest.main()
