from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.manifest import build_manifest, load_manifest, save_manifest
from codexsync.models import FileMeta


class ManifestTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"manifest-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            manifest_path = case_dir / "manifest.json"
            local = {"sessions/a.json": FileMeta("sessions/a.json", Path("/local/sessions/a.json"), 123, 5)}
            cloud = {"sessions/a.json": FileMeta("sessions/a.json", Path("/cloud/sessions/a.json"), 124, 6)}
            manifest = build_manifest(local, cloud, data_version=1, machine_id="machine-a")
            save_manifest(manifest, manifest_path)

            loaded = load_manifest(manifest_path, data_version=1, machine_id="machine-a")
            self.assertIn("sessions/a.json", loaded.files)
            entry = loaded.files["sessions/a.json"]
            assert entry.local is not None
            assert entry.cloud is not None
            self.assertEqual(entry.local.mtime_ns, 123)
            self.assertEqual(entry.cloud.size, 6)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
