"""Discovery tests.

Built against synthetic ``%APPDATA%\\MetaQuotes\\Terminal`` trees rather than the real one, so
they assert behaviour instead of describing this particular machine. The encoding and
edge-case fixtures below mirror things actually observed on it: UTF-16 common.ini, a leftover
install whose terminal64.exe is gone, and a ``bases/Chats`` folder that is not a trade server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.discovery import (
    _clean_label,
    _decode_ini,
    _parse_common_ini,
    discover_terminals,
)

HASH_A = "010E047102812FC0C18890992854220E"
HASH_B = "81A933A9AFC5DE3C23B15CAB19C63850"


def make_terminal(
    root: Path,
    instance_hash: str,
    install_dir: Path,
    *,
    login: int | None = 12345,
    server: str | None = "Broker-Live",
    encoding: str = "utf-16",
    origin_encoding: str = "utf-16",
    bases: tuple[str, ...] = ("Broker-Live", "Chats", "Default"),
    make_exe: bool = True,
    write_origin: bool = True,
) -> Path:
    """Build one fake terminal data folder plus its install directory.

    Defaults to UTF-16 for both files, which is what MT5 actually writes on this machine.
    """
    data = root / instance_hash
    (data / "config").mkdir(parents=True)
    if write_origin:
        (data / "origin.txt").write_bytes(str(install_dir).encode(origin_encoding))

    lines = ["[Common]"]
    if login is not None:
        lines.append(f"Login={login}")
    if server is not None:
        lines.append(f"Server={server}")
    lines += ["ProxyEnable=0", "", "[Charts]", "MaxBars=100000"]
    (data / "config" / "common.ini").write_bytes("\n".join(lines).encode(encoding))

    for b in bases:
        (data / "bases" / b).mkdir(parents=True)

    install_dir.mkdir(parents=True, exist_ok=True)
    if make_exe:
        (install_dir / "terminal64.exe").write_bytes(b"MZ")
    return data


@pytest.fixture
def no_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise process enumeration so tests don't depend on the host's real processes."""
    monkeypatch.setattr("mt5copier.discovery._running_terminals", lambda: {})


# --- ini decoding --------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8", "cp1252"])
def test_decode_ini_handles_each_encoding(encoding: str) -> None:
    assert "Login=99" in _decode_ini("[Common]\nLogin=99".encode(encoding))


def test_decode_ini_does_not_mangle_utf8_as_utf16() -> None:
    """The failure mode that a blind try-in-order had.

    UTF-8 bytes of an even length decode as UTF-16 without raising, producing plausible CJK
    mojibake. BOM-first detection is what prevents that, so assert the real path survives.
    """
    path = "C:\\Program Files\\Broker X MT5"
    assert _decode_ini(path.encode("utf-8")) == path


def test_decode_ini_utf16_without_bom() -> None:
    raw = "[Common]\nLogin=7".encode("utf-16-le")
    assert not raw.startswith(b"\xff\xfe")
    assert "Login=7" in _decode_ini(raw)


def test_discovery_accepts_utf8_origin(tmp_path: Path, no_running: None) -> None:
    """Portable and older installs may write origin.txt as plain UTF-8."""
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "Broker Y MT5", origin_encoding="utf-8")
    assert discover_terminals(root)[0].label == "Broker Y"


def test_parse_common_ini_utf16(tmp_path: Path) -> None:
    ini = tmp_path / "common.ini"
    ini.write_bytes("[Common]\nLogin=1514431066\nServer=FTMO-Demo".encode("utf-16"))
    assert _parse_common_ini(ini) == (1514431066, "FTMO-Demo")


def test_parse_common_ini_ignores_other_sections(tmp_path: Path) -> None:
    """A Login key under a different section must not be picked up."""
    ini = tmp_path / "common.ini"
    ini.write_bytes("[Charts]\nLogin=999\n[Common]\nLogin=111\n".encode("utf-16"))
    assert _parse_common_ini(ini)[0] == 111


def test_parse_common_ini_survives_malformed_lines(tmp_path: Path) -> None:
    """One bad line must not cost us the whole terminal."""
    ini = tmp_path / "common.ini"
    ini.write_bytes("[Common]\ngarbage line\nLogin=notanumber\nServer=Broker-X\n".encode("utf-16"))
    assert _parse_common_ini(ini) == (None, "Broker-X")


def test_parse_common_ini_missing_file(tmp_path: Path) -> None:
    assert _parse_common_ini(tmp_path / "nope.ini") == (None, None)


