"""Single-instance lock tests.

Exercises real OS-level locking against a temp file. ``msvcrt.locking()`` is held per file
handle, not per process, so a second :class:`SingleInstanceLock` on the same path within this
one test process is refused exactly the way a second real process would be -- no need to spawn
one to prove it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.single_instance import AlreadyRunningError, SingleInstanceLock, holder_pid


def test_a_second_lock_on_the_same_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mt5copier.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()
    finally:
        first.release()


def test_releasing_frees_it_for_the_next_acquire(tmp_path: Path) -> None:
    path = tmp_path / "mt5copier.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    first.release()

    second = SingleInstanceLock(path)
    second.acquire()  # must not raise
    second.release()


def test_release_is_safe_to_call_twice_and_without_ever_acquiring(tmp_path: Path) -> None:
    never_acquired = SingleInstanceLock(tmp_path / "never-acquired.lock")
    never_acquired.release()  # no-op, must not raise

    held = SingleInstanceLock(tmp_path / "held.lock")
    held.acquire()
    held.release()
    held.release()  # must not raise a second time


def test_context_manager_holds_the_lock_for_its_block_then_frees_it(tmp_path: Path) -> None:
    path = tmp_path / "ctx.lock"
    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()

    # released on exit -- a fresh acquire now succeeds
    after = SingleInstanceLock(path)
    after.acquire()
    after.release()


def test_acquiring_creates_the_lock_file_and_its_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "mt5copier.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        assert path.exists()
    finally:
        lock.release()


def test_holder_pid_reads_the_pid_while_the_lock_is_held(tmp_path: Path) -> None:
    """The picker's "running" column: readable *during* the hold, despite the byte lock."""
    import os

    path = tmp_path / "mt5copier.lock"
    assert holder_pid(path) is None, "no file yet means nobody holds it"

    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        assert holder_pid(path) == os.getpid()
    finally:
        lock.release()
    assert holder_pid(path) is None, "released means nobody, whatever the file still says"


def test_probing_never_steals_or_breaks_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "mt5copier.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        holder_pid(path)
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()
    finally:
        lock.release()
