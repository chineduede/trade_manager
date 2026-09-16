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


def bundle_dir() -> Path:
    """Directory holding read-only bundled assets (CSS, stubs).

    Under PyInstaller one-file this is the temporary extraction dir (``sys._MEIPASS``);
    from source it is the package directory.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


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


def dryrun_state_path() -> Path:
    """Believed-copy state for the Stage 2a dry run.

    Separate from config because it is machine-owned bookkeeping, not user settings, and
    deleting it must be harmless. Stage 2c replaces it with the SQLite mapping table.
    """
    return user_data_dir() / "dryrun_state.json"


def mt5_terminal_root() -> Path:
    r"""``%APPDATA%\MetaQuotes\Terminal`` — where MT5 keeps one data folder per install."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "MetaQuotes" / "Terminal"


def ensure_dirs() -> None:
    user_data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
