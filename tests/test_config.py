"""Config loading, clamping and theme construction."""

from __future__ import annotations

import json

import pytest

from fun_tab.config import Config, Theme, alpha, mix


def test_defaults_round_trip(tmp_path):
    path = tmp_path / "config.json"
    Config().save(path)
    assert Config.load(path) == Config()


def test_missing_file_writes_a_template(tmp_path):
    path = tmp_path / "nested" / "config.json"
    cfg = Config.load(path)
    assert cfg == Config()
    assert path.exists(), "first run should leave an editable file behind"
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "auto"


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert Config.load(path) == Config()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "light", "nonsense": 5}), encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.theme == "light"
    assert not hasattr(cfg, "nonsense")


def test_wrong_types_are_coerced_or_skipped(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"outer_radius": "200", "max_fps": 90.7, "preview_enabled": 0}),
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.outer_radius == 200
    assert cfg.max_fps == 90
    assert cfg.preview_enabled is False


def test_unparseable_value_keeps_the_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"outer_radius": "wide"}), encoding="utf-8")
    assert Config.load(path).outer_radius == Config().outer_radius


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("scale", 99.0, 3.0),
        ("scale", 0.01, 0.5),
        ("max_fps", 5, 30),
        ("max_fps", 5000, 240),
        ("dim_veil", -20, 0),
        ("outer_radius", 10_000, 480),
        ("theme", "neon", "auto"),
        ("backdrop", "hologram", "blur"),
    ],
)
def test_clamp_pulls_values_into_range(field, value, expected):
    cfg = Config(**{field: value})
    cfg.clamp()
    assert getattr(cfg, field) == expected


def test_inner_radius_cannot_swallow_the_ring():
    cfg = Config(outer_radius=120, inner_radius=400)
    cfg.clamp()
    assert cfg.inner_radius < cfg.outer_radius


def test_valid_choices_survive_clamping():
    for backdrop in ("blur", "dim", "none"):
        cfg = Config(backdrop=backdrop)
        cfg.clamp()
        assert cfg.backdrop == backdrop
    for mode in ("auto", "always", "off"):
        cfg = Config(game_compat=mode)
        cfg.clamp()
        assert cfg.game_compat == mode


def test_unknown_game_compat_falls_back_to_auto():
    cfg = Config(game_compat="stealth")
    cfg.clamp()
    assert cfg.game_compat == "auto"


@pytest.mark.parametrize(
    "old, new",
    [("acrylic", "blur"), ("snapshot", "blur"), ("solid", "dim"), ("off", "none")],
)
def test_retired_backdrop_names_migrate(old, new):
    """A config written by an older build must not resolve to a dead mode."""
    cfg = Config(backdrop=old)
    cfg.clamp()
    assert cfg.backdrop == new


@pytest.mark.parametrize("field", ["aim_needle", "aim_origin"])
def test_the_aiming_helpers_are_on_by_default(field):
    assert getattr(Config(), field) is True


@pytest.mark.parametrize("field", ["aim_needle", "aim_origin"])
def test_the_aiming_helpers_can_be_turned_off_in_the_file(tmp_path, field):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({field: False}), encoding="utf-8")
    assert getattr(Config.load(path), field) is False


def test_a_config_from_before_the_aiming_options_keeps_them_on(tmp_path):
    """Upgrading must not silently switch a feature off."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"theme": "dark", "backdrop": "blur"}), encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.aim_needle is True
    assert cfg.aim_origin is True


def test_group_by_app_is_on_by_default():
    assert Config().group_by_app is True


def test_exe_lists_are_normalised_on_clamp():
    cfg = Config(exclude_exes=["Chrome", r"C:\Apps\Slack.EXE", "chrome.exe", ""])
    cfg.set_slot_order(["Spotify", "chrome"])
    cfg.clamp()
    assert cfg.exclude_exes == ["chrome.exe", "slack.exe"]
    assert cfg.pinned_exes == ["spotify.exe"], "a hidden app cannot also be pinned"


# -- colour helpers ---------------------------------------------------------


def test_mix_interpolates_and_clamps():
    black, white = (0, 0, 0, 0), (255, 255, 255, 255)
    assert mix(black, white, 0.0) == black
    assert mix(black, white, 1.0) == white
    assert mix(black, white, 0.5) == (128, 128, 128, 128)
    assert mix(black, white, 5.0) == white, "t is clamped, not extrapolated"


def test_alpha_replaces_only_the_alpha_channel():
    assert alpha((10, 20, 30, 255), 40) == (10, 20, 30, 40)
    assert alpha((10, 20, 30, 0), 999) == (10, 20, 30, 255)


# -- theme ------------------------------------------------------------------


def test_explicit_theme_beats_the_system_preference():
    assert Theme.build(Config(theme="dark")).is_dark
    assert not Theme.build(Config(theme="light")).is_dark


def test_hex_accent_is_used_verbatim():
    theme = Theme.build(Config(theme="dark", accent="#ff8800"))
    assert theme.accent[:3] == (255, 136, 0)


def test_colors_block_overrides_the_palette():
    theme = Theme.build(Config(theme="dark", colors={"text": "#123456"}))
    assert theme.text == (18, 52, 86, 255)


def test_colors_block_cannot_forge_is_dark():
    theme = Theme.build(Config(theme="dark", colors={"is_dark": False}))
    assert theme.is_dark is True


def test_dark_and_light_palettes_differ_in_luminance():
    dark = Theme.build(Config(theme="dark"))
    light = Theme.build(Config(theme="light"))
    assert sum(dark.ring_fill[:3]) < sum(light.ring_fill[:3])
    assert sum(dark.text[:3]) > sum(light.text[:3])


def test_hover_reads_as_accented_against_idle():
    theme = Theme.build(Config(theme="dark", accent="#ff0000"))
    assert theme.slice_hover[0] > theme.slice_idle[0]
