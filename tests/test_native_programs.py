"""D-021: compiled programs are never synced or copied, whatever the config.

Codex keeps its own executables inside `.codex` (`plugins/.plugin-appserver/`
held codex.exe and three helpers, 416 MB). A copy is useless on another
platform and replaces another machine's newer version with an older one, and
an installer restores them anyway. The test is the header, since a program on
macOS or Linux usually has no extension.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import textwrap
import unittest
import uuid

from codexsync.config import load_config
from codexsync.native_programs import is_native_program
from codexsync.runtime import _build_indexes
from codexsync.state_backup import select_state_files


def _pe(size: int = 256) -> bytes:
    header = bytearray(size)
    header[:2] = b"MZ"
    header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    header[0x80:0x84] = b"PE\0\0"
    return bytes(header)


class HeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"programs-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _file(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def test_programs_of_every_platform_are_recognised(self) -> None:
        samples = {
            "codex.exe": _pe(),
            "codex": b"\x7fELF\x02\x01\x01" + bytes(64),
            "codex-mac": b"\xcf\xfa\xed\xfe" + bytes(64),
            "codex-universal": b"\xca\xfe\xba\xbe" + bytes(64),
            "addon.node": b"\xfe\xed\xfa\xcf" + bytes(64),
        }
        for name, payload in samples.items():
            self.assertTrue(is_native_program(self._file(name, payload)), name)

    def test_ordinary_files_are_not(self) -> None:
        samples = {
            "SKILL.md": b"# A skill\n",
            "notes.txt": b"MZ is how this note happens to start\n" + bytes(64),
            "record.jsonl": b'{"type":"session_meta"}\n',
            "empty": b"",
            "short": b"MZ",
        }
        for name, payload in samples.items():
            self.assertFalse(is_native_program(self._file(name, payload)), name)

    def test_the_running_interpreter_is_a_program(self) -> None:
        # A real binary of whatever platform the suite runs on.
        self.assertTrue(is_native_program(Path(sys.executable)))

    def test_an_unreadable_file_is_not_called_a_program(self) -> None:
        self.assertFalse(is_native_program(self.root / "missing"))


class LeftOutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"programs-sync-{uuid.uuid4().hex}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        for relative in ("local/plugins/tools", "local/skills/mine", "cloud", "ws"):
            (self.root / relative).mkdir(parents=True)
        (self.local / "plugins" / "tools" / "helper.exe").write_bytes(_pe())
        (self.local / "plugins" / "tools" / "helper").write_bytes(b"\x7fELF" + bytes(64))
        (self.local / "plugins" / "tools" / "plugin.json").write_text("{}", encoding="utf-8")
        (self.local / "skills" / "mine" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
        (self.local / "skills" / "mine" / "tool.exe").write_bytes(_pe())
        self.config = self.root / "config.toml"
        self.config.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-a"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            local_state_dir = "{self.local.as_posix()}"
            cloud_root_dir = "{self.cloud.as_posix()}"
            backup_dir = "{(self.root / 'ws' / 'backups').as_posix()}"
            temp_dir = "{(self.root / 'ws' / '.tmp').as_posix()}"

            [guardian]
            root_dir = "{(self.root / 'ws' / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(self.root / 'ws' / 'semantic').as_posix()}"

            [targets]
            include_roots = ["plugins", "skills"]

            [filters]
            exclude_globs = []

            [state]
            manifest_file = "{(self.root / 'ws' / 'manifest.json').as_posix()}"
            """).strip() + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_sync_never_sees_a_program_even_with_no_exclusions(self) -> None:
        local, cloud = _build_indexes(load_config(self.config), self.local, self.cloud)
        self.assertEqual(sorted(local), ["plugins/tools/plugin.json", "skills/mine/SKILL.md"])
        self.assertEqual(cloud, {})

    def test_a_copy_of_codex_leaves_programs_out(self) -> None:
        chosen = [relative for relative, _ in select_state_files(self.local)]
        self.assertIn("skills/mine/SKILL.md", chosen)
        self.assertNotIn("skills/mine/tool.exe", chosen)


if __name__ == "__main__":
    unittest.main()
