"""Enumerate switchable windows, extract icons, capture previews, activate targets."""

from __future__ import annotations

import ctypes
import os
import shutil
import threading
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional

import numpy as np
from PIL import Image

from . import win32_types as w
from .privacy import excluded_exes

ICON_PX = 96
DWMWA_EXTENDED_FRAME_BOUNDS = 9

w.shell32.ExtractIconExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.HICON),
    ctypes.POINTER(wintypes.HICON),
    wintypes.UINT,
]
w.shell32.ExtractIconExW.restype = wintypes.UINT
w.user32.DestroyIcon.argtypes = [wintypes.HICON]
w.user32.DestroyIcon.restype = wintypes.BOOL

_icon_lock = threading.Lock()
_icon_by_hwnd: dict[int, Optional[Image.Image]] = {}
_icon_by_exe: dict[str, Optional[Image.Image]] = {}
_appname_by_exe: dict[str, str] = {}
_exe_by_pid: dict[int, str] = {}
_aumid_by_hwnd: dict[int, str] = {}


@dataclass
class AppWindow:
    hwnd: int
    title: str
    class_name: str
    pid: int
    icon: Optional[Image.Image] = field(default=None, repr=False)
    exe_path: str = ""
    app_name: str = ""
    minimized: bool = False
    maximized: bool = False
    # The taskbar's own grouping identity. Empty for most plain Win32 apps.
    aumid: str = ""
    # Position in the Z-order EnumWindows handed us. Stable while a window
    # lives, unlike MRU rank, so `` ` `` can cycle an app's windows in an order
    # that does not rearrange underneath the user as they cycle it.
    z_index: int = 0
    # When the wheel is grouped, the face window carries how many peers it stands for.
    group_count: int = 1
    peer_hwnds: tuple[int, ...] = ()
    # --- pinned lane -----------------------------------------------------
    # Lane entries are *declared* by config rather than discovered by
    # enumeration, which is what lets a slot survive closing the app.
    slot: int = -1
    launch: str = ""
    running: bool = True

    @property
    def is_pinned(self) -> bool:
        return self.slot >= 0

    @property
    def is_dead(self) -> bool:
        """A pinned slot whose app is not running: selecting it launches."""
        return self.slot >= 0 and not self.running

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe_path) if self.exe_path else ""

    @property
    def display_name(self) -> str:
        """Primary label: the window title, minus a redundant app-name suffix."""
        title = (self.title or "").strip()
        app = (self.app_name or "").strip()
        if not title:
            return app or self.class_name or f"Window {self.hwnd}"
        if app:
            for sep in (" - ", " — ", " – ", " | "):
                suffix = f"{sep}{app}"
                if title.endswith(suffix) and len(title) > len(suffix):
                    return title[: -len(suffix)].strip()
            if title == app:
                return app
        return title

    @property
    def subtitle(self) -> str:
        """Secondary label: which application the window belongs to."""
        app = (self.app_name or "").strip()
        if app and app.lower() != self.display_name.strip().lower():
            return app
        return ""

    @property
    def search_text(self) -> str:
        return f"{self.title} {self.app_name} {self.exe_name}".lower()

    def label(self, *, title_privacy: str = "full") -> str:
        """What the hub / preview should show for this window."""
        if title_privacy == "app":
            return (self.app_name or self.exe_name or self.display_name).strip()
        return self.display_name

    def matches_query(self, needle: str, *, title_privacy: str = "full") -> bool:
        if not needle:
            return True
        if title_privacy == "app":
            hay = f"{self.app_name} {self.exe_name}".lower()
        else:
            hay = self.search_text
        return needle in hay


# ---------------------------------------------------------------------------
# Basic window queries
# ---------------------------------------------------------------------------


def _window_title(hwnd: int) -> str:
    length = w.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    w.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    w.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_cloaked(hwnd: int) -> bool:
    try:
        cloaked = wintypes.DWORD()
        DWMWA_CLOAKED = 14
        hr = w.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


SKIP_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "Windows.UI.Core.CoreWindow",
        "Windows.UI.Composition.DesktopWindowContentBridge",
        "XamlExplorerHostIslandWindow",
        "ForegroundStaging",
        "MultitaskingViewFrame",
        "TaskListThumbnailWnd",
        "EdgeUiInputTopWndClass",
    }
)


