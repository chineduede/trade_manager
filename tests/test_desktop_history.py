"""The desktop History page's bridge: the copied trades that pass the filter, worded rows.

What is kept, grouped and worded per step lives in mt5copier/history.py and is pinned by
tests/test_history.py; these tests pin the bridge's translation -- the narrowing, the counts,
the filter choices, and the tones the page styles by.
"""

from __future__ import annotations

import time
from pathlib import Path

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.desktop.api import DesktopApi
from mt5copier.history import HistoryStore, Step, Trade

T_A = "t-ftmo"
T_B = "t-e8"


class _Engine:
    def __init__(self, store: HistoryStore) -> None:
        self._store = store

    def trade_history(self, terminal_id: str) -> list[Trade]:
        return self._store.trades(terminal_id)


def _cfg(tmp_path: Path) -> AppConfig:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    return AppConfig(
        terminals={
            T_A: TerminalConfig(
                terminal_id=T_A,
                label="FTMO",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=51234,
                last_server="FTMO-Demo",
            ),
            T_B: TerminalConfig(
                terminal_id=T_B,
                label="E8",
                exe_path=exe,
                role=Role.SLAVE,
                last_login=20481,
                last_server="E8-Demo",
            ),
        }
    )


def _step(
    ts: float,
    tid: str,
    ticket: int,
    symbol: str,
    label: str,
    kind: str,
    outcome: str,
    action: str,
    detail: str,
    *,
    problem: bool = False,
    master: str = "",
    lots: float | None = None,
    guard: str = "",
) -> Step:
    return Step(
        ts=ts,
        terminal_id=tid,
        master_ticket=ticket,
        symbol=symbol,
        label=label,
        kind=kind,
        outcome=outcome,
        problem=problem,
        master=master,
        slave=detail,
        lots=lots,
        action=action,
        guard=guard,
    )


def _seeded(now: float) -> HistoryStore:
    store = HistoryStore()
    # #101 on FTMO: copied, stop moved, a breakeven note, closed.
    store.record(
        _step(
            now - 1000,
            T_A,
            101,
            "BTCUSD",
            "buy",
            "open",
            "done",
            "opened",
            "0.45 lots at 60,000, stop 59,000, target 62,000 in 120 ms",
            master="master 1 lots at 60,000",
            lots=0.45,
        )
    )
    store.record(
        _step(
            now - 900,
            T_A,
            101,
            "BTCUSD",
            "buy",
            "modify_sltp",
            "done",
            "stop/target moved",
            "stop 60,100, target 62,000 in 80 ms",
        )
    )
    store.note(
        T_A, 101, "BTCUSD", "buy", "breakeven", "stop moved to entry + commission", now=now - 800
    )
    store.record(
        _step(now - 700, T_A, 101, "BTCUSD", "buy", "close", "done", "closed", "0.45 lots")
    )
    # #201 on E8: refused once, then copied with a guard's resize under it.
    store.record(
        _step(
            now - 600,
            T_B,
            201,
            "EURUSD",
            "buy",
            "open",
            "refused",
            "copy refused",
            "not enough money",
            problem=True,
        )
    )
    store.record(
        _step(
            now - 590,
            T_B,
            201,
            "EURUSD",
            "buy",
            "open",
            "done",
            "opened",
            "0.22 lots at 1.0790 in 95 ms",
            lots=0.22,
            guard="Margin per trade shrunk it: from 0.5 to 0.22 lots",
        )
    )
    # #102 on FTMO: a resting order, the most recently copied trade of all.
    store.record(
        _step(
            now - 300,
            T_A,
            102,
            "XAUUSD",
            "sell limit",
            "place",
            "done",
            "placed",
            "0.3 lots at 2,650, stop 2,660, target 2,600",
            lots=0.3,
        )
    )
    return store


def _api(tmp_path: Path, now: float) -> DesktopApi:
    cfg = _cfg(tmp_path)
    return DesktopApi(cfg=cfg, engine=_Engine(_seeded(now)), guards=None)  # type: ignore[arg-type]


def test_cards_come_newest_first_across_the_slaves(tmp_path: Path) -> None:
    now = time.time()
    out = _api(tmp_path, now).history_state("all", "all", False, "")

    assert out["total"] == 3
    assert out["matched"] == 3
    assert [t["ticket"] for t in out["trades"]] == [102, 201, 101]
    assert [s["name"] for s in out["slaves"]] == ["FTMO", "E8"]
    assert out["symbols"] == ["BTCUSD", "EURUSD", "XAUUSD"]

    closed = out["trades"][2]
    assert closed["slave"] == "FTMO"
    assert closed["symbol"] == "BTCUSD"
    assert closed["label"] == "buy"
    assert closed["lots"] == 0.45
    assert closed["status"] == "closed"
    assert closed["problems"] == 0
    assert [s["action"] for s in closed["steps"]] == [
        "opened",
        "stop/target moved",
        "breakeven",
        "closed",
    ]
    first = closed["steps"][0]
    assert first["when"] == time.strftime("%H:%M:%S", time.localtime(now - 1000))
    assert first["master"] == "master 1 lots at 60,000"
    assert first["tone"] == ""
    # The copier's own step reads as a note, the TUI history page's blue.
    assert closed["steps"][2]["tone"] == "note"

    resting = out["trades"][0]
    assert resting["status"] == "resting"
    assert resting["label"] == "sell limit"


def test_a_failed_step_and_its_guard_keep_their_colours(tmp_path: Path) -> None:
    now = time.time()
    out = _api(tmp_path, now).history_state("all", "all", False, "")
    e8 = out["trades"][1]
    assert e8["ticket"] == 201
    assert e8["status"] == "open"
    assert e8["problems"] == 1
    refused, opened = e8["steps"]
    assert refused["tone"] == "bad"
    assert refused["detail"] == "not enough money"
    assert opened["guard"] == "Margin per trade shrunk it: from 0.5 to 0.22 lots"


def test_the_filters_narrow_in_the_bridge(tmp_path: Path) -> None:
    now = time.time()
    api = _api(tmp_path, now)

    one_slave = api.history_state(T_A, "all", False, "")
    assert one_slave["total"] == 2
    assert [t["ticket"] for t in one_slave["trades"]] == [102, 101]

    # Narrowing by symbol leaves the counts honest and the choices whole.
    by_symbol = api.history_state("all", "EURUSD", False, "")
    assert (by_symbol["total"], by_symbol["matched"]) == (3, 1)
    assert by_symbol["symbols"] == ["BTCUSD", "EURUSD", "XAUUSD"]
    assert by_symbol["trades"][0]["ticket"] == 201

    failed = api.history_state("all", "all", True, "")
    assert [t["ticket"] for t in failed["trades"]] == [201]

    # Find matches the ticket, the symbol, the label or the slave's name, case blind.
    assert [t["ticket"] for t in api.history_state("all", "all", False, "102")["trades"]] == [102]
    assert [t["ticket"] for t in api.history_state("all", "all", False, "e8")["trades"]] == [201]
    assert api.history_state("all", "all", False, "nothing-like-this")["matched"] == 0


def test_an_empty_store_counts_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    api = DesktopApi(cfg=cfg, engine=_Engine(HistoryStore()), guards=None)  # type: ignore[arg-type]
    out = api.history_state("all", "all", False, "")
    assert out["total"] == 0
    assert out["trades"] == []
    assert len(out["slaves"]) == 2
    assert out["days"] == cfg.history_days
