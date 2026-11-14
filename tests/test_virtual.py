"""The virtual master's book: the master side of a profile that trades directly.

Everything here is pure against a temp file -- no engine, no feed, just the book and a
handful of quotes. The one rule that matters above all others is MT5's own: a pending that
fills becomes a position carrying the order's ticket, because that identity is how the
reconciler tells a fill from a cancellation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mt5copier.protocol import OrderType, PositionType
from mt5copier.virtual import VirtualBook

EURUSD = ("EURUSD", (1.08500, 1.08520))
NOW = 1_700_000_000.0


@pytest.fixture
def book(tmp_path: Path) -> VirtualBook:
    return VirtualBook(tmp_path / "virtual_master.json")


def quote() -> tuple[float, float]:
    return EURUSD[1]


# --- market orders ---------------------------------------------------------------------------


def test_buy_fills_at_the_ask(book: VirtualBook) -> None:
    assert book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 1.09, quote(), now=NOW) is None
    (p,) = book.positions()
    assert p.price_open == 1.08520
    assert p.type is PositionType.BUY
    assert p.ticket == 1


def test_sell_fills_at_the_bid(book: VirtualBook) -> None:
    assert book.open_market("EURUSD", PositionType.SELL, 1.0, 1.09, 1.08, quote(), now=NOW) is None
    (p,) = book.positions()
    assert p.price_open == 1.08500


def test_market_order_needs_a_quote(book: VirtualBook) -> None:
    why = book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, None, now=NOW)
    assert why is not None and "no price" in why
    assert not book.positions()


def test_stop_loss_is_required(book: VirtualBook) -> None:
    why = book.open_market("EURUSD", PositionType.BUY, 1.0, 0.0, 0.0, quote(), now=NOW)
    assert why is not None and "stop loss is required" in why


def test_stop_must_be_on_the_losing_side(book: VirtualBook) -> None:
    why = book.open_market("EURUSD", PositionType.BUY, 1.0, 1.09, 0.0, quote(), now=NOW)
    assert why is not None and "below" in why
    why = book.open_market("EURUSD", PositionType.SELL, 1.0, 1.08, 0.0, quote(), now=NOW)
    assert why is not None and "above" in why


def test_target_must_be_on_the_winning_side(book: VirtualBook) -> None:
    why = book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 1.0810, quote(), now=NOW)
    assert why is not None and "target" in why


def test_lots_are_bounded(book: VirtualBook) -> None:
    assert book.open_market("EURUSD", PositionType.BUY, 0.004, 1.08, 0.0, quote(), now=NOW)
    assert book.open_market("EURUSD", PositionType.BUY, 1001.0, 1.08, 0.0, quote(), now=NOW)


def test_symbol_is_cleaned_and_upper_cased(book: VirtualBook) -> None:
    assert book.open_market(" eurusd ", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW) is None
    assert book.positions()[0].symbol == "EURUSD"


# --- pending orders ---------------------------------------------------------------------------


def test_pending_rests_and_keeps_its_side_of_the_quote(book: VirtualBook) -> None:
    assert (
        book.place_pending(
            "EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08000, 1.07500, 0.0, quote(), now=NOW
        )
        is None
    )
    (o,) = book.orders()
    assert o.ticket == 1 and o.type is OrderType.BUY_LIMIT


@pytest.mark.parametrize(
    ("otype", "entry", "hint"),
    [
        (OrderType.BUY_LIMIT, 1.09, "below the ask"),
        (OrderType.BUY_STOP, 1.08, "above the ask"),
        (OrderType.SELL_LIMIT, 1.08, "above the bid"),
        (OrderType.SELL_STOP, 1.09, "below the bid"),
    ],
)
def test_pending_on_the_wrong_side_is_refused(
    book: VirtualBook, otype: OrderType, entry: float, hint: str
) -> None:
    sl = entry - 0.005 if otype.direction is PositionType.BUY else entry + 0.005
    why = book.place_pending("EURUSD", otype, 1.0, entry, sl, 0.0, quote(), now=NOW)
    assert why is not None and hint in why


def test_stop_limit_is_refused(book: VirtualBook) -> None:
    why = book.place_pending(
        "EURUSD", OrderType.BUY_STOP_LIMIT, 1.0, 1.09, 1.08, 0.0, quote(), now=NOW
    )
    assert why is not None and "limit or stop" in why


def test_pending_needs_a_quote_too(book: VirtualBook) -> None:
    why = book.place_pending("EURUSD", OrderType.BUY_LIMIT, 1.0, 1.08, 1.075, 0.0, None, now=NOW)
    assert why is not None and "no price" in why


# --- the market moving on the book ------------------------------------------------------------


def seed_buy_limit(book: VirtualBook) -> int:
    assert (
        book.place_pending(
            "EURUSD", OrderType.BUY_LIMIT, 0.5, 1.08000, 1.07500, 1.09000, quote(), now=NOW
        )
        is None
    )
    return book.orders()[0].ticket


def test_a_filled_pending_becomes_a_position_with_the_same_ticket(book: VirtualBook) -> None:
    """MT5's fill rule, reproduced on purpose: the reconciler reads a departed order ticket
    reappearing as a position as ORDER_FILLED, and anything else as a cancel."""
    ticket = seed_buy_limit(book)
    events = book.tick({"EURUSD": (1.07980, 1.08000)}, NOW + 60)
    assert any("filled at 1.08" in e for e in events)
    assert not book.orders()
    (p,) = book.positions()
    assert p.ticket == ticket
    assert p.price_open == 1.08000
    assert p.sl == 1.07500 and p.tp == 1.09000
    assert p.volume == 0.5


def test_a_pending_does_not_fill_until_the_price_crosses(book: VirtualBook) -> None:
    seed_buy_limit(book)
    assert book.tick({"EURUSD": (1.08480, 1.08500)}, NOW + 60) == []
    assert book.orders() and not book.positions()


def test_an_unquoted_symbol_is_left_alone(book: VirtualBook) -> None:
    seed_buy_limit(book)
    assert book.tick({}, NOW + 60) == []
    assert book.orders()


def test_an_expired_order_is_deleted(book: VirtualBook) -> None:
    assert (
        book.place_pending(
            "EURUSD",
            OrderType.BUY_LIMIT,
            1.0,
            1.08,
            1.075,
            0.0,
            quote(),
            expiration_utc=int(NOW + 300),
            now=NOW,
        )
        is None
    )
    assert book.tick({}, NOW + 299) == []
    events = book.tick({}, NOW + 300)
    assert any("expired" in e for e in events)
    assert not book.orders()


def test_a_long_closes_on_its_stop_at_the_bid(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 1.09000, quote(), now=NOW)
    events = book.tick({"EURUSD": (1.07990, 1.08010)}, NOW + 60)
    assert any("stop loss at 1.08" in e for e in events)
    assert not book.positions()


def test_a_long_closes_on_its_target(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08000, 1.09000, quote(), now=NOW)
    events = book.tick({"EURUSD": (1.09000, 1.09020)}, NOW + 60)
    assert any("target at 1.09" in e for e in events)
    assert not book.positions()


def test_a_short_closes_on_its_stop_at_the_ask(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.SELL, 1.0, 1.09000, 1.07000, quote(), now=NOW)
    events = book.tick({"EURUSD": (1.08980, 1.09000)}, NOW + 60)
    assert any("stop loss" in e for e in events)
    assert not book.positions()


def test_a_gap_past_both_levels_takes_the_stop(book: VirtualBook) -> None:
    """The conservative reading of a price that visited both levels unobserved."""
    book.open_market("EURUSD", PositionType.SELL, 1.0, 1.09000, 1.08000, quote(), now=NOW)
    events = book.tick({"EURUSD": (1.09500, 1.09520)}, NOW + 60)
    assert any("stop loss" in e for e in events)


# --- managing what is open ---------------------------------------------------------------------


def test_partial_close_reduces_the_volume(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    assert book.partial_close(1, 50.0) is None
    assert book.positions()[0].volume == 0.5


def test_partial_close_refuses_a_dust_slice_or_remainder(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 0.02, 1.08, 0.0, quote(), now=NOW)
    assert book.partial_close(1, 10.0) is not None  # 0.002 lots: too small a slice
    book2_why = book.partial_close(1, 99.0)
    assert book2_why is not None and "close the position instead" in book2_why


def test_modify_position_moves_the_levels(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    assert book.modify_position(1, 1.0820, 1.0950) is None
    p = book.positions()[0]
    assert p.sl == 1.0820 and p.tp == 1.0950


def test_modify_position_keeps_the_stop_required(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    why = book.modify_position(1, 0.0, 1.0950)
    assert why is not None and "stop loss is required" in why


def test_modify_order_can_move_the_entry(book: VirtualBook) -> None:
    seed_buy_limit(book)
    assert book.modify_order(1, 1.07800, 1.07300, 1.08800, 0) is None
    o = book.orders()[0]
    assert o.price_open == 1.07800 and o.sl == 1.07300 and o.tp == 1.08800
    assert o.expiration_utc == 0


def test_close_and_cancel_remove_their_targets(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    seed_ticket = book.positions()[0].ticket
    assert book.close_position(seed_ticket) is None
    assert book.close_position(seed_ticket) is not None  # already gone
    seed_buy_limit(book)
    order_ticket = book.orders()[0].ticket
    assert book.cancel_order(order_ticket) is None
    assert not book.orders()


# --- persistence ------------------------------------------------------------------------------


def test_the_book_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "virtual_master.json"
    book = VirtualBook(path)
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 1.09, quote(), now=NOW)
    book.place_pending(
        "XAUUSD", OrderType.SELL_STOP, 0.5, 1990.0, 2001.0, 0.0, (2000.0, 2000.5), now=NOW
    )

    reopened = VirtualBook(path)
    assert [p.ticket for p in reopened.positions()] == [1]
    assert [o.ticket for o in reopened.orders()] == [2]
    assert reopened.orders()[0].type is OrderType.SELL_STOP
    # The counter carries on rather than reusing ticket ids the ledger may still know.
    reopened.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    assert reopened.positions()[-1].ticket == 3


def test_a_corrupt_book_is_moved_aside_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "virtual_master.json"
    path.write_text("{not json", encoding="utf-8")
    book = VirtualBook(path)
    assert not book.positions() and not book.orders()
    bad = list(tmp_path.glob("virtual_master.bad-*.json"))
    assert len(bad) == 1


def test_symbols_lists_everything_the_book_holds(book: VirtualBook) -> None:
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    book.place_pending(
        "XAUUSD", OrderType.SELL_STOP, 0.5, 1990.0, 2001.0, 0.0, (2000.0, 2000.5), now=NOW
    )
    assert book.symbols() == {"EURUSD", "XAUUSD"}


def test_saved_file_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "virtual_master.json"
    book = VirtualBook(path)
    book.open_market("EURUSD", PositionType.BUY, 1.0, 1.08, 0.0, quote(), now=NOW)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["next_ticket"] == 2
    assert raw["positions"][0]["symbol"] == "EURUSD"