def _is_alt_tab_candidate(hwnd: int, exclude: set[int]) -> bool:
    if hwnd in exclude:
        return False
    if not w.user32.IsWindowVisible(hwnd):
        return False

    GW_OWNER = 4
    if w.user32.GetWindow(hwnd, GW_OWNER):
        return False

    if not _window_title(hwnd).strip():
        return False

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    ex = w.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
        return False

    if _is_cloaked(hwnd):
        return False

    class_name = _class_name(hwnd)
    if class_name in SKIP_CLASSES:
        return False
    return True


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def _draw_icon_on(hicon: int, size: int, background: int) -> Optional[np.ndarray]:
    """Rasterise an icon over a solid background and read back RGB."""
    screen_dc = w.user32.GetDC(0)
    if not screen_dc:
        return None
    mem_dc = 0
    bmp = 0
    try:
        mem_dc = w.gdi32.CreateCompatibleDC(screen_dc)
        bmp = w.gdi32.CreateCompatibleBitmap(screen_dc, size, size)
        if not mem_dc or not bmp:
            return None
        old = w.gdi32.SelectObject(mem_dc, bmp)

        brush = w.gdi32.CreateSolidBrush(background)
        rect = w.RECT(0, 0, size, size)
        w.user32.FillRect(mem_dc, ctypes.byref(rect), brush)
        w.gdi32.DeleteObject(brush)

        w.user32.DrawIconEx(mem_dc, 0, 0, hicon, size, size, 0, None, 0x0003)

        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = w.BI_RGB

        buf = (ctypes.c_ubyte * (size * size * 4))()
        got = w.gdi32.GetDIBits(
            mem_dc, bmp, 0, size, buf, ctypes.byref(bmi), w.DIB_RGB_COLORS
        )
        w.gdi32.SelectObject(mem_dc, old)
        if not got:
            return None
        arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(size, size, 4)
        return arr[..., 2::-1].astype(np.int16)  # BGRA -> RGB
    finally:
        if bmp:
            w.gdi32.DeleteObject(bmp)
        if mem_dc:
            w.gdi32.DeleteDC(mem_dc)
        w.user32.ReleaseDC(0, screen_dc)


def _hicon_to_image(hicon: int, size: int = ICON_PX) -> Optional[Image.Image]:
    """Recover true per-pixel alpha by drawing the icon on black and on white.

    Over black a pixel reads ``C*a``; over white it reads ``C*a + (1-a)*255``.
    Subtracting gives the alpha exactly, which keeps dark logos intact — the
    previous "make near-black transparent" trick punched holes in them.
    """
    if not hicon:
        return None

    on_black = _draw_icon_on(hicon, size, 0x000000)
    if on_black is None:
        return None
    on_white = _draw_icon_on(hicon, size, 0xFFFFFF)
    if on_white is None:
        return None

    diff = np.clip(on_white - on_black, 0, 255)
    a = 255 - diff.max(axis=2)
    a = np.clip(a, 0, 255).astype(np.uint16)
    if int(a.max()) == 0:
        return None

    safe = np.maximum(a, 1)[..., None]
    rgb = np.clip(on_black.astype(np.int32) * 255 // safe, 0, 255).astype(np.uint8)

    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = a.astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _icon_from_exe(path: str) -> Optional[Image.Image]:
    if not path:
        return None
    large = wintypes.HICON()
    try:
        count = w.shell32.ExtractIconExW(path, 0, ctypes.byref(large), None, 1)
    except OSError:
        return None
    if count <= 0 or not large.value:
        return None
    try:
        return _hicon_to_image(int(large.value))
    finally:
        try:
            w.user32.DestroyIcon(large)
        except OSError:
            pass


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", ctypes.c_wchar * 260),
        ("szTypeName", ctypes.c_wchar * 80),
    ]


_SHGFI_ICON = 0x000000100
_SHGFI_LARGEICON = 0x000000000
_shell_info_ready = False


def _icon_from_shell(path: str) -> Optional[Image.Image]:
    """The icon Explorer shows for this file — including a shortcut's custom one.

    ExtractIconEx often returns nothing for .lnk files; SHGetFileInfo is the
    same lookup the desktop uses.
    """
    if not path:
        return None
    global _shell_info_ready
    if not _shell_info_ready:
        w.shell32.SHGetFileInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_SHFILEINFOW),
            wintypes.UINT,
            wintypes.UINT,
        ]
        w.shell32.SHGetFileInfoW.restype = ctypes.c_void_p
        _shell_info_ready = True
    info = _SHFILEINFOW()
    try:
        result = w.shell32.SHGetFileInfoW(
            path, 0, ctypes.byref(info), ctypes.sizeof(info), _SHGFI_ICON | _SHGFI_LARGEICON
        )
    except OSError:
        return None
    if not result or not info.hIcon:
        return None
    try:
        return _hicon_to_image(int(info.hIcon))
    finally:
        try:
            w.user32.DestroyIcon(info.hIcon)
        except OSError:
            pass


