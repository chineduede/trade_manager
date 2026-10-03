"""The detail page's view-model: what each position row says and how the gauges read."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mt5copier.config import BreakevenMode, TerminalConfig
from mt5copier.protocol import (
    COPIER_MAGIC,
    Position,
    PositionLive,
    PositionType,
    Snapshot,
    WorkerState,
)
from mt5copier.reconcile import OpenCopy
from mt5copier.terminal_detail import (
    bar,
    breakeven_rule,
    exposure,
    format_duration,
    format_ping,
    format_price,
    held_by_counts,
    margin_gauge,
    position_rows,
    price_digits,
)

TID = "B" * 32


def pos(
    ticket: int = 1,
    symbol: str = "EURUSD",
    ptype: PositionType = PositionType.BUY,
    volume: float = 1.0,
    price_open: float = 1.08500,
    sl: float = 1.08000,
    tp: float = 1.09500,
    magic: int = 0,
) -> Position:
    return Position(
        ticket=ticket,
        symbol=symbol,
        type=ptype,
        volume=volume,
        price_open=price_open,
        sl=sl,
        tp=tp,
        magic=magic,
    )


def snap(
    positions: tuple[Position, ...] | None, live: tuple[PositionLive, ...] = (), **kw: object
) -> Snapshot:
    return Snapshot(
        terminal_id=TID,
        state=WorkerState.CONNECTED,
        open_positions=positions,
        live_positions=None if positions is None else live,
        **kw,  # type: ignore[arg-type]
    )


def copy(master: int, slave: int | None, resting: bool = False) -> OpenCopy:
    return OpenCopy(
        master_ticket=master, slave_ticket=slave, symbol="EURUSD", volume=0.5, resting=resting
    )


# --- position rows --------------------------------------------------------------------------------


def test_unreadable_positions_are_not_an_empty_table() -> None:
    assert position_rows(snap(None), is_master=False) is None
    assert position_rows(snap(()), is_master=False) == []


def test_live_values_sit_beside_their_position() -> None:
    rows = position_rows(
        snap(
            (pos(ticket=7),),
            (PositionLive(ticket=7, price_current=1.08620, profit=120.0, swap=-1.5),),
        ),
        is_master=False,
    )
    assert rows is not None
    (r,) = rows
    assert r.price_current == pytest.approx(1.08620)
    assert r.profit == 120.0
    assert r.swap == -1.5
    assert r.sl_room == pytest.approx(0.00620), "a buy is 62 pips above its stop"
    assert r.tp_room == pytest.approx(0.00880)


def test_room_to_the_stop_is_measured_the_other_way_for_a_sell() -> None:
    rows = position_rows(
        snap(
            (pos(ticket=7, ptype=PositionType.SELL, sl=1.09000, tp=1.08000),),
            (PositionLive(ticket=7, price_current=1.08600),),
        ),
        is_master=False,
    )
    assert rows is not None
    assert rows[0].sl_room == pytest.approx(0.00400)
    assert rows[0].tp_room == pytest.approx(0.00600)
    assert rows[0].side == "SELL"


def test_no_stop_and_no_price_mean_no_room_figure() -> None:
    rows = position_rows(snap((pos(ticket=7, sl=0.0),)), is_master=False)
    assert rows is not None
    assert rows[0].sl_room is None
    assert rows[0].tp_room is None, "no current price yet"
    assert rows[0].profit is None


def test_a_slave_names_the_master_position_each_copy_follows() -> None:
    master = (pos(ticket=100, volume=2.0),)
    rows = position_rows(
        snap((pos(ticket=5001, magic=COPIER_MAGIC),)),
        is_master=False,
        copies=[copy(100, 5001)],
        master_positions=master,
    )
    assert rows is not None
    assert rows[0].link == "copy of #100 (master 2 lots)"
    assert rows[0].link_tier == "good"


@pytest.mark.parametrize(
    ("state", "text", "tier"),
    [
        ({}, "0.3R", "muted"),
        ({"breakeven_at": 1.087}, "at 1.087", "warning"),
        ({"breakeven_sl": 1.0851}, "✓ 1.0851", "good"),
        ({"breakeven_exempt": True}, "exempt", "muted"),
    ],
    ids=["slave-rule", "hand-set-price", "triggered", "exempt"],
)
def test_a_copy_shows_its_breakeven_state(state: dict[str, object], text: str, tier: str) -> None:
    c = replace(copy(100, 5001), **state)  # type: ignore[arg-type]
    rows = position_rows(
        snap((pos(ticket=5001, magic=COPIER_MAGIC),)),
        is_master=False,
        copies=[c],
        breakeven="0.3R",
    )
    assert rows is not None
    assert (rows[0].breakeven, rows[0].breakeven_tier) == (text, tier)
    assert rows[0].master_ticket == 100


def test_a_position_that_is_not_a_copy_has_no_breakeven_state() -> None:
    rows = position_rows(snap((pos(ticket=6000),)), is_master=False, breakeven="1R")
    assert rows is not None
    assert (rows[0].breakeven, rows[0].master_ticket) == ("", None)


def test_the_slaves_own_rule_reads_in_its_own_units() -> None:
    assert breakeven_rule(TerminalConfig(terminal_id=TID)) == "off"
    assert (
        breakeven_rule(
            TerminalConfig(terminal_id=TID, breakeven_mode=BreakevenMode.R, breakeven_trigger_r=1.5)
        )
        == "1.5R"
    )


def test_a_copy_whose_master_is_gone_stands_out() -> None:
    rows = position_rows(
        snap((pos(ticket=5001, magic=COPIER_MAGIC),)),
        is_master=False,
        copies=[copy(100, 5001)],
        master_positions=(),
    )
    assert rows is not None
    assert "closed on master" in rows[0].link
    assert rows[0].link_tier == "serious"


def test_orphans_ours_and_manual_positions_are_told_apart() -> None:
    rows = position_rows(
        snap(
            (
                pos(ticket=1, magic=COPIER_MAGIC),
                pos(ticket=2, magic=COPIER_MAGIC),
                pos(ticket=3, magic=0),
            )
        ),
        is_master=False,
        copies=[],
        orphan_tickets=[1],
    )
    assert rows is not None
    assert [(r.ticket, r.link_tier) for r in rows] == [
        (1, "critical"),
        (2, "warning"),
        (3, "muted"),
    ]


def test_the_master_shows_how_many_slaves_hold_each_position() -> None:
    rows = position_rows(
        snap((pos(ticket=1), pos(ticket=2), pos(ticket=3))),
        is_master=True,
        held_by={1: 2, 2: 1},
        slave_count=2,
    )
    assert rows is not None
    assert [(r.link, r.link_tier) for r in rows] == [
        ("on 2/2 slaves", "good"),
        ("on 1/2 slaves", "warning"),
        ("on 0/2 slaves", "serious"),
    ]


def test_held_by_counts_open_copies_once_per_slave_and_skips_unreadable_ones() -> None:
    counts = held_by_counts(
        [
            [copy(1, 11), copy(2, None, resting=True)],
            [copy(1, 21), copy(1, 22)],
            None,
        ]
    )
    assert counts == {1: 2}


# --- gauges ---------------------------------------------------------------------------------------


def test_margin_gauge_reads_used_fraction_and_level() -> None:
    g = margin_gauge(
        snap((), equity=10_000.0, margin=2_500.0, margin_level=400.0, margin_free=7_500.0)
    )
    assert g.used_fraction == pytest.approx(0.25)
    assert g.level_pct == 400.0
    assert g.level_tier == "good"
    assert g.free == 7_500.0


@pytest.mark.parametrize(("level", "tier"), [(150.0, "warning"), (80.0, "critical")])
def test_a_thin_margin_level_is_coloured(level: float, tier: str) -> None:
    assert margin_gauge(snap((), equity=1.0, margin=1.0, margin_level=level)).level_tier == tier


def test_no_margin_in_use_is_not_a_zero_percent_alarm() -> None:
    """MT5 reports a level of 0 when nothing uses margin -- that is the safest state, not the
    worst one."""
    g = margin_gauge(snap((), equity=10_000.0, margin=0.0, margin_level=0.0))
    assert g.level_pct is None
    assert g.level_tier == "good"
    assert g.used_fraction == 0.0


def test_an_unknown_margin_is_blank() -> None:
    g = margin_gauge(snap(()))
    assert g.used_fraction is None
    assert g.level_tier == "muted"


def test_exposure_nets_buys_against_sells_per_symbol() -> None:
    exp = exposure(
        [
            pos(ticket=1, volume=1.0),
            pos(ticket=2, volume=0.4, ptype=PositionType.SELL),
            pos(ticket=3, symbol="XAUUSD", volume=0.1, ptype=PositionType.SELL),
        ]
    )
    assert [(e.symbol, e.net_lots, e.gross_lots) for e in exp] == [
        ("EURUSD", 0.6, 1.4),
        ("XAUUSD", -0.1, 0.1),
    ]


# --- formatting -----------------------------------------------------------------------------------


def test_price_digits_follow_the_prices_given() -> None:
    assert price_digits(1.08512, 1.08) == 5
    assert price_digits(2345.67, 0.0) == 2
    assert price_digits(150.1234567891) == 8
    assert format_price(1.0851, 5) == "1.08510"
    assert format_price(0.0, 5) == "—"


def test_bar_fills_proportionally_and_shows_unknown_as_a_track() -> None:
    assert bar(0.5, 10) == "█████░░░░░"
    assert bar(None, 4) == "░░░░"
    assert bar(3.0, 4) == "████"


def test_durations_and_ping_read_naturally() -> None:
    assert format_duration(None) == "—"
    assert format_duration(42) == "42s"
    assert format_duration(125) == "2m 05s"
    assert format_duration(3 * 3600 + 7 * 60) == "3h 07m"
    assert format_ping(41_250) == "41 ms"
    assert format_ping(None) == "—"
    assert format_ping(0) == "—"
