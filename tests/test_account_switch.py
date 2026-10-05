"""Logging a terminal into another of its accounts: which ones it has, the login, and the key."""

from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tui_support import TID_BROKEN, TID_MASTER, TID_SLAVE, make_app, make_cfg, toasts

from mt5copier import account_switch as account_mod
from mt5copier.account_switch import Account, LoginResult, known_accounts
from mt5copier.config import AppConfig, Role
from mt5copier.tui.guards import GuardPick
from mt5copier.tui.menu import terminal_actions

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


# --- the key ----------------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    c = make_cfg(tmp_path)
    c.terminals[TID_SLAVE].last_server = "ICMarketsSC-Demo"
    c.account_names["52978102@ICMarketsSC-Demo"] = "IC Markets main"
    return c


def fake_accounts(monkeypatch: pytest.MonkeyPatch, calls: list[Account]) -> None:
    monkeypatch.setattr(
        account_mod,
        "known_accounts",
        lambda tid: [Account(222, "ICMarketsSC-Demo"), Account(52978102, "ICMarketsSC-Demo")],
    )

    def log_in(exe: Path, acct: Account, log_file: Path) -> LoginResult:
        calls.append(acct)
        return LoginResult(True, "logged in", acct.login, acct.server)

    monkeypatch.setattr(account_mod, "log_in", log_in)


async def test_i_on_an_unassigned_terminal_logs_it_in_to_the_picked_account(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Account] = []
    fake_accounts(monkeypatch, calls)
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test(size=(140, 50)) as pilot:
        app._select(TID_SLAVE)
        await pilot.press("i")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, GuardPick)
        assert [w for _, w in picker._options] == [
            "222@ICMarketsSC-Demo  (logged in now)",
            "IC Markets main · 52978102@ICMarketsSC-Demo",
        ]
        await pilot.press("down", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert calls == [Account(52978102, "ICMarketsSC-Demo")]
        assert cfg.terminals[TID_SLAVE].last_login == 52978102
        assert app._switching == set()
        assert any("logged in to IC Markets main" in t for t in toasts(app))


async def test_a_terminal_with_a_role_is_unassigned_first(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Account] = []
    fake_accounts(monkeypatch, calls)
    cfg.set_role(TID_MASTER, Role.MASTER)
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test(size=(140, 50)) as pilot:
        app._select(TID_MASTER)
        await pilot.press("i")
        await pilot.pause()
        assert not isinstance(app.screen, GuardPick)
        assert any("unassign Alpha Broker first" in t for t in toasts(app))


async def test_no_role_is_given_while_it_logs_in(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test(size=(140, 50)) as pilot:
        app._switching.add(TID_MASTER)
        app._select(TID_MASTER)
        await pilot.press("m")
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED
        assert any("is logging in" in t for t in toasts(app))


def test_the_menu_offers_it_only_to_an_installed_unassigned_terminal(cfg: AppConfig) -> None:
    def keys(tid: str) -> list[str]:
        return [
            i.key
            for i in terminal_actions(cfg.terminals[tid], has_master=False, orphans_waiting=False)
        ]

    assert "i" in keys(TID_SLAVE)
    assert "i" not in keys(TID_BROKEN), "nothing to launch"
    cfg.set_role(TID_MASTER, Role.MASTER)
    assert "i" not in keys(TID_MASTER)
