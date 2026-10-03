"""Every setting the shipped config has can be set in the window.

The owner's rule (2026-10-03): whatever the code adds, and whatever a message
points to, must exist in the window and be settable there. The window had a
note sending people to "Settings → New chats from another machine" while that
field sat unnoticed among a dozen others, and `backup.backup_before_overwrite`
had no place in the window at all. This walks the packaged template and fails
on a key no page edits, unless it is listed below with the reason it is not a
setting.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib
import unittest

_HAS_QT = importlib.util.find_spec("PySide6") is not None

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "codexsync" / "config.example.toml"

#: Keys of the template no page edits, each with why it is not a setting.
NOT_SETTINGS = {
    # Statements about what the user is responsible for, not behaviour.
    "assumptions.strict_handoff_required": "a statement, not a setting",
    "assumptions.cloud_readiness_is_user_responsibility": "a statement, not a setting",
    "assumptions.cloud_capacity_is_user_responsibility": "a statement, not a setting",
    # 0.1's termination switches: codexSync never stops Codex, and a config
    # that turns them on is refused (exit 4), so there is nothing to choose.
    "process_detection.allow_terminate_if_running": "legacy, may only be false",
    "process_detection.manual_terminate_confirmation": "legacy, unused",
    "process_detection.terminate_confirmation_mode": "legacy, unused",
    "process_detection.terminate_timeout_seconds": "legacy, unused",
    # CS-335, the owner: the handoff folder is not a setting ("we synced with
    # the cloud -- there is the folder"); the line is an optional override.
    "handoff.root_dir": "CS-335: not a setting by the owner's decision",
}


def _keys(table: dict, prefix: str = ""):
    for key, value in table.items():
        if isinstance(value, dict):
            yield from _keys(value, f"{prefix}{key}.")
        elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            # An array of tables (`[[path_mappings]]`) is edited as a list.
            yield f"{prefix}{key}"
        else:
            yield f"{prefix}{key}"


@unittest.skipUnless(_HAS_QT, "PySide6 is not installed")
class TemplateKeysHaveAPlaceTests(unittest.TestCase):
    def _editable(self) -> set[str]:
        from codexsync.gui.screens import automation
        from codexsync.gui.screens.settings import TABS

        fields = [spec for _tab, specs in TABS for spec in specs]
        for name in ("PERIODIC_FIELDS", "LOGIN_FIELDS", "BACKUP_FIELDS", "HANDOFF_FIELDS"):
            fields.extend(getattr(automation, name))
        editable = {f"{spec.section}.{spec.key}" for spec in fields}
        # The "Project paths" tab edits the rules as a list.
        editable.add("path_mappings")
        return editable

    def test_every_template_key_is_edited_somewhere_or_says_why_not(self) -> None:
        template = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))
        missing = sorted(set(_keys(template)) - self._editable() - set(NOT_SETTINGS))
        self.assertEqual(missing, [], "add a field for these, or say in NOT_SETTINGS why they are not settings")

    def test_the_exceptions_are_still_in_the_template(self) -> None:
        # An exception for a key that is gone hides nothing, and would hide
        # the next key of that name.
        template = set(_keys(tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))))
        self.assertEqual(sorted(set(NOT_SETTINGS) - template), [])

    def test_new_chats_is_first_on_the_sync_tab(self) -> None:
        # Messages on three pages send people to it.
        from codexsync.gui.screens.settings import TABS

        sync = dict(TABS)["sync"]
        self.assertEqual((sync[0].section, sync[0].key, sync[0].default), ("semantic", "new_chats", "same_path"))


if __name__ == "__main__":
    unittest.main()
