"""Refuses to let two full runs share one user data directory at once.

``config.json``, ``mapping.db`` and ``copier.log`` are all fixed paths *within a profile*
(see :mod:`mt5copier.paths`) -- two processes touching them concurrently can silently race on
``config.json`` (last-writer-wins, nothing stops it) or crash ``copier.log``'s rotation on
Windows (``PermissionError`` when a second process holds the same rotating file open). So the
lock is per profile: a second run *of the same profile* refuses to start, while different
profiles run side by side, each holding its own lock -- what keeps those from touching the
same terminal or broker account is :mod:`mt5copier.claims`, not this.

Diagnostic commands (``--list``, ``--check``, ``--paths``) never take this lock: they are
read-only and never touch any of the files above. Neither does the profile picker, which
probes other profiles' locks through :func:`holder_pid` without ever keeping one.
"""

from __future__ import annotations

import msvcrt
import os
from pathlib import Path
from typing import TextIO


class AlreadyRunningError(Exception):
    """Another instance already holds the lock."""


def open_lockable(path: Path) -> TextIO:
    """Open a lock file read-write without truncating, creating it if need be.

    Not ``open(path, "w")`` -- truncating before the lock attempt can fail would wipe what
    the holder wrote -- and not ``"a+"`` either: append mode sends *every* write to the end
    of the file whatever was seeked, which on a fresh file is offset 0, the locked byte
    itself. ``O_RDWR | O_CREAT`` is the plain thing both of those pretend to be.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    return os.fdopen(fd, "r+")


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

        Opened without truncating (see :func:`open_lockable`): a *refused* second launch must
        not wipe what the holder wrote. And the PID goes *after* the locked byte, because
        Windows byte locks are mandatory -- a reader hitting the locked byte itself gets
        ``PermissionError``, and the PID is written to be read by others (the profile
        picker's "running" column) while the lock is held.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        f = open_lockable(self._path)  # held for the app's lifetime, not closed here
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            f.close()
            raise AlreadyRunningError(
                f"another instance is already running (lock: {self._path})"
            ) from exc
        f.seek(1)
        f.write(f"{os.getpid()}\n")
        f.truncate()
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


def holder_pid(path: Path) -> int | None:
    """The PID of the process holding this lock, or ``None`` when nobody does.

    Read-only in effect: it probes by taking the lock and releasing it straight away, which
    is the only honest test -- any content-based check (a PID file's) reintroduces exactly
    the staleness problem the class docstring rejects. Probing an unheld lock is harmless;
    nothing reads the lock file but this function, and the content is rewritten on every
    acquire.
    """
    if not path.exists():
        return None
    try:
        probe = open_lockable(path)
    except OSError:
        return None
    try:
        probe.seek(0)
        try:
            msvcrt.locking(probe.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            # Held. The PID sits after the locked byte, so it is readable even now.
            try:
                with open(path) as f:
                    f.seek(1)
                    return int(f.read().strip() or 0) or None
            except (OSError, ValueError):
                return None
        try:
            probe.seek(0)
            msvcrt.locking(probe.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return None
    finally:
        probe.close()
