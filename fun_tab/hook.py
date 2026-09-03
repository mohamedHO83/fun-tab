"""Low-level keyboard hook that intercepts Alt+Tab and drives the wheel.

The callback only records intent and wakes the UI thread — it never paints or
allocates. Windows silently unhooks a low-level hook that takes too long, and
the symptom is a keyboard that stops responding, so this path stays trivial.
"""

from __future__ import annotations

import ctypes
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from . import win32_types as w

# Action kinds emitted to the UI thread.
OPEN = "open"
CYCLE = "cycle"
COMMIT = "commit"
CANCEL = "cancel"
CLOSE = "close"
MINIMIZE = "minimize"
TYPE = "type"
BACKSPACE = "backspace"
CLEAR = "clear"
JUMP = "jump"
FIRST = "first"
LAST = "last"
SAME_APP = "same_app"
PREPARE = "prepare"  # Alt went down: get the backdrop ready before Tab lands


@dataclass(frozen=True)
class Action:
    kind: str
    value: object = 0


class AltTabHook:
    """Installs WH_KEYBOARD_LL. Idle cost is zero — it only runs on key events."""

    def __init__(self) -> None:
        self._hook = None
        self._proc = None
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

    # -- lifecycle ---------------------------------------------------------

    def set_ui_thread(self, thread_id: int) -> None:
        self._ui_thread_id = int(thread_id)

    def install(self) -> None:
        @w.HOOKPROC
        def proc(n_code, wparam, lparam):
            if n_code == 0:  # HC_ACTION
                info = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
                try:
                    if self._handle(int(wparam), info):
                        return 1  # swallow
                except Exception:
                    pass  # never let an exception kill the hook
            return w.user32.CallNextHookEx(self._hook, n_code, wparam, lparam)

        self._proc = proc
        self._hook = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, proc, None, 0)
        if not self._hook:
            raise OSError("SetWindowsHookExW failed — try running again")

    def uninstall(self) -> None:
        if self._hook:
            w.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

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

    def _query_active(self) -> bool:
        return bool(self.has_query and self.has_query())

    def _sticky(self) -> bool:
        return bool(self.is_sticky and self.is_sticky())

    # -- core --------------------------------------------------------------

    def _handle(self, wparam: int, info: w.KBDLLHOOKSTRUCT) -> bool:
        vk = int(info.vkCode)
        is_down = wparam in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN)
        is_up = wparam in (w.WM_KEYUP, w.WM_SYSKEYUP)
        opened = bool(self.is_open and self.is_open())

        if vk in (w.VK_LMENU, w.VK_RMENU, w.VK_MENU):
            if is_down:
                if not self._alt_down and not opened:
                    # Blurring the desktop means reading it back, which is far
                    # too slow to do once Tab arrives. Start it on the way in;
                    # auto-repeat and Alt-as-a-modifier presses are filtered
                    # out here, and the overlay ignores it if a recent capture
                    # is still good.
                    self._emit(PREPARE)
                self._alt_down = True
            elif is_up:
                self._alt_down = False
                self._swallowed_tab = False
                if opened and not self._sticky():
                    self._emit(COMMIT)
                # Alt-up always passes through: swallowing it leaves apps
                # believing Alt is still held (stuck modifier / menu mode).
            return False

        if info.flags & w.LLKHF_ALTDOWN:
            self._alt_down = True

        if not is_down:
            return False

        if opened:
            return self._handle_open(vk)
        return self._handle_closed(vk)

    # -- wheel closed ------------------------------------------------------

    def _handle_closed(self, vk: int) -> bool:
        if not self._alt_held():
            return False

        if vk == w.VK_TAB:
            self._swallowed_tab = True
            sticky = self._ctrl_held()
            reverse = self._shift_held()
            self._emit(OPEN, {"sticky": sticky, "reverse": reverse})
            return True

        if vk == w.VK_OEM_3:  # Alt+` — cycle windows of the current app
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

        # Number keys jump straight to a slice when nothing is typed yet.
        digit = _digit_for(vk)
        if digit is not None and self.digit_jump and not typing:
            self._emit(JUMP, digit)
            return True

        if self.search_enabled and not self._ctrl_held():
            char = _printable_for(vk)
            if char:
                self._emit(TYPE, char)
                return True

        # Anything else is swallowed so stray keys never reach the app behind.
        return True


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