def _icon_from_file(path: str) -> Optional[Image.Image]:
    """Best available icon for an .exe or a shortcut on disk."""
    if not path:
        return None
    return _icon_from_exe(path) or _icon_from_shell(path)


def _extract_icon(hwnd: int, exe_path: str) -> Optional[Image.Image]:
    with _icon_lock:
        if hwnd in _icon_by_hwnd:
            return _icon_by_hwnd[hwnd]
        cached = _icon_by_exe.get(exe_path) if exe_path else None
    if cached is not None:
        with _icon_lock:
            _icon_by_hwnd[hwnd] = cached
        return cached

    icon: Optional[Image.Image] = None
    for which in (w.ICON_BIG, w.ICON_SMALL2, w.ICON_SMALL):
        hicon = w.send_message_timeout(hwnd, w.WM_GETICON, which, 0, ms=60)
        icon = _hicon_to_image(hicon)
        if icon is not None:
            break

    if icon is None:
        for index in (w.GCLP_HICON, w.GCLP_HICONSM):
            icon = _hicon_to_image(w.get_class_long(hwnd, index))
            if icon is not None:
                break

    if icon is None:
        icon = _icon_from_file(exe_path)

    if icon is None:
        icon = _placeholder_icon()

    with _icon_lock:
        _icon_by_hwnd[hwnd] = icon
        if exe_path:
            _icon_by_exe.setdefault(exe_path, icon)
    return icon


def _placeholder_icon() -> Image.Image:
    from PIL import ImageDraw

    img = Image.new("RGBA", (ICON_PX, ICON_PX), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, 88, 88), radius=20, fill=(196, 202, 214, 235))
    draw.rounded_rectangle((8, 8, 88, 30), radius=10, fill=(150, 158, 176, 235))
    return img


def _icon_key(name: str) -> str:
    raw = os.path.basename(str(name or "").strip()).lower()
    if not raw:
        return ""
    keep = []
    for ch in raw:
        keep.append(ch if ch.isalnum() or ch in "._-" else "_")
    return "".join(keep)[:80]


def stock_icon(name: str, icon: Optional[Image.Image]) -> None:
    """Remember a pin's logo so the closed slot can show it later."""
    key = _icon_key(name)
    if not key or icon is None:
        return
    try:
        from .config import _ensure_private_dir, icon_dir

        folder = icon_dir()
        _ensure_private_dir(folder)
        path = folder / f"{key}.png"
        icon.convert("RGBA").resize((ICON_PX, ICON_PX), Image.Resampling.LANCZOS).save(
            path, format="PNG"
        )
    except OSError:
        return
    with _icon_lock:
        _icon_by_exe[key] = icon
        _icon_by_exe[name] = icon


def load_stocked_icon(name: str) -> Optional[Image.Image]:
    key = _icon_key(name)
    if not key:
        return None
    with _icon_lock:
        cached = _icon_by_exe.get(key) or _icon_by_exe.get(name)
    if cached is not None:
        return cached
    try:
        from .config import icon_dir

        path = icon_dir() / f"{key}.png"
        if not path.is_file():
            return None
        icon = Image.open(path).convert("RGBA")
    except OSError:
        return None
    with _icon_lock:
        _icon_by_exe.setdefault(key, icon)
    return icon


def prune_icon_cache(live_hwnds: Iterable[int]) -> None:
    live = set(int(h) for h in live_hwnds)
    with _icon_lock:
        for hwnd in [h for h in _icon_by_hwnd if h not in live]:
            _icon_by_hwnd.pop(hwnd, None)
        for hwnd in [h for h in _aumid_by_hwnd if h not in live]:
            _aumid_by_hwnd.pop(hwnd, None)


def _app_id(hwnd: int) -> str:
    """Cached AppUserModelID. One COM call per window, once per window's life."""
    with _icon_lock:
        cached = _aumid_by_hwnd.get(hwnd)
    if cached is not None:
        return cached
    value = w.window_app_id(hwnd)
    with _icon_lock:
        _aumid_by_hwnd[hwnd] = value
    return value


