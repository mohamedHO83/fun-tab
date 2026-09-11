"""The settings window's value mapping.

The window itself needs a display, but the part that can be wrong in a way
that loses someone's settings is the mapping between controls and `Config`,
and that is deliberately pure.
"""

from __future__ import annotations

import pytest

from fun_tab.config import Config
from fun_tab.settings_ui import (
    CONFIG_KEYS,
    GROUPS,
    SETTINGS,
    Setting,
    config_with,
    format_percent,
    values_from,
)


def test_every_control_names_a_real_config_field():
    fields = set(Config().__dataclass_fields__)
    for setting in SETTINGS:
        if setting.key == "autostart":
            continue  # a registry entry, not a setting in the file
        assert setting.key in fields, f"{setting.key} is not a Config field"


def test_every_control_lands_on_a_tab_that_exists():
    for setting in SETTINGS:
        assert setting.group in GROUPS, f"{setting.key} is in a tab nobody shows"


def test_no_control_appears_twice():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys))


def test_choices_cover_the_default_value():
    """A dropdown that cannot represent the current value would silently change it."""
    cfg = Config()
    for setting in SETTINGS:
        if setting.kind != "choice":
            continue
        values = [value for value, _label in setting.choices]
        assert getattr(cfg, setting.key) in values, setting.key


def test_choice_labels_are_unique_within_a_control():
    """Labels are what comes back out of the combobox, so duplicates are ambiguous."""
    for setting in SETTINGS:
        labels = [label for _value, label in setting.choices]
        assert len(labels) == len(set(labels)), setting.key


def test_sliders_bracket_their_default():
    cfg = Config()
    for setting in SETTINGS:
        if setting.kind != "slider":
            continue
        assert setting.low <= float(getattr(cfg, setting.key)) <= setting.high, setting.key


def test_values_round_trip_through_a_config():
    cfg = Config()
    assert config_with(cfg, values_from(cfg)) == cfg


def test_editing_one_value_leaves_the_others_alone():
    cfg = Config(theme="light", outer_radius=200, exclude_exes=["x.exe"])
    values = values_from(cfg)
    values["aim_needle"] = False

    updated = config_with(cfg, values)
    assert updated.aim_needle is False
    assert updated.theme == "light"
    assert updated.outer_radius == 200, "a field the window never shows was dropped"
    assert updated.exclude_exes == ["x.exe"]


def test_saving_is_clamped_like_a_load():
    """Tk sliders hand back floats, and nothing else re-checks the range."""
    cfg = Config()
    updated = config_with(cfg, {"scale": 9.0, "dim_veil": -30.0, "backdrop": "nonsense"})
    assert updated.scale == 3.0
    assert updated.dim_veil == 0
    assert updated.backdrop == "blur"


def test_integer_fields_survive_a_float_from_a_slider():
    updated = config_with(Config(), {"dim_veil": 41.7})
    assert updated.dim_veil == 42
    assert isinstance(updated.dim_veil, int)


def test_checkbox_values_become_real_booleans():
    updated = config_with(Config(), {"aim_origin": 0, "show_hints": 1})
    assert updated.aim_origin is False
    assert updated.show_hints is True


def test_unknown_keys_are_ignored():
    updated = config_with(Config(), {"autostart": True, "not_a_setting": 1})
    assert updated == Config()


def test_a_bad_value_keeps_the_previous_one():
    updated = config_with(Config(), {"dim_veil": "quite dark"})
    assert updated.dim_veil == Config().dim_veil


def test_the_aiming_toggles_are_offered():
    """The two features this window exists to expose, in plain language."""
    keys = {s.key: s for s in SETTINGS}
    assert keys["aim_needle"].kind == "check"
    assert keys["aim_origin"].kind == "check"
    for key in ("aim_needle", "aim_origin"):
        assert "needle" in keys[key].label.lower() or "aim" in keys[key].label.lower()


def test_the_backdrop_controls_are_offered():
    keys = {s.key for s in SETTINGS}
    assert {"backdrop", "dim_blur", "dim_veil"} <= keys


@pytest.mark.parametrize(
    "percent_of, value, expected",
    [(1.0, 1.0, "100%"), (1.0, 0.5, "50%"), (200.0, 40, "20%"), (None, 1.5, "1.5")],
)
def test_readouts_are_plain_percentages(percent_of, value, expected):
    setting = Setting("k", "l", "slider", "Look", percent_of=percent_of)
    assert format_percent(setting, value) == expected


def test_config_keys_excludes_the_registry_toggle():
    assert "autostart" not in CONFIG_KEYS
    assert "aim_needle" in CONFIG_KEYS
