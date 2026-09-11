"""Drive the settings window the way a person would, and check it takes.

Runs the real window against the real app: edits a few settings, saves, and
confirms the running switcher noticed the file on its own and now renders
with the change. Your own `config.json` is backed up and put back.

    python settings_check.py
"""

from __future__ import annotations

import shutil
import time

import fun_tab.settings_ui as ui
from fun_tab.app import FunTabApp, _config_stamp
from fun_tab.config import Config, config_path


def pump(window, seconds: float = 0.8) -> None:
    """Let Tk run without handing it the process for good."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        window.root.update()
        time.sleep(0.02)


def choose(window, key: str, value) -> None:
    setting = next(s for s in ui.SETTINGS if s.key == key)
    window.vars[key].set(ui._label_for(setting, value) if setting.choices else value)
    window.on_change()


def main() -> int:
    path = config_path()
    backup = path.with_suffix(".json.checkbak") if path.exists() else None
    if backup:
        shutil.copy2(path, backup)

    try:
        scene, dpi = ui._gather_scene()
        window = ui.SettingsWindow(Config.load(), scene, dpi=dpi)
        pump(window, 1.2)

        print(f"window built at {dpi} dpi, preview "
              f"{'drawn' if window._image_ref else 'MISSING'}")
        if window._image_ref is None:
            return 1

        # Three settings a person would plausibly change in one sitting.
        choose(window, "aim_needle", False)
        choose(window, "dim_veil", 160.0)
        choose(window, "theme", "light")
        pump(window)

        edited = window.collect()
        print(
            f"edited: aim_needle={edited.aim_needle} dim_veil={edited.dim_veil} "
            f"theme={edited.theme!r}"
        )
        if edited.aim_needle or edited.dim_veil != 160 or edited.theme != "light":
            print("FAIL: the controls did not reach the config")
            return 1

        before_save = _config_stamp()
        window.on_save()
        if not window.saved:
            print("FAIL: saving did not go through")
            return 1
        if _config_stamp() == before_save:
            print("FAIL: the settings file did not change")
            return 1
        print(f"saved to {path}")

        # The running app should pick the file up with nobody asking it to,
        # which is the only signal the two processes share.
        app = FunTabApp()
        app._config_stamp = before_save  # as if it had been running all along
        if not app._config_file_edited():
            print("FAIL: the app did not notice the file changed")
            return 1
        app.reload_config()
        print(
            f"app reloaded: aim_needle={app.cfg.aim_needle} "
            f"dim_veil={app.cfg.dim_veil} theme={app.cfg.theme!r}"
        )
        if app.cfg.aim_needle or app.cfg.theme != "light":
            print("FAIL: the app is still running the old settings")
            return 1
        if app._config_file_edited():
            print("FAIL: the app would reload again on the next tick")
            return 1

        # And the wheel really does stop drawing the needle.
        from fun_tab.preview import _demo_apps

        overlay = app.overlay
        overlay._apps = _demo_apps(5)
        overlay._all_apps = list(overlay._apps)
        overlay._visible = True
        overlay._rebuild_layers()
        overlay._set_aim_angle(1.0)
        if overlay._aim_angle is not None:
            print("FAIL: the needle is still being drawn")
            return 1
        print("wheel honours the saved settings")

        print("OK")
        return 0
    finally:
        if backup:
            shutil.copy2(backup, path)
            backup.unlink(missing_ok=True)
            print(f"restored {path}")


if __name__ == "__main__":
    raise SystemExit(main())
