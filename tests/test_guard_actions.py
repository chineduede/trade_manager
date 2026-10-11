"""What the guards did, made visible: on the trade, in its history, and on the guards page.

A guard that shrank a trade used to say so only in the worker's own log file, which no page
shows -- the trade simply came out smaller than the slave's settings asked for.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_day_book import TID_S1, action, pos, result
from test_tui_guards import _open
from test_tui_guards import app as app  # noqa: F401  (fixture)
from test_tui_guards import cfg as cfg  # noqa: F401  (fixture)
from textual.widgets import Static
from tui_support import TID_SLAVE, plain

from mt5copier.config import AppConfig
from mt5copier.day_book import DayBook, Outcome, SlaveOutcome, describe_guard
from mt5copier.engine import Engine
from mt5copier.guards import AccountGuards, GuardRule, GuardsDoc
from mt5copier.history import GuardAction, HistoryStore
from mt5copier.protocol import GuardNote, Snapshot, WorkerState
from mt5copier.reconcile import SkipReason
from mt5copier.tui import CopierApp
from mt5copier.tui.guards import actions_list, last_cell
from mt5copier.tui.history import card_body

SHRUNK = GuardNote(
    "Margin per trade",
    "shrunk",
    "from 3.5 to 1.2 lots: 6,130.40 margin needed, 2,101.97 allowed",
    "m1",
)


def _snap(**kw: object) -> Snapshot:
    base: dict[str, object] = {
        "terminal_id": TID_S1,
        "state": WorkerState.CONNECTED,
        "login": 7,
        "server": "Prop",
        "balance": 10_000.0,
        "equity": 10_000.0,
        "open_positions": (),
        "pending_orders": (),
    }
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


# --- the day book and the trade's history ----------------------------------------------------


def test_the_workers_note_rides_on_the_outcome() -> None:
    book = DayBook()
    heard: list[SlaveOutcome] = []
    book.listener = lambda head, out, act: heard.append(out)
    book.begin_tick(100.0, [pos()], [])
    a = action()
    book.add(a, Outcome.SENDING)
    book.on_sent(a, 7, sent_at=10.0)
    book.on_result(result(7, filled_volume=1.2, guard=(SHRUNK,)), now=10.1, ts=10.1)
    [out] = heard
    assert out.guard == (SHRUNK,)
    assert describe_guard(out) == SHRUNK.text()


def test_a_decline_its_reason_already_says_is_not_said_twice() -> None:
    declined = GuardNote("Margin per trade", "declined", "2 lots: 2,000.00 margin needed", "m1")
    out = SlaveOutcome(TID_S1, Outcome.REFUSED, reason=declined.text(), guard=(declined,))
    assert describe_guard(out) == ""


def test_the_engine_keeps_it_with_the_trade_and_for_the_account(tmp_path: Path) -> None:
    eng = Engine(AppConfig(), history_path=tmp_path / "h.db")
    eng._snapshots[TID_S1] = _snap()
    eng._day.begin_tick(time.time(), [pos()], [])
    a = action()
    eng._day.add(a, Outcome.SENDING)
    eng._day.on_sent(a, 7, sent_at=time.monotonic())
    eng._day.on_result(
        result(7, filled_volume=1.2, guard=(SHRUNK,)), now=time.monotonic(), ts=time.time()
    )

    [trade] = eng.trade_history(TID_S1)
    [opened] = trade.steps
    assert opened.guard == SHRUNK.text()
    assert "GUARD  Margin per trade shrunk it: from 3.5 to 1.2 lots" in card_body(trade).plain

    [kept] = eng.guard_actions(TID_S1)
    assert (kept.account, kept.rule_id, kept.action, kept.master_ticket) == (
        "7@Prop",
        "m1",
        "shrunk",
        11,
    )


def _held_engine(tmp_path: Path) -> Engine:
    doc = GuardsDoc(
        accounts={
            "7@Prop": AccountGuards(
                initial_balance=10_000.0,
                rules=(
                    GuardRule(id="dd", type="max_drawdown", params={"limit": 500, "unit": "money"}),
                ),
            )
        }
    )
    cfg = AppConfig()
    from mt5copier.config import Role, TerminalConfig

    cfg.terminals[TID_S1] = TerminalConfig(terminal_id=TID_S1, label="S1", role=Role.SLAVE)
    eng = Engine(
        cfg,
        guards=doc,
        guard_state_path=tmp_path / "state.json",
        history_path=tmp_path / "h.db",
    )
    eng._snapshots[TID_S1] = _snap()
    return eng


def test_a_hold_is_kept_once_when_it_starts(tmp_path: Path) -> None:
    eng = _held_engine(tmp_path)
    eng._judge(_snap(equity=9_700.0))
    assert eng.guard_actions(TID_S1) == []
    eng._judge(_snap(equity=9_550.0))
    eng._judge(_snap(equity=9_540.0))  # still held: not news
    [held] = eng.guard_actions(TID_S1)
    assert (held.rule, held.action, held.master_ticket) == (
        "Max drawdown",
        "stopped new trades",
        None,
    )


def test_a_trade_the_hold_declines_names_the_rule(tmp_path: Path) -> None:
    eng = _held_engine(tmp_path)
    eng._judge(_snap(equity=9_550.0))
    [note] = eng._gate_notes(action(skip_reason=SkipReason.GUARD_HOLDS))
    assert (note.rule, note.action, note.rule_id) == ("Max drawdown", "declined", "dd")


# --- the store -------------------------------------------------------------------------------


def test_guard_actions_are_read_back_newest_first_and_pruned(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db", keep_days=7)
    now = time.time()
    old = GuardAction(now - 10 * 86400, "7@Prop", TID_S1, "m1", "Margin per trade", "shrunk", "x")
    store.record_guard(old)
    store.record_guard(replace(old, ts=now - 60, action="declined"))
    store.record_guard(replace(old, ts=now - 30))
    store.record_guard(replace(old, ts=now, account="8@Other"))
    assert [a.ts for a in store.guard_actions("7@Prop")] == [now - 30, now - 60, now - 10 * 86400]
    store.prune(now=now)
    assert [a.action for a in store.guard_actions("7@Prop")] == ["shrunk", "declined"]


# --- the guards page -------------------------------------------------------------------------


def test_the_list_says_what_each_rule_did() -> None:
    now = time.time()
    shrunk = GuardAction(
        now, "7@Prop", TID_S1, "m1", SHRUNK.rule, "shrunk", SHRUNK.detail, 4021, "EURUSD", "buy"
    )
    text = actions_list([shrunk], now).plain
    assert "Margin per trade  #4021 EURUSD buy  SHRUNK" in text
    assert "6,130.40 margin needed, 2,101.97 allowed" in text
    cell = last_cell(shrunk, now).plain
    assert "#4021 shrunk" in cell and "from 3.5 to 1.2 lots" in cell
    assert "Nothing yet" in actions_list([], now).plain


@pytest.mark.asyncio
async def test_the_page_lists_them(app: CopierApp) -> None:  # noqa: F811
    now = time.time()
    app._engine.guard_log[TID_SLAVE] = [  # type: ignore[attr-defined]
        GuardAction(now, "222@", TID_SLAVE, "m1", SHRUNK.rule, "shrunk", SHRUNK.detail, 4021)
    ]
    async with app.run_test(size=(140, 50)) as pilot:
        page = await _open(app, pilot)
        said = plain(page.query_one("#gd-actions", Static).content)
        assert "Margin per trade" in said and "SHRUNK" in said
        assert "GUARD ACTIONS ON THIS ACCOUNT" in plain(
            page.query_one("#gd-actions-head", Static).content
        )
