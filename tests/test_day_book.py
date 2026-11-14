"""The day book: master trades grouped with each slave's outcome. Display only."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mt5copier.config import AppConfig, Role, TerminalConfig
from mt5copier.day_book import (
    DayBook,
    MasterEvent,
    Outcome,
    SlaveOutcome,
    describe_master,
    describe_master_full,
    describe_outcome,
)
from mt5copier.engine import Engine
from mt5copier.protocol import (
    ActionKind,
    OrderCommand,
    OrderResult,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import Action, SkipReason, SlaveLink

TID_MASTER = "A" * 32
TID_S1 = "B" * 32
TID_S2 = "C" * 32


def pos(ticket: int = 11, volume: float = 0.78, ptype: PositionType = PositionType.BUY) -> Position:
    return Position(
        ticket=ticket,
        symbol="BTCUSD",
        type=ptype,
        volume=volume,
        price_open=84854.0,
        sl=84532.7,
        tp=85165.81,
    )


def action(
    kind: ActionKind = ActionKind.OPEN,
    terminal_id: str = TID_S1,
    ticket: int = 11,
    **kw: object,
) -> Action:
    base: dict[str, object] = {
        "kind": kind,
        "terminal_id": terminal_id,
        "master_ticket": ticket,
        "master_symbol": "BTCUSD",
        "slave_symbol": "BTCUSD.r",
        "type": PositionType.BUY,
        "master_volume": 0.78,
        "entry": 84854.0,
        "sl": 84532.7,
        "tp": 85165.81,
    }
    base.update(kw)
    return Action(**base)  # type: ignore[arg-type]


def result(command_id: int, ok: bool = True, **kw: object) -> OrderResult:
    base: dict[str, object] = {
        "command_id": command_id,
        "terminal_id": TID_S1,
        "op": ActionKind.OPEN,
        "master_ticket": 11,
        "ok": ok,
        "filled_volume": 3.11,
        "attempts": 1,
    }
    base.update(kw)
    return OrderResult(**base)  # type: ignore[arg-type]


# --- the book on its own --------------------------------------------------------------------


def test_one_tick_groups_every_slave_under_the_masters_trade() -> None:
    book = DayBook()
    book.begin_tick(100.0, [pos()], [])
    book.add(action(terminal_id=TID_S1), Outcome.SENDING)
    book.add(action(terminal_id=TID_S2), Outcome.SKIPPED, "this slave is paused")
    book.end_tick([], {})

    [event] = book.events()
    assert event.kind is ActionKind.OPEN
    assert describe_master(event) == "opened BTCUSD buy 0.78 at 84854"
    assert [(o.terminal_id, o.outcome) for o in event.outcomes] == [
        (TID_S1, Outcome.SENDING),
        (TID_S2, Outcome.SKIPPED),
    ]


def test_an_answer_settles_the_outcome_with_lots_and_latency() -> None:
    book = DayBook()
    book.begin_tick(100.0, [pos()], [])
    a = action()
    book.add(a, Outcome.SENDING)
    book.on_sent(a, 7, sent_at=10.0)
    book.end_tick([], {})
    book.on_result(result(7), now=10.36, ts=10.36)

    [out] = book.events()[0].outcomes
    assert out.outcome is Outcome.DONE
    assert out.lots == pytest.approx(3.11)
    assert out.latency_ms == pytest.approx(360.0)
    assert describe_outcome(ActionKind.OPEN, out) == "opened 3.11 lots in 360 ms"


def test_a_refusal_and_a_link_failure_are_told_apart() -> None:
    book = DayBook()
    book.begin_tick(100.0, [pos()], [])
    one, two = action(terminal_id=TID_S1), action(terminal_id=TID_S2)
    for n, a in enumerate((one, two), start=1):
        book.add(a, Outcome.SENDING)
        book.on_sent(a, n, sent_at=0.0)
    book.end_tick([], {})
    book.on_result(result(1, ok=False, error="account is not a demo"), now=0.1, ts=0.1)
    book.on_result(result(2, ok=False, retryable=True, error="link down"), now=0.1, ts=0.1)

    outs = {o.terminal_id: o for o in book.events()[0].outcomes}
    assert outs[TID_S1].outcome is Outcome.REFUSED
    assert describe_outcome(ActionKind.OPEN, outs[TID_S1]) == "refused: account is not a demo"
    assert outs[TID_S2].outcome is Outcome.FAILED
    assert book.events()[0].problems == 2


def test_an_unanswered_command_ends_as_no_answer() -> None:
    book = DayBook()
    book.begin_tick(100.0, [pos()], [])
    a = action()
    book.add(a, Outcome.SENDING)
    book.on_sent(a, 3, sent_at=0.0)
    book.end_tick([], {})
    book.on_expired(TID_S1, 11, ts=1.0)
    book.on_result(result(3), now=1.0, ts=1.0)  # the engine would refuse it; the book ignores it

    [out] = book.events()[0].outcomes
    assert out.outcome is Outcome.NO_ANSWER


def test_a_held_slave_is_listed_as_not_copied_on_an_open() -> None:
    book = DayBook()
    book.begin_tick(100.0, [pos()], [])
    book.add(action(terminal_id=TID_S1), Outcome.DRY_RUN)
    links = [
        SlaveLink(cfg=TerminalConfig(terminal_id=TID_S1, role=Role.SLAVE), connected=True),
        SlaveLink(cfg=TerminalConfig(terminal_id=TID_S2, role=Role.SLAVE), connected=False),
    ]
    book.end_tick(links, {TID_S2: "1 untracked position(s) of ours: #9"})

    outs = {o.terminal_id: o for o in book.events()[0].outcomes}
    assert outs[TID_S2].outcome is Outcome.NOT_COPIED
    assert "untracked" in (outs[TID_S2].reason or "")


def test_a_held_slave_is_not_listed_on_a_close() -> None:
    """It has nothing to close on that ticket that it could be blamed for missing here: its
    copies are dealt with when it comes back."""
    book = DayBook()
    book.begin_tick(100.0, [], [])
    book.add(action(kind=ActionKind.CLOSE), Outcome.DRY_RUN)
    links = [SlaveLink(cfg=TerminalConfig(terminal_id=TID_S2, role=Role.SLAVE), connected=False)]
    book.end_tick(links, {})
    assert [o.terminal_id for o in book.events()[0].outcomes] == [TID_S1]


def test_a_close_reads_the_master_from_the_previous_tick() -> None:
    """A closed position is gone from the master's list; the event still names the master's
    own symbol and direction, not the slave's."""
    book = DayBook()
    book.begin_tick(1.0, [pos(volume=0.69)], [])
    book.end_tick([], {})
    book.begin_tick(2.0, [], [])
    book.add(action(kind=ActionKind.CLOSE, type=PositionType.SELL), Outcome.DRY_RUN)
    book.end_tick([], {})

    assert describe_master(book.events()[0]) == "closed BTCUSD buy 0.69"


