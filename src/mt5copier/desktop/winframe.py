"""Native window behaviour for the frameless window, straight from Win32.

pywebview's frameless window is a borderless WinForms form, and Windows does not treat a
borderless window as a real one: no Snap (Win+arrow, drag-to-edge tiling), no system menu,
and pywebview's own dragging is JavaScript nudging the window once per mousemove. The fix
is the one every custom-frame app (Electron among them) uses:

* give the window its ordinary styles back -- ``WS_CAPTION | WS_THICKFRAME`` and the
  min/max boxes -- so the shell sees a normal resizable window and Snap works;
* swallow ``WM_NCCALCSIZE`` so none of the frame those styles would draw ever appears,
  which keeps the window looking exactly as designed;
* start drags and resizes by handing the mouse to the system (``WM_NCLBUTTONDOWN`` with
  the right hit-test code), which runs the native move loop -- fluid dragging, snap
  while dragging to an edge, restore-on-drag from maximized, all for free.

ctypes rather than pythonnet: subclassing a WndProc through .NET is fragile from Python,
while ``SetWindowSubclass`` is one call and the callback is plain C.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from ..logging_setup import get_logger

log = get_logger(__name__)

_user32 = ctypes.windll.user32
_comctl32 = ctypes.windll.comctl32

_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_WS_SYSMENU = 0x00080000

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020

_WM_NCCALCSIZE = 0x0083
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2

_HIT_CODES = {
    "left": 10,
    "right": 11,
    "top": 12,
    "topleft": 13,
    "topright": 14,
    "bottom": 15,
    "bottomleft": 16,
    "bottomright": 17,
}

_LRESULT = ctypes.c_longlong
_SUBCLASSPROC = ctypes.WINFUNCTYPE(
    _LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    ctypes.c_size_t,
    ctypes.c_size_t,
)

_user32.GetWindowLongPtrW.restype = ctypes.c_longlong
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
_comctl32.DefSubclassProc.restype = _LRESULT
_comctl32.DefSubclassProc.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]


@_SUBCLASSPROC
def _frameless_proc(hwnd: int, msg: int, wparam: int, lparam: int, _uid: int, _ref: int) -> int:
    # WM_NCCALCSIZE with a truthy wparam asks how much of the window the frame takes;
    # answering 0 says "none", so the caption and borders exist for the shell but are
    # never painted and take no space.
    if msg == _WM_NCCALCSIZE and wparam:
        return 0
    return int(_comctl32.DefSubclassProc(hwnd, msg, wparam, lparam))


def adopt(hwnd: int) -> None:
    """Make one borderless window behave like a titled one, without showing a title."""
    style = _user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
    style |= _WS_CAPTION | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX | _WS_SYSMENU
    _user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style)
    if not _comctl32.SetWindowSubclass(hwnd, _frameless_proc, 1, 0):
        # Without the subclass the styles would paint a real title bar, which is worse
        # than no Snap: put the window back the way it was.
        _user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style & ~(_WS_CAPTION | _WS_THICKFRAME))
        log.warning("could not subclass the window; Snap stays unavailable")
        return
    _user32.SetWindowPos(
        hwnd, 0, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED
    )
    log.info("window adopted native frame behaviours (Snap, native drag and resize)")


def begin_drag(hwnd: int) -> None:
    """Hand the mouse to the system as a title-bar drag; the native move loop does the
    rest, edge snapping and restore-from-maximized included."""
    _user32.ReleaseCapture()
    _user32.PostMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)


def begin_resize(hwnd: int, edge: str) -> None:
    code = _HIT_CODES.get(edge)
    if code is None:
        return
    _user32.ReleaseCapture()
    _user32.PostMessageW(hwnd, _WM_NCLBUTTONDOWN, code, 0)