# ---------------------------------------------------------------------------
# Application names (from the executable's version resource)
# ---------------------------------------------------------------------------


def _file_description(path: str) -> str:
    if not path:
        return ""
    cached = _appname_by_exe.get(path)
    if cached is not None:
        return cached

    name = os.path.splitext(os.path.basename(path))[0]
    result = name.replace("_", " ").title() if name else ""
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]

        size = version.GetFileVersionInfoSizeW(path, None)
        if size:
            data = ctypes.create_string_buffer(size)
            if version.GetFileVersionInfoW(path, 0, size, data):
                block = ctypes.c_void_p()
                length = wintypes.UINT()
                if version.VerQueryValueW(
                    data,
                    r"\VarFileInfo\Translation",
                    ctypes.byref(block),
                    ctypes.byref(length),
                ) and length.value >= 4:
                    codes = ctypes.cast(
                        block, ctypes.POINTER(wintypes.WORD * 2)
                    ).contents
                    key = rf"\StringFileInfo\{codes[0]:04x}{codes[1]:04x}\FileDescription"
                    text = ctypes.c_void_p()
                    if version.VerQueryValueW(
                        data, key, ctypes.byref(text), ctypes.byref(length)
                    ) and text.value:
                        # Read to the NUL: the reported length is not a reliable
                        # character count and over-reads into the next entry.
                        value = ctypes.wstring_at(text.value).strip()
                        if value and value.isprintable() and len(value) <= 64:
                            result = value
    except Exception:
        pass

    _appname_by_exe[path] = result
    return result


def _exe_for_pid(pid: int) -> str:
    if not pid:
        return ""
    cached = _exe_by_pid.get(pid)
    if cached is not None:
        return cached
    path = w.process_image_path(pid)
    _exe_by_pid[pid] = path
    return path


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def enumerate_windows(
    self_hwnd: int | None = None,
    *,
    exclude_hwnds: Iterable[int] = (),
    exclude_exes: Iterable[str] = (),
    exclude_titles: Iterable[str] = (),
    rank: Optional[Callable[[int], int]] = None,
    minimized_last: bool = False,
) -> list[AppWindow]:
    exclude = {int(h) for h in exclude_hwnds if h}
    if self_hwnd:
        exclude.add(int(self_hwnd))
    exclude.discard(0)

    blocked_exes = excluded_exes(exclude_exes)
    blocked_titles = [t.lower() for t in exclude_titles if t]

    hwnds: list[int] = []

    @w.WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        if _is_alt_tab_candidate(int(hwnd), exclude):
            hwnds.append(int(hwnd))
        return True

    w.user32.EnumWindows(enum_proc, 0)

    result: list[AppWindow] = []
    for z_index, hwnd in enumerate(hwnds):
        pid = wintypes.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_path = _exe_for_pid(int(pid.value))
        exe_name = os.path.basename(exe_path).lower()
        if exe_name and exe_name in blocked_exes:
            continue
        title = _window_title(hwnd)
        low_title = title.lower()
        if any(pattern in low_title for pattern in blocked_titles):
            continue
        result.append(
            AppWindow(
                hwnd=hwnd,
                title=title,
                class_name=_class_name(hwnd),
                pid=int(pid.value),
                icon=_extract_icon(hwnd, exe_path),
                exe_path=exe_path,
                app_name=_file_description(exe_path),
                minimized=bool(w.user32.IsIconic(hwnd)),
                maximized=bool(w.user32.IsZoomed(hwnd)),
                aumid=_app_id(hwnd),
                z_index=z_index,
            )
        )

    result = order_windows(result, hwnds, rank=rank, minimized_last=minimized_last)

    prune_icon_cache(hwnds)
    live_pids = {a.pid for a in result}
    for pid in [p for p in _exe_by_pid if p not in live_pids]:
        _exe_by_pid.pop(pid, None)  # PIDs get recycled; never trust a dead one
    return result


def order_windows(
    apps: list[AppWindow],
    discovery_order: Iterable[int] = (),
    *,
    rank: Optional[Callable[[int], int]] = None,
    minimized_last: bool = False,
) -> list[AppWindow]:
    """Sort windows for display: most-recently-used first, minimised last.

    ``rank`` comes from the foreground tracker and is authoritative; windows it
    has never seen all share one large rank, so z-order (the order Windows
    handed them to us) breaks those ties instead of leaving them arbitrary.
    """
    ordered = list(apps)
    if rank is not None:
        z_order = {hwnd: i for i, hwnd in enumerate(discovery_order)}
        ordered.sort(key=lambda a: (rank(a.hwnd), z_order.get(a.hwnd, len(z_order))))
    if minimized_last:
        ordered.sort(key=lambda a: a.minimized)  # stable: keeps the order above
    return ordered


