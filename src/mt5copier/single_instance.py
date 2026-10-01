"""Refuses to let two full runs share one user data directory at once.

``config.json``, ``mapping.db`` and ``copier.log`` are all fixed paths with no per-instance
override (see :mod:`mt5copier.paths`) -- two processes touching them concurrently can silently
race on ``config.json`` (last-writer-wins, nothing stops it) or crash ``copier.log``'s rotation
on Windows (``PermissionError`` when a second process holds the same rotating file open). See
``ROADMAP.md``'s "Multi-master support" note for the full story; this closes the accidental
case off at the source rather than documenting around it -- a second full run refuses to start.

Diagnostic commands (``--list``, ``--check``, ``--paths``) never take this lock: they are
read-only and never touch any of the files above.
"""

from __future__ import annotations

import msvcrt
import os
from pathlib import Path
from typing import TextIO


class AlreadyRunningError(Exception):
    """Another instance already holds the lock."""


class SingleInstanceLock:
    """An OS-held exclusive lock on one file, released by Windows the instant this process
    exits -- crash or not.

    Deliberately not a PID file: a PID file can go stale if the process dies without cleanup,
    which then either wrongly blocks a real second launch or, if the staleness check is
    sloppy, wrongly lets a second real instance through. There is no staleness question here --
    the lock cannot outlive the process holding it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: TextIO | None = None

    def acquire(self) -> None:
        """Raise :class:`AlreadyRunningError` if another process already holds it.

        Also refuses a second acquire from a second handle within this same process -- the OS
        lock is per file handle, not per process, which is exactly what lets the tests exercise
        the real locking behaviour without spawning a second process.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self._path, "w")  # noqa: SIM115 -- held for the app's lifetime, not closed here
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            f.close()
            raise AlreadyRunningError(
                f"another instance is already running (lock: {self._path})"
            ) from exc
        f.write(str(os.getpid()))
        f.flush()
        self._file = f

    def release(self) -> None:
        """Safe to call more than once, and safe to call without a successful ``acquire()``."""
        f, self._file = self._file, None
        if f is None:
            return
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
