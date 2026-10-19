# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Gstore macOS app bundle.

Build from the project root:

    .venv/bin/pyinstaller packaging/gstore.spec

Produces ``dist/Gstore.app``. Bundles the packaged data files the app reads at
runtime via importlib.resources — the SQL schema (``gstore/db/*.sql``) and the
OAuth Desktop client (``gstore/auth/client_config.json``). The client file is
gitignored, so this embeds the copy present on the build machine.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Only the resources the app actually reads at runtime; keeps the bundle lean.
datas = collect_data_files("gstore", includes=["**/*.sql", "**/*.json"])
hiddenimports = collect_submodules("gstore")

a = Analysis(
    [os.path.join(SPECPATH, "run_gstore.py")],  # noqa: F821 (SPECPATH is injected)
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PySide6.QtWebEngineCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Gstore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Gstore",
)

app = BUNDLE(
    coll,
    name="Gstore.app",
    icon=None,
    bundle_identifier="com.gstore.app",
    version="0.0.2",
    info_plist={
        "CFBundleName": "Gstore",
        "CFBundleDisplayName": "Gstore",
        "CFBundleShortVersionString": "0.0.2",
        "CFBundleVersion": "0.0.2",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
