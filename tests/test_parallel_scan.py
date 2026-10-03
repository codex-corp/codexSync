"""Reading sessions in parallel changes how long it takes, never what it finds.

A full sync reads `.codex` and the cloud mirror, twice (the plan, then its
rebuild before the write): 9 s + 30 s on the machine this was measured on,
most of the mirror's share unpacking xz. Compressed files are now unpacked by a
pool and both sides are read at once. What has to hold is that the catalogue
is the one a single thread produces -- same descriptors, same order -- and that
the progress a window draws still only goes up and ends at the total.
"""
from __future__ import annotations

import json
import lzma
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync import session_catalog
from codexsync.session_catalog import scan_both_sides, scan_sessions


def _records(session_id: str, lines: int) -> bytes:
    rows = [{"type": "session_meta", "payload": {"id": session_id, "cwd": "C:/work"}}]
    rows += [{"type": "event", "timestamp": f"2026-10-03T00:00:{n:02d}Z", "n": n} for n in range(lines)]
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class ParallelScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"parallel-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.mirror = self.root / "mirror"
        for index in range(7):
            self._write(self.local, f"sessions/2026/10/rollout-l{index}.jsonl", f"l{index}", index + 1)
        for index in range(9):
            # Mostly compressed, as a mirror is, with a plain one in between.
            suffix = ".jsonl" if index == 4 else ".jsonl.xz"
            self._write(self.mirror, f"sessions/2026/10/rollout-m{index}{suffix}", f"m{index}", index + 2)
        self._write(self.mirror, "archived_sessions/rollout-old.jsonl.xz", "old", 3)
        # A container cut short, as a cloud client leaves one mid-copy.
        broken = self.mirror / "sessions" / "2026" / "10" / "rollout-broken.jsonl.xz"
        broken.write_bytes(lzma.compress(_records("broken", 4))[:-12])
        self.reports: list[tuple[str, int, int]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, root: Path, relative: str, session_id: str, lines: int) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _records(session_id, lines)
        path.write_bytes(lzma.compress(payload) if relative.endswith(".xz") else payload)

    def _collect(self, phase: str, done: int, total: int) -> None:
        self.reports.append((phase, done, total))

    def test_the_pool_finds_exactly_what_one_thread_finds_in_the_same_order(self) -> None:
        with patch.object(session_catalog, "SCAN_WORKERS", 1):
            alone = scan_sessions(self.mirror)
        with patch.object(session_catalog, "SCAN_WORKERS", 4):
            pooled = scan_sessions(self.mirror)
        self.assertEqual(pooled.descriptors, alone.descriptors)
        self.assertIn("READ_ERROR", next(d for d in pooled.descriptors if "broken" in d.relative_path).codes)

    def test_pooled_progress_only_goes_up_and_ends_at_the_total(self) -> None:
        with patch.object(session_catalog, "SCAN_WORKERS", 4):
            catalog = scan_sessions(self.mirror, progress=self._collect)
        counts = [done for _phase, done, _total in self.reports]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(self.reports[-1][1:], (len(catalog.descriptors), len(catalog.descriptors)))

    def test_both_sides_at_once_are_each_what_their_own_scan_returns(self) -> None:
        local, remote = scan_both_sides(
            self.local, self.mirror, local_machine="desktop", remote_machine="laptop",
            progress=self._collect,
        )
        self.assertEqual(local.descriptors, scan_sessions(self.local, source_machine="desktop").descriptors)
        self.assertEqual(remote.descriptors, scan_sessions(self.mirror, source_machine="laptop").descriptors)
        # One bar for the pair: a single phase, never going backwards, ending
        # at both totals together.
        self.assertEqual({phase for phase, _d, _t in self.reports}, {"sessions"})
        counts = [done for _phase, done, _total in self.reports]
        self.assertEqual(counts, sorted(counts))
        total = len(local.descriptors) + len(remote.descriptors)
        self.assertEqual(self.reports[-1][1:], (total, total))

    def test_a_failure_on_one_side_is_raised_not_swallowed(self) -> None:
        real = session_catalog._scan_jsonl

        def failing(path, *args, **kwargs):
            if "m3" in path.name:
                raise RuntimeError("disk said no")
            return real(path, *args, **kwargs)

        with patch.object(session_catalog, "_scan_jsonl", side_effect=failing):
            with self.assertRaises(RuntimeError):
                scan_both_sides(self.local, self.mirror, local_machine=None, remote_machine=None)


if __name__ == "__main__":
    unittest.main()
