"""Filesystem layout, resolved correctly whether running from source or a frozen exe.

Centralised deliberately: getting this wrong is what makes a PyInstaller build "work on my
machine" and then silently write its data into whatever directory Explorer happened to supply
as the CWD. Nothing else in the package may build paths from relative strings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MT5Copier"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def exe_dir() -> Path:
    """Directory containing the running exe (frozen) or the project root (source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    r"""Writable per-user directory for config, logs and state.

    Uses ``%APPDATA%\MT5Copier``. A ``portable.txt`` marker next to the exe switches to an
    exe-adjacent ``data/`` folder instead, so the app can live on a USB stick.
    """
    if (exe_dir() / "portable.txt").exists():
        return exe_dir() / "data"
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME


def config_path() -> Path:
    return user_data_dir() / "config.json"


def log_dir() -> Path:
    return user_data_dir() / "logs"


def lock_path() -> Path:
    """The single-instance lock file -- see :mod:`mt5copier.single_instance`."""
    return user_data_dir() / "mt5copier.lock"


def mapping_db_path() -> Path:
    """SQLite store of which slave position belongs to which master position.

    This is the copier's memory of what it owns. Losing it does not lose money, but it does
    orphan every live copy -- the positions stay open with nothing tracking them -- so it
    lives beside the config rather than anywhere temporary.
    """
    return user_data_dir() / "mapping.db"


def guards_path() -> Path:
    """Each account's guard rules, and the person's saved templates of them. Apart from
    ``config.json`` so that a slip editing rules cannot disturb copy settings."""
    return user_data_dir() / "guards.json"


def guard_state_path() -> Path:
    """What the guards have recorded from earlier snapshots, such as the equity at a reset."""
    return user_data_dir() / "guard_state.json"


def dryrun_store_path() -> Path:
    """Believed copies for a dry run -- simulated, and kept well away from the real thing.

    A separate file from :func:`mapping_db_path` on purpose. A simulated position recorded in
    the live table would later be indistinguishable from one that actually exists, and the
    copier would try to close something that was never opened.
    """
    return user_data_dir() / "dryrun.db"


def mt5_terminal_root() -> Path:
    r"""``%APPDATA%\MetaQuotes\Terminal`` — where MT5 keeps one data folder per install."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "MetaQuotes" / "Terminal"


def ensure_dirs() -> None:
    user_data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
