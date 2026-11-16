"""The desktop account-login flow: the TUI's i key as an ask/commit bridge pair.

Which accounts a terminal knows and what a login does live in mt5copier/account_switch.py and
are pinned by its own tests; these pin the bridge's translation -- who may be logged in, the
rows the pick shows, and what a finished login records.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier import account_switch
from mt5copier.account_switch import Account, LoginResult
from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi

T_FREE = "t-free"
T_SLAVE = "t-slave"
T_CT = "t-ctrader"


class _Engine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sync(self) -> None:
        self.calls.append("sync")


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        account_names={"51234@FTMO-Demo": "ftmo eval"},
        terminals={
            T_FREE: TerminalConfig(
                terminal_id=T_FREE,
                label="Spare",
                exe_path=exe,
                role=Role.UNASSIGNED,
                last_login=20481,
                last_server="E8-Demo",
            ),
            T_SLAVE: TerminalConfig(
                terminal_id=T_SLAVE,
                label="FTMO",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=51234,
                last_server="FTMO-Demo",
            ),
            T_CT: TerminalConfig(
                terminal_id=T_CT,
                label="IC cTrader",
                role=Role.UNASSIGNED,
                ctrader_account_id=7,
            ),
        },
    )


_KNOWN = [Account(51234, "FTMO-Demo"), Account(20481, "E8-Demo")]


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DesktopApi, _Engine]:
    engine = _Engine()
    api = DesktopApi(cfg=_cfg(tmp_path), engine=engine)  # type: ignore[arg-type]
    monkeypatch.setattr(account_switch, "known_accounts", lambda tid, root=None: list(_KNOWN))
    return api, engine


def test_the_ask_lists_the_saved_accounts_and_marks_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    out = api.account_login_ask(T_FREE)
    assert out["ok"] is True
    assert out["name"] == "Spare"
    assert out["accounts"] == [
        {"key": "51234@FTMO-Demo", "words": "ftmo eval · 51234@FTMO-Demo", "now": False},
        {"key": "20481@E8-Demo", "words": "20481@E8-Demo", "now": True},
    ]


def test_only_an_unassigned_mt5_terminal_with_an_exe_may_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    # The slave is named by its account's given name, as every page names it.
    assert "unassign ftmo eval first" in api.account_login_ask(T_SLAVE)["why"]
    assert "cTrader" in api.account_login_ask(T_CT)["why"]
    assert "gone" in api.account_login_ask("t-nothing")["why"]

    monkeypatch.setattr(account_switch, "known_accounts", lambda tid, root=None: [])
    assert "no accounts found for Spare" in api.account_login_ask(T_FREE)["why"]


def test_a_login_records_the_account_the_terminal_landed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, engine = _api(tmp_path, monkeypatch)
    seen: list[tuple[Path, Account]] = []

    def fake_log_in(exe: Path, acct: Account, log_file: Path) -> LoginResult:
        seen.append((exe, acct))
        return LoginResult(True, "logged in", acct.login, acct.server)

    monkeypatch.setattr(account_switch, "log_in", fake_log_in)
    out = api.account_login(T_FREE, "51234@FTMO-Demo")
    assert out["ok"] is True
    assert out["message"] == "Spare is logged in to ftmo eval (51234@FTMO-Demo)"
    assert seen == [(api._cfg.terminals[T_FREE].exe_path, Account(51234, "FTMO-Demo"))]
    tcfg = api._cfg.terminals[T_FREE]
    assert (tcfg.last_login, tcfg.last_server) == (51234, "FTMO-Demo")
    assert "sync" in engine.calls


def test_a_failed_login_changes_nothing_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, engine = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(
        account_switch,
        "log_in",
        lambda exe, acct, log_file: LoginResult(False, "is its password saved in the terminal?"),
    )
    out = api.account_login(T_FREE, "51234@FTMO-Demo")
    assert out["ok"] is False
    assert "Spare was not logged in to ftmo eval (51234@FTMO-Demo)" in out["message"]
    tcfg = api._cfg.terminals[T_FREE]
    assert (tcfg.last_login, tcfg.last_server) == (20481, "E8-Demo")
    assert engine.calls == []

    # Already on it: nothing to do, and that is said.
    already = api.account_login(T_FREE, "20481@E8-Demo")
    assert already["ok"] is False
    assert "already logged in" in already["message"]
