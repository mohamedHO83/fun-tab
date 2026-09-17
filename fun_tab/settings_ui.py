"""A settings window, for people who should not have to meet JSON.

Runs as its own process (``python -m fun_tab.settings_ui``) rather than a
thread of the switcher: Tk wants to own a thread's message loop, and the
switcher's loop is already spoken for by the keyboard hook. Saving writes
``config.json``, which the running app notices and reloads on its own.

The list of controls is data, and the two functions that move values between
it and a `Config` are pure, so the mapping can be tested without a display.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from dataclasses import dataclass, replace
from typing import Any

from .config import Config, config_path

PANEL_W = 560  # preview width at 100% scaling
# Tall enough to sit beside the preview; the Windows tab scrolls rather than
# packing every control into a wall of checkboxes.
TAB_W = 540
TAB_H = 488
# The app finds an already-open window by title rather than starting a second.
WINDOW_TITLE = "Fun Tab settings"


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str  # check | choice | slider | accent | hotkey
    group: str
    choices: tuple[tuple[Any, str], ...] = ()
    low: float = 0.0
    high: float = 1.0
    step: float = 0.05
    percent_of: float | None = None  # value that reads as 100%
    hint: str = ""
    invert: bool = False  # if True, slider is displayed inverted (high=good)
    section: str = ""  # heading inside a tab; empty means no extra heading


GROUPS = ("Look", "Background", "Aiming", "Windows", "Privacy")

SETTINGS: tuple[Setting, ...] = (
    # -- Look ---------------------------------------------------------------
    Setting(
        "theme",
        "Colours",
        "choice",
        "Look",
        choices=(("auto", "Match Windows"), ("dark", "Always dark"), ("light", "Always light")),
    ),
    Setting("accent", "Highlight colour", "accent", "Look"),
    Setting("scale", "Wheel size", "slider", "Look", low=0.6, high=1.8, percent_of=1.0),
    Setting(
        "icon_scale", "Icon size", "slider", "Look", low=0.6, high=1.6, percent_of=1.0
    ),
    Setting("show_counter", "Show the position counter in the middle", "check", "Look"),
    Setting("show_subtitle", "Show the app name under the title", "check", "Look"),
    Setting("show_hints", "Show the keyboard hints at the bottom", "check", "Look"),
    # -- Background ---------------------------------------------------------
    Setting(
        "backdrop",
        "Behind the wheel",
        "choice",
        "Background",
        choices=(
            ("blur", "Blur the desktop"),
            ("dim", "Darken the desktop, no blur"),
            ("none", "Leave the desktop alone"),
        ),
    ),
    Setting(
        "dim_blur",
        "How soft the acrylic is",
        "slider",
        "Background",
        low=0.0,
        high=1.0,
        percent_of=1.0,
    ),
    Setting(
        "dim_veil",
        "How dark",
        "slider",
        "Background",
        low=0.0,
        high=200.0,
        step=5.0,
        percent_of=200.0,
    ),
    Setting(
        "dim_scale",
        "Backdrop quality",
        "slider",
        "Background",
        low=1.0,
        high=8.0,
        step=1.0,
        hint="Higher quality is sharper but slightly slower to open.",
        invert=True,  # internally 1=max quality, 8=fastest; invert so right=better
    ),
    # -- Aiming -------------------------------------------------------------
    Setting(
        "aim_needle",
        "Show the needle that points where you are aiming",
        "check",
        "Aiming",
    ),
    Setting(
        "aim_origin",
        "Mark the spot your aim is measured from",
        "check",
        "Aiming",
        hint="A small ring appears where the mouse was when the wheel opened.",
    ),
    Setting("search_enabled", "Type letters to filter the list", "check", "Aiming"),
    Setting("digit_jump", "Press 1-9 to jump straight to a window", "check", "Aiming"),
    Setting("wrap_navigation", "Wrap around past the last window", "check", "Aiming"),
    # -- Windows ------------------------------------------------------------
    Setting(
        "open_hotkey",
        "Open shortcut",
        "hotkey",
        "Windows",
        hint="Click the button, then press a key chord or a mouse side-button (4 or 5).",
        section="Opening",
    ),
    Setting(
        "same_app_hotkey",
        "Current-app shortcut",
        "hotkey",
        "Windows",
        hint="Opens the wheel already fanned out on the app you are in (default Alt + `). "
        "While the wheel is open, the same key steps through that app's windows.",
        section="Opening",
    ),
    Setting(
        "open_sticky",
        "Keep the wheel open after releasing the shortcut",
        "check",
        "Windows",
        hint="Off = classic hold-to-switch: keep holding Alt / your modifiers / the mouse button, then release to switch.",
        section="Opening",
    ),
    Setting(
        "autostart",
        "Start Fun Tab when Windows starts",
        "check",
        "Windows",
        section="Opening",
    ),
    Setting(
        "game_compat",
        "Game compatibility",
        "choice",
        "Windows",
        choices=(
            ("auto", "Automatic (when a game is in front)"),
            ("always", "Always on"),
            ("off", "Off"),
        ),
        hint="When on, Windows keeps Alt+Tab. Use your open shortcut (or Ctrl+Alt+Tab) instead.",
        section="Games",
    ),
    Setting(
        "pause_in_games",
        "Pause Fun Tab completely while a game is in front",
        "check",
        "Windows",
        hint="Safest for anti-cheat: removes the keyboard/mouse hooks until you leave the game.",
        section="Games",
    ),
    Setting(
        "mru_order",
        "List the most recently used window first",
        "check",
        "Windows",
        section="The wheel",
    ),
    Setting(
        "group_by_app",
        "One slice per application",
        "check",
        "Windows",
        hint="Many windows of the same app become one slice. Cycle them with the current-app shortcut.",
        section="The wheel",
    ),
    Setting(
        "group_pips",
        "Show a dot per window on each slice",
        "check",
        "Windows",
        hint="Tells you which slices hold several windows before you pick one.",
        section="The wheel",
    ),
    Setting(
        "subring",
        "Reach past the ring to pick a single window",
        "check",
        "Aiming",
        hint="Overshoot a slice and its windows fan out further out. Pull back for apps again.",
    ),
    Setting(
        "pin_lane",
        "Give pinned apps fixed places on the wheel",
        "check",
        "Windows",
        hint="Pinned apps sit at the bottom at the same angle every time, open or not."
        " Selecting a closed one starts it.",
        section="The wheel",
    ),
    Setting(
        "minimized_last",
        "Push minimised windows to the end",
        "check",
        "Windows",
        section="The wheel",
    ),
    Setting(
        "close_key_enabled",
        "Let Delete and Ctrl+W close a window",
        "check",
        "Windows",
        section="Closing",
    ),
    Setting(
        "close_confirm",
        "Ask before closing a window the first time each session",
        "check",
        "Windows",
        section="Closing",
    ),
    Setting(
        "preview_enabled",
        "Show a picture of the selected window",
        "check",
        "Windows",
        section="Picture",
    ),
    Setting(
        "preview_position",
        "Where that picture goes",
        "choice",
        "Windows",
        choices=(
            ("top-left", "Top left"),
            ("top-right", "Top right"),
            ("bottom-left", "Bottom left"),
            ("bottom-right", "Bottom right"),
            ("bottom-center", "Bottom middle"),
        ),
        section="Picture",
    ),
    Setting(
        "prefetch_previews",
        "Capture other windows in the background too",
        "check",
        "Windows",
        hint="Off by default. When on, Fun Tab photographs nearby windows before you select them.",
        section="Picture",
    ),
    Setting(
        "capture_minimized",
        "Include minimised windows in that picture",
        "check",
        "Windows",
        hint="Off by default. When on, Fun Tab briefly restores the window off-screen to photograph it.",
        section="Picture",
    ),
    # -- Privacy ------------------------------------------------------------
    Setting(
        "privacy_mode",
        "Privacy mode",
        "check",
        "Privacy",
        hint="Turns off previews, prefetch, minimised capture, and close keys; shows app names only; darkens the desktop heavily.",
    ),
    Setting(
        "title_privacy",
        "Window labels",
        "choice",
        "Privacy",
        choices=(
            ("full", "Show the full window title"),
            ("app", "Show the application name only"),
        ),
        hint="App-only hides document names, URLs and email subjects on the wheel.",
    ),
)

# `autostart` is a registry entry, not a config field.
CONFIG_KEYS = frozenset(s.key for s in SETTINGS) - {"autostart"}


def values_from(cfg: Config) -> dict[str, Any]:
    """Current value of every config-backed control."""
    return {key: getattr(cfg, key) for key in CONFIG_KEYS}


def config_with(base: Config, values: dict[str, Any]) -> Config:
    """`base` with the edited values applied, clamped the same way a load is.

    Untouched fields come through unchanged, so settings this window does not
    show survive being saved from it.
    """
    cfg = replace(base)
    for key, value in values.items():
        if key not in CONFIG_KEYS:
            continue
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = int(round(float(value)))
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, str):
                value = str(value)
            elif isinstance(current, list):
                value = [str(item) for item in value]
        except (TypeError, ValueError):
            continue
        setattr(cfg, key, value)
    cfg.clamp()
    return cfg


_QUALITY_LABELS = {1: "Max", 2: "High", 3: "Good", 4: "Medium", 5: "Low", 6: "Low", 7: "Fast", 8: "Fast"}


def format_percent(setting: Setting, value: float) -> str:
    if setting.key == "dim_scale":
        # dim_scale is a divisor: 1 = max quality, 8 = fastest.
        # Invert so the readout reads as quality, not internal divisor.
        label = _QUALITY_LABELS.get(int(round(value)), "Low")
        return label
    if setting.percent_of:
        return f"{round(float(value) / setting.percent_of * 100)}%"
    return f"{float(value):g}"


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class PreviewPump:
    """Renders scene previews off the UI thread, newest request wins.

    A render is ~40ms, which is too long to do between slider steps on the UI
    thread, and queueing them all up would leave the picture lagging seconds
    behind the slider.
    """

    def __init__(self, scene, fit: tuple[int, int]) -> None:
        self._scene = scene
        self._fit = fit
        self.out: queue.Queue = queue.Queue()
        self._want: Config | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request(self, cfg: Config) -> None:
        with self._lock:
            self._want = cfg
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def _run(self) -> None:
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                cfg, self._want = self._want, None
            if cfg is None:
                continue
            try:
                self.out.put(self._scene.render(cfg, fit=self._fit))
            except Exception:
                pass  # a preview is never worth taking the window down for


class SettingsWindow:
    def __init__(self, cfg: Config, scene, dpi: int = 96) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.cfg = cfg
        self.saved = False
        self._image_ref = None
        self._snapping = False
        self._hotkey_buttons: dict = {}
        self._exclude_exes = list(cfg.exclude_exes)
        self._pinned_slots = [dict(slot) for slot in cfg.slots]
        self._listboxes: dict = {}

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.resizable(True, True)
        # DPI awareness is on for the screenshot to be at real pixels, which
        # means Tk will not scale itself and everything must be told to.
        self.ui = dpi / 96.0
        self.root.tk.call("tk", "scaling", dpi / 72.0)
        self._set_icon()
        self._hint_wrap = self.px(TAB_W - 36)
        self._style_labels()

        self.vars: dict[str, Any] = {}
        self._labels: dict[str, Any] = {}
        self._autostart = tk.BooleanVar(value=_autostart_enabled())

        # Controls beside the preview rather than under it: stacked, the window
        # is taller than a 1080p screen once the picture is big enough to read.
        # Tabs scroll instead, so this column can stay beside the preview.
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        body = ttk.Frame(self.root, padding=self.px(16))
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, self.px(16)))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="n")

        self.notebook = notebook = ttk.Notebook(left)
        notebook.grid(row=0, column=0, sticky="nsew")
        for group in GROUPS:
            notebook.add(self._build_group(notebook, group), text=group)

        buttons = ttk.Frame(left)
        buttons.grid(row=1, column=0, sticky="ew", pady=(self.px(12), 0))
        buttons.columnconfigure(1, weight=1)
        ttk.Button(buttons, text="Reset to defaults", command=self.on_reset).grid(
            row=0, column=0
        )
        ttk.Button(buttons, text="Cancel", command=self.on_cancel).grid(
            row=0, column=2, padx=(0, self.px(6))
        )
        save = ttk.Button(buttons, text="Save", command=self.on_save)
        save.grid(row=0, column=3)
        save.focus_set()

        self.status = ttk.Label(left, text="", foreground="#1a7f37")
        self.status.grid(row=2, column=0, sticky="w", pady=(self.px(8), 0))
        self.root.minsize(self.px(TAB_W + PANEL_W + 64), self.px(TAB_H + 120))

        self.preview = ttk.Label(right, anchor="center")
        self.preview.grid(row=0, column=0)
        ttk.Label(
            right,
            text="Your own desktop. Changes show here before you save.",
            foreground="#666666",
        ).grid(row=1, column=0, sticky="w", pady=(self.px(6), 0))

        self.pump = PreviewPump(scene, (self.px(PANEL_W), self.px(int(PANEL_W * 9 / 16))))
        self.root.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.root.bind("<Escape>", lambda _e: self.on_cancel())
        self._load(cfg)
        self.pump.request(self.collect())
        self._poll_id = self.root.after(60, self._poll_preview)

    # -- layout ------------------------------------------------------------

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.ui)))

    def _set_icon(self) -> None:
        from .paths import asset_path

        icon = asset_path("fun-tab.ico")
        if icon.exists():
            try:
                self.root.iconbitmap(str(icon))
            except Exception:
                pass

    def _style_labels(self) -> None:
        ttk = self.ttk
        style = ttk.Style(self.root)
        style.configure("Hint.TLabel", foreground="#666666")
        try:
            import tkinter.font as tkfont

            heading = tkfont.nametofont("TkDefaultFont").copy()
            heading.configure(weight="bold")
            style.configure("Heading.TLabel", font=heading)
        except Exception:
            pass

    def _scrollable_page(self, parent):
        tk, ttk = self.tk, self.ttk
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        bg = ttk.Style(self.root).lookup("TFrame", "background") or "SystemButtonFace"
        canvas = tk.Canvas(
            holder,
            width=self.px(TAB_W),
            height=self.px(TAB_H),
            highlightthickness=0,
            borderwidth=0,
            background=bg,
        )
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(self.px(16), self.px(12), self.px(10), self.px(16)))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        scrollbar.grid_remove()

        def sync(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all") or (0, 0, 0, 0))
            bbox = canvas.bbox("all")
            overflow = bool(bbox and bbox[3] > canvas.winfo_height() + 1)
            if overflow:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()
                canvas.yview_moveto(0)

        def stretch(event) -> None:
            canvas.itemconfigure(window_id, width=max(event.width, 1))
            sync()

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", stretch)

        def on_wheel(event) -> None:
            bbox = canvas.bbox("all")
            if not bbox or bbox[3] <= canvas.winfo_height() + 1:
                return
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def bind_tree(widget) -> None:
            if widget.winfo_class() != "Listbox":
                widget.bind("<MouseWheel>", on_wheel)
            for child in widget.winfo_children():
                bind_tree(child)

        holder.after_idle(lambda: bind_tree(holder))
        return holder, inner

    def _build_section(self, frame, title: str, row: int) -> int:
        pad_top = 0 if row == 0 else self.px(18)
        self.ttk.Label(frame, text=title, style="Heading.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(pad_top, self.px(6))
        )
        return row + 1

    def _build_group(self, parent, group: str):
        holder, frame = self._scrollable_page(parent)
        frame.columnconfigure(1, weight=1)
        row = 0
        section = ""
        for setting in SETTINGS:
            if setting.group != group:
                continue
            if setting.section and setting.section != section:
                section = setting.section
                row = self._build_section(frame, section, row)
            row = self._build_control(frame, setting, row)
        if group == "Windows":
            row = self._build_pin_list(frame, row)
        if group == "Privacy":
            row = self._build_exe_list(
                frame,
                row,
                attr="_exclude_exes",
                title="Hidden apps",
                hint="These never appear on the wheel. Password managers are always hidden.",
            )
        return holder

    def _build_control(self, frame, setting: Setting, row: int) -> int:
        tk, ttk = self.tk, self.ttk
        bottom = self.px(2) if setting.hint else self.px(8)
        pad = {"pady": (self.px(4), bottom)}

        if setting.kind == "check":
            var = self._autostart if setting.key == "autostart" else tk.BooleanVar()
            self.vars[setting.key] = var
            ttk.Checkbutton(
                frame, text=setting.label, variable=var, command=self.on_change
            ).grid(row=row, column=0, columnspan=3, sticky="w", **pad)

        elif setting.kind == "choice":
            var = tk.StringVar()
            self.vars[setting.key] = var
            ttk.Label(frame, text=setting.label).grid(row=row, column=0, sticky="w", **pad)
            longest = max((len(label) for _value, label in setting.choices), default=18)
            box = ttk.Combobox(
                frame,
                textvariable=var,
                state="readonly",
                width=min(42, max(18, longest + 1)),
                values=[label for _value, label in setting.choices],
            )
            box.grid(row=row, column=1, columnspan=2, sticky="w", padx=(self.px(8), 0), **pad)
            box.bind("<<ComboboxSelected>>", lambda _e: self.on_change())

        elif setting.kind == "slider":
            var = tk.DoubleVar()
            self.vars[setting.key] = var
            ttk.Label(frame, text=setting.label).grid(row=row, column=0, sticky="w", **pad)
            # Inverted sliders flip from_/to so dragging right means "better".
            s_from = setting.high if setting.invert else setting.low
            s_to   = setting.low  if setting.invert else setting.high
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=1, columnspan=2, sticky="w", padx=(self.px(8), 0), **pad)
            ttk.Scale(
                holder,
                from_=s_from,
                to=s_to,
                variable=var,
                command=lambda _v, s=setting: self.on_slide(s),
                length=self.px(220),
            ).grid(row=0, column=0, sticky="w")
            readout = ttk.Label(holder, text="", width=6)
            readout.grid(row=0, column=1, sticky="w", padx=(self.px(8), 0))
            self._labels[setting.key] = readout

        elif setting.kind == "accent":
            var = tk.StringVar()
            self.vars[setting.key] = var
            ttk.Label(frame, text=setting.label).grid(row=row, column=0, sticky="w", **pad)
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=1, columnspan=2, sticky="w", padx=(self.px(8), 0), **pad)
            ttk.Radiobutton(
                holder,
                text="Match Windows",
                value="system",
                variable=var,
                command=self.on_change,
            ).grid(row=0, column=0)
            self._swatch = tk.Button(
                holder, text="Pick a colour…", command=self.on_pick_colour, relief="groove"
            )
            self._swatch.grid(row=0, column=1, padx=(self.px(8), 0))

        elif setting.kind == "hotkey":
            from .hotkey import parse_hotkey

            var = tk.StringVar()
            self.vars[setting.key] = var
            ttk.Label(frame, text=setting.label).grid(row=row, column=0, sticky="w", **pad)
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=1, columnspan=2, sticky="w", padx=(self.px(8), 0), **pad)
            button = tk.Button(
                holder,
                text=parse_hotkey(var.get() or getattr(Config(), setting.key)).label(),
                width=28,
                command=lambda s=setting: self.on_capture_hotkey(s),
                relief="groove",
            )
            button.grid(row=0, column=0)
            self._hotkey_buttons[setting.key] = button
            ttk.Button(
                holder, text="Reset", command=lambda s=setting: self.on_reset_hotkey(s)
            ).grid(row=0, column=1, padx=(self.px(6), 0))

        if setting.hint:
            row += 1
            indent = self.px(22) if setting.kind == "check" else 0
            self.ttk.Label(
                frame,
                text=setting.hint,
                style="Hint.TLabel",
                wraplength=self._hint_wrap,
                justify="left",
            ).grid(
                row=row,
                column=0,
                columnspan=3,
                sticky="w",
                padx=(indent, 0),
                pady=(0, self.px(10)),
            )
        return row + 1

    def _build_exe_list(self, frame, row: int, *, attr: str, title: str, hint: str) -> int:
        ttk = self.ttk
        row = self._build_section(frame, title, row)
        box = self.tk.Listbox(frame, height=5, exportselection=False)
        box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=self.px(2))
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=2, sticky="n", padx=(self.px(8), 0))
        ttk.Button(
            buttons, text="Add…", command=lambda a=attr: self._add_exe(a)
        ).grid(row=0, column=0, sticky="ew", pady=(0, self.px(6)))
        ttk.Button(
            buttons, text="Remove", command=lambda a=attr: self._remove_exe(a)
        ).grid(row=1, column=0, sticky="ew")
        self._listboxes[attr] = box
        self._fill_exe_list(attr)
        row += 1
        ttk.Label(
            frame,
            text=hint,
            style="Hint.TLabel",
            wraplength=self._hint_wrap,
            justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(self.px(6), self.px(4)))
        return row + 1

    def _build_pin_list(self, frame, row: int) -> int:
        ttk = self.ttk
        row = self._build_section(frame, "Pinned apps", row)
        box = self.tk.Listbox(frame, height=5, exportselection=False)
        box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=self.px(2))
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=2, sticky="n", padx=(self.px(8), 0))
        ttk.Button(buttons, text="Browse…", command=self._add_pin).grid(
            row=0, column=0, sticky="ew", pady=(0, self.px(6))
        )
        ttk.Button(buttons, text="Remove", command=self._remove_pin).grid(
            row=1, column=0, sticky="ew"
        )
        self._listboxes["_pinned_slots"] = box
        self._fill_pin_list()
        row += 1
        ttk.Label(
            frame,
            text="In slot order. Browse for an .exe or a shortcut (.lnk) so a closed "
            "app can still be started from the wheel.",
            style="Hint.TLabel",
            wraplength=self._hint_wrap,
            justify="left",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(self.px(6), self.px(4)))
        return row + 1

    def _exe_names(self, attr: str) -> list[str]:
        return list(getattr(self, attr))

    def _fill_exe_list(self, attr: str) -> None:
        box = self._listboxes.get(attr)
        if box is None:
            return
        box.delete(0, "end")
        for name in self._exe_names(attr):
            box.insert("end", name)

    def _add_exe(self, attr: str) -> None:
        from .config import _normalize_exe_names

        picked = _pick_exe(self.root, self.ui)
        if not picked:
            return
        names = _normalize_exe_names(self._exe_names(attr) + [picked])
        setattr(self, attr, names)
        self._fill_exe_list(attr)
        self.on_change()

    def _remove_exe(self, attr: str) -> None:
        box = self._listboxes.get(attr)
        if box is None:
            return
        selection = list(box.curselection())
        if not selection:
            return
        names = self._exe_names(attr)
        drop = {names[i] for i in selection if 0 <= i < len(names)}
        setattr(self, attr, [name for name in names if name not in drop])
        self._fill_exe_list(attr)
        self.on_change()

    def _pin_label(self, slot: dict) -> str:
        label = str(slot.get("label") or "").strip()
        exe = str(slot.get("exe") or "").strip()
        if label and exe:
            return f"{label}  ({exe})"
        return label or exe or "(pin)"

    def _fill_pin_list(self) -> None:
        box = self._listboxes.get("_pinned_slots")
        if box is None:
            return
        box.delete(0, "end")
        for slot in self._pinned_slots:
            box.insert("end", self._pin_label(slot))

    def _add_pin(self) -> None:
        from tkinter import filedialog

        from .config import MAX_SLOTS
        from .windows_enum import slot_from_path

        picked = filedialog.askopenfilename(
            parent=self.root,
            title="Pin an app",
            filetypes=(
                ("Programs and shortcuts", "*.exe *.lnk"),
                ("Programs", "*.exe"),
                ("Shortcuts", "*.lnk"),
                ("All files", "*.*"),
            ),
        )
        if not picked:
            return
        slot = slot_from_path(picked)
        if not slot:
            self.status.configure(
                text="Pick an .exe or a shortcut (.lnk).",
                foreground="#b3261e",
            )
            return
        exe = str(slot.get("exe") or "").lower()
        replaced = False
        if exe:
            for existing in self._pinned_slots:
                if str(existing.get("exe") or "").lower() == exe:
                    existing.update(slot)
                    replaced = True
                    break
        if not replaced:
            if len(self._pinned_slots) >= MAX_SLOTS:
                self.status.configure(
                    text=f"The wheel keeps at most {MAX_SLOTS} pinned apps.",
                    foreground="#666666",
                )
                return
            self._pinned_slots.append(slot)
        for index, item in enumerate(self._pinned_slots):
            item["index"] = index
        self._fill_pin_list()
        self.on_change()

    def _remove_pin(self) -> None:
        box = self._listboxes.get("_pinned_slots")
        if box is None:
            return
        selection = list(box.curselection())
        if not selection:
            return
        drop = set(selection)
        self._pinned_slots = [
            slot for i, slot in enumerate(self._pinned_slots) if i not in drop
        ]
        for index, slot in enumerate(self._pinned_slots):
            slot["index"] = index
        self._fill_pin_list()
        self.on_change()

    # -- values ------------------------------------------------------------

    def _load(self, cfg: Config) -> None:
        current = values_from(cfg)
        for setting in SETTINGS:
            var = self.vars.get(setting.key)
            if var is None or setting.key not in current:
                continue
            value = current[setting.key]
            if setting.kind == "choice":
                var.set(_label_for(setting, value))
            else:
                var.set(value)
            if setting.kind == "slider":
                self._labels[setting.key].configure(
                    text=format_percent(setting, float(value))
                )
            if setting.kind == "hotkey":
                self._refresh_hotkey_button(setting.key)
        self._exclude_exes = list(cfg.exclude_exes)
        self._pinned_slots = [dict(slot) for slot in cfg.slots]
        self._fill_exe_list("_exclude_exes")
        self._fill_pin_list()
        self._refresh_swatch()

    def _refresh_hotkey_button(self, key: str) -> None:
        from .hotkey import parse_hotkey

        button = getattr(self, "_hotkey_buttons", {}).get(key)
        if button is None:
            return
        button.configure(
            text=parse_hotkey(
                str(self.vars[key].get()) or getattr(Config(), key)
            ).label()
        )

    def collect(self) -> Config:
        values: dict[str, Any] = {}
        for setting in SETTINGS:
            var = self.vars.get(setting.key)
            if var is None or setting.key not in CONFIG_KEYS:
                continue
            values[setting.key] = (
                _value_for(setting, var.get()) if setting.kind == "choice" else var.get()
            )
        cfg = config_with(self.cfg, values)
        cfg.exclude_exes = list(self._exclude_exes)
        cfg.slots = [dict(slot) for slot in self._pinned_slots]
        cfg.clamp()
        return cfg

    def _refresh_swatch(self) -> None:
        value = str(self.vars["accent"].get())
        custom = value != "system"
        self._swatch.configure(
            background=value if custom else "SystemButtonFace",
            text="Pick a colour…" if not custom else value.upper(),
        )

    # -- events ------------------------------------------------------------

    def on_change(self) -> None:
        self._refresh_swatch()
        self.status.configure(text="", foreground="#1a7f37")
        self.pump.request(self.collect())

    def on_slide(self, setting: Setting) -> None:
        if self._snapping:
            return
        var = self.vars[setting.key]
        value = float(var.get())
        if setting.step:
            value = min(max(round(value / setting.step) * setting.step, setting.low), setting.high)
            if abs(value - float(var.get())) > 1e-9:
                # Writing the variable drives the widget's command again, and
                # the guard is what stops that turning into a loop.
                self._snapping = True
                try:
                    var.set(value)
                finally:
                    self._snapping = False
        self._labels[setting.key].configure(text=format_percent(setting, value))
        self.on_change()

    def on_pick_colour(self) -> None:
        from tkinter import colorchooser

        current = str(self.vars["accent"].get())
        chosen = colorchooser.askcolor(
            color=current if current != "system" else "#4a9eff",
            parent=self.root,
            title="Highlight colour",
        )
        if chosen and chosen[1]:
            self.vars["accent"].set(str(chosen[1]))
            self.on_change()

    def on_reset_hotkey(self, setting: Setting) -> None:
        self.vars[setting.key].set(getattr(Config(), setting.key))
        self._refresh_hotkey_button(setting.key)
        self.on_change()

    def on_capture_hotkey(self, setting: Setting) -> None:
        """Listen for the next key chord or mouse side-button."""
        from . import win32_types as w
        from .hotkey import parse_hotkey

        button = self._hotkey_buttons[setting.key]
        button.configure(text="Press a shortcut…")
        self.root.update_idletasks()

        captured = _capture_hotkey(timeout_s=6.0)
        if captured is None:
            self._refresh_hotkey_button(setting.key)
            self.status.configure(text="No shortcut captured.", foreground="#666666")
            return
        if captured.kind == "key" and captured.code == w.VK_ESCAPE:
            self._refresh_hotkey_button(setting.key)
            return

        text = captured.text()
        self.vars[setting.key].set(text)
        self._refresh_hotkey_button(setting.key)
        self.on_change()
        self.status.configure(
            text=f"{setting.label} set to {parse_hotkey(text).label()}",
            foreground="#1a7f37",
        )

    def on_reset(self) -> None:
        self._load(Config())
        self.on_change()
        self.status.configure(text="Defaults restored — press Save to keep them.")

    def on_save(self) -> None:
        cfg = self.collect()
        if not cfg.save():
            self.status.configure(
                text=f"Could not write {config_path()}", foreground="#b3261e"
            )
            return
        _set_autostart(bool(self._autostart.get()))
        self.cfg = cfg
        self.saved = True
        self._stop_window()

    def on_cancel(self) -> None:
        self._stop_window()

    def _stop_window(self) -> None:
        self.pump.stop()
        poll_id = getattr(self, "_poll_id", None)
        if poll_id is not None:
            try:
                self.root.after_cancel(poll_id)
            except Exception:
                pass
        self.root.destroy()

    # -- preview -----------------------------------------------------------

    def _poll_preview(self) -> None:
        image = None
        try:
            while True:
                image = self.pump.out.get_nowait()
        except queue.Empty:
            pass
        if image is not None:
            self._show(image)
        if self.root.winfo_exists():
            self._poll_id = self.root.after(60, self._poll_preview)

    def _show(self, image) -> None:
        from PIL import ImageTk

        self._image_ref = ImageTk.PhotoImage(image.convert("RGB"))
        self.preview.configure(image=self._image_ref)

    def run(self) -> bool:
        self.root.mainloop()
        return self.saved


def _capture_hotkey(timeout_s: float = 6.0):
    """Block briefly waiting for a key or mouse-4/5 press. Returns a Hotkey."""
    import time

    from . import win32_types as w
    from .hotkey import Hotkey, MOUSE4, MOUSE5

    result: list = []
    kb_hook = None
    mouse_hook = None
    kb_proc = None
    mouse_proc = None

    def mods() -> tuple[bool, bool, bool, bool]:
        return (
            bool(w.user32.GetAsyncKeyState(w.VK_CONTROL) & 0x8000),
            bool(w.user32.GetAsyncKeyState(w.VK_MENU) & 0x8000),
            bool(w.user32.GetAsyncKeyState(w.VK_SHIFT) & 0x8000),
            bool(
                (w.user32.GetAsyncKeyState(w.VK_LWIN) & 0x8000)
                or (w.user32.GetAsyncKeyState(w.VK_RWIN) & 0x8000)
            ),
        )

    @w.HOOKPROC
    def on_key(n_code, wparam, lparam):
        if n_code == 0 and wparam in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN) and not result:
            info = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
            vk = int(info.vkCode)
            if vk in (
                w.VK_CONTROL,
                w.VK_LCONTROL,
                w.VK_RCONTROL,
                w.VK_SHIFT,
                w.VK_LSHIFT,
                w.VK_RSHIFT,
                w.VK_MENU,
                w.VK_LMENU,
                w.VK_RMENU,
                w.VK_LWIN,
                w.VK_RWIN,
            ):
                return w.user32.CallNextHookEx(kb_hook, n_code, wparam, lparam)
            ctrl, alt, shift, win_key = mods()
            # The trigger key itself may still report as held — ignore that.
            result.append(
                Hotkey(ctrl=ctrl, alt=alt, shift=shift, win=win_key, kind="key", code=vk)
            )
            return 1
        return w.user32.CallNextHookEx(kb_hook, n_code, wparam, lparam)

    @w.HOOKPROC
    def on_mouse(n_code, wparam, lparam):
        if n_code == 0 and wparam in (w.WM_XBUTTONDOWN, w.WM_XBUTTONDBLCLK) and not result:
            info = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
            button = int((info.mouseData >> 16) & 0xFFFF)
            if button in (MOUSE4, MOUSE5):
                ctrl, alt, shift, win_key = mods()
                result.append(
                    Hotkey(
                        ctrl=ctrl,
                        alt=alt,
                        shift=shift,
                        win=win_key,
                        kind="mouse",
                        code=button,
                    )
                )
                return 1
        return w.user32.CallNextHookEx(mouse_hook, n_code, wparam, lparam)

    kb_proc = on_key
    mouse_proc = on_mouse
    try:
        kb_hook = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, kb_proc, None, 0)
        mouse_hook = w.user32.SetWindowsHookExW(w.WH_MOUSE_LL, mouse_proc, None, 0)
        deadline = time.perf_counter() + timeout_s
        msg = w.MSG()
        while not result and time.perf_counter() < deadline:
            # Peek so the hooks run without freezing the machine for 6s.
            while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)
    finally:
        if mouse_hook:
            w.user32.UnhookWindowsHookEx(mouse_hook)
        if kb_hook:
            w.user32.UnhookWindowsHookEx(kb_hook)

    return result[0] if result else None


def _pick_exe(parent, ui: float = 1.0) -> str:
    """Ask for an executable, offering the apps that are open right now."""
    from tkinter import simpledialog

    from .config import _normalize_exe_names
    from .windows_enum import enumerate_windows

    choices: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        for app in enumerate_windows():
            name = app.exe_name.lower()
            if not name or name in seen:
                continue
            seen.add(name)
            label = app.app_name or name
            choices.append((name, f"{label}  ({name})"))
    except Exception:
        choices = []
    choices.sort(key=lambda item: item[1].lower())

    if choices:
        import tkinter as tk
        from tkinter import ttk

        result: list[str] = []
        dialog = tk.Toplevel(parent)
        dialog.title("Hide or pin an app")
        dialog.transient(parent)
        dialog.resizable(False, False)
        pad = max(1, int(round(10 * ui)))
        ttk.Label(dialog, text="Open apps on this PC, or type an executable name.").pack(
            anchor="w", padx=pad, pady=(pad, 4)
        )
        box = tk.Listbox(dialog, height=min(12, max(4, len(choices))), width=48)
        box.pack(fill="both", expand=True, padx=pad)
        for _name, label in choices:
            box.insert("end", label)
        typed = ttk.Entry(dialog, width=40)
        typed.pack(fill="x", padx=pad, pady=pad)
        typed.focus_set()

        def accept(_event=None):
            selection = box.curselection()
            if selection:
                result.append(choices[int(selection[0])][0])
            else:
                names = _normalize_exe_names([typed.get()])
                if names:
                    result.append(names[0])
            dialog.destroy()

        def cancel():
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=pad, pady=(0, pad))
        ttk.Button(buttons, text="OK", command=accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(0, pad))
        box.bind("<Double-Button-1>", accept)
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _e: cancel())
        dialog.grab_set()
        parent.wait_window(dialog)
        return result[0] if result else ""

    entered = simpledialog.askstring(
        "Executable",
        "Executable name (for example chrome.exe):",
        parent=parent,
    )
    names = _normalize_exe_names([entered or ""])
    return names[0] if names else ""


def _label_for(setting: Setting, value: Any) -> str:
    for candidate, label in setting.choices:
        if candidate == value:
            return label
    return setting.choices[0][1] if setting.choices else str(value)


def _value_for(setting: Setting, label: str) -> Any:
    for value, candidate in setting.choices:
        if candidate == label:
            return value
    return setting.choices[0][0] if setting.choices else label


def _autostart_enabled() -> bool:
    try:
        from . import autostart

        return bool(autostart.is_enabled())
    except Exception:
        return False


def _set_autostart(enabled: bool) -> None:
    try:
        from . import autostart

        if bool(autostart.is_enabled()) != enabled:
            autostart.toggle()
    except Exception:
        pass


def _gather_scene():
    """Screenshot and window list for the preview, before any of our UI exists."""
    from PIL import ImageGrab

    from . import win32_types as w
    from .preview import ScenePreview, _demo_apps
    from .windows_enum import enumerate_windows

    w.enable_dpi_awareness()

    point = w.POINT()
    w.user32.GetCursorPos(ctypes.byref(point))
    monitor = w.user32.MonitorFromPoint(point, w.MONITOR_DEFAULTTONEAREST)
    dpi = w.monitor_dpi(monitor)

    try:
        apps = enumerate_windows()
    except Exception:
        apps = []
    if len(apps) < 3:
        apps = (apps + _demo_apps(6))[:6]

    desktop = ImageGrab.grab()
    origin = (int(point.x), int(point.y))
    if not (0 <= origin[0] < desktop.width and 0 <= origin[1] < desktop.height):
        origin = (desktop.width * 3 // 4, desktop.height * 3 // 4)

    scene = ScenePreview(desktop, apps[:12], dpi=dpi, origin=origin)
    return scene, dpi


def _demo_scene():
    """Same window, but a fake desktop — nothing personal ends up in a screenshot."""
    from . import win32_types as w
    from .preview import demo_scene

    w.enable_dpi_awareness()
    return demo_scene(dpi=96), 96


def _save_screenshot(window: SettingsWindow, dest) -> int:
    """Print the settings window itself, not the desktop underneath it."""
    import time
    from pathlib import Path

    from . import win32_types as w
    from .windows_enum import _print_window_to_image

    dest = Path(dest)
    window.root.lift()
    try:
        window.root.attributes("-topmost", True)
    except Exception:
        pass

    deadline = time.perf_counter() + 4.0
    while time.perf_counter() < deadline:
        window.root.update()
        if window._image_ref is not None:
            break
        time.sleep(0.02)
    window.root.update_idletasks()
    time.sleep(0.2)
    window.root.update()

    hwnd = w.user32.FindWindowW(None, WINDOW_TITLE) or int(window.root.winfo_id())
    rect = w.RECT()
    if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        print("Could not locate the settings window", file=sys.stderr)
        window.on_cancel()
        return 1
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    image = _print_window_to_image(hwnd, width, height)
    if image is None:
        from PIL import ImageGrab

        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    window.on_cancel()
    if image is None:
        print("Could not capture the settings window", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(dest)
    print(f"Wrote {dest}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    flags = set(args)
    screenshot = None
    for i, arg in enumerate(args):
        if arg.startswith("--screenshot="):
            screenshot = arg.split("=", 1)[1]
        elif arg == "--screenshot":
            nxt = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None
            screenshot = nxt or "assets/settings-window.png"

    try:
        if screenshot or "--demo" in flags:
            scene, dpi = _demo_scene()
        else:
            scene, dpi = _gather_scene()
    except Exception as exc:  # pragma: no cover - depends on the display
        print(f"Could not prepare the preview: {exc}", file=sys.stderr)
        return 1

    cfg = Config() if screenshot else Config.load()
    window = SettingsWindow(cfg, scene, dpi=dpi)
    if screenshot:
        return _save_screenshot(window, screenshot)
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