def group_key(app: AppWindow) -> str:
    """Identity used to collapse windows of the same application.

    AppUserModelID first, because that is what the taskbar groups by. Keying on
    the executable alone collapses every Chrome profile, every installed PWA and
    every Electron app sharing a runtime into a single slice, which is not what
    the user sees anywhere else on their desktop. Most plain Win32 apps declare
    no AUMID, and those fall back to exactly the old behaviour.
    """
    if app.aumid:
        return f"aumid:{app.aumid.lower()}"
    if app.exe_path:
        return os.path.normcase(app.exe_path)
    return f"class:{app.class_name}"


def group_windows(apps: list[AppWindow]) -> list[AppWindow]:
    """One face window per application, in the incoming order of first seen.

    The face is the most-recent window of that app (first in ``apps`` for that
    key), so the wheel opens on the window you would expect. ``peer_hwnds`` is
    ordered by Z-order instead, because that is what the sub-ring fans out and
    a fan whose entries reshuffle as you step through them cannot be stepped
    through reliably.
    """
    buckets: dict[str, list[AppWindow]] = {}
    order: list[str] = []
    for app in apps:
        key = group_key(app)
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(app)

    faces: list[AppWindow] = []
    for key in order:
        peers = buckets[key]
        face = peers[0]
        hwnds = tuple(peer.hwnd for peer in sorted(peers, key=lambda p: p.z_index))
        faces.append(replace(face, group_count=len(peers), peer_hwnds=hwnds))
    return faces


# ---------------------------------------------------------------------------
# The pinned lane
# ---------------------------------------------------------------------------


def slot_matches(app: AppWindow, slot: dict) -> bool:
    """Whether a live window belongs to a configured slot.

    AUMID is checked first for the same reason ``group_key`` prefers it, and
    the executable name is accepted as well so a slot written by hand (or
    migrated from ``pinned_exes``) still finds its app.
    """
    aumid = str(slot.get("aumid") or "").strip().lower()
    if aumid and app.aumid and app.aumid.lower() == aumid:
        return True
    exe = str(slot.get("exe") or "").strip().lower()
    return bool(exe) and app.exe_name.lower() == exe


def _dead_slot(slot: dict, index: int) -> AppWindow:
    """A lane entry for an app that is not running.

    Built entirely from config: nothing here ever asked Windows whether the app
    exists, which is precisely why the slot keeps its position when it closes.
    """
    exe = str(slot.get("exe") or "").strip()
    launch = str(slot.get("launch") or "").strip() or exe
    label = str(slot.get("label") or "").strip()
    if not label:
        source = launch or exe
        label = os.path.splitext(os.path.basename(source))[0].title() if source else "Empty slot"
    icon_src = launch if os.path.isfile(launch) else exe
    exe_path = launch if os.path.splitext(launch)[1].lower() == ".exe" else exe
    return AppWindow(
        hwnd=0,
        title=label,
        class_name="",
        pid=0,
        # Prefer the recorded launch file (exe or shortcut) so a closed pin
        # still has an icon. A slot the user cannot recognise is not worth
        # reserving an angle for.
        icon=_slot_icon(icon_src, key=exe),
        exe_path=exe_path,
        app_name=label,
        aumid=str(slot.get("aumid") or "").strip(),
        slot=index,
        launch=launch,
        running=False,
    )


def _slot_icon(path: str, *, key: str = "") -> Optional[Image.Image]:
    """Icon for a closed app: stocked logo first, then whatever is on disk."""
    stocked = load_stocked_icon(key or path)
    if stocked is not None:
        return stocked
    if not path:
        return _placeholder_icon()
    with _icon_lock:
        cached = _icon_by_exe.get(path)
    if cached is not None:
        return cached
    icon = _icon_from_file(path) or _resolve_exe_icon(path)
    if icon is None:
        return _placeholder_icon()
    with _icon_lock:
        _icon_by_exe.setdefault(path, icon)
    return icon


def _resolve_exe_icon(exe: str) -> Optional[Image.Image]:
    """Find a bare ``name.exe`` on PATH so a hand-written slot still gets an icon."""
    if os.path.dirname(exe):
        return None
    found = shutil.which(exe)
    return _icon_from_file(found) if found else None


