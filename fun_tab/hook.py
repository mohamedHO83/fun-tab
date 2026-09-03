"""Low-level keyboard hook that intercepts Alt+Tab.

The hook callback only records intent — never paints or does heavy work.
Windows will silently remove slow LL hooks, which makes the keyboard feel dead.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Callable, Optional

from . import win32_types as w


class AltTabHook:
    """
    Installs WH_KEYBOARD_LL. Idle cost is essentially zero — Windows only
    invokes the callback on keyboard events.
    """

    def __init__(self) -> None:
        self._hook = None
        self._proc = None
        self._alt_down = False
        self._lock = threading.Lock()
        self._pending_open = False
        self._pending_cycle = 0
        self._pending_commit = False
        self._pending_cancel = False
        self.is_open: Optional[Callable[[], bool]] = None
        self._ui_thread_id = 0

    def set_ui_thread(self, thread_id: int) -> None:
        self._ui_thread_id = int(thread_id)

    def install(self) -> None:
        @w.HOOKPROC
        def proc(n_code, wparam, lparam):
            if n_code == 0:  # HC_ACTION
                info = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
                if self._handle(int(wparam), info):
                    return 1  # swallow
            return w.user32.CallNextHookEx(self._hook, n_code, wparam, lparam)

        self._proc = proc
        self._hook = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, proc, None, 0)
        if not self._hook:
            raise OSError("SetWindowsHookExW failed — try running again")

    def uninstall(self) -> None:
        if self._hook:
            w.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def drain(self) -> dict:
        """Return and clear pending actions (call from the UI thread)."""
        with self._lock:
            out = {
                "open": self._pending_open,
                "cycle": self._pending_cycle,
                "commit": self._pending_commit,
                "cancel": self._pending_cancel,
            }
            self._pending_open = False
            self._pending_cycle = 0
            self._pending_commit = False
            self._pending_cancel = False
            return out

    def _wake_ui(self) -> None:
        """Unblock MsgWaitForMultipleObjects immediately."""
        if self._ui_thread_id:
            w.user32.PostThreadMessageW(self._ui_thread_id, w.WM_NULL, 0, 0)

    def _queue_open(self) -> None:
        with self._lock:
            self._pending_open = True
        self._wake_ui()

    def _queue_cycle(self, delta: int) -> None:
        with self._lock:
            self._pending_cycle += delta
        self._wake_ui()

    def _queue_commit(self) -> None:
        with self._lock:
            self._pending_commit = True
            self._pending_open = False
            self._pending_cycle = 0
        self._wake_ui()

    def _queue_cancel(self) -> None:
        with self._lock:
            self._pending_cancel = True
            self._pending_open = False
            self._pending_cycle = 0
            self._pending_commit = False
        self._wake_ui()

    def _alt_held(self) -> bool:
        return self._alt_down or bool(
            w.user32.GetAsyncKeyState(w.VK_MENU) & 0x8000
        )

    def _handle(self, wparam: int, info: w.KBDLLHOOKSTRUCT) -> bool:
        vk = info.vkCode
        is_down = wparam in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN)
        is_up = wparam in (w.WM_KEYUP, w.WM_SYSKEYUP)
        opened = bool(self.is_open and self.is_open())

        if vk in (w.VK_LMENU, w.VK_RMENU, w.VK_MENU):
            if is_down:
                self._alt_down = True
            elif is_up:
                self._alt_down = False
                if opened:
                    self._queue_commit()
                    # Pass Alt-up through — swallowing it leaves apps thinking Alt
                    # is still held (stuck keyboard / menu mode).
            return False

        if info.flags & w.LLKHF_ALTDOWN:
            self._alt_down = True

        if vk == w.VK_ESCAPE and is_down and opened:
            self._queue_cancel()
            return True

        if not is_down:
            return False

        # Wheel open: Tab / arrows always cycle so the keyboard stays usable.
        if opened and vk == w.VK_TAB:
            shift = w.user32.GetAsyncKeyState(w.VK_SHIFT) & 0x8000
            self._queue_cycle(-1 if shift else 1)
            return True

        if opened and vk in (w.VK_LEFT, w.VK_RIGHT):
            self._queue_cycle(-1 if vk == w.VK_LEFT else 1)
            return True

        # Wheel closed: Alt+Tab opens (and swallows OS switcher).
        if vk == w.VK_TAB and self._alt_held():
            self._queue_open()
            return True

        return False
