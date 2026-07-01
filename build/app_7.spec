# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Camins Rals v7 — Windows 64-bit
#
# Run from the repo root with:
#   pyinstaller build/app_7.spec
#
# Expects ms-playwright/ folder next to this spec (copied by the build script).

import os, sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ── Playwright Chromium ───────────────────────────────────────────────────────
# The GitHub Actions workflow copies %LOCALAPPDATA%\ms-playwright here so that
# we can bundle Chromium without it needing to be in any fixed system location.
_here = os.path.dirname(os.path.abspath(SPEC))  # repo root
_pw_src = os.path.join(_here, 'ms-playwright')
playwright_datas = [(_pw_src, 'ms-playwright')] if os.path.isdir(_pw_src) else []
if not playwright_datas:
    print("WARNING: ms-playwright/ not found — export will fail on machines "
          "that don't have Playwright Chromium installed separately.")

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [os.path.join(_here, 'app_7.py')],
    pathex=[_here],
    binaries=[],
    datas=playwright_datas + collect_data_files('matplotlib'),
    hiddenimports=[
        # Qt WebEngine
        'PyQt5.QtWebEngineWidgets',
        'PyQt5.QtWebEngineCore',
        'PyQt5.QtWebChannel',
        'PyQt5.sip',
        # Matplotlib backends
        'matplotlib.backends.backend_qt5agg',
        'matplotlib.backends.backend_agg',
        'matplotlib.figure',
        # App deps
        'gpxpy',
        'gpxpy.gpx',
        'playwright',
        'playwright.sync_api',
        'playwright._impl._driver',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter',
        'scipy', 'pandas', 'IPython', 'notebook',
        'PyQt5.QtBluetooth', 'PyQt5.QtNfc',
        'PyQt5.QtLocation', 'PyQt5.QtQuick',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CaminsRals',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can break Qt DLLs — keep off
    console=False,      # No black terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=None,          # Add an .ico path here if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CaminsRals',
)