def test_a_reversed_slave_does_not_flip_the_masters_side() -> None:
    """Pieced together from the action, a reversed copy is turned back into the master's
    trade: direction flipped back, stop and target swapped back."""
    book = DayBook()
    book.begin_tick(1.0, [], [])
    book.add(
        action(kind=ActionKind.CLOSE, type=PositionType.SELL, sl=2.0, tp=1.0),
        Outcome.DRY_RUN,
        invert=True,
    )
    book.end_tick([], {})
    [event] = book.events()
    assert event.label == "buy"
    assert (event.sl, event.tp) == (1.0, 2.0)


def test_a_partial_close_says_how_much_was_left() -> None:
    book = DayBook()
    book.begin_tick(1.0, [pos(volume=1.0)], [])
    book.end_tick([], {})
    book.begin_tick(2.0, [pos(volume=0.4)], [])
    book.add(action(kind=ActionKind.REDUCE, close_fraction=0.6), Outcome.DRY_RUN)
    book.end_tick([], {})
    assert describe_master(book.events()[0]) == "closed part of BTCUSD buy: 1 to 0.4"


def test_a_resting_order_is_labelled_with_its_order_type() -> None:
    order = PendingOrder(
        ticket=20, symbol="EURUSD", type=OrderType.BUY_LIMIT, volume=0.5, price_open=1.08
    )
    book = DayBook()
    book.begin_tick(1.0, [], [order])
    book.add(
        action(kind=ActionKind.PLACE, ticket=20, order_type=OrderType.BUY_LIMIT), Outcome.DRY_RUN
    )
    book.end_tick([], {})
    assert describe_master(book.events()[0]) == "placed EURUSD buy limit 0.5 at 1.08"


