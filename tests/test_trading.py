"""The trading page and the virtual master's face on the Map.

The book's own rules (what fills, what is refused) are tested in ``test_virtual.py`` and the
engine's wiring in ``test_direct_engine.py``; here it is the UI: the ticket reads what was
typed, every key lands on the right engine call, and refusals are shown as the book said
them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static
from tui_support import TID_SLAVE, make_app, make_cfg, plain, toasted

from mt5copier.config import VIRTUAL_MASTER_ID, AppConfig, AppMode, Role
from mt5copier.protocol import (
    Candle,
    OrderType,
    PendingOrder,
    Position,
    PositionType,
    Quote,
    RatesResult,
)
from mt5copier.tui import CopierApp
from mt5copier.tui.cards import virtual_master_card
from mt5copier.tui.settings import Choice
from mt5copier.tui.trading import (
    TradingScreen,
    parse_numbers,
    parse_ticket,
    quote_line,
    row_for_order,
    row_for_position,
)
from mt5copier.tui.widgets import Card


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    """A profile that trades directly, with one assigned slave."""
    out = make_cfg(tmp_path)
    out.mode = AppMode.TRADING
    out.terminals[TID_SLAVE].role = Role.SLAVE
    return out


@pytest.fixture
def app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CopierApp:
    return make_app(cfg, tmp_path, monkeypatch)


def ticket(**over: object) -> object:
    base: dict[str, object] = dict(
        symbol="eurusd",
        side="buy",
        kind="market",
        lots="1",
        entry="",
        sl="1.08",
        tp="",
        expiry_minutes="",
        now=1_000.0,
    )
    base.update(over)
    now = base.pop("now")
    return parse_ticket(**base, now=now)  # type: ignore[arg-type]


# --- the ticket, read ----------------------------------------------------------------------


def test_a_market_ticket_reads_clean() -> None:
    t = ticket()
    assert (t.symbol, t.side, t.kind) == ("EURUSD", PositionType.BUY, "market")
    assert (t.lots, t.sl, t.tp, t.entry, t.expiration_utc) == (1.0, 1.08, 0.0, 0.0, 0)


def test_the_symbol_is_required_and_uppercased() -> None:
    assert ticket(symbol="  ") == "name the symbol to trade"
    assert ticket(symbol=" xauusd ").symbol == "XAUUSD"


def test_the_stop_is_required_on_every_direct_trade() -> None:
    assert ticket(sl="") == "the stop loss is required on every direct trade"


def test_a_comma_decimal_is_hinted_at() -> None:
    assert ticket(lots="0,5") == "lots is not a number (use a dot for decimals)"


def test_nothing_reads_negative() -> None:
    assert ticket(sl="-1") == "the stop loss cannot be negative"


def test_a_pending_ticket_needs_its_entry() -> None:
    assert ticket(kind="limit") == "the entry price is required"
    t = ticket(kind="limit", entry="1.0750", side="sell")
    assert (t.kind, t.entry, t.side) == ("limit", 1.075, PositionType.SELL)


def test_the_expiry_is_minutes_from_now() -> None:
    t = ticket(kind="stop", entry="1.09", expiry_minutes="30", now=1_000.0)
    assert t.expiration_utc == 1_000 + 30 * 60
    assert ticket(kind="stop", entry="1.09", expiry_minutes="").expiration_utc == 0
    assert "minutes from now" in ticket(kind="stop", entry="1.09", expiry_minutes="x")


def test_parse_numbers() -> None:
    assert parse_numbers("1.08 1.09", 2) == [1.08, 1.09]
    assert parse_numbers("50", 1) == [50.0]
    assert parse_numbers("1 2 3", 2) is None, "more numbers than asked for"
    assert parse_numbers("", 2) is None
    assert parse_numbers("ten", 1) is None


# --- the quote line and the book's rows ----------------------------------------------------


def test_the_quote_line_reads_the_price_and_where_it_comes_from() -> None:
    said = quote_line("EURUSD", Quote("EURUSD", 1.085, 1.0852), "Beta Broker").plain
    assert "bid " in said and "1.085" in said and "1.0852" in said and "Beta Broker" in said


def test_the_quote_line_says_why_there_is_no_price() -> None:
    assert "Type a symbol" in quote_line("", None, None).plain
    assert "no feed" in quote_line("EURUSD", None, None).plain
    assert "waiting for Beta" in quote_line("EURUSD", None, "Beta").plain


def test_a_positions_row_marks_at_the_closing_side() -> None:
    quote = Quote("EURUSD", 1.0850, 1.0852)
    buy = Position(ticket=7, symbol="EURUSD", type=PositionType.BUY, volume=0.5, price_open=1.08)
    sell = Position(ticket=8, symbol="EURUSD", type=PositionType.SELL, volume=0.5, price_open=1.09)
    assert [c.plain for c in row_for_position(buy, quote)][-1] == "1.085", "a buy closes at bid"
    assert [c.plain for c in row_for_position(sell, quote)][-1] == "1.0852", "a sell at ask"
    cells = [c.plain for c in row_for_position(buy, None)]
    assert cells[-1] == "—" and cells[6] == "—" and cells[7] == "—"


def test_an_orders_row_says_when_it_expires() -> None:
    order = PendingOrder(
        ticket=9, symbol="EURUSD", type=OrderType.BUY_LIMIT, volume=1.0, price_open=1.07
    )
    assert [c.plain for c in row_for_order(order, now=1_000.0)][-1] == "GTC"
    timed = PendingOrder(
        ticket=9,
        symbol="EURUSD",
        type=OrderType.BUY_LIMIT,
        volume=1.0,
        price_open=1.07,
        expiration_utc=1_000 + 45 * 60,
    )
    cells = [c.plain for c in row_for_order(timed, now=1_000.0)]
    assert cells[-1] == "expires 45m" and cells[3] == "buy limit"


def test_the_virtual_masters_card() -> None:
    said = plain(virtual_master_card(feed="Beta Broker", positions=2, orders=1, slaves=3))
    assert "feed Beta Broker" in said
    assert "Direct trading" in said and "you are the master" in said
    assert "2 open · 1 resting" in said and "feeds 3 slaves" in said
    assert "order ticket" in said


def test_the_virtual_masters_card_without_a_feed() -> None:
    said = plain(virtual_master_card(feed=None, positions=0, orders=0, slaves=1))
    assert "no feed" in said and "connect an MT5 slave" in said
    assert "nothing open" in said and "feeds 1 slave" in said


# --- the Map in trading mode ---------------------------------------------------------------


async def test_the_virtual_master_heads_the_map(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)):
        assert app._order[0] == VIRTUAL_MASTER_ID
        card = app.query_one(".card.master", Card)
        assert card.terminal_id == VIRTUAL_MASTER_ID
        assert "Direct trading" in plain(card.content, width=40)


async def test_n_opens_the_order_ticket_only_in_trading_mode(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("n")
        assert isinstance(app.screen, TradingScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, TradingScreen)
        # Leaving the page lets the feed drop the ticket's preview symbol.
        assert app._engine.watched[-1] is None  # type: ignore[attr-defined]


async def test_enter_on_the_virtual_card_opens_the_ticket(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("a")  # select the master card
        await pilot.press("enter")
        assert isinstance(app.screen, TradingScreen)


async def test_a_copying_profile_refuses_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_cfg = make_cfg(tmp_path)
    copy_app = make_app(copy_cfg, tmp_path, monkeypatch)
    async with copy_app.run_test(size=(160, 50)) as pilot:
        await pilot.press("n")
        assert not isinstance(copy_app.screen, TradingScreen)
        assert await toasted(pilot, "Direct trading is its own profile mode")


# --- the trading page ----------------------------------------------------------------------


async def open_ticket(app: CopierApp, pilot) -> TradingScreen:
    await pilot.press("n")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, TradingScreen)
    return screen


async def test_typing_a_symbol_watches_it(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        await pilot.pause()
        assert app._engine.watched[-1] == "EURUSD"  # type: ignore[attr-defined]


async def test_placing_a_market_order(app: CopierApp) -> None:
    engine = app._engine
    engine.feed_tid = TID_SLAVE  # type: ignore[attr-defined]
    engine.quotes["EURUSD"] = Quote("EURUSD", 1.0850, 1.0852)  # type: ignore[attr-defined]
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        screen.query_one("#dt-lots", Input).value = "0.5"
        screen.query_one("#dt-sl", Input).value = "1.08"
        screen.query_one("#dt-tp", Input).value = "1.095"
        await pilot.pause()
        screen.action_place()
        await pilot.pause()
        assert engine.direct_calls == [  # type: ignore[attr-defined]
            ("market", "EURUSD", PositionType.BUY, 0.5, 1.08, 1.095)
        ]
        assert "opened: EURUSD buy 0.5 lots" in plain(
            screen.query_one("#dt-status", Static).content
        )


async def test_placing_a_pending_order(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        screen.query_one("#dt-side", Choice).value = "sell"
        screen.query_one("#dt-kind", Choice).value = "limit"
        screen.query_one("#dt-entry", Input).value = "1.0950"
        screen.query_one("#dt-sl", Input).value = "1.10"
        await pilot.pause()
        screen.action_place()
        await pilot.pause()
        call = app._engine.direct_calls[-1]  # type: ignore[attr-defined]
        assert call[:3] == ("pending", "EURUSD", OrderType.SELL_LIMIT)
        assert call[3:] == (1.0, 1.095, 1.10, 0.0, 0)
        assert "resting" in plain(screen.query_one("#dt-status", Static).content)


async def test_a_refusal_is_shown_as_the_book_said_it(app: CopierApp) -> None:
    app._engine.direct_refusal = "a buy limit rests below the market"  # type: ignore[attr-defined]
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        screen.query_one("#dt-sl", Input).value = "1.08"
        screen.action_place()
        await pilot.pause()
        assert "rests below the market" in plain(screen.query_one("#dt-status", Static).content)


async def test_the_missing_stop_never_reaches_the_engine(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        screen.action_place()
        await pilot.pause()
        assert app._engine.direct_calls == []  # type: ignore[attr-defined]
        said = plain(screen.query_one("#dt-status", Static).content)
        assert "the stop loss is required on every direct trade" in said


def wire_book(app: CopierApp) -> None:
    engine = app._engine
    engine.book_positions.append(  # type: ignore[attr-defined]
        Position(
            ticket=7, symbol="EURUSD", type=PositionType.BUY, volume=1.0, price_open=1.085, sl=1.08
        )
    )
    engine.book_orders.append(  # type: ignore[attr-defined]
        PendingOrder(
            ticket=8,
            symbol="GBPUSD",
            type=OrderType.SELL_STOP,
            volume=0.5,
            price_open=1.26,
            sl=1.27,
        )
    )


async def test_c_closes_the_selected_position_and_cancels_the_selected_order(
    app: CopierApp,
) -> None:
    wire_book(app)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        table = screen.query_one("#dt-book", DataTable)
        table.focus()
        await pilot.press("c")
        table.move_cursor(row=1)
        await pilot.press("c")
        assert app._engine.direct_calls == [("close", 7), ("cancel", 8)]  # type: ignore[attr-defined]


async def test_x_asks_the_percent_then_partially_closes(app: CopierApp) -> None:
    wire_book(app)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-book", DataTable).focus()
        await pilot.press("x")
        box = screen.query_one("#dt-ask-box", Input)
        assert screen.query_one("#dt-ask").display and box.has_focus
        box.value = "25"
        await pilot.press("enter")
        assert app._engine.direct_calls == [("partial", 7, 25.0)]  # type: ignore[attr-defined]


async def test_m_moves_a_positions_stop_and_target(app: CopierApp) -> None:
    wire_book(app)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-book", DataTable).focus()
        await pilot.press("m")
        box = screen.query_one("#dt-ask-box", Input)
        assert box.value == "1.08", "prefilled with the stop as it is"
        box.value = "1.082 1.095"
        await pilot.press("enter")
        assert app._engine.direct_calls == [("modify", 7, 1.082, 1.095)]  # type: ignore[attr-defined]


async def test_m_moves_a_resting_orders_entry_too(app: CopierApp) -> None:
    wire_book(app)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        table = screen.query_one("#dt-book", DataTable)
        table.focus()
        table.move_cursor(row=1)
        await pilot.press("m")
        box = screen.query_one("#dt-ask-box", Input)
        assert box.value == "1.26 1.27", "prefilled with the order as it is"
        box.value = "1.255 1.265"
        await pilot.press("enter")
        calls = app._engine.direct_calls  # type: ignore[attr-defined]
        assert calls == [("modify_order", 8, 1.255, 1.265, 0.0, 0)]


async def test_flatten_needs_all_typed(app: CopierApp) -> None:
    wire_book(app)
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-book", DataTable).focus()
        await pilot.press("F")
        box = screen.query_one("#dt-ask-box", Input)
        box.value = "yes"
        await pilot.press("enter")
        assert app._engine.direct_calls == []  # type: ignore[attr-defined]
        await pilot.press("F")
        box.value = "ALL"
        await pilot.press("enter")
        assert app._engine.direct_calls == [("flatten",)]  # type: ignore[attr-defined]
        assert "closed everything: 2" in plain(screen.query_one("#dt-status", Static).content)


# --- switching the profile's mode ----------------------------------------------------------


async def test_the_general_form_switches_the_mode_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mt5copier.tui.general import GeneralForm

    copy_cfg = make_cfg(tmp_path)
    copy_app = make_app(copy_cfg, tmp_path, monkeypatch)
    async with copy_app.run_test(size=(140, 50)) as pilot:
        await pilot.press("g")
        await pilot.pause()
        form = copy_app.screen
        assert isinstance(form, GeneralForm)
        form.query_one("#f-profile_mode", Choice).value = "trading"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(copy_app.screen, GeneralForm)
        assert copy_cfg.mode is AppMode.TRADING
        assert await toasted(pilot, "This profile now trades directly")


async def test_a_refused_switch_keeps_the_form_open_and_saves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mt5copier.tui.general import GeneralForm

    copy_cfg = make_cfg(tmp_path)
    copy_app = make_app(copy_cfg, tmp_path, monkeypatch)
    copy_app._engine.mode_refusal = "2 copies are still open"  # type: ignore[attr-defined]
    async with copy_app.run_test(size=(140, 50)) as pilot:
        await pilot.press("g")
        await pilot.pause()
        form = copy_app.screen
        assert isinstance(form, GeneralForm)
        form.query_one("#f-profile_mode", Choice).value = "trading"
        form.query_one("#f-poll_interval_ms", Input).value = "500"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(copy_app.screen, GeneralForm), "refused: the form stays open"
        assert "2 copies are still open" in plain(form.query_one("#e-profile_mode", Static).content)
        assert copy_cfg.mode is AppMode.COPY
        assert copy_cfg.poll_interval_ms == 250, "nothing else is saved on a refusal"


# --- the minichart on the page -------------------------------------------------------------


async def test_the_chart_draws_the_feeds_bars(app: CopierApp) -> None:
    engine = app._engine
    engine.feed_tid = TID_SLAVE  # type: ignore[attr-defined]
    engine.quotes["EURUSD"] = Quote("EURUSD", 1.0850, 1.0852)  # type: ignore[attr-defined]
    engine.rates = RatesResult(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        symbol="EURUSD",
        timeframe=5,
        candles=tuple(Candle(ts=i, open=1.08, high=1.09, low=1.07, close=1.085) for i in range(3)),
    )
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        said = plain(screen.query_one("#dt-chart", Static).content)
        assert "type a symbol to chart it" in said
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        await pilot.pause()
        screen.refresh_page()
        assert ("EURUSD", 5) in engine.rates_asked  # type: ignore[attr-defined]
        drawn = plain(screen.query_one("#dt-chart", Static).content)
        assert any(glyph in drawn for glyph in "┃╹╻╿╽"), "the bars are drawn"
        head = plain(screen.query_one("#dt-chart-head", Static).content)
        assert "EURUSD" in head and "M5" in head

        screen.action_timeframe()
        head = plain(screen.query_one("#dt-chart-head", Static).content)
        assert "M15" in head, "t steps to the next timeframe"
        said = plain(screen.query_one("#dt-chart", Static).content)
        assert "waiting for Beta Broker" in said, "the M15 bars have not landed yet"
        assert ("EURUSD", 15) in engine.rates_asked  # type: ignore[attr-defined]


async def test_the_chart_says_why_there_are_no_bars(app: CopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        await pilot.pause()
        screen.refresh_page()
        said = plain(screen.query_one("#dt-chart", Static).content)
        assert "no feed" in said, "no feed terminal: the chart says so, not a blank"


async def test_the_stop_line_drags_into_the_ticket(app: CopierApp) -> None:
    engine = app._engine
    engine.feed_tid = TID_SLAVE  # type: ignore[attr-defined]
    engine.quotes["EURUSD"] = Quote("EURUSD", 1.0850, 1.0852)  # type: ignore[attr-defined]
    engine.rates = RatesResult(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        symbol="EURUSD",
        timeframe=5,
        candles=tuple(Candle(ts=i, open=1.08, high=1.09, low=1.07, close=1.085) for i in range(3)),
    )
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        screen.query_one("#dt-sl", Input).value = "1.075"
        await pilot.pause()
        screen.refresh_page()
        scale = screen._chart_scale
        assert scale is not None, "a drawn chart arms the mouse"
        chart_widget = screen.query_one("#dt-chart")
        assert chart_widget.ALLOW_SELECT is False, "dragging must not highlight the chart"
        assert screen.chart_grab(scale.row(1.075)) == "sl", "a press on the line takes hold"
        screen.chart_drag(0)
        moved = screen.query_one("#dt-sl", Input).value
        assert moved == scale.fmt(scale.price(0)), "the ticket's field follows the drag"
        screen.chart_release()
        assert not screen.chart_dragging
        said = plain(screen.query_one("#dt-status", Static).content)
        assert f"stop dragged to {moved}" in said
        # With the stop now at the top and no target typed, a press low on the chart
        # takes hold of nothing.
        assert screen.chart_grab(13) is None


async def test_a_fresh_ticket_seeds_its_lines_on_the_chart(app: CopierApp) -> None:
    engine = app._engine
    engine.feed_tid = TID_SLAVE  # type: ignore[attr-defined]
    engine.quotes["EURUSD"] = Quote("EURUSD", 1.0850, 1.0852)  # type: ignore[attr-defined]
    engine.rates = RatesResult(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        symbol="EURUSD",
        timeframe=5,
        candles=tuple(Candle(ts=i, open=1.08, high=1.09, low=1.07, close=1.085) for i in range(3)),
    )
    async with app.run_test(size=(160, 50)) as pilot:
        screen = await open_ticket(app, pilot)
        screen.query_one("#dt-symbol", Input).value = "eurusd"
        await pilot.pause()
        screen.refresh_page()
        sl_box = screen.query_one("#dt-sl", Input)
        tp_box = screen.query_one("#dt-tp", Input)
        assert float(sl_box.value) < 1.0850 < float(tp_box.value), (
            "a buy starts with the stop below the bid and the target above"
        )
        drawn = plain(screen.query_one("#dt-chart", Static).content)
        assert "≡ SL" in drawn and "≡ TP" in drawn, "the lines wear their handles"

        # Flipping the side flips lines the person has not touched.
        screen.query_one("#dt-side", Choice).value = "sell"
        await pilot.pause()
        screen.refresh_page()
        assert float(sl_box.value) > 1.0850 > float(tp_box.value)

        # A typed stop is the person's own: side flips move only the untouched target.
        sl_box.value = "1.0800"
        screen.query_one("#dt-side", Choice).value = "buy"
        await pilot.pause()
        screen.refresh_page()
        assert sl_box.value == "1.0800"
        assert float(tp_box.value) > 1.0850

        # A pending kind seeds the entry on its resting side, with its own handle.
        screen.query_one("#dt-kind", Choice).value = "limit"
        await pilot.pause()
        screen.refresh_page()
        assert float(screen.query_one("#dt-entry", Input).value) < 1.0850, (
            "a buy limit rests below the market"
        )
        assert "≡ ENTRY" in plain(screen.query_one("#dt-chart", Static).content)
        scale = screen._chart_scale
        assert scale is not None
        entry = float(screen.query_one("#dt-entry", Input).value)
        assert screen.chart_grab(scale.row(entry)) == "entry", "the entry line drags too"
        screen.chart_release()
