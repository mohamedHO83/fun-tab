# -*- mode: python ; coding: utf-8 -*-
"""Standalone Windows build. Run ``build.bat`` rather than this file."""

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["launch.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/fun-tab.ico", "assets")],
    hiddenimports=collect_submodules("fun_tab")
    + [
        "tkinter",
        "tkinter.ttk",
        "tkinter.colorchooser",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FunTab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/fun-tab.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FunTab",
)
