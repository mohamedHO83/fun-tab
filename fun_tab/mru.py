"""Most-recently-used window tracking.

``EnumWindows`` returns Z-order, which drifts away from what the user actually
touched last (topmost tool windows, always-on-top players, restored minimises).
A ``WINEVENT_OUTOFCONTEXT`` hook on foreground changes gives us the same
ordering the real Alt+Tab uses, so "tap Alt+Tab" always lands on the previous
window.
"""

from __future__ import annotations

import threading

from . import win32_types as w


class ForegroundTracker:
    def __init__(self, limit: int = 96) -> None:
        self._order: list[int] = []
        self._lock = threading.Lock()
        self._limit = limit
        self._hook = None
        self._proc = None

    def install(self) -> bool:
        @w.WINEVENTPROC
        def proc(_hook, event, hwnd, id_object, id_child, _thread, _time):
            if id_object != w.OBJID_WINDOW or id_child != 0 or not hwnd:
                return
            if event == w.EVENT_OBJECT_DESTROY:
                self.forget(int(hwnd))
            else:
                self.note(int(hwnd))

        self._proc = proc
        self._hook = w.user32.SetWinEventHook(
            w.EVENT_SYSTEM_FOREGROUND,
            w.EVENT_SYSTEM_FOREGROUND,
            None,
            proc,
            0,
            0,
            w.WINEVENT_OUTOFCONTEXT | w.WINEVENT_SKIPOWNPROCESS,
        )
        current = int(w.user32.GetForegroundWindow() or 0)
        if current:
            self.note(current)
        return bool(self._hook)

    def uninstall(self) -> None:
        if self._hook:
            w.user32.UnhookWinEvent(self._hook)
            self._hook = None
        self._proc = None

    def note(self, hwnd: int) -> None:
        """Record hwnd as the most recently used window."""
        root = int(w.user32.GetAncestor(hwnd, w.GA_ROOT) or hwnd) if hwnd else 0
        if not root:
            return
        with self._lock:
            if self._order and self._order[0] == root:
                return
            try:
                self._order.remove(root)
            except ValueError:
                pass
            self._order.insert(0, root)
            del self._order[self._limit :]

    def forget(self, hwnd: int) -> None:
        with self._lock:
            try:
                self._order.remove(int(hwnd))
            except ValueError:
                pass

    def rank(self, hwnd: int) -> int:
        """Position in the MRU list; unseen windows sort after known ones."""
        with self._lock:
            try:
                return self._order.index(int(hwnd))
            except ValueError:
                return self._limit + 1

    def snapshot(self) -> list[int]:
        with self._lock:
            return list(self._order)