def assign_slots(
    apps: list[AppWindow], slots: Iterable[dict] = ()
) -> tuple[list[AppWindow], list[AppWindow]]:
    """Split the wheel into the fixed pinned lane and the elastic MRU list.

    Replaces the old ``pin_windows`` reordering. Reordering could only ever move
    something already running; a lane entry has to exist whether or not its app
    does, so the two sources are merged here rather than one being sorted.

    A pinned app that *is* running leaves the MRU list, so the arc cost is only
    paid for slots whose app is closed.
    """
    ordered = sorted(
        (s for s in slots if isinstance(s, dict)),
        key=lambda s: int(s.get("index", 0)),
    )
    lane: list[AppWindow] = []
    claimed: set[int] = set()
    for position, slot in enumerate(ordered):
        live = next(
            (a for a in apps if a.hwnd not in claimed and slot_matches(a, slot)), None
        )
        if live is None:
            lane.append(_dead_slot(slot, position))
            continue
        claimed.add(live.hwnd)
        claimed.update(live.peer_hwnds)
        launch = str(slot.get("launch") or "").strip() or launch_target(live)
        exe = str(slot.get("exe") or live.exe_name or "").strip()
        if live.icon is not None and exe:
            stock_icon(exe, live.icon)
        lane.append(replace(live, slot=position, launch=launch, running=True))

    mru = [a for a in apps if a.hwnd not in claimed]
    return lane, mru


def present_windows(
    apps: list[AppWindow],
    *,
    group_by_app: bool = False,
    slots: Iterable[dict] = (),
) -> tuple[list[AppWindow], list[AppWindow]]:
    """What the wheel should show, as ``(lane, mru)``.

    With no slots configured the lane is empty and the MRU list is everything,
    which is byte-identical to the wheel before the lane existed.
    """
    items = group_windows(list(apps)) if group_by_app else list(apps)
    return assign_slots(items, slots)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


# Anything installed here is packaged; its executable is not directly runnable.
_PACKAGED_MARKER = os.path.normcase(f"{os.sep}windowsapps{os.sep}")


def launch_target(app: AppWindow) -> str:
    """A string ShellExecute can open to start this app fresh.

    Recorded when a pin is created rather than resolved at launch time, so the
    app does not have to be running for the slot to work.

    The executable wins where there is a usable one, because plenty of desktop
    apps report an AUMID that is a grouping label rather than a real shell
    identity (Chrome reports ``Chrome``, which names nothing in AppsFolder).
    Packaged apps are the other way round: their exe lives under WindowsApps
    where it cannot be launched, so those go through the shell namespace, the
    same route the Start menu takes.
    """
    exe = app.exe_path or ""
    packaged = _PACKAGED_MARKER in os.path.normcase(exe)
    if exe and not packaged and os.path.splitext(exe)[1].lower() == ".exe":
        return exe
    if app.aumid:
        return f"shell:AppsFolder\\{app.aumid}"
    return exe


def launch_app(target: str, fallback: str = "") -> bool:
    """Start an app that is not running. False if every route Windows refused."""
    if w.shell_execute(target):
        return True
    return bool(fallback) and fallback != target and w.shell_execute(fallback)


_PIN_FILE_EXTS = {".exe", ".lnk", ".bat", ".cmd", ".com"}


def slot_from_path(path: str) -> dict | None:
    """A lane slot from an .exe or a shortcut the user picked in Settings.

    The full path is recorded as ``launch`` so a closed pin can still be
    started. Bare executable names cannot — ShellExecute has nowhere to look.
    """
    raw = os.path.expandvars(str(path or "").strip().strip('"'))
    if not raw:
        return None
    full = os.path.abspath(os.path.normpath(raw))
    if not os.path.isfile(full):
        return None
    ext = os.path.splitext(full)[1].lower()
    if ext not in _PIN_FILE_EXTS:
        return None
    target = full
    if ext == ".lnk":
        resolved = w.resolve_shortcut(full)
        if resolved and os.path.splitext(resolved)[1].lower() == ".exe":
            target = resolved
    base = os.path.basename(target)
    stem, base_ext = os.path.splitext(base)
    exe = (base if base_ext.lower() == ".exe" else f"{stem}.exe").lower()
    label = os.path.splitext(os.path.basename(full))[0].strip() or stem
    icon = _icon_from_file(full) or _icon_from_file(target)
    if icon is not None:
        stock_icon(exe, icon)
    slot = {"exe": exe, "launch": full, "label": label[:64]}
    return slot


