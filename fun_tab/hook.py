"""Low-level keyboard + mouse hooks that open the wheel and drive it.

The callbacks only record intent and wake the UI thread — they never paint or
allocate. Windows silently unhooks a low-level hook that takes too long, and
the symptom is a keyboard that stops responding, so these paths stay trivial.
"""

from __future__ import annotations

import ctypes
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from . import win32_types as w
from .hotkey import Hotkey, is_classic_alt_tab, modifiers_match, parse_hotkey

# Action kinds emitted to the UI thread.
OPEN = "open"
CYCLE = "cycle"
COMMIT = "commit"
CANCEL = "cancel"
CLOSE = "close"
MINIMIZE = "minimize"
HIDE = "hide"
PIN = "pin"
TYPE = "type"
BACKSPACE = "backspace"
CLEAR = "clear"
JUMP = "jump"
FIRST = "first"
LAST = "last"
SAME_APP = "same_app"
PREPARE = "prepare"  # get the backdrop ready before the open lands


@dataclass(frozen=True)
class Action:
    kind: str
    value: object = 0


class AltTabHook:
    """Installs WH_KEYBOARD_LL and WH_MOUSE_LL. Idle cost is zero off events."""

    def __init__(self) -> None:
        self._kb_hook = None
        self._mouse_hook = None
        self._kb_proc = None
        self._mouse_proc = None
        self._alt_down = False
        self._lock = threading.Lock()
        self._queue: deque[Action] = deque(maxlen=64)
        self._ui_thread_id = 0
        self._swallowed_tab = False

        # Wired up by the app.
        self.is_open: Optional[Callable[[], bool]] = None
        self.is_sticky: Optional[Callable[[], bool]] = None
        self.has_query: Optional[Callable[[], bool]] = None
        self.search_enabled = True
        self.digit_jump = True
        self.close_key_enabled = True
        # When True, leave native Alt+Tab alone (game compatibility).
        self.compat_active = False
        self.open_hotkey: Hotkey = parse_hotkey("alt+tab")
        self.open_sticky = False
        # What must stay held for a non-sticky open (classic Alt+Tab style).
        # "alt" / "mods" / "key" / "mouse" / None (sticky or closed).
        self._hold_kind: str | None = None
        self._hold_mods: frozenset[str] = frozenset()
        self._held_mouse: int | None = None
        self._held_vk: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def set_ui_thread(self, thread_id: int) -> None:
        self._ui_thread_id = int(thread_id)

    def set_open_hotkey(self, text: str) -> None:
        self.open_hotkey = parse_hotkey(text)

    def _want_sticky(self, *, force: bool = False) -> bool:
        if force or self.open_sticky:
            return True
        return False

    def install(self) -> None:
        if self._kb_hook:
            return  # already installed
        @w.HOOKPROC
        def kb_proc(n_code, wparam, lparam):
            if n_code == 0:  # HC_ACTION
                info = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
                try:
                    if self._handle_keyboard(int(wparam), info):
                        return 1  # swallow
                except Exception:
                    pass
            return w.user32.CallNextHookEx(self._kb_hook, n_code, wparam, lparam)

        @w.HOOKPROC
        def mouse_proc(n_code, wparam, lparam):
            if n_code == 0:
                info = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
                try:
                    if self._handle_mouse(int(wparam), info):
                        return 1
                except Exception:
                    pass
            return w.user32.CallNextHookEx(self._mouse_hook, n_code, wparam, lparam)

        self._kb_proc = kb_proc
        self._mouse_proc = mouse_proc
        self._kb_hook = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, kb_proc, None, 0)
        if not self._kb_hook:
            raise OSError("SetWindowsHookExW (keyboard) failed — try running again")
        self._mouse_hook = w.user32.SetWindowsHookExW(w.WH_MOUSE_LL, mouse_proc, None, 0)
        if not self._mouse_hook:
            # Keyboard still works; mouse shortcuts just will not.
            self._mouse_hook = None

    def uninstall(self) -> None:
        if self._mouse_hook:
            w.user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None
        if self._kb_hook:
            w.user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        self._held_mouse = None
        self._held_vk = None
        self._hold_kind = None
        self._hold_mods = frozenset()
        self._kb_proc = None
        self._mouse_proc = None

    @property
    def installed(self) -> bool:
        return bool(self._kb_hook)

    def _clear_hold(self) -> None:
        self._hold_kind = None
        self._hold_mods = frozenset()
        self._held_mouse = None
        self._held_vk = None

    def _arm_hold(self, hotkey: Hotkey) -> None:
        """Remember what must stay down until commit (classic hold-to-switch)."""
        self._clear_hold()
        if hotkey.kind == "mouse":
            self._hold_kind = "mouse"
            self._held_mouse = hotkey.code
            return
        if hotkey.alt:
            # Alt+Tab style: hold Alt; the trigger key can be released.
            self._hold_kind = "alt"
            return
        mods = frozenset(
            name
            for name, on in (
                ("ctrl", hotkey.ctrl),
                ("shift", hotkey.shift),
                ("win", hotkey.win),
            )
            if on
        )
        if mods:
            # Ctrl+Shift+U style: keep the modifiers held; U can be released.
            self._hold_kind = "mods"
            self._hold_mods = mods
            return
        # Bare key (e.g. F8): hold that key.
        self._hold_kind = "key"
        self._held_vk = hotkey.code

    def hold_released(self) -> bool:
        """True when the open hold was dropped — used as a safety valve."""
        if self._sticky() or self._hold_kind is None:
            return False
        opened = bool(self.is_open and self.is_open())
        if not opened:
            return False
        if self._hold_kind == "alt":
            return not self._alt_held()
        if self._hold_kind == "mods":
            current = self._mods()
            return any(not current[name] for name in self._hold_mods)
        if self._hold_kind == "key":
            if self._held_vk is None:
                return True
            return not bool(w.user32.GetAsyncKeyState(self._held_vk) & 0x8000)
        if self._hold_kind == "mouse":
            # XBUTTON state: VK_XBUTTON1 / VK_XBUTTON2
            if self._held_mouse == 1:
                return not bool(w.user32.GetAsyncKeyState(w.VK_XBUTTON1) & 0x8000)
            if self._held_mouse == 2:
                return not bool(w.user32.GetAsyncKeyState(w.VK_XBUTTON2) & 0x8000)
            return True
        return False

    # -- queue -------------------------------------------------------------

    def drain(self) -> list[Action]:
        """Pop every pending action, oldest first (call from the UI thread)."""
        with self._lock:
            actions = list(self._queue)
            self._queue.clear()
        return actions

    def _emit(self, kind: str, value: object = 0) -> None:
        with self._lock:
            self._queue.append(Action(kind, value))
        if self._ui_thread_id:
            w.user32.PostThreadMessageW(self._ui_thread_id, w.WM_NULL, 0, 0)

    # -- state helpers -----------------------------------------------------

    def _alt_held(self) -> bool:
        return self._alt_down or bool(w.user32.GetAsyncKeyState(w.VK_MENU) & 0x8000)

    @staticmethod
    def _shift_held() -> bool:
        return bool(w.user32.GetAsyncKeyState(w.VK_SHIFT) & 0x8000)

    @staticmethod
    def _ctrl_held() -> bool:
        return bool(w.user32.GetAsyncKeyState(w.VK_CONTROL) & 0x8000)

    @staticmethod
    def _win_held() -> bool:
        return bool(
            (w.user32.GetAsyncKeyState(w.VK_LWIN) & 0x8000)
            or (w.user32.GetAsyncKeyState(w.VK_RWIN) & 0x8000)
        )

    def _query_active(self) -> bool:
        return bool(self.has_query and self.has_query())

    def _sticky(self) -> bool:
        return bool(self.is_sticky and self.is_sticky())

    def _mods(self) -> dict[str, bool]:
        return {
            "ctrl": self._ctrl_held(),
            "alt": self._alt_held(),
            "shift": self._shift_held(),
            "win": self._win_held(),
        }

    # -- mouse -------------------------------------------------------------

    def _handle_mouse(self, wparam: int, info: w.MSLLHOOKSTRUCT) -> bool:
        button = int((info.mouseData >> 16) & 0xFFFF)
        opened = bool(self.is_open and self.is_open())
        hotkey = self.open_hotkey

        if wparam == w.WM_XBUTTONUP:
            if (
                opened
                and not self._sticky()
                and self._hold_kind == "mouse"
                and self._held_mouse is not None
                and button == self._held_mouse
            ):
                self._clear_hold()
                self._emit(COMMIT)
                return True
            return False

        if wparam not in (w.WM_XBUTTONDOWN, w.WM_XBUTTONDBLCLK):
            return False
        if opened:
            return False  # navigation stays on the wheel / keyboard

        if hotkey.kind != "mouse" or hotkey.code != button:
            return False
        if not modifiers_match(hotkey, **self._mods()):
            return False

        sticky = self._want_sticky()
        if sticky:
            self._clear_hold()
        else:
            self._arm_hold(hotkey)

        self._emit(PREPARE)
        self._emit(
            OPEN,
            {
                "sticky": sticky,
                "reverse": self._shift_held() and not hotkey.shift,
            },
        )
        return True

    # -- keyboard ----------------------------------------------------------

    def _handle_keyboard(self, wparam: int, info: w.KBDLLHOOKSTRUCT) -> bool:
        vk = int(info.vkCode)
        is_down = wparam in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN)
        is_up = wparam in (w.WM_KEYUP, w.WM_SYSKEYUP)
        opened = bool(self.is_open and self.is_open())

        if vk in (w.VK_LMENU, w.VK_RMENU, w.VK_MENU):
            if is_down:
                if not self._alt_down and not opened:
                    if self._should_prepare_on_alt():
                        self._emit(PREPARE)
                self._alt_down = True
            elif is_up:
                self._alt_down = False
                self._swallowed_tab = False
                if opened and not self._sticky() and self._hold_kind == "alt":
                    self._clear_hold()
                    self._emit(COMMIT)
            return False

        if info.flags & w.LLKHF_ALTDOWN:
            self._alt_down = True

        if is_up:
            if opened and not self._sticky():
                # Modifier hold (Ctrl+Shift+U): releasing any required modifier commits.
                if self._hold_kind == "mods":
                    name = _mod_name_for_vk(vk)
                    if name and name in self._hold_mods:
                        # Re-read after the up so a still-held sibling Ctrl doesn't false-commit.
                        if not self._mods()[name]:
                            self._clear_hold()
                            self._emit(COMMIT)
                            return True
                if (
                    self._hold_kind == "key"
                    and self._held_vk is not None
                    and vk == self._held_vk
                ):
                    self._clear_hold()
                    self._emit(COMMIT)
                    return True
            return False

        if not is_down:
            return False

        if opened:
            return self._handle_open(vk)
        return self._handle_closed(vk)

    def _should_prepare_on_alt(self) -> bool:
        hotkey = self.open_hotkey
        if hotkey.kind != "key" or not hotkey.alt:
            return False
        if self.compat_active and is_classic_alt_tab(hotkey):
            # Compat leaves Alt+Tab to Windows; only warm if Ctrl is also down
            # (Ctrl+Alt+Tab style) or the hotkey itself asks for Ctrl.
            return hotkey.ctrl or self._ctrl_held()
        return True

    # -- wheel closed ------------------------------------------------------

    def _handle_closed(self, vk: int) -> bool:
        hotkey = self.open_hotkey
        mods = self._mods()

        # Custom open shortcut (keyboard).
        if hotkey.kind == "key" and vk == hotkey.code:
            steal = True
            if self.compat_active and is_classic_alt_tab(hotkey):
                # Native Alt+Tab stays with Windows. Fall back to Ctrl+Alt+Tab
                # so Fun Tab is still reachable until they pick a custom chord.
                steal = mods["ctrl"]
            if steal and modifiers_match(hotkey, **mods):
                sticky = self._want_sticky(
                    force=(
                        (mods["ctrl"] and not hotkey.ctrl)
                        or (self.compat_active and is_classic_alt_tab(hotkey))
                    )
                )
                if sticky:
                    self._clear_hold()
                else:
                    # Classic hold: keep Alt / modifiers down; trigger may lift.
                    hold = hotkey
                    if self.compat_active and is_classic_alt_tab(hotkey) and mods["ctrl"]:
                        # Ctrl+Alt+Tab fallback while compat owns plain Alt+Tab.
                        from .hotkey import Hotkey as HK

                        hold = HK(ctrl=True, alt=True, kind="key", code=w.VK_TAB)
                    self._arm_hold(hold)
                reverse = mods["shift"] and not hotkey.shift
                self._swallowed_tab = True
                self._emit(OPEN, {"sticky": sticky, "reverse": reverse})
                return True

        # Warm backdrop when Ctrl joins an Alt-based open chord, or when
        # game-compat is using Ctrl+Alt+Tab as the fallback for classic Alt+Tab.
        if (
            vk in (w.VK_CONTROL, w.VK_LCONTROL, w.VK_RCONTROL)
            and self._alt_held()
            and hotkey.kind == "key"
            and hotkey.alt
            and (hotkey.ctrl or (self.compat_active and is_classic_alt_tab(hotkey)))
        ):
            self._emit(PREPARE)
            return False

        # Same-app Alt+` — only when we are allowed to own Alt chords.
        if (
            not self.compat_active
            and self._alt_held()
            and vk == w.VK_OEM_3
            and not (hotkey.kind == "key" and hotkey.code == w.VK_OEM_3)
        ):
            self._swallowed_tab = True
            self._emit(OPEN, {"same_app": True, "reverse": self._shift_held()})
            return True

        return False

    # -- wheel open --------------------------------------------------------

    def _handle_open(self, vk: int) -> bool:
        typing = self.search_enabled and self._query_active()

        if vk == w.VK_ESCAPE:
            self._emit(CLEAR if typing else CANCEL)
            return True

        if vk == w.VK_TAB:
            self._emit(CYCLE, -1 if self._shift_held() else 1)
            return True

        if vk in (w.VK_LEFT, w.VK_UP):
            self._emit(CYCLE, -1)
            return True
        if vk in (w.VK_RIGHT, w.VK_DOWN):
            self._emit(CYCLE, 1)
            return True

        if vk == w.VK_HOME:
            self._emit(FIRST)
            return True
        if vk == w.VK_END:
            self._emit(LAST)
            return True

        if vk == w.VK_RETURN:
            self._emit(COMMIT)
            return True

        if vk == w.VK_SPACE and not typing:
            self._emit(COMMIT)
            return True

        if vk == w.VK_BACK:
            self._emit(BACKSPACE)
            return True

        if vk == w.VK_OEM_3:
            self._emit(SAME_APP, -1 if self._shift_held() else 1)
            return True

        if self.close_key_enabled:
            close_combo = vk == w.VK_DELETE or (
                self._ctrl_held() and vk in (0x57, 0x51)  # Ctrl+W / Ctrl+Q
            )
            if close_combo:
                self._emit(CLOSE)
                return True
            if self._ctrl_held() and vk == 0x4D:  # Ctrl+M
                self._emit(MINIMIZE)
                return True

        if self._ctrl_held() and vk == 0x48:  # Ctrl+H
            self._emit(HIDE)
            return True
        if self._ctrl_held() and vk == 0x50:  # Ctrl+P
            self._emit(PIN)
            return True

        digit = _digit_for(vk)
        if digit is not None and self.digit_jump and not typing:
            self._emit(JUMP, digit)
            return True

        if self.search_enabled and not self._ctrl_held():
            char = _printable_for(vk)
            if char:
                self._emit(TYPE, char)
                return True

        return True


def _mod_name_for_vk(vk: int) -> str | None:
    if vk in (w.VK_CONTROL, w.VK_LCONTROL, w.VK_RCONTROL):
        return "ctrl"
    if vk in (w.VK_SHIFT, w.VK_LSHIFT, w.VK_RSHIFT):
        return "shift"
    if vk in (w.VK_LWIN, w.VK_RWIN):
        return "win"
    if vk in (w.VK_MENU, w.VK_LMENU, w.VK_RMENU):
        return "alt"
    return None


def _digit_for(vk: int) -> int | None:
    if 0x30 <= vk <= 0x39:
        return vk - 0x30
    if w.VK_NUMPAD0 <= vk <= w.VK_NUMPAD0 + 9:
        return vk - w.VK_NUMPAD0
    return None


def _printable_for(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:  # A-Z
        return chr(vk).lower()
    digit = _digit_for(vk)
    if digit is not None:
        return str(digit)
    if vk == w.VK_SPACE:
        return " "
    char = w.vk_to_char(vk)
    if char and (char.isalnum() or char in "-_.+#/&"):
        return char.lower()
    return ""
