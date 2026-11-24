"""Direct trading through the engine: the virtual master driving the ordinary diff.

The engine is built exactly as the supervisor tests build it -- spawning stubbed, snapshots
seeded by hand -- and ``virtual_tick()`` is called directly instead of letting the tick
thread run, so every test is synchronous. The slave runs in dry-run, which exercises the
whole derivation path without an executor.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fake_workers import FakeWorker

from mt5copier.config import (
    VIRTUAL_MASTER_ID,
    AppConfig,
    AppMode,
    Role,
    TerminalConfig,
    virtual_master,
)
from mt5copier.engine import Engine
from mt5copier.protocol import (
    ActionKind,
    Candle,
    OrderType,
    Position,
    PositionType,
    Quote,
    RatesRequest,
    RatesResult,
    Snapshot,
    WarmCommand,
    WatchCommand,
    WorkerState,
)
from mt5copier.shared_db import SharedDb

SLAVE = "B" * 32
OTHER = "D" * 32

EURUSD_QUOTE = Quote(symbol="EURUSD", bid=1.08500, ask=1.08520)


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    path = tmp_path / "terminal64.exe"
    path.write_bytes(b"MZ")
    return path


@pytest.fixture
def cfg(exe: Path) -> AppConfig:
    return AppConfig(
        mode=AppMode.TRADING,
        copying_enabled=True,
        dry_run=True,
        terminals={
            SLAVE: TerminalConfig(
                terminal_id=SLAVE,
                label="Beta",
                exe_path=exe,
                role=Role.SLAVE,
                copy_pending=True,
            ),
        },
    )


@pytest.fixture
def engine(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    eng = Engine(
        cfg,
        state_path=tmp_path / "dryrun.db",
        mapping_path=tmp_path / "mapping.db",
        shared_db=SharedDb(tmp_path / "guard_state.db"),
        history_path=tmp_path / "history.db",
        virtual_path=tmp_path / "virtual_master.json",
    )
    monkeypatch.setattr(
        eng,
        "_spawn_worker",
        lambda tcfg: eng._workers.__setitem__(tcfg.terminal_id, FakeWorker(tcfg.terminal_id)),
    )
    eng.sync()
    return eng


def feed(engine: Engine, *quotes: Quote, seq: int = 1) -> None:
    """Seed the slave's snapshot: connected, flat, balanced, quoting the given symbols."""
    engine._snapshots[SLAVE] = Snapshot(
        terminal_id=SLAVE,
        state=WorkerState.CONNECTED,
        seq=seq,
        balance=10_000.0,
        open_positions=(),
        live_positions=(),
        pending_orders=(),
        quotes=tuple(quotes),
    )
    engine._received_at[SLAVE] = time.monotonic()


def kinds(engine: Engine) -> list[ActionKind]:
    return [r.action.kind for r in engine.recent_actions()]


# --- the virtual master standing where a real one stands --------------------------------------


def test_trading_mode_has_the_virtual_master(cfg: AppConfig) -> None:
    master = cfg.master
    assert master is not None
    assert master.terminal_id == VIRTUAL_MASTER_ID
    assert master is virtual_master()
    assert cfg.assigned() == list(cfg.slaves), "the virtual master is not a terminal to spawn"


def test_sync_spawns_only_the_slave(engine: Engine) -> None:
    assert set(engine._workers) == {SLAVE}


def test_set_role_refuses_a_master_in_trading_mode(cfg: AppConfig) -> None:
    with pytest.raises(ValueError, match="trades directly"):
        cfg.set_role(SLAVE, Role.MASTER)


# --- placing direct trades ---------------------------------------------------------------------


def test_market_order_needs_the_feed(engine: Engine) -> None:
    feed(engine)  # connected, but quoting nothing
    engine.virtual_tick()
    why = engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0)
    assert why is not None and "no price" in why


def test_market_order_fills_and_copies_to_the_slave(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline: the slave must watch the trade appear
    assert engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 1.09500) is None
    engine.virtual_tick()
    assert ActionKind.OPEN in kinds(engine)
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.master_ticket == 1
    assert copy.slave_symbol == "EURUSD"
    assert copy.synced_sl == 1.08000


def test_a_direct_trade_without_a_stop_is_refused_at_the_ticket(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    why = engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 0.0, 0.0)
    assert why is not None and "stop loss is required" in why


