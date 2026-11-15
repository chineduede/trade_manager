"""The desktop bridge's translation layer: tickets in, rows out.

The window's rules live in the book and the workers; what is tested here is only that the
bridge carries a page's ticket to the right engine call with the right shapes -- above all
that every direct trade is recorded at the nominal one lot, since the page has no lots
field and the slaves size themselves.
"""

from __future__ import annotations

import time
from typing import Any

from mt5copier.desktop.api import _NOMINAL_LOTS, DesktopApi, _order_row, _position_row
from mt5copier.protocol import OrderType, PendingOrder, Position, PositionType


class _Engine:
    """Only what the bridge's acting methods reach for, recorded rather than done."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.answer: str | None = None

    def place_direct_market(self, *a: Any) -> str | None:
        self.calls.append(("market", a))
        return self.answer

    def place_direct_pending(self, *a: Any) -> str | None:
        self.calls.append(("pending", a))
        return self.answer

    def close_direct(self, *a: Any) -> str | None:
        self.calls.append(("close", a))
        return self.answer

    def cancel_direct(self, *a: Any) -> str | None:
        self.calls.append(("cancel", a))
        return self.answer

    def modify_direct(self, *a: Any) -> str | None:
        self.calls.append(("modify", a))
        return self.answer

    def modify_direct_order(self, *a: Any) -> str | None:
        self.calls.append(("modify_order", a))
        return self.answer

    def direct_orders(self) -> tuple[PendingOrder, ...]:
        return (
            PendingOrder(
                ticket=7,
                symbol="EURUSD",
                type=OrderType.SELL_LIMIT,
                volume=1.0,
                price_open=1.09,
                sl=1.095,
                tp=1.08,
                expiration_utc=4200,
            ),
        )


def _api(engine: _Engine) -> DesktopApi:
    return DesktopApi(cfg=None, engine=engine)  # type: ignore[arg-type]


def test_market_ticket_lands_at_the_nominal_lot() -> None:
    engine = _Engine()
    out = _api(engine).place(
        {"symbol": " btcusd ", "side": "buy", "kind": "market", "sl": 64000.0, "tp": 65000.0}
    )
    assert out["ok"] is True
    kind, args = engine.calls[0]
    assert kind == "market"
    assert args == ("BTCUSD", PositionType.BUY, _NOMINAL_LOTS, 64000.0, 65000.0)


def test_pending_ticket_maps_side_and_kind_to_the_order_type() -> None:
    engine = _Engine()
    before = time.time()
    out = _api(engine).place(
        {
            "symbol": "EURUSD",
            "side": "sell",
            "kind": "limit",
            "entry": 1.09,
            "sl": 1.095,
            "tp": 1.08,
            "expiryMinutes": 45,
        }
    )
    assert out["ok"] is True
    kind, args = engine.calls[0]
    assert kind == "pending"
    assert args[:6] == ("EURUSD", OrderType.SELL_LIMIT, _NOMINAL_LOTS, 1.09, 1.095, 1.08)
    assert before + 45 * 60 - 5 <= args[6] <= time.time() + 45 * 60 + 5


def test_a_refusal_comes_back_as_the_message() -> None:
    engine = _Engine()
    engine.answer = "a stop loss is required"
    out = _api(engine).place(
        {"symbol": "EURUSD", "side": "buy", "kind": "market", "sl": 0.0, "tp": 0.0}
    )
    assert out == {"ok": False, "message": "a stop loss is required"}


def test_an_unreadable_ticket_is_refused_without_reaching_the_engine() -> None:
    engine = _Engine()
    out = _api(engine).place({"symbol": "EURUSD", "side": "buy", "kind": "market", "sl": "x"})
    assert out["ok"] is False
    assert engine.calls == []


def test_modifying_a_resting_order_keeps_its_expiry() -> None:
    engine = _Engine()
    out = _api(engine).modify_order(7, 1.091, 1.096, 1.081)
    assert out["ok"] is True
    kind, args = engine.calls[0]
    assert kind == "modify_order"
    assert args == (7, 1.091, 1.096, 1.081, 4200)


def test_rows_carry_what_the_page_shows() -> None:
    p = Position(
        ticket=3,
        symbol="BTCUSD",
        type=PositionType.BUY,
        volume=0.5,
        price_open=64799.0,
        sl=64317.0,
        tp=65521.0,
    )
    copies = [{"name": "IC demo", "state": "live"}, {"name": "FTMO", "state": "closed"}]
    row = _position_row(p, bid=64784.0, ask=64799.0, copies=copies)
    assert row["side"] == "buy"
    assert row["kind"] == "market"
    assert row["now"] == 64784.0  # a buy closes at the bid
    assert row["copies"] == copies

    o = _Engine().direct_orders()[0]
    row = _order_row(o, now=4200 - 600)
    assert row["side"] == "sell"
    assert row["kind"] == "limit"
    assert row["expiresMin"] == 10
