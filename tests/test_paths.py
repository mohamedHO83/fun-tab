"""Frozen vs source launch paths — settings, autostart, assets."""

from __future__ import annotations

import sys
from pathlib import Path

from fun_tab import autostart, paths


def test_settings_reinvokes_the_exe_when_frozen(monkeypatch, tmp_path):
    exe = tmp_path / "FunTab.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.settings_command() == [str(exe), "--settings"]


def test_settings_uses_the_module_when_running_from_source(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.settings_command() == [str(exe), "-m", "fun_tab.settings_ui"]


def test_settings_prefers_pythonw_next_to_python(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    windowed = tmp_path / "pythonw.exe"
    windowed.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.settings_command()[0] == str(windowed)


def test_autostart_from_a_frozen_build_is_just_the_exe(monkeypatch, tmp_path):
    exe = tmp_path / "FunTab.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert autostart.command() == f'"{exe}"'


def test_autostart_from_source_writes_a_shim(monkeypatch, tmp_path):
    exe = tmp_path / "python.exe"
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(autostart, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "install_dir", lambda: tmp_path / "src")
    cmd = autostart.command()
    shim = tmp_path / autostart.LAUNCHER
    assert shim.is_file()
    text = shim.read_text(encoding="utf-8")
    assert repr(str(tmp_path / "src")) in text
    assert "from fun_tab.app import main" in text
    assert cmd == f'"{exe}" "{shim}"'


def test_asset_path_follows_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.asset_path("fun-tab.ico") == tmp_path / "assets" / "fun-tab.ico"


def test_asset_path_from_source_is_the_repo_assets_folder():
    expected = Path(paths.__file__).resolve().parent.parent / "assets" / "fun-tab.ico"
    assert paths.asset_path("fun-tab.ico") == expected
