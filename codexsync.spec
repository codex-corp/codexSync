# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import copy_metadata


project_root = Path(SPECPATH).resolve()

# The version resource (CS-273): version, product, description per language and
# copyright, all read from package metadata and the language files -- see
# scripts/exe_version_info.py. Nothing in it is written by hand here.
sys.path.insert(0, str(project_root / "scripts"))
from exe_version_info import build_version_info  # noqa: E402

entrypoint = project_root / "scripts" / "pyinstaller_entrypoint.py"
template = project_root / "src" / "codexsync" / "config.example.toml"


a = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root / "src")],
    binaries=[],
    # `copy_metadata` carries the `dist-info` that `version.py` reads; without
    # it the exe reports `0.0.0+unknown` and stamps that into snapshot
    # manifests. Hardcoding the version instead is forbidden by design.
    # The language files carry no Qt: the handoff watcher reads its
    # notification texts from them (`notifications.py`), in the user's language.
    datas=copy_metadata("codexsync") + [(str(template), "codexsync")] + [
        (str(path), "codexsync/gui/locale")
        for path in sorted((project_root / "src" / "codexsync" / "gui" / "locale").glob("*.json"))
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The CLI exe must never grow a Qt payload just because the build machine
    # happens to have the optional extra installed. Nothing in the CLI import
    # graph reaches PySide6, and this makes that a build-time guarantee rather
    # than a property of whatever was in site-packages that day.
    excludes=["PySide6", "shiboken6", "PySide6.QtCore", "PySide6.QtWidgets", "PySide6.QtGui"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="codexsync",
    version=build_version_info("console"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
