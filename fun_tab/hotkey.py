"""Open-shortcut strings: keyboard chords and mouse side-buttons.

Stored in config as a single string, e.g. ``alt+tab``, ``ctrl+alt+tab``,
``mouse4``, ``ctrl+mouse5``. Parsing and matching are pure so the settings
window and the hook share one rule without needing a display.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import win32_types as w

# Mouse side buttons as Windows reports them in MSLLHOOKSTRUCT.mouseData.
MOUSE4 = 1  # XBUTTON1 — usually "Back"
MOUSE5 = 2  # XBUTTON2 — usually "Forward"

_MOD_ORDER = ("ctrl", "alt", "shift", "win")

_KEY_NAMES: dict[str, int] = {
    "tab": w.VK_TAB,
    "space": w.VK_SPACE,
    "escape": w.VK_ESCAPE,
    "esc": w.VK_ESCAPE,
    "enter": w.VK_RETURN,
    "return": w.VK_RETURN,
    "backspace": w.VK_BACK,
    "delete": w.VK_DELETE,
    "home": w.VK_HOME,
    "end": w.VK_END,
    "left": w.VK_LEFT,
    "right": w.VK_RIGHT,
    "up": w.VK_UP,
    "down": w.VK_DOWN,
    "grave": w.VK_OEM_3,
    "backtick": w.VK_OEM_3,
    "`": w.VK_OEM_3,
}
for _i in range(1, 13):
    _KEY_NAMES[f"f{_i}"] = 0x70 + _i - 1
for _ch in "abcdefghijklmnopqrstuvwxyz":
    _KEY_NAMES[_ch] = ord(_ch.upper())
for _d in "0123456789":
    _KEY_NAMES[_d] = ord(_d)

_VK_TO_NAME = {vk: name for name, vk in _KEY_NAMES.items() if name not in ("esc", "return", "`")}
# Prefer the pretty names when several map to the same vk.
_VK_TO_NAME[w.VK_ESCAPE] = "escape"
_VK_TO_NAME[w.VK_RETURN] = "enter"
_VK_TO_NAME[w.VK_OEM_3] = "backtick"

_MOUSE_NAMES = {
    "mouse4": MOUSE4,
    "mouse5": MOUSE5,
    "xbutton1": MOUSE4,
    "xbutton2": MOUSE5,
    "back": MOUSE4,
    "forward": MOUSE5,
}
_MOUSE_TO_NAME = {MOUSE4: "mouse4", MOUSE5: "mouse5"}


@dataclass(frozen=True)
class Hotkey:
    """One open shortcut: optional modifiers plus a key or a mouse button."""

    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    win: bool = False
    kind: str = "key"  # key | mouse
    code: int = w.VK_TAB

    @property
    def sticky(self) -> bool:
        """Whether releasing modifiers should leave the wheel open.

        Mouse opens, and chords without Alt, have nothing natural to "let go
        of to commit", so they open sticky.
        """
        return self.kind == "mouse" or not self.alt

    def text(self) -> str:
        parts: list[str] = []
        if self.ctrl:
            parts.append("ctrl")
        if self.alt:
            parts.append("alt")
        if self.shift:
            parts.append("shift")
        if self.win:
            parts.append("win")
        if self.kind == "mouse":
            parts.append(_MOUSE_TO_NAME.get(self.code, f"mouse{self.code + 3}"))
        else:
            parts.append(_VK_TO_NAME.get(self.code, f"vk{self.code}"))
        return "+".join(parts)

    def label(self) -> str:
        """Human-readable form for the settings window."""
        parts: list[str] = []
        if self.ctrl:
            parts.append("Ctrl")
        if self.alt:
            parts.append("Alt")
        if self.shift:
            parts.append("Shift")
        if self.win:
            parts.append("Win")
        if self.kind == "mouse":
            parts.append({MOUSE4: "Mouse 4 (Back)", MOUSE5: "Mouse 5 (Forward)"}.get(self.code, "Mouse"))
        else:
            name = _VK_TO_NAME.get(self.code, f"Key {self.code}")
            parts.append(name.upper() if len(name) == 1 else name.title())
        return " + ".join(parts)


def parse_hotkey(text: str | None) -> Hotkey:
    """Turn a config string into a Hotkey. Bad input becomes Alt+Tab."""
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        return Hotkey(alt=True, kind="key", code=w.VK_TAB)

    parts = [p for p in raw.split("+") if p]
    ctrl = alt = shift = win = False
    trigger: str | None = None
    for part in parts:
        if part in ("ctrl", "control", "ctl"):
            ctrl = True
        elif part in ("alt", "menu"):
            alt = True
        elif part in ("shift",):
            shift = True
        elif part in ("win", "super", "meta"):
            win = True
        else:
            trigger = part

    if trigger is None:
        return Hotkey(alt=True, kind="key", code=w.VK_TAB)

    if trigger in _MOUSE_NAMES:
        return Hotkey(
            ctrl=ctrl,
            alt=alt,
            shift=shift,
            win=win,
            kind="mouse",
            code=_MOUSE_NAMES[trigger],
        )

    vk = _KEY_NAMES.get(trigger)
    if vk is None:
        return Hotkey(alt=True, kind="key", code=w.VK_TAB)
    return Hotkey(ctrl=ctrl, alt=alt, shift=shift, win=win, kind="key", code=vk)


def modifiers_match(
    hotkey: Hotkey,
    *,
    ctrl: bool,
    alt: bool,
    shift: bool,
    win: bool = False,
) -> bool:
    """Required modifiers must be held; extras are allowed for sticky/reverse.

    Alt+Tab still accepts Ctrl (sticky) and Shift (reverse). A chord that
    asks for Ctrl requires it, and so on.
    """
    if hotkey.ctrl and not ctrl:
        return False
    if hotkey.alt and not alt:
        return False
    if hotkey.shift and not shift:
        return False
    if hotkey.win and not win:
        return False
    # If the hotkey itself does not ask for a modifier, still allow the
    # usual sticky/reverse extras when the trigger is Tab under Alt.
    return True


def is_classic_alt_tab(hotkey: Hotkey) -> bool:
    """The default Windows gesture we must not steal in game-compat mode."""
    return (
        hotkey.kind == "key"
        and hotkey.code == w.VK_TAB
        and hotkey.alt
        and not hotkey.ctrl
        and not hotkey.shift
        and not hotkey.win
    )
