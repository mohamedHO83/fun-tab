"""Run Fun Tab when Windows starts (per-user, no admin rights needed)."""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

from .config import config_dir

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FunTab"
LAUNCHER = "fun-tab-autostart.pyw"


def _pythonw() -> Path:
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return windowed if windowed.exists() else exe


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _write_launcher() -> Path:
    """A tiny .pyw shim: the Run key cannot set PYTHONPATH or a working dir."""
    path = config_dir() / LAUNCHER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import sys\n"
        f"sys.path.insert(0, r{str(_project_root())!r})\n"
        "from fun_tab.app import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return path


def command() -> str:
    return f'"{_pythonw()}" "{_write_launcher()}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def enable() -> bool:
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        return True
    except OSError:
        return False


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def toggle() -> bool:
    if is_enabled():
        disable()
        return False
    enable()
    return True
