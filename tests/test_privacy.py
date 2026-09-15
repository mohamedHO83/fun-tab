"""Privacy denylist, consent bar, and the privacy-mode preset."""

from __future__ import annotations

from fun_tab.config import Config
from fun_tab.privacy import (
    CONSENT_VERSION,
    PRIVACY_EXCLUDE_EXES,
    apply_privacy_bundle,
    excluded_exes,
)
from fun_tab.windows_enum import AppWindow


def test_denylist_always_includes_password_managers():
    names = excluded_exes()
    assert "1password.exe" in names
    assert "bitwarden.exe" in names
    assert "keepassxc.exe" in names


def test_user_excludes_merge_with_the_denylist():
    names = excluded_exes(["teams.exe", "BITWARDEN.EXE"])
    assert "teams.exe" in names
    assert "bitwarden.exe" in names
    assert names >= {e.lower() for e in PRIVACY_EXCLUDE_EXES}


def test_safer_preview_defaults():
    cfg = Config()
    assert cfg.capture_minimized is False
    assert cfg.prefetch_previews is False
    assert cfg.backdrop_ttl == 0.5
    assert cfg.close_confirm is True
    assert cfg.privacy_mode is False
    assert cfg.title_privacy == "full"
    assert cfg.consent_version == 0


def test_privacy_mode_forces_the_bundle():
    cfg = Config(
        privacy_mode=True,
        preview_enabled=True,
        prefetch_previews=True,
        capture_minimized=True,
        close_key_enabled=True,
        title_privacy="full",
        backdrop="blur",
    )
    cfg.clamp()
    assert cfg.preview_enabled is False
    assert cfg.prefetch_previews is False
    assert cfg.capture_minimized is False
    assert cfg.close_key_enabled is False
    assert cfg.title_privacy == "app"
    assert cfg.backdrop == "dim"
    assert cfg.dim_veil == 100


def test_apply_privacy_bundle_leaves_none_backdrop_alone():
    cfg = Config(backdrop="none")
    apply_privacy_bundle(cfg)
    assert cfg.backdrop == "none"


def test_app_label_hides_document_titles():
    app = AppWindow(
        hwnd=1,
        title="secrets.docx - Word",
        class_name="OpusApp",
        pid=1,
        exe_path=r"C:\Program Files\Word\WINWORD.EXE",
        app_name="Word",
    )
    assert app.label(title_privacy="full") == "secrets.docx"
    assert app.label(title_privacy="app") == "Word"
    assert app.matches_query("secrets", title_privacy="full")
    assert not app.matches_query("secrets", title_privacy="app")
    assert app.matches_query("word", title_privacy="app")


def test_consent_version_constant_is_positive():
    assert CONSENT_VERSION >= 1


def test_atomic_save_round_trips(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(theme="light", privacy_mode=False, consent_version=CONSENT_VERSION)
    assert cfg.save(path) is True
    loaded = Config.load(path)
    assert loaded.theme == "light"
    assert loaded.consent_version == CONSENT_VERSION
    assert loaded.capture_minimized is False
