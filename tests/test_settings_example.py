"""The settings form's worked example: the sizing arithmetic on one of the master's trades."""

from __future__ import annotations

from mt5copier.config import Role, SizingMode, TerminalConfig
from mt5copier.settings_example import MasterTrade, PriceValue, pick, work


def slave(**over: object) -> TerminalConfig:
    base: dict[str, object] = {
        "terminal_id": "s" * 16,
        "label": "Beta",
        "role": Role.SLAVE,
        "risk_percent": 1.0,
        "commission_per_lot_round_turn": 3.5,
    }
    return TerminalConfig.model_validate({**base, **over})


BTC = MasterTrade(11, "BTCUSD", True, entry=84854.0, sl=84554.0, tp=85165.0)
ONE_DOLLAR = PriceValue(1.0, 0.01)


def test_the_designs_own_example() -> None:
    ex = work(slave(), 97512.61, BTC, ONE_DOLLAR)
    assert ex.lead == "Master buys BTCUSD with its stop 300.00 away."
    assert ex.working == "975.13 risk ÷ (300.00 stop + 3.50 commission) ="
    assert ex.result == "this slave buys about 3.21 lots"


def test_slippage_is_shown_as_money_and_counted() -> None:
    """Ten points each side on a 0.01-point symbol is 0.20 of price, so 0.20 a lot."""
    ex = work(slave(slippage_buffer_points=10), 97512.61, BTC, ONE_DOLLAR)
    assert ex.working == "975.13 risk ÷ (300.00 stop + 0.20 slippage + 3.50 commission) ="
    assert ex.result == "this slave buys about 3.21 lots"


def test_a_fixed_cash_slave_needs_no_balance() -> None:
    cfg = slave(sizing_mode=SizingMode.FIXED_CASH_RISK, risk_cash=607.0)
    ex = work(cfg, None, BTC, ONE_DOLLAR)
    assert ex.working.startswith("607.00 risk")
    assert ex.result == "this slave buys about 2 lots"


def test_the_max_lot_caps_it_and_says_so() -> None:
    ex = work(slave(max_lot=1.5), 97512.61, BTC, ONE_DOLLAR)
    assert ex.result == "this slave buys 1.5 lots"
    assert "capped" in ex.note


def test_with_no_price_value_it_says_what_it_would_divide_by() -> None:
    ex = work(slave(), 97512.61, BTC, None)
    assert ex.result == ""
    assert "what one lot loses over 300" in ex.working
    assert "BTCUSD" in ex.note


def test_an_unread_balance_gives_no_figure() -> None:
    ex = work(slave(), None, BTC, ONE_DOLLAR)
    assert ex.result == ""
    assert "not read yet" in ex.working


def test_a_reversed_slave_sizes_from_the_target_and_sells() -> None:
    ex = work(slave(invert=True), 97512.61, BTC, ONE_DOLLAR)
    assert "target 311.00 away" in ex.lead
    assert ex.result.startswith("this slave sells")


def test_with_no_trade_it_says_so() -> None:
    ex = work(slave(), 97512.61, None, None)
    assert "no open trade with a stop" in ex.lead


def test_pick_prefers_a_trade_it_can_price_and_prices_a_symbol_by_any_fill() -> None:
    newer = MasterTrade(13, "XAUUSD", True, 2400.0, 2390.0)
    older = MasterTrade(12, "BTCUSD", False, 84854.0, 85154.0)
    no_stop = MasterTrade(14, "BTCUSD", True, 84854.0)
    trade, price = pick(
        [no_stop, newer, older],
        fills={11: ONE_DOLLAR},
        symbols={11: "BTCUSD", 12: "BTCUSD", 13: "XAUUSD"},
        invert=False,
    )
    assert trade is older, "the BTCUSD fill on ticket 11 prices ticket 12; 14 has no stop"
    assert price is ONE_DOLLAR

    trade, price = pick([newer], fills={}, symbols={}, invert=False)
    assert trade is newer and price is None
