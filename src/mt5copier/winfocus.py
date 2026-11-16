"""Brings another copier instance's window to the front.

Used at exactly one moment: ``--desktop`` is launched for a profile whose window is already
open. The frozen exe has no console, so a printed refusal goes nowhere -- the honest answer
to a second double-click is the first window, focused. The PID comes from the single-instance
lock (:func:`mt5copier.single_instance.holder_pid`); this module only turns a PID into a
focused window.

ctypes only, deliberately: this runs *instead of* the desktop package, whose import pulls in
the engine and pywebview -- far too much for a process whose whole job is to wave at another
one and exit.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_GW_OWNER = 4
_SW_RESTORE = 9


def focus_window_of_pid(pid: int) -> bool:
    """Focus the first visible, unowned top-level window belonging to ``pid``.

    ``False`` when no such window exists -- the holder may be a TUI run, or mid startup or
    shutdown -- and the caller falls back to words. ``SetForegroundWindow`` can be demoted
    by Windows to a taskbar flash when this process lacks foreground rights; either way the
    person sees where the running window is, which is the point.
    """
    user32 = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _take_first_main_window(hwnd: int, _unused: int) -> bool:
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if (
            owner_pid.value == pid
            and user32.IsWindowVisible(hwnd)
            and not user32.GetWindow(hwnd, _GW_OWNER)
        ):
            hits.append(hwnd)
            return False  # stop enumerating; EnumWindows then "fails", which is fine
        return True

    user32.EnumWindows(_take_first_main_window, 0)
    if not hits:
        return False
    hwnd = hits[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True