def activate_window(hwnd: int) -> bool:
    """Bring a window to the foreground reliably from a background process."""
    if not hwnd or not w.user32.IsWindow(hwnd):
        return False

    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindow(hwnd, w.SW_RESTORE)

    pid = wintypes.DWORD()
    w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    target_pid = int(pid.value or 0)

    # Prefer SwitchToThisWindow — it is the shell-approved path and does not
    # open the session-wide ASFW_ANY hole.
    try:
        w.user32.SwitchToThisWindow(hwnd, True)
    except OSError:
        pass
    if int(w.user32.GetForegroundWindow() or 0) == hwnd:
        return True

    if target_pid:
        try:
            w.user32.AllowSetForegroundWindow(target_pid)
        except OSError:
            pass

    fg = w.user32.GetForegroundWindow()
    if fg == hwnd:
        return True

    target_tid = w.user32.GetWindowThreadProcessId(hwnd, None)
    fg_tid = w.user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_tid = w.kernel32.GetCurrentThreadId()

    attached_fg = False
    attached_target = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(w.user32.AttachThreadInput(cur_tid, fg_tid, True))
        if target_tid and target_tid != cur_tid and target_tid != fg_tid:
            attached_target = bool(w.user32.AttachThreadInput(cur_tid, target_tid, True))

        w.user32.BringWindowToTop(hwnd)
        w.user32.ShowWindow(hwnd, w.SW_SHOW)
        w.user32.SetForegroundWindow(hwnd)
        w.user32.SetActiveWindow(hwnd)
    finally:
        if attached_target:
            w.user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            w.user32.AttachThreadInput(cur_tid, fg_tid, False)

    if int(w.user32.GetForegroundWindow() or 0) == hwnd:
        return True

    try:
        w.user32.SwitchToThisWindow(hwnd, True)
    except OSError:
        return False
    return int(w.user32.GetForegroundWindow() or 0) == hwnd


def close_window(hwnd: int) -> bool:
    """Ask a window to close. False when we should not touch it (e.g. elevated)."""
    if not hwnd or not w.user32.IsWindow(hwnd):
        return False
    if window_is_elevated(hwnd):
        return False
    w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
    return True


def minimize_window(hwnd: int) -> bool:
    if not hwnd or not w.user32.IsWindow(hwnd):
        return False
    if window_is_elevated(hwnd):
        return False
    w.user32.ShowWindow(hwnd, w.SW_SHOWMINNOACTIVE)
    return True


def window_is_elevated(hwnd: int) -> bool:
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return w.process_is_elevated(int(pid.value or 0))


def foreground_hwnd() -> int:
    return int(w.user32.GetForegroundWindow() or 0)


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


def _bits_to_image(hdc_mem, hbmp, width: int, height: int) -> Optional[Image.Image]:
    bmi = w.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = w.BI_RGB

    buf = (ctypes.c_ubyte * (width * height * 4))()
    if not w.gdi32.GetDIBits(
        hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), w.DIB_RGB_COLORS
    ):
        return None
    # The alpha byte of a compatible bitmap is undefined, so drop it entirely.
    arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(height, width, 4)
    return Image.fromarray(np.ascontiguousarray(arr[..., 2::-1]), "RGB")


def _is_blank_capture(img: Image.Image) -> bool:
    """PrintWindow often "succeeds" with a flat frame for windows that never painted."""
    try:
        small = img.resize((48, 48), Image.Resampling.BILINEAR)
        arr = np.asarray(small.convert("RGB"), dtype=np.int16)
        if arr.std() < 6:
            return True
        mean = float(arr.mean())
        return mean < 8 or mean > 247
    except Exception:
        return False


def _frame_inset(hwnd: int, rect: w.RECT) -> tuple[int, int, int, int]:
    """Invisible resize border around a window, so previews are not padded."""
    try:
        bounds = w.RECT()
        hr = w.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(bounds),
            ctypes.sizeof(bounds),
        )
        if hr != 0:
            return (0, 0, 0, 0)
        left = max(0, bounds.left - rect.left)
        top = max(0, bounds.top - rect.top)
        right = max(0, rect.right - bounds.right)
        bottom = max(0, rect.bottom - bounds.bottom)
        if left + right >= (rect.right - rect.left) or top + bottom >= (
            rect.bottom - rect.top
        ):
            return (0, 0, 0, 0)
        return (left, top, right, bottom)
    except Exception:
        return (0, 0, 0, 0)