# --- label cleaning ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FTMO Global Markets MT5 Terminal", "FTMO Global Markets"),
        ("Five Percent Online MetaTrader 5 #1", "Five Percent Online #1"),
        ("MetaTrader 5 IC Markets Global #2", "IC Markets Global #2"),
        ("VT Markets (Pty) MT5 Terminal", "VT Markets (Pty)"),
        ("MetaTrader 5 EXNESS", "EXNESS"),
        ("Goat Funded MT5 Terminal", "Goat Funded"),
        ("MetaTrader 5", ""),
    ],
)
def test_clean_label(raw: str, expected: str) -> None:
    assert _clean_label(raw) == expected


# --- discovery -----------------------------------------------------------------------------


def test_discovers_terminal_with_full_metadata(tmp_path: Path, no_running: None) -> None:
    root = tmp_path / "Terminal"
    make_terminal(
        root,
        HASH_A,
        tmp_path / "Program Files" / "Broker X MT5 Terminal",
        login=777,
        server="BrokerX-Live",
    )
    found = discover_terminals(root)
    assert len(found) == 1
    t = found[0]
    assert t.terminal_id == HASH_A
    assert t.label == "Broker X"
    assert (t.login, t.server) == (777, "BrokerX-Live")
    assert t.usable and not t.is_running and t.notes == ()


def test_chats_is_not_reported_as_a_server(tmp_path: Path, no_running: None) -> None:
    """bases/Chats holds MQL5 community chat state, not a trade server."""
    root = tmp_path / "Terminal"
    make_terminal(
        root,
        HASH_A,
        tmp_path / "pf" / "B MT5",
        bases=("Broker-Live", "Chats", "Default", "signals"),
    )
    assert discover_terminals(root)[0].servers_seen == ("Broker-Live",)


def test_skips_non_instance_directories(tmp_path: Path, no_running: None) -> None:
    """Common/, Community/ and Help/ are shared state, not terminals."""
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "B MT5")
    for sibling in ("Common", "Community", "Help"):
        (root / sibling).mkdir(parents=True)
    assert len(discover_terminals(root)) == 1


def test_missing_exe_is_flagged_unusable_not_dropped(tmp_path: Path, no_running: None) -> None:
    """An uninstalled-but-not-cleaned-up terminal should still be visible, clearly marked."""
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "Gone MT5", make_exe=False)
    t = discover_terminals(root)[0]
    assert not t.usable
    assert any("exe missing" in n for n in t.notes)


def test_missing_origin_is_flagged(tmp_path: Path, no_running: None) -> None:
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "B MT5", write_origin=False)
    t = discover_terminals(root)[0]
    assert t.install_path is None and not t.usable
    assert "no origin.txt" in t.notes


def test_never_logged_in_is_flagged(tmp_path: Path, no_running: None) -> None:
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "B MT5", login=None, server=None)
    t = discover_terminals(root)[0]
    assert t.login is None
    assert "never logged in" in t.notes


def test_generic_install_name_falls_back_to_server(tmp_path: Path, no_running: None) -> None:
    """Several installs here live in a plain 'MetaTrader 5' folder; the broker comes from the
    server name so two unrelated prop accounts don't both render as 'MetaTrader 5'."""
    root = tmp_path / "Terminal"
    make_terminal(
        root, HASH_A, tmp_path / "pf" / "MetaTrader 5", login=9000, server="AquaFunded-Server"
    )
    assert discover_terminals(root)[0].label == "AquaFunded"


def test_results_sorted_by_label(tmp_path: Path, no_running: None) -> None:
    root = tmp_path / "Terminal"
    make_terminal(root, HASH_A, tmp_path / "pf" / "Zeta MT5")
    make_terminal(root, HASH_B, tmp_path / "pf" / "Alpha MT5")
    assert [t.label for t in discover_terminals(root)] == ["Alpha", "Zeta"]


def test_running_terminal_is_marked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Terminal"
    install = tmp_path / "pf" / "Live MT5"
    make_terminal(root, HASH_A, install)
    exe = install / "terminal64.exe"
    monkeypatch.setattr(
        "mt5copier.discovery._running_terminals",
        lambda: {str(exe).lower(): (4321, exe)},
    )
    t = discover_terminals(root)[0]
    assert t.is_running and t.pid == 4321


def test_portable_terminal_found_via_process_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A /portable terminal keeps data next to its exe, so %APPDATA% never sees it."""
    root = tmp_path / "Terminal"
    root.mkdir(parents=True)
    exe = tmp_path / "Portable" / "SomeBroker MT5" / "terminal64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(
        "mt5copier.discovery._running_terminals",
        lambda: {str(exe).lower(): (99, exe)},
    )
    found = discover_terminals(root)
    assert len(found) == 1
    assert found[0].terminal_id.startswith("PORTABLE:")
    assert found[0].is_running and found[0].data_path is None


def test_missing_terminal_root_returns_empty(tmp_path: Path, no_running: None) -> None:
    assert discover_terminals(tmp_path / "does-not-exist") == []
