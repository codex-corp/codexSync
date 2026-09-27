"""The Home page's numbers (CS-275), read without Qt.

Three properties are checked: the sync counters come from the journals and
only count what the journals prove (a failed run moved nothing); one part that
cannot be read costs its own tile and nothing else; and the cache of expensive
counts holds counts only, per config, and is ignored rather than trusted when
it is not what this version writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.chat_directory import Association, ChatDirectory, ChatEntry, ChatKind
from codexsync.config_edit import create_config
from codexsync.home_stats import (
    StateStats,
    _sync_stats,
    read_home_summary,
    read_state_stats,
    state_stats_from_directory,
    write_state_stats,
)
from codexsync.recovery import JournalInfo
from codexsync.session_catalog import SessionState

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _journal(created: str, state: str, counts=None, family: str = "sync", readable: bool = True) -> JournalInfo:
    terminal = state in ("COMMITTED", "FAILED", "ABORTED")
    return JournalInfo(
        f"op-{created}", family, state, created, 1, None, terminal, readable, False, False, None,
        counts=counts,
    )


def _chat(session_id: str, *, kind=ChatKind.TOP_LEVEL, association=Association.BOUND,
          state=SessionState.ACTIVE, size=100) -> ChatEntry:
    return ChatEntry(session_id, f"sessions/{session_id}.jsonl", state, kind, association,
                     "p1" if association is not Association.NONE else None, None, None, 3, byte_count=size)


class SyncStatsTests(unittest.TestCase):
    def test_counts_cover_thirty_days_and_only_committed_runs_moved_files(self) -> None:
        journals = [
            _journal("2026-09-24T10:00:00Z", "COMMITTED", {"to_cloud": 3, "to_local": 1}),
            _journal("2026-09-20T10:00:00Z", "FAILED", {"to_cloud": 50, "to_local": 50}),
            _journal("2026-09-10T10:00:00Z", "COMMITTED", {"to_cloud": 2, "to_local": 0}),
            _journal("2026-07-01T10:00:00Z", "COMMITTED", {"to_cloud": 99, "to_local": 99}),
            _journal("2026-09-24T11:00:00Z", "COMMITTED", {"to_cloud": 7}, family="sessions"),
        ]
        stats = _sync_stats(journals, NOW)
        self.assertEqual((stats.runs, stats.failed, stats.to_cloud, stats.to_local), (3, 1, 5, 1))
        self.assertEqual(stats.last.created_at_utc, "2026-09-24T10:00:00Z")

    def test_a_journal_from_before_counts_existed_counts_as_a_run(self) -> None:
        stats = _sync_stats([_journal("2026-09-24T10:00:00Z", "COMMITTED", None)], NOW)
        self.assertEqual((stats.runs, stats.to_cloud), (1, 0))


class StateStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"home-stats-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        self.config.write_text("", encoding="utf-8")

    def test_counts_come_from_the_chat_directory(self) -> None:
        directory = ChatDirectory(
            projects={"p1": object(), "p2": object()},
            chats=(
                _chat("a"), _chat("b", association=Association.NONE),
                _chat("c", association=Association.DERIVED_VIA_MAPPING, state=SessionState.ARCHIVED),
                _chat("d", kind=ChatKind.SUB_THREAD, size=50),
            ),
            schema_id="electron-v2",
        )
        stats = state_stats_from_directory(directory, now=NOW)
        self.assertEqual(
            (stats.chats, stats.sub_threads, stats.archived, stats.projects,
             stats.chats_without_project, stats.chats_via_mapping, stats.session_bytes),
            (3, 1, 1, 2, 1, 1, 350),
        )

    def test_the_cache_round_trips_counts_only_per_config(self) -> None:
        stats = StateStats("2026-09-25T12:00:00Z", 152, 98, 23, 16, 2, 0, 900)
        self.assertTrue(write_state_stats(self.config, stats, cache_root=self.root / "cache"))
        self.assertEqual(read_state_stats(self.config, cache_root=self.root / "cache"), stats)
        other = self.root / "other.toml"
        other.write_text("", encoding="utf-8")
        self.assertIsNone(read_state_stats(other, cache_root=self.root / "cache"))
        (cached,) = (self.root / "cache").iterdir()
        payload = json.loads(cached.read_text(encoding="utf-8"))
        self.assertEqual(set(payload) - {"format"}, set(StateStats.__dataclass_fields__))
        self.assertNotIn(str(self.root), cached.read_text(encoding="utf-8"), "no path is kept")

    def test_a_cache_this_version_did_not_write_is_ignored(self) -> None:
        stats = StateStats("2026-09-25T12:00:00Z", 1, 0, 0, 1, 0, 0, 1)
        write_state_stats(self.config, stats, cache_root=self.root / "cache")
        (cached,) = (self.root / "cache").iterdir()
        for broken in ('{"format": "other"}', "not json", json.dumps({"format": "codexsync-home-stats-v1", "chats": -1})):
            with self.subTest(broken=broken):
                cached.write_text(broken, encoding="utf-8")
                self.assertIsNone(read_state_stats(self.config, cache_root=self.root / "cache"))


class HomeSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"home-summary-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(self.config, machine_id="laptop", local_state_dir=str(self.root / "codex"),
                      workspace_root_dir=str(self.root / "workspace"))

    def test_one_part_failing_costs_only_that_part(self) -> None:
        with mock.patch("codexsync.home_stats._codex_state", return_value="stopped"), \
                mock.patch("codexsync.home_stats.read_guardian_inventory", side_effect=RuntimeError("broken store")):
            summary = read_home_summary(
                self.config, now=lambda: NOW, cache_root=self.root / "cache",
                automation=lambda _path: None,
            )
        self.assertIsNone(summary.guardian)
        self.assertIn("broken store", summary.errors["guardian"])
        self.assertEqual(summary.codex, "stopped")
        self.assertIsNotNone(summary.sync)
        self.assertEqual(summary.open_journals, 0)
        self.assertFalse(summary.copies_configured)
        self.assertIsNone(summary.state)

    def test_it_writes_nothing(self) -> None:
        before = sorted(self.root.rglob("*"))
        with mock.patch("codexsync.home_stats._codex_state", return_value="unknown"):
            read_home_summary(self.config, now=lambda: NOW, cache_root=self.root / "cache",
                              automation=lambda _path: None)
        self.assertEqual(sorted(self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
