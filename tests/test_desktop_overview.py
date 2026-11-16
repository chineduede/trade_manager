"""The desktop detail page's Overview tab: the TUI enter page's columns as one bridge call.

The numbers themselves are mt5copier/terminal_detail.py's and copy_stats.py's, pinned by their
own tests; these pin the bridge's translation -- the wording and tones of the three columns,
the positions and orders rows with their links, R and breakeven cells, and the honest states
(not connected, unreadable positions, a terminal that is gone).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mt5copier.config import AppConfig, BreakevenMode, Role, TerminalConfig
from mt5copier.copy_stats import TerminalStats
from mt5copier.desktop.api import DesktopApi, _ov_exposure, _ov_slippage
from mt5copier.engine import TerminalView
from mt5copier.protocol import (
    ACCOUNT_TRADE_MODE_DEMO,
    OrderType,
    PendingOrder,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import OpenCopy

T_M = "t-master"
T_A = "t-ftmo"
T_B = "t-e8"


class _Engine:
    """Only what the overview bridge reads, handed back from plain dicts."""

    def __init__(self) -> None:
        self._views: list[TerminalView] = []
        self.stats: dict[str, TerminalStats] = {}
        self.copies: dict[str, list[OpenCopy]] = {}
        self.orphan_list: dict[str, list[Any]] = {}
        self.direct = False
        self.virtual_book: tuple[Position, ...] = ()

    def direct_mode(self) -> bool:
        return self.direct

    def direct_positions(self) -> tuple[Position, ...]:
        return self.virtual_book

    def views(self) -> list[TerminalView]:
        return self._views

    def holds(self) -> dict[str, str]:
        return {}

    def terminal_stats(self, tid: str) -> TerminalStats:
        return self.stats.get(tid, TerminalStats())

    def open_copies(self, tid: str) -> list[OpenCopy] | None:
        return self.copies.get(tid, [])

    def orphans(self, tid: str) -> list[Any]:
        return self.orphan_list.get(tid, [])

    def guard_report(self, tid: str) -> Any:
        return SimpleNamespace(rules=(), holding_back=False, worst=None)


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")

    def t(tid: str, label: str, role: Role) -> TerminalConfig:
        return TerminalConfig(terminal_id=tid, label=label, exe_path=exe, role=role)

    return AppConfig(
        terminals={
            T_M: t(T_M, "Axi", Role.MASTER),
            T_A: t(T_A, "FTMO", Role.SLAVE),
            T_B: t(T_B, "E8", Role.SLAVE),
        }
    )


def _api(cfg: AppConfig) -> tuple[DesktopApi, _Engine]:
    engine = _Engine()
    api = DesktopApi(cfg=cfg, engine=engine)  # type: ignore[arg-type]
    return api, engine


def _view(tcfg: TerminalConfig, snap: Snapshot | None) -> TerminalView:
    return TerminalView(
        cfg=tcfg,
        snapshot=snap,
        received_at=time.monotonic(),
        process_alive=True,
        supervised=True,
    )


def _snap(tid: str, **kw: Any) -> Snapshot:
    base: dict[str, Any] = {
        "login": 928374,
        "server": "Broker-Demo",
        "currency": "USD",
        "balance": 10_000.0,
        "equity": 10_050.0,
        "margin": 500.0,
        "margin_free": 9_550.0,
        "margin_level": 2_010.0,
        "leverage": 100,
        "trade_mode": ACCOUNT_TRADE_MODE_DEMO,
        "ping_last_us": 32_000,
    }
    base.update(kw)
    return Snapshot(terminal_id=tid, state=WorkerState.CONNECTED, **base)


MASTER_POS = Position(
    ticket=900, symbol="EURUSD", type=PositionType.BUY, volume=1.0, price_open=1.1000
)


def test_a_slaves_overview_carries_all_three_columns_and_the_tables(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.terminals[T_A].breakeven_mode = BreakevenMode.R
    cfg.terminals[T_A].breakeven_trigger_r = 1.0
    api, engine = _api(cfg)
    engine._views = [
        _view(cfg.terminals[T_M], _snap(T_M, open_positions=(MASTER_POS,))),
        _view(
            cfg.terminals[T_A],
            _snap(
                T_A,
                open_positions=(
                    Position(
                        ticket=11,
                        symbol="EURUSD",
                        type=PositionType.BUY,
                        volume=0.5,
                        price_open=1.1000,
                        sl=1.0950,
                        tp=1.1100,
                    ),
                ),
                live_positions=(
                    PositionLive(ticket=11, price_current=1.1050, profit=25.0, swap=-1.2),
                ),
                pending_orders=(
                    PendingOrder(
                        ticket=7,
                        symbol="EURUSD",
                        type=OrderType.BUY_LIMIT,
                        volume=0.2,
                        price_open=1.0900,
                        sl=1.0850,
                        tp=1.1000,
                        time_setup=int(time.time() - 120),
                    ),
                ),
            ),
        ),
    ]
    engine.copies[T_A] = [
        OpenCopy(
            master_ticket=900,
            slave_ticket=11,
            symbol="EURUSD",
            volume=0.5,
            resting=False,
            risk_distance=0.0050,
        ),
        OpenCopy(master_ticket=901, slave_ticket=7, symbol="EURUSD", volume=0.2, resting=True),
    ]
    engine.stats[T_A] = TerminalStats(
        latencies_ms=(120.0, 80.0),
        slippage_points=(5.0, -3.0),
        succeeded=2,
        refused=1,
        skipped=(("news guard", 3),),
        connected_for_s=65.0,
        disconnects=1,
    )

    out = api.terminal_overview_state(T_A)
    assert out["ok"] is True
    assert out["role"] == "slave"
    assert out["accountKind"] == "demo"
    assert out["connected"] == "1m 05s"
    assert out["ping"] == "32 ms"
    assert out["leverage"] == 100

    a = out["account"]
    assert a["balance"] == 10_000.0
    assert a["floating"] == 50.0
    assert a["usedFraction"] == 500.0 / 10_050.0
    assert a["levelTier"] == "good"

    rule = {r["label"]: r for r in out["rule"] if r["label"]}
    assert rule["Risk per trade"]["text"] == "1% of balance"
    assert rule["Once open"]["text"].startswith("the master manages it")
    assert rule["Breakeven"]["text"] == "the master's stop moves"
    assert rule["Symbols copied"]["text"] == "all"
    assert rule["Name on this broker"]["text"] == "the same as the master's"

    copying = {r["label"]: r for r in out["copying"]["rows"]}
    assert copying["Orders copied"] == {"label": "Orders copied", "text": "2", "tone": "blue"}
    assert copying["Skipped"]["text"] == "3"
    assert copying["  most often"]["text"] == "news guard x3"
    assert copying["Refused, retried, late"] == {
        "label": "Refused, retried, late",
        "text": "1 / 0 / 0",
        "tone": "red",
    }
    assert copying["Delay, last / median / max"]["text"] == "80 / 100 / 120 ms"
    assert copying["Slippage, last"] == {
        "label": "Slippage, last",
        "text": "3 pts better",
        "tone": "blue",
    }
    assert copying["Slippage, average"]["tone"] == ""  # the mean is never coloured
    assert copying["Slippage, worst"]["tone"] == "red"
    assert copying["Disconnects"] == {"label": "Disconnects", "text": "1", "tone": "yellow"}
    assert out["copying"]["latencies"] == [120.0, 80.0]

    p = out["positions"]
    assert p["unreadable"] is False
    assert p["exposure"] == "EURUSD long 0.5 lots"
    assert p["total"] == 25.0
    (row,) = p["rows"]
    assert row["link"] == {"text": "#900 (master 1 lots)", "tone": ""}
    assert row["rNow"] == 1.0
    assert row["rTarget"] == 2.0
    # digits are read off the prices themselves (1.095 needs three), the TUI's own rule
    assert row["breakeven"]["text"] == "1R at 1.105"
    assert row["slHit"] is False

    o = out["orders"]
    assert o["unreadable"] is False
    (orow,) = o["rows"]
    assert orow["kind"] == "buy limit"
    assert orow["side"] == "buy"
    assert orow["link"] == {"text": "#901", "tone": ""}
    assert orow["placed"].endswith(" ago")
    assert o["anyStopLimit"] is False


def test_the_masters_overview_shows_its_slaves_and_the_connection(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)
    engine._views = [
        _view(cfg.terminals[T_M], _snap(T_M, open_positions=(MASTER_POS,))),
        _view(cfg.terminals[T_A], _snap(T_A)),
        _view(cfg.terminals[T_B], _snap(T_B)),
    ]
    engine.copies[T_A] = [
        OpenCopy(master_ticket=900, slave_ticket=11, symbol="EURUSD", volume=0.5, resting=False)
    ]
    engine.copies[T_B] = []

    out = api.terminal_overview_state(T_M)
    assert out["role"] == "master"
    assert "rule" not in out
    assert out["copiedTo"]["who"] == "master manages"
    assert [s["name"] for s in out["copiedTo"]["slaves"]] == ["FTMO", "E8"]
    assert out["copiedTo"]["slaves"][0]["card"]["tid"] == T_A

    assert "copying" not in out
    rows = {r["label"]: r for r in out["connection"]["rows"]}
    assert rows["Ping"]["text"] == "32 ms"
    assert out["connection"]["note"].startswith("The master is only read from")

    # One of two slaves holds the copy: a shortfall the row says in yellow.
    (row,) = out["positions"]["rows"]
    assert row["link"] == {"text": "on 1/2 slaves", "tone": "yellow"}
    assert row["breakeven"] is None


def test_honest_blanks_not_connected_unreadable_and_gone(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api, engine = _api(cfg)

    # No view at all: nothing is invented.
    out = api.terminal_overview_state(T_A)
    assert out["ok"] is True
    assert out["account"] is None
    assert out["positions"] is None
    assert out["orders"] is None
    assert out["connected"] is None
    assert out["ping"] is None

    # Connected, but the positions could not be read; resting orders likewise.
    engine._views = [
        _view(cfg.terminals[T_A], _snap(T_A, open_positions=None, pending_orders=None))
    ]
    out = api.terminal_overview_state(T_A)
    assert out["positions"]["unreadable"] is True
    assert out["positions"]["rows"] == []
    assert out["positions"]["exposure"] is None
    assert out["orders"]["unreadable"] is True

    assert api.terminal_overview_state("t-gone")["ok"] is False


def test_the_rule_column_words_the_unusual_settings(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    t = cfg.terminals[T_A]
    t.invert = True
    t.master_manages = False
    t.symbol_prefix = ""
    t.symbol_suffix = ".r"
    t.symbol_map = {"US30": "US30.cash", "GER40": "DE40", "NAS100": "USTEC", "UK100": "FTSE"}
    t.allow_live_trading = True
    api, engine = _api(cfg)

    rule = api.terminal_overview_state(T_A)["rule"]
    by = {r["label"]: r for r in rule if r["label"]}
    assert by["Direction"] == {"label": "Direction", "text": "reversed", "tone": "yellow"}
    assert by["Once open"]["text"] == "this slave manages it"
    assert by["Breakeven"]["text"] == "off"  # the slave's own trigger, off by default
    assert by["Name on this broker"]["text"] == "EURUSD is traded as EURUSD.r"
    assert by["Live trading"] == {"label": "Live trading", "text": "allowed", "tone": "red"}
    renamed = [r["text"] for r in rule if "is traded as" in r["text"]][1:]
    assert len(renamed) == 3  # three shown, the fourth said as "+1 more"
    assert any("+1 more" in r["text"] for r in rule)


def test_direct_mode_links_copies_to_the_virtual_book(tmp_path: Path) -> None:
    """A direct profile has no master terminal: the virtual book is the master side,
    or every healthy copy would read "closed on master" in red."""
    cfg = _cfg(tmp_path)
    del cfg.terminals[T_M]
    api, engine = _api(cfg)
    engine.direct = True
    engine.virtual_book = (MASTER_POS,)
    engine._views = [
        _view(
            cfg.terminals[T_A],
            _snap(
                T_A,
                open_positions=(
                    Position(
                        ticket=11,
                        symbol="EURUSD",
                        type=PositionType.BUY,
                        volume=0.5,
                        price_open=1.1000,
                        sl=1.0950,
                        tp=1.1100,
                    ),
                ),
                live_positions=(
                    PositionLive(ticket=11, price_current=1.1050, profit=25.0, swap=0.0),
                ),
            ),
        ),
    ]
    engine.copies[T_A] = [
        OpenCopy(
            master_ticket=900,
            slave_ticket=11,
            symbol="EURUSD",
            volume=0.5,
            resting=False,
            risk_distance=0.0050,
        ),
    ]

    (row,) = api.terminal_overview_state(T_A)["positions"]["rows"]
    assert row["link"] == {"text": "#900 (master 1 lots)", "tone": ""}

    # And a copy whose virtual ticket truly is gone still says so.
    engine.virtual_book = ()
    (row,) = api.terminal_overview_state(T_A)["positions"]["rows"]
    assert "closed on master" in row["link"]["text"]
    assert row["link"]["tone"] == "red"


def test_the_small_helpers_say_things_the_tui_way() -> None:
    assert _ov_slippage(None) == ("—", "muted")
    assert _ov_slippage(0.4) == ("none", "")
    assert _ov_slippage(1175.0) == ("1,175 pts worse", "red")
    assert _ov_slippage(-12.0) == ("12 pts better", "blue")
    assert _ov_slippage(-12.0, colour=False) == ("12 pts better", "")

    assert _ov_exposure(None) is None
    assert _ov_exposure(()) == "flat"
    sells = (
        Position(ticket=1, symbol="BTCUSD", type=PositionType.BUY, volume=3.11, price_open=1.0),
        Position(ticket=2, symbol="BTCUSD", type=PositionType.SELL, volume=1.0, price_open=1.0),
    )
    assert _ov_exposure(sells) == "BTCUSD long 2.11 lots (gross 4.11)"
