"""Where the app lives on disk — a source checkout or a frozen exe.

PyInstaller sets ``sys.frozen`` and unpacks bundled files into ``_MEIPASS``.
Settings, autostart and the tray icon all have to follow that, otherwise a
copy you give to people can start and then fail to open settings or find its
tray icon.
"""

from __future__ import annotations

import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Directory that contains ``assets/`` (repo root, or PyInstaller's extract)."""
    if frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_dir() -> Path:
    """Folder of the running program — the one you zip and hand to people."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def asset_path(name: str) -> Path:
    return bundle_dir() / "assets" / name


def launcher() -> Path:
    """Executable used to start Fun Tab again (settings window, logon)."""
    exe = Path(sys.executable)
    if frozen():
        return exe
    windowed = exe.with_name("pythonw.exe")
    return windowed if windowed.exists() else exe


def settings_command() -> list[str]:
    exe = str(launcher())
    if frozen():
        return [exe, "--settings"]
    return [exe, "-m", "fun_tab.settings_ui"]