def test_pending_copies_as_a_place(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    assert (
        engine.place_direct_pending("EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 1.09000)
        is None
    )
    engine.virtual_tick()
    assert ActionKind.PLACE in kinds(engine)
    (order,) = engine._ledger.believed_orders(SLAVE)
    assert order.master_ticket == 1


def test_pending_fill_hands_the_copy_over_to_the_position(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_pending("EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 1.09000)
    engine.virtual_tick()
    assert engine._ledger.believed_orders(SLAVE)
    # The feed crosses the limit's price: the book fills it, same ticket.
    feed(engine, Quote(symbol="EURUSD", bid=1.07980, ask=1.08000), seq=2)
    engine.virtual_tick()
    assert ActionKind.ORDER_FILLED in kinds(engine)
    assert not engine._ledger.believed_orders(SLAVE)
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.master_ticket == 1


def test_stop_hit_on_the_book_closes_the_copy(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    assert engine._ledger.believed(SLAVE)
    feed(engine, Quote(symbol="EURUSD", bid=1.07990, ask=1.08010), seq=2)
    engine.virtual_tick()
    assert ActionKind.CLOSE in kinds(engine)
    assert not engine._ledger.believed(SLAVE)


def test_partial_close_mirrors_as_a_reduce(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    assert engine.partial_close_direct(1, 50.0) is None
    engine.virtual_tick()
    assert ActionKind.REDUCE in kinds(engine)


def test_modify_mirrors_as_a_modify_sltp(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    assert engine.modify_direct(1, 1.08200, 1.09800) is None
    engine.virtual_tick()
    assert ActionKind.MODIFY_SLTP in kinds(engine)
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.synced_sl == 1.08200 and copy.synced_tp == 1.09800


def test_an_armed_breakeven_fires_on_the_feed_and_mirrors_to_the_copy(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    assert engine.set_direct_breakeven_at(1, 1.08700) is None
    assert engine.direct_breakevens() == {1: 1.08700}
    engine.virtual_tick()  # still short of the trigger: nothing moves
    assert engine.direct_positions()[0].sl == 1.08000
    feed(engine, Quote(symbol="EURUSD", bid=1.08700, ask=1.08720), seq=2)
    engine.virtual_tick()
    assert engine.direct_positions()[0].sl == 1.08520  # the entry (filled at the ask)
    assert engine.direct_breakevens() == {}
    assert ActionKind.MODIFY_SLTP in kinds(engine)
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.synced_sl == 1.08520


def _give_the_copy_costs(engine: Engine) -> None:
    """A dry run's fill reports no costs; give the copy what a real deal would carry."""
    engine._ledger._store.update(
        SLAVE, 1, point=0.00001, digits=5, price_value=100_000.0, commission_per_lot=7.0
    )


def test_exit_at_entry_parks_the_target_past_the_copys_costs(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 1.09500)
    engine.virtual_tick()
    _give_the_copy_costs(engine)
    assert engine.direct_exit_at_entry(1) is None
    p = engine.direct_positions()[0]
    # The entry (filled at the ask) plus the copy's commission as a distance: 7.0 over
    # what a 1.0 move is worth on a lot. The stop has not moved.
    assert p.tp == 1.08527 and p.sl == 1.08000
    engine.virtual_tick()
    assert ActionKind.MODIFY_SLTP in kinds(engine)
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.synced_tp == 1.08527
    # The price comes back to the entry: the target closes the trade, and the copy with it.
    feed(engine, Quote(symbol="EURUSD", bid=1.08530, ask=1.08550), seq=2)
    engine.virtual_tick()
    assert not engine.direct_positions()
    assert ActionKind.CLOSE in kinds(engine)


def test_exit_at_entry_for_a_sell_sits_below_the_entry(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.SELL, 1.0, 1.09000, 0.0)
    engine.virtual_tick()
    _give_the_copy_costs(engine)
    assert engine.direct_exit_at_entry(1) is None
    p = engine.direct_positions()[0]
    assert p.tp == 1.08493  # the entry (the bid) minus the commission distance


def test_exit_at_entry_needs_a_copy_with_costs(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    why = engine.direct_exit_at_entry(1)  # the dry run's copy carries no point size
    assert why is not None and "costs" in why


def test_exit_at_entry_refuses_a_stop_already_past_the_entry(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    _give_the_copy_costs(engine)
    assert engine.modify_direct(1, 1.08520, 0.0) is None  # breakeven by hand
    why = engine.direct_exit_at_entry(1)
    assert why is not None and "cannot lose" in why


def test_exit_at_entry_refuses_a_trade_already_past_the_level(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    _give_the_copy_costs(engine)
    feed(engine, Quote(symbol="EURUSD", bid=1.09000, ask=1.09020), seq=2)
    engine.virtual_tick()
    why = engine.direct_exit_at_entry(1)
    assert why is not None and "close it instead" in why


def test_cancel_mirrors_as_a_cancel(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_pending("EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 0.0)
    engine.virtual_tick()
    assert engine.cancel_direct(1) is None
    engine.virtual_tick()
    assert ActionKind.CANCEL in kinds(engine)
    assert not engine._ledger.believed_orders(SLAVE)


def test_flatten_closes_everything(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.place_direct_pending("EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 0.0)
    assert engine.flatten_direct() == 2
    assert not engine.direct_positions() and not engine.direct_orders()


# --- the feed ----------------------------------------------------------------------------------


def test_the_feed_is_watched_for_the_books_symbols(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    watches = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WatchCommand)]
    assert watches and watches[-1].symbols == ("EURUSD",)


def test_the_ticket_preview_symbol_is_watched_too(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.watch_direct("xauusd")
    engine.virtual_tick()
    watches = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WatchCommand)]
    assert watches and watches[-1].symbols == ("XAUUSD",)


def test_the_ticket_symbol_warms_every_slave_once(engine: Engine) -> None:
    """Choosing a symbol on the ticket selects it on every connected MT5 slave right away,
    so its first tick has usually started before an order lands on it."""
    feed(engine, EURUSD_QUOTE)
    engine.watch_direct("xauusd")
    warms = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WarmCommand)]
    assert warms and warms[-1].symbols == ("XAUUSD",)
    engine.watch_direct("xauusd")
    warms = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WarmCommand)]
    assert len(warms) == 1, "the same symbol is not re-warmed"


def test_an_unchanged_watch_is_not_resent(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.watch_direct("EURUSD")
    engine.virtual_tick()
    engine.virtual_tick()
    watches = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WatchCommand)]
    assert len(watches) == 1


def test_direct_quote_reads_the_feed(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()
    quote = engine.direct_quote("EURUSD")
    assert quote is not None and quote.bid == 1.08500
    assert engine.direct_feed() == SLAVE


def test_no_connected_slave_means_no_feed(engine: Engine) -> None:
    engine.virtual_tick()
    assert engine.direct_feed() is None
    why = engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0)
    assert why is not None and "no price" in why


def test_the_first_connected_slave_keeps_the_feed(
    engine: Engine, cfg: AppConfig, exe: Path
) -> None:
    """Unpinned, the feed is first-come-keeps-it: a slave connecting later never takes the
    job over mid-session, even one that sorts ahead -- only losing the feed re-picks."""
    cfg.terminals[OTHER] = TerminalConfig(
        terminal_id=OTHER, label="Alpha", exe_path=exe, role=Role.SLAVE
    )
    engine.sync()
    feed(engine, EURUSD_QUOTE)  # Beta connects first and starts feeding
    engine.virtual_tick()
    assert engine.direct_feed() == SLAVE
    # Alpha connects later; it sorts before Beta, but Beta is already feeding.
    engine._snapshots[OTHER] = Snapshot(
        terminal_id=OTHER, state=WorkerState.CONNECTED, seq=1, open_positions=(), pending_orders=()
    )
    engine._received_at[OTHER] = time.monotonic()
    engine.virtual_tick()
    assert engine.direct_feed() == SLAVE, "the feed does not change hands while it works"
    # Beta drops: now Alpha takes over -- and keeps it when Beta returns.
    del engine._snapshots[SLAVE]
    del engine._received_at[SLAVE]
    engine.virtual_tick()
    assert engine.direct_feed() == OTHER
    feed(engine, EURUSD_QUOTE, seq=2)
    engine.virtual_tick()
    assert engine.direct_feed() == OTHER, "a returning slave does not grab the feed back"


def test_a_pinned_feed_wins_and_fails_over(engine: Engine, cfg: AppConfig, exe: Path) -> None:
    cfg.terminals[OTHER] = TerminalConfig(
        terminal_id=OTHER, label="Alpha", exe_path=exe, role=Role.SLAVE
    )
    engine.sync()
    feed(engine, EURUSD_QUOTE)
    engine._snapshots[OTHER] = Snapshot(
        terminal_id=OTHER, state=WorkerState.CONNECTED, seq=1, open_positions=(), pending_orders=()
    )
    engine._received_at[OTHER] = time.monotonic()
    cfg.feed_terminal_id = SLAVE
    engine.virtual_tick()
    assert engine.direct_feed() == SLAVE
    # The pin drops off: auto picks the other connected slave rather than going blind.
    del engine._snapshots[SLAVE]
    del engine._received_at[SLAVE]
    engine.virtual_tick()
    assert engine.direct_feed() == OTHER


# --- switching modes -----------------------------------------------------------------------------


def test_mode_switch_is_refused_with_open_copies(engine: Engine, cfg: AppConfig) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    why = engine.set_mode(AppMode.COPY)
    assert why is not None and "still open" in why
    assert cfg.mode is AppMode.TRADING


def test_mode_switch_works_when_flat(engine: Engine, cfg: AppConfig) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    engine.close_direct(1)
    engine.virtual_tick()
    assert engine.set_mode(AppMode.COPY) is None
    assert cfg.mode is AppMode.COPY
    assert cfg.master is None
    assert not engine.direct_mode()
    assert engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0) is not None


def test_switching_to_trading_demotes_the_real_master(tmp_path: Path, exe: Path) -> None:
    cfg = AppConfig(
        terminals={
            OTHER: TerminalConfig(terminal_id=OTHER, label="M", exe_path=exe, role=Role.MASTER),
            SLAVE: TerminalConfig(terminal_id=SLAVE, label="S", exe_path=exe, role=Role.SLAVE),
        }
    )
    eng = Engine(
        cfg,
        state_path=tmp_path / "dryrun.db",
        mapping_path=tmp_path / "mapping.db",
        shared_db=SharedDb(tmp_path / "guard_state.db"),
        history_path=tmp_path / "history.db",
        virtual_path=tmp_path / "virtual_master.json",
    )
    assert eng.set_mode(AppMode.TRADING) is None
    assert cfg.terminals[OTHER].role is Role.UNASSIGNED
    assert cfg.terminals[SLAVE].role is Role.SLAVE, "the slaves keep their roles"
    assert cfg.master is not None and cfg.master.terminal_id == VIRTUAL_MASTER_ID
    assert eng.direct_mode()


def test_the_book_survives_a_restart_through_the_engine(
    engine: Engine, cfg: AppConfig, tmp_path: Path
) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    eng2 = Engine(
        cfg,
        state_path=tmp_path / "dryrun.db",
        mapping_path=tmp_path / "mapping.db",
        shared_db=SharedDb(tmp_path / "guard_state.db"),
        history_path=tmp_path / "history2.db",
        virtual_path=tmp_path / "virtual_master.json",
    )
    assert [p.ticket for p in eng2.direct_positions()] == [1]


# --- the minichart's bars ---------------------------------------------------------------


def test_direct_rates_asks_the_feed_worker_once_per_key(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    assert engine.direct_rates("EURUSD", 5) is None, "nothing has landed yet"
    worker = engine._workers[SLAVE]
    asked = [c for c in worker.cmd_q.sent if isinstance(c, RatesRequest)]
    assert len(asked) == 1 and (asked[0].symbol, asked[0].timeframe) == ("EURUSD", 5)
    engine.direct_rates("EURUSD", 5)
    asked = [c for c in worker.cmd_q.sent if isinstance(c, RatesRequest)]
    assert len(asked) == 1, "an unchanged ask inside the refresh window is not resent"
    engine.direct_rates("EURUSD", 15)
    asked = [c for c in worker.cmd_q.sent if isinstance(c, RatesRequest)]
    assert len(asked) == 2, "a new timeframe asks at once"


def test_direct_rates_answers_the_feeds_bars(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    bars = RatesResult(
        terminal_id=SLAVE,
        symbol="EURUSD",
        timeframe=5,
        candles=(Candle(ts=1, open=1.08, high=1.09, low=1.07, close=1.085),),
    )
    engine._rates = bars  # what the drain thread would have filed
    assert engine.direct_rates("EURUSD", 5) is bars
    assert engine.direct_rates("EURUSD", 1) is None, "another timeframe waits for its own"
    assert engine.direct_rates("XAUUSD", 5) is None, "so does another symbol"


def test_direct_rates_needs_a_feed(engine: Engine) -> None:
    assert engine.direct_rates("EURUSD", 5) is None
    worker = engine._workers[SLAVE]
    assert not any(isinstance(c, RatesRequest) for c in worker.cmd_q.sent)


# --- what still stands behind a ticket on the slaves -------------------------------------------


def test_copy_status_reads_the_slaves_snapshots(engine: Engine) -> None:
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline
    engine.place_direct_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 0.0)
    engine.virtual_tick()
    (copy,) = engine._ledger.believed(SLAVE)

    # A dry-run copy's ticket is synthetic (the master's negated), so no broker can
    # vouch for it: unknown, not live and not closed.
    assert copy.slave_ticket == -1
    assert engine.direct_copy_status() == {
        1: [{"name": "Beta", "state": "unknown", "tid": SLAVE, "ticket": None}]
    }

    # As if it had filled live as #777. In the slave's snapshot: live.
    engine._ledger._store.update(SLAVE, copy.master_ticket, slave_ticket=777)

    def snap(*positions: Position, seq: int) -> None:
        engine._snapshots[SLAVE] = Snapshot(
            terminal_id=SLAVE,
            state=WorkerState.CONNECTED,
            seq=seq,
            balance=10_000.0,
            open_positions=positions,
            live_positions=(),
            pending_orders=(),
            quotes=(EURUSD_QUOTE,),
        )
        engine._received_at[SLAVE] = time.monotonic()

    held = Position(
        ticket=777,
        symbol="EURUSD",
        type=PositionType.BUY,
        volume=0.1,
        price_open=1.08520,
        sl=1.08000,
        tp=0.0,
    )
    snap(held, seq=2)
    assert engine.direct_copy_status() == {
        1: [{"name": "Beta", "state": "live", "tid": SLAVE, "ticket": 777}]
    }

    # Gone from a readable snapshot: closed on the slave on its own.
    snap(seq=3)
    assert engine.direct_copy_status() == {
        1: [{"name": "Beta", "state": "closed", "tid": SLAVE, "ticket": 777}]
    }

    # A stale snapshot proves nothing either way; the ticket is still worth naming.
    engine._received_at[SLAVE] = time.monotonic() - engine._cfg.heartbeat_timeout_s - 1.0
    assert engine.direct_copy_status() == {
        1: [{"name": "Beta", "state": "unknown", "tid": SLAVE, "ticket": 777}]
    }


# --- the feed broker's decoration never reaches the book ---------------------------------------


def test_a_symbol_typed_as_the_feed_lists_it_is_cleaned(engine: Engine, cfg: AppConfig) -> None:
    """The feed broker decorates (EURUSD.FX); the book and every copy start from EURUSD,
    or every other slave refuses the trade as a symbol it does not list."""
    cfg.terminals[SLAVE].symbol_suffix = ".FX"
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # baseline, and the feed is picked
    assert engine.place_direct_market("EURUSD.FX", PositionType.BUY, 1.0, 1.08000, 0.0) is None
    (position,) = engine.direct_positions()
    assert position.symbol == "EURUSD"
    engine.virtual_tick()
    opened = [r.action for r in engine.recent_actions() if r.action.kind is ActionKind.OPEN]
    assert opened and opened[-1].master_symbol == "EURUSD"
    (copy,) = engine._ledger.believed(SLAVE)
    assert copy.slave_symbol == "EURUSD.FX", "this slave is the feed, and decorates it back on"


def test_a_decorated_pending_and_its_watch_are_cleaned_too(engine: Engine, cfg: AppConfig) -> None:
    cfg.terminals[SLAVE].symbol_suffix = ".FX"
    feed(engine, EURUSD_QUOTE)
    engine.virtual_tick()  # the feed is picked, so its decoration is known
    assert (
        engine.place_direct_pending("EURUSD.FX", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 0.0)
        is None
    )
    (order,) = engine.direct_orders()
    assert order.symbol == "EURUSD"
    engine.watch_direct("XAUUSD.FX")
    engine.virtual_tick()
    watches = [c for c in engine._workers[SLAVE].cmd_q.sent if isinstance(c, WatchCommand)]
    assert watches and watches[-1].symbols == ("EURUSD", "XAUUSD")


def test_an_explicit_feed_mapping_is_reversed_at_the_ticket(engine: Engine, cfg: AppConfig) -> None:
    """The feed maps clean XAUUSD to its own GOLD; GOLD typed on the ticket is XAUUSD."""
    cfg.terminals[SLAVE].symbol_map = {"XAUUSD": "GOLD"}
    feed(engine, Quote(symbol="XAUUSD", bid=2400.0, ask=2400.5))
    engine.virtual_tick()
    quote = engine.direct_quote("GOLD")
    assert quote is not None and quote.bid == 2400.0