def test_an_order_fill_is_shown_as_following_not_skipped() -> None:
    book = DayBook()
    book.begin_tick(1.0, [], [])
    book.add(
        action(
            kind=ActionKind.ORDER_FILLED,
            order_type=OrderType.BUY_LIMIT,
            skip_reason=SkipReason.NOTHING_TO_SEND,
        ),
        Outcome.SKIPPED,
        SkipReason.NOTHING_TO_SEND.value,
    )
    book.end_tick([], {})
    [out] = book.events()[0].outcomes
    assert out.outcome is Outcome.FOLLOWS
    assert not out.outcome.problem


def test_a_preexisting_trade_is_a_notice_not_a_new_open() -> None:
    book = DayBook()
    book.begin_tick(1.0, [pos()], [])
    book.add(
        action(skip_reason=SkipReason.PREEXISTING),
        Outcome.SKIPPED,
        SkipReason.PREEXISTING.value,
    )
    book.end_tick([], {})
    [event] = book.events()
    assert event.preexisting
    assert describe_master(event).startswith("already had BTCUSD buy 0.78 open")


def test_events_are_copies_and_the_limit_keeps_the_newest() -> None:
    book = DayBook()
    for n in range(5):
        book.begin_tick(float(n), [pos(ticket=n)], [])
        book.add(action(ticket=n), Outcome.DRY_RUN)
        book.end_tick([], {})
    assert [e.master_ticket for e in book.events(limit=2)] == [3, 4]
    assert book.events(limit=0) == []


def test_full_description_adds_stop_and_target() -> None:
    event = MasterEvent(
        id=1,
        ts=0.0,
        kind=ActionKind.OPEN,
        preexisting=False,
        master_ticket=1,
        symbol="BTCUSD",
        label="buy",
        volume=0.69,
        volume_before=None,
        entry=84767.3,
        sl=84417.6,
        tp=85133.5,
    )
    assert describe_master_full(event) == (
        "opened BTCUSD buy 0.69 at 84767.3, stop 84417.6, target 85133.5"
    )


def test_outcome_wording() -> None:
    gone = SlaveOutcome(TID_S1, Outcome.DONE, target_gone=True, latency_ms=12)
    assert describe_outcome(ActionKind.CLOSE, gone) == "already gone from the slave in 12 ms"
    retried = SlaveOutcome(TID_S1, Outcome.DONE, lots=2.74, latency_ms=2443, attempts=3)
    assert describe_outcome(ActionKind.CLOSE, retried) == "closed 2.74 lots in 2,443 ms (3 tries)"
    assert describe_outcome(ActionKind.OPEN, SlaveOutcome(TID_S1, Outcome.SENDING)) == "sending..."


def test_a_rescued_pending_says_how_it_was_rescued() -> None:
    """A copy that is not resting the way the master's is must never look like one that is."""
    flipped = SlaveOutcome(
        TID_S1,
        Outcome.DONE,
        lots=0.5,
        latency_ms=12,
        fallback="as a buy stop: the broker refused the master's type",
    )
    assert describe_outcome(ActionKind.PLACE, flipped) == (
        "placed 0.5 lots as a buy stop: the broker refused the master's type in 12 ms"
    )
    market = SlaveOutcome(
        TID_S1,
        Outcome.DONE,
        lots=0.5,
        fallback="at market: the broker refused it resting",
    )
    assert describe_outcome(ActionKind.PLACE, market) == (
        "placed 0.5 lots at market: the broker refused it resting"
    )


# --- through the engine ---------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    exe = tmp_path / "terminal64.exe"
    exe.write_bytes(b"MZ")
    cfg = AppConfig(
        copying_enabled=True,
        dry_run=False,
        terminals={
            TID_MASTER: TerminalConfig(
                terminal_id=TID_MASTER, label="Master", exe_path=exe, role=Role.MASTER
            ),
            TID_S1: TerminalConfig(terminal_id=TID_S1, label="One", exe_path=exe, role=Role.SLAVE),
            TID_S2: TerminalConfig(
                terminal_id=TID_S2, label="Two", exe_path=exe, role=Role.SLAVE, paused=True
            ),
        },
    )
    return Engine(cfg, state_path=tmp_path / "dry.db", mapping_path=tmp_path / "map.db")


class _Queue:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def put_nowait(self, item: object) -> None:
        self.sent.append(item)

    def close(self) -> None:
        pass


class _Worker:
    def __init__(self) -> None:
        self.cmd_q = _Queue()

        class _P:
            def is_alive(self) -> bool:
                return True

        self.process = _P()


