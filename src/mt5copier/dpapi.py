"""Windows' Data Protection API: encrypting secrets so that only the same Windows user, on the
same machine, can read them back.

The key is Windows' own, derived from the user's logon, so there is nothing for the app to keep.
What it protects against is a **copy of the file** -- a backup, a synced profile, a stolen
disk, another user of the machine. It does not protect against a program running as the same
user, which can ask Windows to decrypt exactly as this app does; nothing kept locally can.

Through ``ctypes`` rather than a package, so it needs nothing beyond Windows itself.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_ENTROPY = b"mt5copier"
"""Mixed into every blob, so a program that decrypts whatever DPAPI blobs it finds, without
knowing this, gets nothing from ours. **Fixed**: changing it makes every saved blob unreadable."""

_UI_FORBIDDEN = 0x1
"""Fail rather than show a prompt: the app may run with nobody to answer one."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    """A blob over ``data``, and the buffer it points into, which must outlive it."""
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _call(name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = getattr(crypt32, name)
    func.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    func.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    given, _given_buffer = _blob(data)
    entropy, _entropy_buffer = _blob(_ENTROPY)
    out = _Blob()
    if not func(
        ctypes.byref(given),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(out),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    """``data`` encrypted for the current Windows user on this machine."""
    return _call("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    """What :func:`protect` encrypted. Raises ``OSError`` for a blob made by another user, on
    another machine, or damaged."""
    return _call("CryptUnprotectData", blob)