def _print_window_to_image(hwnd: int, src_w: int, src_h: int) -> Optional[Image.Image]:
    hdc_screen = w.user32.GetDC(0)
    if not hdc_screen:
        return None
    hdc_mem = 0
    full_bmp = 0
    try:
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        full_bmp = w.gdi32.CreateCompatibleBitmap(hdc_screen, src_w, src_h)
        if not hdc_mem or not full_bmp:
            return None
        old = w.gdi32.SelectObject(hdc_mem, full_bmp)

        ok = w.user32.PrintWindow(hwnd, hdc_mem, w.PW_RENDERFULLCONTENT)
        if not ok:
            ok = w.user32.PrintWindow(hwnd, hdc_mem, 0)
        img = _bits_to_image(hdc_mem, full_bmp, src_w, src_h) if ok else None

        w.gdi32.SelectObject(hdc_mem, old)
        if img is not None and _is_blank_capture(img):
            return None
        return img
    finally:
        if full_bmp:
            w.gdi32.DeleteObject(full_bmp)
        if hdc_mem:
            w.gdi32.DeleteDC(hdc_mem)
        w.user32.ReleaseDC(0, hdc_screen)


def _capture_minimized(hwnd: int) -> Optional[Image.Image]:
    """Minimised windows never paint — restore them off-screen, grab, re-minimise."""
    placement = w.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(w.WINDOWPLACEMENT)
    if not w.user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None

    normal = placement.rcNormalPosition
    src_w = max(8, normal.right - normal.left)
    src_h = max(8, normal.bottom - normal.top)

    flags = (
        w.SWP_NOSIZE
        | w.SWP_NOZORDER
        | w.SWP_NOACTIVATE
        | w.SWP_SHOWWINDOW
        | w.SWP_NOSENDCHANGING
    )
    w.user32.SetWindowPos(hwnd, None, -32000, -32000, 0, 0, flags)
    w.user32.ShowWindow(hwnd, w.SW_SHOWNOACTIVATE)
    w.user32.MsgWaitForMultipleObjects(0, None, False, 40, 0)

    rect = w.RECT()
    w.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    rw = rect.right - rect.left
    rh = rect.bottom - rect.top
    if rw < 8 or rh < 8:
        rw, rh = src_w, src_h

    img = _print_window_to_image(hwnd, rw, rh)

    placement.length = ctypes.sizeof(w.WINDOWPLACEMENT)
    placement.showCmd = w.SW_SHOWMINIMIZED
    w.user32.SetWindowPlacement(hwnd, ctypes.byref(placement))
    w.user32.ShowWindow(hwnd, w.SW_SHOWMINNOACTIVE)
    return img


def capture_thumbnail(
    hwnd: int,
    max_width: int = 480,
    max_height: int = 270,
    *,
    allow_minimized: bool = True,
) -> Optional[Image.Image]:
    """Preview bitmap for a window, using the window's own pixels (PrintWindow).

    A screen BitBlt would be sharper but captures whatever sits on top of the
    window — including our own overlay.
    """
    if not hwnd or not w.user32.IsWindow(hwnd):
        return None
    if w.window_excludes_capture(hwnd):
        return None
    if window_is_elevated(hwnd):
        return None

    if w.user32.IsIconic(hwnd):
        if not allow_minimized:
            return None
        img = _capture_minimized(hwnd)
    else:
        rect = w.RECT()
        if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        src_w = rect.right - rect.left
        src_h = rect.bottom - rect.top
        if src_w < 8 or src_h < 8:
            return None
        img = _print_window_to_image(hwnd, src_w, src_h)
        if img is not None:
            left, top, right, bottom = _frame_inset(hwnd, rect)
            if left or top or right or bottom:
                img = img.crop((left, top, src_w - right, src_h - bottom))

    if img is None:
        return None

    src_w, src_h = img.size
    fit = min(max_width / src_w, max_height / src_h, 1.0)
    if fit < 1.0:
        # Two-step downscale: REDUCE is far cheaper than LANCZOS for big factors.
        step = max(1, int(1.0 / fit) // 2)
        if step > 1:
            img = img.reduce(step)
            src_w, src_h = img.size
            fit = min(max_width / src_w, max_height / src_h, 1.0)
        img = img.resize(
            (max(1, int(src_w * fit)), max(1, int(src_h * fit))),
            Image.Resampling.LANCZOS,
        )
    return img.convert("RGBA")
