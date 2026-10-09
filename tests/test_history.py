"""Each trade's history per slave: the store, its wording, and how the day book feeds it."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_day_book import TID_S1, TID_S2, action, pos, result
from textual.widgets import Static
from tui_support import TID_MASTER, TID_SLAVE, make_app, make_cfg, plain

from mt5copier.config import AppConfig, Role
from mt5copier.day_book import DayBook, MasterEvent, Outcome, SlaveOutcome
from mt5copier.engine import Engine
from mt5copier.history import (
    NOTE,
    ORPHANED,
    HistoryStore,
    Step,
    Trade,
    describe_step,
    step_for,
)
from mt5copier.protocol import ActionKind
from mt5copier.reconcile import Action
from mt5copier.tui.history import HistoryScreen, card_body, card_title

DAY = 86400.0


def step(
    ts: float,
    kind: ActionKind | str = ActionKind.OPEN,
    *,
    ticket: int = 11,
    tid: str = TID_S1,
    outcome: Outcome | str = Outcome.DONE,
    slave: str = "opened 1 lots",
    problem: bool = False,
) -> Step:
    return Step(
        ts, tid, ticket, "BTCUSD", "buy", str(kind), str(outcome), problem, "master did", slave
    )


# --- the store -------------------------------------------------------------------------------


def test_steps_are_grouped_per_trade_and_slave_newest_copy_first(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(step(100.0, ticket=1), now=100.0)
    store.record(step(200.0, ticket=2), now=200.0)
    store.record(step(300.0, ActionKind.MODIFY_SLTP, ticket=1, slave="stop set"), now=300.0)
    store.record(step(310.0, ticket=1, tid=TID_S2), now=310.0)

    trades = store.trades(TID_S1)
    assert [t.master_ticket for t in trades] == [2, 1], "the trade copied last comes first"
    assert [s.kind for s in trades[1].steps] == ["open", "modify_sltp"]
    assert [t.master_ticket for t in store.trades(TID_S2)] == [1]


def test_it_survives_reopening(tmp_path: Path) -> None:
    HistoryStore(tmp_path / "h.db").record(step(100.0), now=100.0)
    assert len(HistoryStore(tmp_path / "h.db").trades(TID_S1)) == 1


def test_a_step_repeated_back_to_back_is_kept_once(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(step(100.0), now=100.0)
    store.record(step(500.0), now=500.0)
    assert len(store.trades(TID_S1)[0].steps) == 1


def test_only_trades_copied_to_the_slave_are_read_back(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.record(step(100.0, ticket=1, outcome=Outcome.SKIPPED, slave="copy skipped"), now=100.0)
    store.record(step(110.0, ActionKind.CLOSE, ticket=1, slave="closed"), now=110.0)
    store.record(step(200.0, ticket=2), now=200.0)
    assert [t.master_ticket for t in store.trades(TID_S1)] == [2]


def test_steps_older_than_the_days_kept_are_deleted(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db", keep_days=7)
    store.record(step(0.0, ticket=1), now=0.0)
    store.record(step(8 * DAY, ticket=2), now=8 * DAY)
    assert [t.master_ticket for t in store.trades(TID_S1)] == [2]


def test_a_note_does_not_name_the_trade(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.note(TID_S1, 11, "BTCUSD.r", "buy", "breakeven", "reached at 85010", now=50.0)
    store.record(step(10.0), now=60.0)
    [trade] = store.trades(TID_S1)
    assert trade.symbol == "BTCUSD"
    assert [s.kind for s in trade.steps] == ["open", NOTE]


def test_where_a_trade_stands_is_read_from_its_steps() -> None:
    def trade(*steps: Step) -> Trade:
        return Trade(TID_S1, 11, "BTCUSD", "buy", steps)

    assert trade(step(1.0)).status == "open"
    assert trade(step(1.0), step(2.0, ActionKind.CLOSE)).status == "closed"
    assert trade(step(1.0, outcome=Outcome.SKIPPED)).status == "not copied"
    given_up = replace(step(2.0, NOTE, outcome=""), action=ORPHANED)
    assert trade(step(1.0), given_up).status == "orphaned"
    assert trade(step(1.0, outcome=Outcome.REFUSED, problem=True)).problems == 1


# --- wording ---------------------------------------------------------------------------------


def _done(kind: ActionKind, **kw: object) -> SlaveOutcome:
    base: dict[str, object] = {"terminal_id": TID_S1, "outcome": Outcome.DONE, "ts": 1.0}
    base.update(kw)
    return SlaveOutcome(**base)  # type: ignore[arg-type]


def test_a_copy_reads_as_it_is_on_the_slave() -> None:
    a = action(sl=85158.9, tp=86845.2)
    out = _done(ActionKind.OPEN, lots=1.37, price=86019.0, latency_ms=120.0, slippage_points=24.0)
    assert describe_step(ActionKind.OPEN, out, a) == (
        "opened",
        "1.37 lots at 86,019, stop 85,158.9, target 86,845.2 in 120 ms, "
        "24 points worse than the master",
    )
    closed = _done(ActionKind.CLOSE, lots=0.5, price=86048.0)
    assert describe_step(ActionKind.REDUCE, closed, None) == (
        "partly closed",
        "0.5 lots at 86,048",
    )
    moved = action(ActionKind.MODIFY_SLTP, sl=85024.8, tp=85657.8)
    assert describe_step(ActionKind.MODIFY_SLTP, _done(ActionKind.MODIFY_SLTP), moved) == (
        "stop/target moved",
        "stop 85,024.8, target 85,657.8",
    )


def test_a_declined_trade_says_why() -> None:
    out = SlaveOutcome(TID_S1, Outcome.SKIPPED, "copy_sltp is off for this slave", ts=1.0)
    moved = action(ActionKind.MODIFY_SLTP, sl=85024.8, tp=85657.8)
    assert describe_step(ActionKind.MODIFY_SLTP, out, moved) == (
        "stop/target change skipped",
        "copy_sltp is off for this slave (asked for stop 85,024.8, target 85,657.8)",
    )
    gone = _done(ActionKind.CLOSE, target_gone=True)
    assert describe_step(ActionKind.CLOSE, gone, None) == (
        "close",
        "not needed: the copy was already gone from the slave",
    )


# --- fed by the day book ---------------------------------------------------------------------


def _listening() -> tuple[DayBook, list[tuple[MasterEvent, SlaveOutcome, Action | None]]]:
    heard: list[tuple[MasterEvent, SlaveOutcome, Action | None]] = []
    book = DayBook()
    book.listener = lambda head, out, act: heard.append((head, out, act))
    return book, heard


def test_an_outcome_is_told_once_it_settles() -> None:
    book, heard = _listening()
    book.begin_tick(100.0, [pos()], [])
    a = action()
    book.add(a, Outcome.SENDING)
    book.on_sent(a, 7, sent_at=10.0)
    book.add(action(terminal_id=TID_S2), Outcome.SKIPPED, "this slave is paused")
    book.end_tick([], {})
    assert [(o.terminal_id, o.outcome) for _, o, _ in heard] == [(TID_S2, Outcome.SKIPPED)]

    book.on_result(result(7, price=86019.0), now=10.1, ts=10.1)
    head, out, act = heard[-1]
    assert (out.terminal_id, out.outcome, out.price) == (TID_S1, Outcome.DONE, 86019.0)
    assert act is a and head.master_ticket == 11
    kept = step_for(head, out, act)
    assert kept.master == "master 0.78 lots at 84,854"
    assert kept.lots == pytest.approx(3.11)


def test_the_engine_keeps_what_the_day_book_settles(tmp_path: Path) -> None:
    cfg = AppConfig()
    eng = Engine(cfg, history_path=tmp_path / "h.db")
    eng._day.begin_tick(time.time(), [pos()], [])
    eng._day.add(action(), Outcome.DRY_RUN)
    eng._day.add(action(ticket=12), Outcome.SKIPPED, "a guard is holding this slave back")
    eng._day.end_tick([], {})
    [trade] = eng.trade_history(TID_S1)
    assert trade.master_ticket == 11 and trade.status == "open"


# --- the page --------------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    cfg = make_cfg(tmp_path)
    cfg.set_role(TID_MASTER, Role.MASTER)
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    return cfg


def _trades() -> list[Trade]:
    opened = replace(
        step(1000.0, tid=TID_SLAVE, slave="1.37 lots at 86,019"), lots=1.37, action="opened"
    )
    refused = step(
        2000.0,
        ActionKind.MODIFY_SLTP,
        tid=TID_SLAVE,
        outcome=Outcome.REFUSED,
        problem=True,
        slave="invalid stops",
    )
    refused = replace(refused, action="stop/target change refused")
    other = step(500.0, tid=TID_SLAVE, ticket=12, slave="opened 2 lots")
    return [
        Trade(TID_SLAVE, 11, "BTCUSD", "buy", (opened, refused)),
        Trade(TID_SLAVE, 12, "BTCUSD", "buy", (other,)),
    ]


def test_a_card_says_each_step_in_order_once() -> None:
    trade = _trades()[0]
    assert card_title(trade) == " BTCUSD buy 1.37 lots · #11 "
    body = card_body(trade)
    text = plain(body)
    assert text.index("OPENED  1.37 lots") < text.index("STOP/TARGET CHANGE REFUSED  invalid stops")
    bold = [body.plain[sp.start : sp.end] for sp in body.spans if "bold" in str(sp.style)]
    assert bold == ["OPENED", "STOP/TARGET CHANGE REFUSED"], "the action stands out"
    assert text.count("BTCUSD") == 0, "the card's title names the trade; its lines do not"


async def test_h_on_a_slave_opens_its_history(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._engine.history[TID_SLAVE] = _trades()  # type: ignore[attr-defined]
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("1", "h")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        cards = list(screen.query(".hs-card").results(Static))
        assert len(cards) == 2
        assert "STOP/TARGET CHANGE REFUSED  invalid stops" in plain(cards[0].content)
        assert cards[0].has_class("-failed")

        await pilot.press("f")
        await pilot.pause()
        assert len(screen.query(".hs-card")) == 1, "failed only"
        await pilot.press("escape")
        assert not isinstance(app.screen, HistoryScreen)


async def test_h_on_the_master_says_history_is_per_slave(
    cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(cfg, tmp_path, monkeypatch)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("a", "h")
        await pilot.pause()
        assert not isinstance(app.screen, HistoryScreen)
