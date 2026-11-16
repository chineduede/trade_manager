"""Logging a terminal into another of its accounts: which ones it has, the login, and the key."""

from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mt5copier import account_switch as account_mod
from mt5copier.account_switch import Account, LoginResult, known_accounts

TID = "A" * 32


def account_dirs(root: Path, *paths: str) -> None:
    for p in paths:
        (root / TID / "bases" / p).mkdir(parents=True)


def test_the_accounts_are_the_login_folders_under_each_server(tmp_path: Path) -> None:
    account_dirs(
        tmp_path,
        "ICMarketsSC-MT5-3/trades/8088642",
        "ICMarketsSC-Demo/trades/52978102",
        "ICMarketsSC-Demo/trades/5297",
        "ICMarketsSC-Demo/history/EURUSD",
        "Default/trades/0",
        "Custom",
    )
    (tmp_path / TID / "bases" / "ICMarketsSC-Demo" / "trades" / "notes").mkdir()
    assert known_accounts(TID, tmp_path) == [
        Account(5297, "ICMarketsSC-Demo"),
        Account(52978102, "ICMarketsSC-Demo"),
        Account(8088642, "ICMarketsSC-MT5-3"),
    ]


def test_a_terminal_with_no_data_folder_has_no_accounts(tmp_path: Path) -> None:
    assert known_accounts(TID, tmp_path) == []


class FakeMT5:
    def __init__(self, *, login_ok: bool = True, reports: int | None = None) -> None:
        self.login_ok = login_ok
        self.reports = reports
        self.logins: list[tuple[int, dict[str, Any]]] = []
        self.shut = False

    def initialize(self, path: str, timeout: int) -> bool:
        return True

    def login(self, login: int, **kwargs: Any) -> bool:
        self.logins.append((login, kwargs))
        if self.login_ok and self.reports is None:
            self.reports = login
        return self.login_ok

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(login=self.reports, server="ICMarketsSC-Demo")

    def last_error(self) -> tuple[int, str]:
        return (-6, "Terminal: Authorization failed")

    def shutdown(self) -> None:
        self.shut = True


def run(fake: FakeMT5, tmp_path: Path) -> LoginResult:
    out: queue.Queue[LoginResult] = queue.Queue()
    account_mod.run_login(
        str(tmp_path / "terminal64.exe"),
        52978102,
        "ICMarketsSC-Demo",
        out,
        str(tmp_path / "login.log"),
        _mt5=fake,
    )
    return out.get_nowait()


def test_the_login_names_no_password_and_reads_the_account_back(tmp_path: Path) -> None:
    fake = FakeMT5()
    result = run(fake, tmp_path)
    assert result == LoginResult(True, "logged in", 52978102, "ICMarketsSC-Demo")
    login, kwargs = fake.logins[0]
    assert login == 52978102 and kwargs["server"] == "ICMarketsSC-Demo"
    assert "password" not in kwargs, "MT5 uses the password saved in the terminal"
    assert fake.shut, "the connection is closed; the terminal is left running"


def test_a_refused_login_says_to_save_the_password(tmp_path: Path) -> None:
    result = run(FakeMT5(login_ok=False), tmp_path)
    assert not result.ok
    assert "Authorization failed" in result.said and "Save password" in result.said


def test_a_login_that_lands_on_another_account_is_a_failure(tmp_path: Path) -> None:
    result = run(FakeMT5(reports=111), tmp_path)
    assert not result.ok and result.login == 111
