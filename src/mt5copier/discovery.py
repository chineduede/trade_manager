"""Passive discovery of installed MT5 terminals.

Why passive: the official docs state that ``initialize()`` will *launch* the terminal if it is
not already running. On this machine there are 15 installs, so probing them all through the MT5
API would cold-start 15 terminals. Nothing in this module imports MetaTrader5 or opens a
connection.

What we can learn without connecting turns out to be most of what the UI needs:

==========================  ==========================================================
Source                      Yields
==========================  ==========================================================
data folder name            terminal_id (32-hex) -- the stable identity
origin.txt                  install directory
install dir name            human-readable broker label
config/common.ini           last-used Login and Server, in plaintext (UTF-16)
bases/<name>/               trade servers this install has connected to
psutil                      whether it is running right now, and its pid
==========================  ==========================================================

What genuinely requires a live connection: balance, equity, free margin, currency, leverage,
open position count, and trade_allowed. Those arrive later via worker snapshots.

``config/accounts.dat`` is encrypted and is not parsed. This app never handles MT5 passwords --
it relies on each terminal already being logged in, which is also why no credential fields
exist anywhere in the config schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psutil

from .logging_setup import get_logger
from .paths import mt5_terminal_root

log = get_logger(__name__)

# MT5 data folders are named with a 32-character hex instance hash. The sibling directories
# Common/, Community/ and Help/ are shared state, not terminals.
_INSTANCE_DIR_RE = re.compile(r"^[0-9A-F]{32}$", re.IGNORECASE)

_TERMINAL_EXE_NAMES = ("terminal64.exe", "terminal.exe")

# Vendor boilerplate that appears anywhere in an install directory name.
_LABEL_NOISE_RE = re.compile(
    r"\b(metatrader\s*5|metatrader|mt5|mt4|terminal|client)\b", re.IGNORECASE
)

# A trailing "#2" is the only thing distinguishing two installs of the same broker.
_LABEL_SUFFIX_RE = re.compile(r"(#\s*\d+)\s*$")


@dataclass(frozen=True, slots=True)
class DiscoveredTerminal:
    """One MT5 installation, as seen from the filesystem alone.

    ``terminal_id`` is the 32-hex instance hash, stable across terminal upgrades. Config is
    keyed on it rather than on the install path, because a reinstall can move the path while
    the data folder -- and therefore the logged-in account -- stays put.
    """

    terminal_id: str
    data_path: Path | None
    install_path: Path | None
    exe_path: Path | None
    label: str
    login: int | None = None
    server: str | None = None
    is_running: bool = False
    pid: int | None = None
    notes: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """Whether an executable was found that we could actually connect to."""
        return self.exe_path is not None and self.exe_path.is_file()


def _decode_ini(raw: bytes) -> str:
    """Decode an MT5 .ini or .txt config file.

    MT5 writes both origin.txt and common.ini as UTF-16 LE with a BOM (verified: they start
    ``ff fe``), but older installs and portable copies use UTF-8 or the ANSI codepage.

    Detection is BOM-first and deliberately not a blind try-in-order. Decoding UTF-8 bytes as
    UTF-16 frequently *succeeds* and yields plausible-looking CJK mojibake rather than raising,
    so an ordered try would silently mangle any non-UTF-16 file instead of falling through.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")

    # No BOM. Interleaved NULs mean UTF-16 written without one.
    if raw.count(b"\x00") > len(raw) // 4:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                text = raw.decode(encoding)
            except UnicodeError:
                continue
            if "\x00" not in text:
                return text

    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _parse_common_ini(path: Path) -> tuple[int | None, str | None]:
    """Extract (login, server) from a terminal's ``config/common.ini``.

    Hand-parsed rather than via configparser: these files occasionally carry duplicate keys and
    stray unsectioned lines, which configparser rejects outright. Losing a terminal from the
    list because of one malformed line would be worse than ignoring the line.
    """
    try:
        text = _decode_ini(path.read_bytes())
    except OSError as exc:
        log.debug("could not read %s: %s", path, exc)
        return None, None

    login: int | None = None
    server: str | None = None
    section = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section not in ("common", ""):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "login" and login is None:
            try:
                login = int(value)
            except ValueError:
                log.debug("non-numeric Login=%r in %s", value, path)
        elif key == "server" and server is None and value:
            server = value

    return login, server


def _read_origin(data_path: Path) -> Path | None:
    """Read ``origin.txt``, which names the install directory this data folder belongs to."""
    try:
        text = _decode_ini((data_path / "origin.txt").read_bytes()).strip()
    except OSError:
        return None
    return Path(text) if text else None


def _find_exe(install_path: Path) -> Path | None:
    for name in _TERMINAL_EXE_NAMES:
        candidate = install_path / name
        if candidate.is_file():
            return candidate
    return None


