# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Gstore macOS app bundle.

Build from the project root:

    .venv/bin/pyinstaller packaging/gstore.spec      # macOS
    .venv\\Scripts\\pyinstaller packaging/gstore.spec   # Windows

Produces ``dist/Gstore.app`` on macOS and ``dist/Gstore/Gstore.exe`` on Windows.
PyInstaller does not cross-compile: each platform's build must run on that
platform (see .github/workflows/release.yml, which does both). Bundles the packaged data files the app reads at
runtime via importlib.resources — the SQL schema (``gstore/db/*.sql``) and the
OAuth Desktop client (``gstore/auth/client_config.json``). The client file is
gitignored, so this embeds the copy present on the build machine.

The app icon comes from ``assets/Gstore.icns``; run ``packaging/make_icon.py``
to (re)generate it from the logo master.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# App icon, generated from the logo master by packaging/make_icon.py. Built
# separately (the .icns needs macOS sips/iconutil) so a missing file degrades to
# an unbranded build instead of breaking it. Windows wants the .ico.
IS_MAC = sys.platform == "darwin"
ASSETS = os.path.join(SPECPATH, os.pardir, "assets")  # noqa: F821
ICON = os.path.join(ASSETS, "Gstore.icns" if IS_MAC else "Gstore.ico")
ICON = ICON if os.path.isfile(ICON) else None

# Single source of truth: the version lives in gstore/__init__.py. Repeating it
# here meant the bundle could claim a version the app did not report.
sys.path.insert(0, os.path.join(SPECPATH, os.pardir, "src"))  # noqa: F821
from gstore import __version__ as VERSION  # noqa: E402

# Only the resources the app actually reads at runtime; keeps the bundle lean.
datas = collect_data_files("gstore", includes=["**/*.sql", "**/*.json"])
hiddenimports = collect_submodules("gstore")

# certifi, named explicitly rather than left to dependency analysis.
#
# gstore.tls imports it inside a function, guarded by try/except ImportError —
# the shape a bundler is entitled to treat as optional. If its cacert.pem were
# dropped, every stdlib HTTPS call would fail exactly as it did before the fix,
# silently, and only in the packaged build. That is the bug being fixed here,
# so it is not left to inference.
datas += collect_data_files("certifi")
hiddenimports += ["certifi", "gstore.tls"]

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
    console=False,  # GUI app: no terminal window on Windows
    icon=ICON,      # ignored on macOS, where BUNDLE carries the icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Gstore",
)

# BUNDLE is macOS-only. On Windows and Linux the COLLECT directory above is the
# deliverable: dist/Gstore/Gstore.exe next to its libraries.
app = BUNDLE(
    coll,
    name="Gstore.app",
    icon=ICON,
    bundle_identifier="com.gstore.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "Gstore",
        "CFBundleDisplayName": "Gstore",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
) if IS_MAC else None
