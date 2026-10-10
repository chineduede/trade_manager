"""Booking profit at breakeven: when a copy's breakeven fires, its slave closes a set percent of
it, once. Built on the breakeven tests' live engine (a slave long 0.5 lots of EURUSD at
1.08500, its stop 50 pips away and its trigger at 0.2R)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_breakeven import TID_MASTER, TID_SLAVE, _slave_at, _tick, live, master_pos, slave

from mt5copier import reconcile
from mt5copier.config import BREAKEVEN_FIELDS, BreakevenMode
from mt5copier.engine import Engine
from mt5copier.protocol import ActionKind, OrderCommand, OrderResult, Snapshot, WorkerState
from mt5copier.settings_edit import apply_edits
from mt5copier.store import MappingStore

__all__ = ["live"]  # the fixture, imported for the tests below


@pytest.fixture
def booking(live: Engine) -> Engine:
    """The live engine, with its slave booking half of each copy at breakeven."""
    cfg = live.config.terminals[TID_SLAVE]
    live.config.replace_terminal(cfg.model_copy(update={"breakeven_book_percent": 50.0}))
    return live


def _by_op(cmds: list[OrderCommand]) -> dict[ActionKind, OrderCommand]:
    found = {c.op: c for c in cmds}
    assert len(found) == len(cmds), "one command per op"
    return found


def _answer(
    eng: Engine,
    cmd: OrderCommand,
    *,
    ok: bool = True,
    filled: float | None = None,
    unsplittable: bool = False,
    error: str | None = None,
) -> None:
    eng._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_SLAVE,
            op=cmd.op,
            master_ticket=11,
            ok=ok,
            slave_ticket=900,
            filled_volume=filled,
            unsplittable=unsplittable,
            error=error if error is not None else (None if ok else "OFF_QUOTES"),
        )
    )


def _copy(eng: Engine) -> reconcile.CopiedPosition:
    copy = eng._ledger.get_position(TID_SLAVE, 11)
    assert copy is not None
    return copy


def _fire(eng: Engine) -> dict[ActionKind, OrderCommand]:
    """Cross the trigger: the stop move and the booking go out on the same tick."""
    _slave_at(eng, 1.08610)
    return _by_op(_tick(eng))


def test_breakeven_books_the_slaves_percent_once(booking: Engine) -> None:
    sent = _fire(booking)
    assert set(sent) == {ActionKind.MODIFY_SLTP, ActionKind.BOOK}
    assert sent[ActionKind.BOOK].close_fraction == pytest.approx(0.5)

    _answer(booking, sent[ActionKind.BOOK], filled=0.25)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    copy = _copy(booking)
    assert (copy.booked, copy.book_fraction) == (True, 0.0)
    assert copy.volume == pytest.approx(0.25)
    assert _tick(booking) == [], "booked once"


def test_nothing_is_booked_with_the_percent_at_zero(live: Engine) -> None:
    assert set(_fire(live)) == {ActionKind.MODIFY_SLTP}


def test_a_hand_set_breakeven_books_too_with_breakeven_off(booking: Engine) -> None:
    cfg = booking.config.terminals[TID_SLAVE]
    booking.config.replace_terminal(cfg.model_copy(update={"breakeven_mode": BreakevenMode.OFF}))
    _slave_at(booking, 1.08550)
    _tick(booking)
    assert booking.set_breakeven_at(TID_SLAVE, 11, 1.08540) is None
    assert ActionKind.BOOK in _by_op(_tick(booking))


def test_a_breakeven_that_fires_again_books_nothing_more(booking: Engine) -> None:
    """A refused stop move clears the floor and fires again later: the booking must not."""
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.BOOK], filled=0.25)
    _answer(booking, sent[ActionKind.MODIFY_SLTP], ok=False, error="INVALID_STOPS")
    _slave_at(booking, 1.08700)
    assert set(_by_op(_tick(booking))) == {ActionKind.MODIFY_SLTP}


def test_a_refused_booking_is_tried_again_three_times_then_given_up(
    booking: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    book = sent[ActionKind.BOOK]
    _answer(booking, book, ok=False)
    assert _tick(booking) == [], "the next try waits"

    monkeypatch.setattr(reconcile, "BOOK_RETRY_S", 0.0)
    booking._ledger._book_wait.clear()
    tries = 1
    with caplog.at_level("WARNING"):
        while True:
            again = _by_op(_tick(booking)).get(ActionKind.BOOK)
            if again is None:
                break
            tries += 1
            assert again.close_fraction == pytest.approx(0.5)
            _answer(booking, again, ok=False)
    assert tries == 1 + reconcile.BOOK_RETRIES
    copy = _copy(booking)
    assert (copy.booked, copy.book_fraction) == (True, 0.0)
    assert any("given up" in r.getMessage() for r in caplog.records)


def test_a_booking_too_small_to_cut_is_said_once_and_the_stop_still_moves(
    booking: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    sent = _fire(booking)
    with caplog.at_level("WARNING"):
        _answer(
            booking,
            sent[ActionKind.BOOK],
            ok=False,
            unsplittable=True,
            error="cannot book 50% of 0.01 lots",
        )
        _answer(booking, sent[ActionKind.MODIFY_SLTP])
        assert _tick(booking) == []
    said = [r for r in caplog.records if "no profit booked" in r.getMessage()]
    assert len(said) == 1
    copy = _copy(booking)
    assert copy.booked and copy.synced_sl == pytest.approx(1.08510)


def test_a_booking_never_answered_is_not_sent_again(booking: Engine) -> None:
    """It may have closed the slice; closing a second one cannot be undone."""
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    booking._ledger.expire_inflight(timeout_s=-1.0)
    assert _copy(booking).booked
    assert _tick(booking) == []


def test_a_restart_while_a_booking_is_out_does_not_send_it_again(booking: Engine) -> None:
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    booking._ledger._last.clear()  # what a restart forgets; the store keeps the rest
    booking._ledger._inflight.clear()
    assert _tick(booking) == []


def test_an_answer_after_a_restart_still_marks_it_booked(booking: Engine) -> None:
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    booking._ledger._last.clear()  # what a restart between sending and answering leaves
    _answer(booking, sent[ActionKind.BOOK], filled=0.25)
    assert _copy(booking).booked
    assert _tick(booking) == []


def test_a_master_partial_is_not_copied_to_a_copy_whose_slave_books_its_own(
    booking: Engine,
) -> None:
    """A slave that books at its own breakeven manages the copy, so the master halving its
    trade closes nothing more of it: the two would otherwise close the same profit twice."""
    sent = _fire(booking)
    _answer(booking, sent[ActionKind.BOOK], filled=0.25)
    _answer(booking, sent[ActionKind.MODIFY_SLTP])
    copy = _copy(booking)
    assert copy.master_volume == 1.0
    assert copy.volume == pytest.approx(0.25)

    booking._reconcile(
        Snapshot(
            terminal_id=TID_MASTER,
            state=WorkerState.CONNECTED,
            open_positions=(replace(master_pos(), volume=0.5),),
            pending_orders=(),
        )
    )
    q = booking._workers[TID_SLAVE].cmd_q
    assert [c for c in q.sent if isinstance(c, OrderCommand)] == []  # type: ignore[attr-defined]


# --- belief and settings --------------------------------------------------------------------


def test_the_store_keeps_the_booking(tmp_path: Path) -> None:
    with MappingStore(tmp_path / "m.db") as store:
        store.remember_position(
            terminal_id=TID_SLAVE,
            master_ticket=11,
            slave_symbol="EURUSD",
            direction=reconcile.PositionType.BUY,
            master_volume=1.0,
            slave_ticket=900,
        )
        store.update(TID_SLAVE, 11, book_fraction=0.5, book_tries=2)
    with MappingStore(tmp_path / "m.db") as store:
        copy = store.get_position(TID_SLAVE, 11)
        assert copy is not None
        assert (copy.book_fraction, copy.book_tries, copy.booked) == (0.5, 2, False)
        store.update(TID_SLAVE, 11, book_fraction=0.0, booked=True)
    with MappingStore(tmp_path / "m.db") as store:
        assert store.get_position(TID_SLAVE, 11).booked  # type: ignore[union-attr]


def test_the_percent_is_under_100_and_editing_it_keeps_the_copies() -> None:
    assert "breakeven_book_percent" in BREAKEVEN_FIELDS
    with pytest.raises(ValidationError):
        slave(breakeven_book_percent=100.0)
    outcome = apply_edits(slave(), {"breakeven_book_percent": "30"})
    assert outcome.ok and outcome.config is not None
    assert outcome.config.breakeven_book_percent == 30.0
    assert not apply_edits(slave(), {"breakeven_book_percent": "-5"}).ok