def _clean_label(name: str) -> str:
    """Strip vendor boilerplate from an install directory name, keeping any "#N" suffix.

    "Five Percent Online MetaTrader 5 #1" -> "Five Percent Online #1"
    "MetaTrader 5 IC Markets Global #2"   -> "IC Markets Global #2"
    "MetaTrader 5"                        -> ""   (caller falls back to the server name)
    """
    suffix_match = _LABEL_SUFFIX_RE.search(name)
    if suffix_match:
        suffix = suffix_match.group(1).replace(" ", "")
        core = name[: suffix_match.start()]
    else:
        suffix = ""
        core = name
    core = _LABEL_NOISE_RE.sub(" ", core)
    core = re.sub(r"\s{2,}", " ", core).strip(" -_")
    return f"{core} {suffix}".strip()


def _label_from_install(install_path: Path | None, server: str | None, fallback: str) -> str:
    """Derive a readable broker name for a terminal.

    Prefers the install directory name. Several installs here sit in a generic
    ``C:\\Program Files\\MetaTrader 5``, which cleans down to nothing -- for those the broker is
    recovered from the server name instead ("AquaFunded-Server" -> "AquaFunded"), since
    otherwise two unrelated prop accounts would both read as "MetaTrader 5".
    """
    if install_path is None:
        return fallback
    cleaned = _clean_label(install_path.name)
    if cleaned and not cleaned.startswith("#"):
        return cleaned
    if server:
        broker = server.split("-")[0].split()[0].strip()
        if broker:
            return f"{broker} {cleaned}".strip()
    return cleaned or install_path.name


def _running_terminals() -> dict[str, tuple[int, Path]]:
    """Map lowercased exe path to (pid, exe path) for every running MT5 terminal."""
    running: dict[str, tuple[int, Path]] = {}
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            if (info.get("name") or "").lower() not in _TERMINAL_EXE_NAMES:
                continue
            exe = info.get("exe")
            if not exe:
                continue
            running[str(Path(exe)).lower()] = (info["pid"], Path(exe))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return running


def find_terminal_pid(exe_path: Path) -> int | None:
    """The pid of the terminal currently running from this exe, or None if none is.

    A narrow wrapper over :func:`_running_terminals` for a caller that wants one answer rather
    than the whole map -- the worker, deciding whether it is the one that launched the terminal
    it just connected to, so that only it may later close it.
    """
    match = _running_terminals().get(str(exe_path).lower())
    return match[0] if match else None


def discover_terminals(terminal_root: Path | None = None) -> list[DiscoveredTerminal]:
    """Enumerate every MT5 terminal on this machine without launching or connecting to any.

    Sorted by label so the UI ordering stays stable between rescans.
    """
    root = terminal_root or mt5_terminal_root()
    running = _running_terminals()
    matched_exes: set[str] = set()
    found: list[DiscoveredTerminal] = []

    if not root.is_dir():
        log.warning("MetaQuotes terminal root not found: %s", root)
    else:
        for data_path in sorted(root.iterdir()):
            if not data_path.is_dir() or not _INSTANCE_DIR_RE.match(data_path.name):
                continue

            notes: list[str] = []
            install_path = _read_origin(data_path)
            exe_path = _find_exe(install_path) if install_path else None

            if install_path is None:
                notes.append("no origin.txt")
            elif exe_path is None:
                notes.append("terminal exe missing (uninstalled?)")

            login, server = _parse_common_ini(data_path / "config" / "common.ini")
            if login is None:
                notes.append("never logged in")

            pid: int | None = None
            is_running = False
            if exe_path is not None:
                key = str(exe_path).lower()
                if key in running:
                    pid = running[key][0]
                    is_running = True
                    matched_exes.add(key)

            found.append(
                DiscoveredTerminal(
                    terminal_id=data_path.name.upper(),
                    data_path=data_path,
                    install_path=install_path,
                    exe_path=exe_path,
                    label=_label_from_install(install_path, server, data_path.name[:8]),
                    login=login,
                    server=server,
                    is_running=is_running,
                    pid=pid,
                    notes=tuple(notes),
                )
            )

    # A terminal launched in /portable mode keeps its data next to the exe, so it never appears
    # under %APPDATA%. Pick those up from the process list so they are at least visible.
    for key, (pid, exe) in running.items():
        if key in matched_exes:
            continue
        install_path = exe.parent
        found.append(
            DiscoveredTerminal(
                terminal_id=f"PORTABLE:{install_path.name}",
                data_path=None,
                install_path=install_path,
                exe_path=exe,
                label=_label_from_install(install_path, None, install_path.name),
                is_running=True,
                pid=pid,
                notes=("portable or unregistered install; account info needs a connection",),
            )
        )

    found.sort(key=lambda t: (t.label.lower(), t.terminal_id))
    log.info(
        "discovered %d terminals (%d running, %d usable)",
        len(found),
        sum(1 for t in found if t.is_running),
        sum(1 for t in found if t.usable),
    )
    return found
