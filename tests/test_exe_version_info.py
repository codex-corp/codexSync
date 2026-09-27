"""The exes' version resource (CS-273), checked without PyInstaller.

Every value must come from somewhere that already exists -- the package
metadata `pyproject.toml` writes, and the window's language files -- so these
tests feed the generator a metadata record and compare what it says with it.
Whether the built exe carries the resource is checked by
`scripts/check_exe_version.py` against a real build, not here: CI has no
PyInstaller, and half of it is not Windows.
"""
from __future__ import annotations

from email.message import Message
from importlib.metadata import PackageNotFoundError
import importlib.util
import json
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("exe_version_info", REPO_ROOT / "scripts" / "exe_version_info.py")
assert _spec is not None and _spec.loader is not None
evi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evi)


def _meta(version: str = "0.2.0", author: str | None = "Fyodor Malkov", email: str | None = None) -> Message:
    meta = Message()
    meta["Name"] = "codexsync"
    meta["Version"] = version
    if author is not None:
        meta["Author"] = author
    if email is not None:
        meta["Author-email"] = email
    meta["Project-URL"] = "Repository, https://example.invalid/repo"
    meta["Project-URL"] = "Homepage, https://github.com/owner/codexSync"
    return meta


class NumericVersionTests(unittest.TestCase):
    def test_a_release_gets_a_fourth_zero(self) -> None:
        self.assertEqual(evi.numeric_version("0.2.0"), (0, 2, 0, 0))

    def test_a_suffix_keeps_its_leading_numbers(self) -> None:
        self.assertEqual(evi.numeric_version("0.2.0.dev3"), (0, 2, 0, 0))
        self.assertEqual(evi.numeric_version("0.0.0+unknown"), (0, 0, 0, 0))
        self.assertEqual(evi.numeric_version("1.2"), (1, 2, 0, 0))

    def test_a_version_the_resource_cannot_hold_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            evi.numeric_version("unknown")
        with self.assertRaises(ValueError):
            evi.numeric_version("70000.0")


class VersionFieldsTests(unittest.TestCase):
    def test_every_field_comes_from_the_metadata(self) -> None:
        fields = evi.version_fields("window", meta=_meta("1.4.2"), year=2026)
        self.assertEqual(fields["numeric"], (1, 4, 2, 0))
        english = fields["tables"]["en"]
        self.assertEqual(english["FileVersion"], "1.4.2")
        self.assertEqual(english["ProductVersion"], "1.4.2")
        self.assertEqual(english["ProductName"], "CodexSync")
        self.assertEqual(english["OriginalFilename"], "codexsync-gui.exe")
        self.assertEqual(english["LegalCopyright"], "© 2026 Fyodor Malkov — https://github.com/owner/codexSync")

    def test_the_two_builds_are_told_apart(self) -> None:
        window = evi.version_fields("window", meta=_meta(), year=2026)["tables"]["en"]
        console = evi.version_fields("console", meta=_meta(), year=2026)["tables"]["en"]
        self.assertEqual(console["OriginalFilename"], "codexsync.exe")
        self.assertEqual(console["ProductName"], "codexsync")
        self.assertNotEqual(window["FileDescription"], console["FileDescription"])

    def test_no_company_is_invented(self) -> None:
        for table in evi.version_fields("console", meta=_meta(), year=2026)["tables"].values():
            self.assertNotIn("CompanyName", table)

    def test_one_table_per_window_language_with_its_own_description(self) -> None:
        fields = evi.version_fields("window", meta=_meta(), year=2026)
        languages = sorted(path.stem for path in evi.LOCALE_DIR.glob("*.json"))
        self.assertEqual(sorted(fields["tables"]), languages)
        self.assertEqual(list(fields["tables"])[0], "en")
        for language, table in fields["tables"].items():
            expected = json.loads((evi.LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
            self.assertEqual(table["FileDescription"], expected["exe.description.window"])
        self.assertEqual(
            fields["translations"],
            [(evi.LANGUAGE_IDS[language], evi.UNICODE_CODEPAGE) for language in fields["tables"]],
        )

    def test_the_copyright_runs_to_the_build_year(self) -> None:
        later = evi.version_fields("console", meta=_meta(), year=2028)["tables"]["en"]["LegalCopyright"]
        self.assertTrue(later.startswith("© 2026-2028 Fyodor Malkov"))

    def test_an_author_given_with_an_email_keeps_only_the_name(self) -> None:
        meta = _meta(author=None, email="Fyodor Malkov <owner@example.invalid>")
        line = evi.version_fields("console", meta=meta, year=2026)["tables"]["en"]["LegalCopyright"]
        self.assertIn("Fyodor Malkov —", line)
        self.assertNotIn("@", line)

    def test_metadata_without_an_author_is_refused_rather_than_left_blank(self) -> None:
        with self.assertRaises(ValueError):
            evi.version_fields("console", meta=_meta(author=None), year=2026)

    def test_an_unknown_build_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            evi.version_fields("service", meta=_meta(), year=2026)

    def test_the_installed_metadata_is_enough(self) -> None:
        # The real source, as the spec files read it: an author and a homepage
        # must be in pyproject.toml, or the build would fail here first.
        try:
            fields = evi.version_fields("window")
        except PackageNotFoundError:
            self.skipTest("codexsync is not installed; there is no metadata to read")
        self.assertTrue(fields["tables"]["en"]["LegalCopyright"].startswith("© "))


class SpecFilesTests(unittest.TestCase):
    def test_both_spec_files_stamp_the_resource(self) -> None:
        for name, kind in (("codexsync.spec", "console"), ("codexsync-gui.spec", "window")):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            self.assertIn(f'version=build_version_info("{kind}")', text, name)


if __name__ == "__main__":
    unittest.main()