def master_snap(*positions: Position) -> Snapshot:
    return Snapshot(
        terminal_id=TID_MASTER,
        state=WorkerState.CONNECTED,
        open_positions=positions,
        pending_orders=(),
        balance=10_000.0,
    )


def wire(eng: Engine, tid: str) -> _Queue:
    eng._snapshots[tid] = Snapshot(
        terminal_id=tid,
        state=WorkerState.CONNECTED,
        open_positions=(),
        pending_orders=(),
        balance=10_000.0,
    )
    eng._received_at[tid] = time.monotonic()
    worker = _Worker()
    eng._workers[tid] = worker  # type: ignore[assignment]
    return worker.cmd_q


def test_the_engine_files_a_live_open_and_its_answer(engine: Engine) -> None:
    q1 = wire(engine, TID_S1)
    wire(engine, TID_S2)
    engine._reconcile(master_snap())
    engine._reconcile(master_snap(pos(ticket=11, ptype=PositionType.BUY)))

    [event] = engine.day_book()
    outs = {o.terminal_id: o for o in event.outcomes}
    assert outs[TID_S1].outcome is Outcome.SENDING
    assert outs[TID_S2].outcome is Outcome.SKIPPED, "paused"

    [cmd] = q1.sent
    assert isinstance(cmd, OrderCommand)
    engine._on_result(
        OrderResult(
            command_id=cmd.command_id,
            terminal_id=TID_S1,
            op=cmd.op,
            master_ticket=11,
            ok=True,
            slave_ticket=5001,
            filled_volume=3.11,
        )
    )
    done = {o.terminal_id: o for o in engine.day_book()[0].outcomes}[TID_S1]
    assert done.outcome is Outcome.DONE
    assert done.lots == pytest.approx(3.11)
    assert done.latency_ms is not None


def test_an_order_that_cannot_be_sent_is_settled_at_once(engine: Engine) -> None:
    wire(engine, TID_S1)
    engine._workers.pop(TID_S1)  # no worker: the engine abandons the command on the spot
    engine._reconcile(master_snap())
    engine._reconcile(master_snap(pos(ticket=11)))

    out = {o.terminal_id: o for o in engine.day_book()[0].outcomes}[TID_S1]
    assert out.outcome is Outcome.FAILED
    assert "no worker" in (out.reason or "")


def test_a_dry_run_files_every_slave_as_not_sent(engine: Engine) -> None:
    engine.config.dry_run = True
    wire(engine, TID_S1)
    engine._reconcile(master_snap())
    engine._reconcile(master_snap(pos(ticket=11)))
    out = {o.terminal_id: o for o in engine.day_book()[0].outcomes}[TID_S1]
    assert out.outcome is Outcome.DRY_RUN


def test_clearing_detection_state_empties_the_book(engine: Engine) -> None:
    engine.config.dry_run = True
    wire(engine, TID_S1)
    engine._reconcile(master_snap())
    engine._reconcile(master_snap(pos(ticket=11)))
    engine.clear_detection_state()
    assert engine.day_book() == []


def test_a_stop_move_says_what_was_set_and_on_which_trade() -> None:
    book = DayBook()
    book.begin_tick(1.0, [pos(ptype=PositionType.SELL)], [])
    book.add(action(kind=ActionKind.MODIFY_SLTP), Outcome.DRY_RUN)
    book.end_tick([], {})
    assert describe_master(book.events()[0]) == (
        "set stop 84532.7, target 85165.81 on BTCUSD sell 0.78"
    )


def test_the_masters_suffix_is_taken_off_before_any_slave_sees_the_name(engine: Engine) -> None:
    """The slave's filter, decoration and the day book all work on the plain name."""
    from dataclasses import replace

    engine.config.terminals[TID_MASTER].symbol_suffix = "p"
    engine.config.terminals[TID_S1].symbol_suffix = ".r"
    engine.config.terminals[TID_S1].symbol_whitelist = ["BTCUSD"]
    q1 = wire(engine, TID_S1)
    engine._reconcile(master_snap())
    engine._reconcile(master_snap(replace(pos(ticket=11), symbol="BTCUSDp")))

    [cmd] = q1.sent
    assert isinstance(cmd, OrderCommand)
    assert cmd.master_symbol == "BTCUSD", "what the slave resolves, adding its own .r"
    [event] = engine.day_book()
    assert event.symbol == "BTCUSD"
    out = {o.terminal_id: o for o in event.outcomes}[TID_S1]
    assert out.outcome is Outcome.SENDING, "the whitelist names the plain symbol"
