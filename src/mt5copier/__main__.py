"""Application entry point.

Multiprocessing note: ``freeze_support()`` must be the first thing that runs. Under PyInstaller
on Windows the spawn start method re-launches *this exe* to create each worker, so without it
every worker would re-enter the UI and fork a set of workers of its own.

``--list`` exists mainly so a frozen build can be verified without a tty: it exercises the
imports that matter (including the MetaTrader5 extension) and the discovery path, then exits.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys

from . import __version__
from .logging_setup import configure
from .paths import (
    active_profile,
    config_path,
    ensure_dirs,
    is_frozen,
    lock_path,
    log_dir,
    set_active_profile,
    user_data_dir,
)
from .single_instance import AlreadyRunningError, SingleInstanceLock, holder_pid


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mt5copier", description="MT5 multi-terminal trade copier (stage 1)"
    )
    parser.add_argument("--version", action="version", version=f"mt5copier {__version__}")
    parser.add_argument(
        "--list",
        action="store_true",
        help="print discovered terminals and exit; launches no terminal and connects to none",
    )
    parser.add_argument(
        "--paths", action="store_true", help="print resolved config/log locations and exit"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the MetaTrader5 extension can be imported, then exit",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        # The desktop window is the only UI now. The flag stays accepted because existing
        # shortcuts pass it, and the profiles pill respawns this exe with it.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="open this profile instead of the one that ran last; "
        "it must already exist (profiles are made in the window's titlebar pill)",
    )
    return parser.parse_args(argv)


def _cmd_paths() -> int:
    print(f"frozen:      {is_frozen()}")
    print(f"profile:     {active_profile()}")
    print(f"user data:   {user_data_dir()}")
    print(f"config:      {config_path()}")
    print(f"logs:        {log_dir()}")
    return 0


def _cmd_check() -> int:
    """Import the MT5 extension without connecting.

    A plain import is enough to catch the failure mode that shipped in zele-mt5, where the
    extension was never bundled and the exe only discovered this at runtime.
    """
    try:
        import MetaTrader5 as mt5  # noqa: N813
    except Exception as exc:
        print(f"FAIL: cannot import MetaTrader5: {exc!r}")
        return 1
    print(f"OK: MetaTrader5 {getattr(mt5, '__version__', '?')} importable (not connected)")
    return 0


def _cmd_list() -> int:
    from .discovery import discover_terminals

    terminals = discover_terminals()
    print(f"{len(terminals)} terminals discovered\n")
    print(f"{'RUN':<5}{'LOGIN':>12}  {'SERVER':<30} {'BROKER':<26} NOTES")
    for t in terminals:
        notes = "; ".join(t.notes)
        if not t.usable:
            notes = f"UNUSABLE {notes}".strip()
        print(
            f"{'live' if t.is_running else '':<5}"
            f"{t.login if t.login else '-':>12}  "
            f"{(t.server or '-'):<30} {t.label:<26} {notes}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    mp.freeze_support()
    args = _parse_args(argv)

    from . import profiles

    if args.paths or args.check or args.list:
        # Diagnostics stay lockless and must not create anything: an existing profile named
        # on the flag is reported on, a missing one is only *said* to be missing -- a typo
        # here must not leave an empty profile directory behind.
        if args.profile is not None:
            name = args.profile.strip()
            if profiles.exists(name):
                set_active_profile(name)
            else:
                print(f"(no profile called {name!r}; showing the default)")
        ensure_dirs()
        configure(log_dir() / "copier.log")
        if args.paths:
            return _cmd_paths()
        if args.check:
            return _cmd_check()
        return _cmd_list()

    if args.profile is not None:
        name = args.profile.strip()
        if not profiles.exists(name):
            known = ", ".join(p.name for p in profiles.list_profiles())
            print(f"no profile called {name!r}. Profiles: {known}")
            print("new profiles are made in the window's titlebar pill: run with no --profile")
            return 2
    else:
        # The window manages profiles itself (the titlebar pill), so a bare launch opens
        # whichever profile ran last -- the default until any other ever has. list_profiles
        # puts the default first, and max keeps the first of equals.
        name = max(profiles.list_profiles(), key=lambda p: p.last_opened or 0.0).name

    set_active_profile(name)
    ensure_dirs()
    configure(log_dir() / "copier.log")

    lock = SingleInstanceLock(lock_path())
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        # A launch of a profile whose window is already open: focus that window instead.
        # The frozen exe has no console, so without this a second double-click would
        # simply do nothing visible.
        from .winfocus import focus_window_of_pid

        pid = holder_pid(lock_path())
        if pid is not None and focus_window_of_pid(pid):
            return 0
        print(f"profile {active_profile()!r} is already running: {exc}")
        return 1

    try:
        from .desktop import run

        run()
    except KeyboardInterrupt:
        return 130
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
