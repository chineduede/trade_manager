"""The trading page: ``n`` in a profile that trades directly.

The order ticket writes into the virtual master's book, and the ordinary diff copies the
book to every enabled slave -- so this page never talks to a broker. Everything is refused
in words, not form chrome: the book owns the rules (a stop is required, a limit rests below
the ask) and its refusals are shown as typed, which keeps this module thin and the rules in
one testable place.

Under the ticket, the book itself: every open position and resting order, with the keys to
manage one (close, partial, modify, cancel) or all of them. Each act lands on the copies on
the next tick, each slave's own rules still gating it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Static

from ..protocol import CHART_TIMEFRAMES, OrderType, PendingOrder, Position, PositionType, Quote
from .minichart import Scale, chart, next_timeframe, scale_of, timeframe_name, window
from .settings import Choice
from .style import BLUE, FG, GREEN, MUTED, RED, YELLOW, key_hint, left_right, spaced
from .widgets import KeyBar, QuickInput, TopBar

if TYPE_CHECKING:
    from .app import CopierApp

_SIDES = (("buy", "buy"), ("sell", "sell"))
_KINDS = (("market", "market"), ("limit", "limit"), ("stop", "stop"))

_ORDER_TYPES: dict[tuple[str, str], OrderType] = {
    ("buy", "limit"): OrderType.BUY_LIMIT,
    ("buy", "stop"): OrderType.BUY_STOP,
    ("sell", "limit"): OrderType.SELL_LIMIT,
    ("sell", "stop"): OrderType.SELL_STOP,
}

_NUMERIC = r"[0-9., ]*"

_CHART_HEIGHT = 14
"""Rows the minichart draws in: one place, because the mouse maps rows back to prices."""


@dataclass(frozen=True, slots=True)
class Ticket:
    """What the form said, parsed and ready for the engine. ``entry`` and ``expiration_utc``
    are meaningful only when ``kind`` is not market."""

    symbol: str
    side: PositionType
    kind: str
    lots: float
    entry: float
    sl: float
    tp: float
    expiration_utc: int


def _number(text: str, what: str, *, required: bool) -> float | str:
    """A typed number, 0.0 for blank-and-optional, or why it will not read."""
    text = text.strip()
    if not text:
        return f"{what} is required" if required else 0.0
    try:
        value = float(text)
    except ValueError:
        hint = " (use a dot for decimals)" if "," in text else ""
        return f"{what} is not a number{hint}"
    if value < 0:
        return f"{what} cannot be negative"
    return value


def parse_ticket(
    symbol: str,
    side: str,
    kind: str,
    lots: str,
    entry: str,
    sl: str,
    tp: str,
    expiry_minutes: str,
    *,
    now: float,
) -> Ticket | str:
    """The form's raw text as a :class:`Ticket`, or the first thing wrong with it.

    Only *reading* problems are answered here -- not a number, nothing typed. Whether the
    numbers make a placeable order is the book's ruling, so the two can never disagree.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        return "name the symbol to trade"
    lots_v = _number(lots, "lots", required=True)
    if isinstance(lots_v, str):
        return lots_v
    sl_v = _number(sl, "the stop loss", required=True)
    if isinstance(sl_v, str):
        return sl_v + (" on every direct trade" if not sl.strip() else "")
    tp_v = _number(tp, "the target", required=False)
    if isinstance(tp_v, str):
        return tp_v
    entry_v = 0.0
    expiration = 0
    if kind != "market":
        parsed = _number(entry, "the entry price", required=True)
        if isinstance(parsed, str):
            return parsed
        entry_v = parsed
        minutes = _number(expiry_minutes, "the expiry", required=False)
        if isinstance(minutes, str):
            return minutes + " (minutes from now; blank rests it until cancelled)"
        if minutes:
            expiration = int(now + minutes * 60)
    return Ticket(
        symbol=symbol,
        side=PositionType.BUY if side == "buy" else PositionType.SELL,
        kind=kind,
        lots=lots_v,
        entry=entry_v,
        sl=sl_v,
        tp=tp_v,
        expiration_utc=expiration,
    )


def parse_numbers(text: str, count: int) -> list[float] | None:
    """Up to ``count`` space-separated numbers, or None when any of them will not read."""
    parts = text.replace(",", " ").split()
    if not parts or len(parts) > count:
        return None
    out: list[float] = []
    for part in parts:
        try:
            out.append(float(part))
        except ValueError:
            return None
    return out


def quote_line(symbol: str, quote: Quote | None, feed: str | None) -> Text:
    """``EURUSD  bid 1.08500 · ask 1.08520`` with where it comes from, or why there is none."""
    out = Text()
    if not symbol:
        out.append("Type a symbol to see its price.", style=MUTED)
        return out
    out.append(symbol, style=f"bold {FG}")
    if quote is None:
        if feed is None:
            out.append("  no feed: connect an MT5 slave to quote prices", style=f"bold {RED}")
        else:
            out.append(f"  waiting for {feed} to quote it…", style=YELLOW)
        return out
    out.append("  bid ", style=MUTED)
    out.append(f"{quote.bid:g}", style=f"bold {FG}")
    out.append("  ask ", style=MUTED)
    out.append(f"{quote.ask:g}", style=f"bold {FG}")
    if feed is not None:
        out.append(f"  · {feed}", style=MUTED)
    return out


def row_for_position(p: Position, quote: Quote | None) -> tuple[Text, ...]:
    now = ""
    if quote is not None:
        now = f"{quote.bid if p.type is PositionType.BUY else quote.ask:g}"
    side = p.type.name.lower()
    return (
        Text("open", style=f"bold {GREEN}"),
        Text(f"#{p.ticket}", style=MUTED),
        Text(p.symbol, style=f"bold {FG}"),
        Text(side, style=f"bold {BLUE if side == 'buy' else RED}"),
        Text(f"{p.volume:g}"),
        Text(f"{p.price_open:g}"),
        Text(f"{p.sl:g}" if p.sl else "—", style=FG if p.sl else MUTED),
        Text(f"{p.tp:g}" if p.tp else "—", style=FG if p.tp else MUTED),
        Text(now or "—", style=FG if now else MUTED),
    )


def row_for_order(o: PendingOrder, now: float) -> tuple[Text, ...]:
    kind = o.type.name.lower().replace("_", " ")
    left = ""
    if o.expiration_utc:
        minutes = max(0, int((o.expiration_utc - now) // 60))
        left = f"expires {minutes}m" if minutes else "expiring"
    return (
        Text("resting", style=f"bold {YELLOW}"),
        Text(f"#{o.ticket}", style=MUTED),
        Text(o.symbol, style=f"bold {FG}"),
        Text(kind, style=f"bold {BLUE if o.type.direction is PositionType.BUY else RED}"),
        Text(f"{o.volume:g}"),
        Text(f"{o.price_open:g}"),
        Text(f"{o.sl:g}" if o.sl else "—", style=FG if o.sl else MUTED),
        Text(f"{o.tp:g}" if o.tp else "—", style=FG if o.tp else MUTED),
        Text(left or "GTC", style=MUTED),
    )


class ChartView(Static):
    """The chart, with the ticket's lines draggable by mouse.

    Only the mouse work lives here -- press, drag, let go, translated to rows. What a row
    means is the screen's (:meth:`TradingScreen.chart_grab` and friends), because only the
    screen holds the scale the picture was drawn with."""

    ALLOW_SELECT = False
    """Textual's own copy-text selection would highlight the chart on the very drag that
    moves a line. The chart is a picture, not prose: nothing to copy, nothing to select."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        screen = self.screen
        if isinstance(screen, TradingScreen) and screen.chart_grab(int(event.y)) is not None:
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        screen = self.screen
        if isinstance(screen, TradingScreen) and screen.chart_dragging:
            screen.chart_drag(int(event.y))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        screen = self.screen
        if isinstance(screen, TradingScreen) and screen.chart_dragging:
            self.release_mouse()
            screen.chart_release()
            event.stop()


class TradingScreen(Screen[None]):
    """The order ticket and the virtual master's book. Esc goes back to the Map."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("backspace", "back", "Back", show=False),
        Binding("c", "close_row", "Close / cancel", show=False),
        Binding("x", "partial_row", "Partial close", show=False),
        Binding("m", "modify_row", "Modify", show=False),
        Binding("F", "flatten", "Close everything", show=False),
        Binding("t", "timeframe", "Timeframe", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._asking: str | None = None
        """What the ask row is for: ``"partial"``, ``"modify"`` or ``"flatten"``."""
        self._ask_ticket: int | None = None
        self._ask_resting = False
        self._drawn: tuple | None = None
        self._watched = ""
        self._timeframe = CHART_TIMEFRAMES[1][1]
        """The minichart's timeframe, M5 to begin with; t cycles it."""
        self._chart_scale: Scale | None = None
        """The scale the chart was last drawn with, so the mouse can map rows to prices.
        None whenever no candles are drawn, which is also what disarms dragging."""
        self._dragging: str | None = None
        """The line being dragged: ``"sl"``, ``"tp"``, ``"entry"``, or None."""
        self._auto: dict[str, str] = {}
        """What each level field was last seeded with, so a value the person typed or
        dragged (anything different) is recognised as theirs and never reseeded."""
        self._seeded_ctx: tuple[str, str, str] | None = None
        """The (symbol, side, kind) the levels were last seeded for."""

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with VerticalScroll(id="dt-page"):
            yield Static("", id="dt-band")
            yield Static("TICKET", classes="heading")
            with Horizontal(classes="dt-row"):
                yield Static(Text("symbol ", style=MUTED), classes="dt-label")
                yield Input(id="dt-symbol", compact=True, classes="dt-symbol")
                yield Static(Text("  lots ", style=MUTED), classes="dt-label")
                yield Input(
                    value="1", id="dt-lots", compact=True, classes="dt-num", restrict=_NUMERIC
                )
                yield Static(Text("  "), classes="dt-label")
                yield Choice(_SIDES, "buy", id="dt-side")
                yield Static(Text("  "), classes="dt-label")
                yield Choice(_KINDS, "market", id="dt-kind")
            with Horizontal(classes="dt-row"):
                yield Static(Text("entry ", style=MUTED), classes="dt-label", id="dt-entry-label")
                yield Input(id="dt-entry", compact=True, classes="dt-num", restrict=_NUMERIC)
                yield Static(Text("  stop ", style=MUTED), classes="dt-label")
                yield Input(id="dt-sl", compact=True, classes="dt-num", restrict=_NUMERIC)
                yield Static(Text("  target ", style=MUTED), classes="dt-label")
                yield Input(id="dt-tp", compact=True, classes="dt-num", restrict=_NUMERIC)
                yield Static(
                    Text("  expires in ", style=MUTED), classes="dt-label", id="dt-expiry-label"
                )
                yield Input(
                    id="dt-expiry",
                    compact=True,
                    classes="dt-num",
                    restrict=_NUMERIC,
                    placeholder="min",
                )
                yield Static(Text("  "), classes="dt-label")
                yield Button("Place  enter", id="dt-place", classes="keep", compact=True)
            yield Static("", id="dt-quote")
            yield Static("", id="dt-status")
            yield Static("", id="dt-chart-head", classes="heading")
            yield ChartView("", id="dt-chart")
            yield Static("", id="dt-book-head", classes="heading")
            yield DataTable(id="dt-book", cursor_type="row")
            with Horizontal(id="dt-ask"):
                yield Static("", id="dt-ask-what")
                yield QuickInput(id="dt-ask-box", compact=True)
                yield Static(Text("  enter does it · esc leaves it", style=MUTED), classes="w")
        yield KeyBar("", id="keybar")

    def on_mount(self) -> None:
        table = self.query_one("#dt-book", DataTable)
        table.add_columns("", "#", "SYMBOL", "SIDE", "LOTS", "ENTRY", "STOP", "TARGET", "NOW")
        self.query_one("#dt-ask").display = False
        self.set_interval(0.5, self._on_timer)
        self._show_kind()
        self.refresh_page()
        self.query_one("#dt-symbol", Input).focus()

    def on_screen_resume(self) -> None:
        self._watch_symbol()

    def on_unmount(self) -> None:
        # The ticket's preview symbol is this page's; leaving the page lets the feed drop it.
        self.copier._engine.watch_direct(None)

    # --- drawing ------------------------------------------------------------------------------

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            pass

    def refresh_page(self) -> None:
        app = self.copier
        engine = app._engine
        self.query_one("#topbar", TopBar).update(app.topbar(crumb="direct trading"))

        feed_tid = engine.direct_feed()
        feed = app._cfg.display_name(feed_tid) if feed_tid is not None else None
        band = Text(spaced("direct trading"), style="bold")
        slaves = len(app._cfg.slaves)
        right = Text(f"copies to {slaves} slave{'s' if slaves != 1 else ''}", style=MUTED)
        if not app._cfg.copying_enabled:
            right = Text(
                "copying is off: d starts it, nothing copies until then", style=f"bold {YELLOW}"
            )
        self.query_one("#dt-band", Static).update(left_right(band, right))

        symbol = self.query_one("#dt-symbol", Input).value.strip().upper()
        quote = engine.direct_quote(symbol) if symbol else None
        self.query_one("#dt-quote", Static).update(quote_line(symbol, quote, feed))
        self._draw_chart(symbol, quote, feed)

        positions = engine.direct_positions()
        orders = engine.direct_orders()
        self.query_one("#dt-book-head", Static).update(
            left_right(
                Text("THE BOOK", style="bold"),
                Text(f"{len(positions)} open · {len(orders)} resting", style=MUTED),
            )
        )
        self._draw_book(positions, orders)
        self._refresh_keys()

    def _draw_book(self, positions: tuple[Position, ...], orders: tuple[PendingOrder, ...]) -> None:
        engine = self.copier._engine
        now = time.time()
        quotes = {p.symbol: engine.direct_quote(p.symbol) for p in positions}
        shape = (
            tuple((p, quotes[p.symbol]) for p in positions),
            orders,
            tuple(int((o.expiration_utc - now) // 60) for o in orders if o.expiration_utc),
        )
        if shape == self._drawn:
            return
        self._drawn = shape
        table = self.query_one("#dt-book", DataTable)
        selected = None
        if table.cursor_row is not None and 0 <= table.cursor_row < table.row_count:
            selected = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        table.clear()
        for p in positions:
            table.add_row(*row_for_position(p, quotes[p.symbol]), key=f"p{p.ticket}")
        for o in orders:
            table.add_row(*row_for_order(o, now), key=f"o{o.ticket}")
        if selected is not None:
            try:
                row = table.get_row_index(selected)
                table.move_cursor(row=row)
            except Exception:
                pass

    def _draw_chart(self, symbol: str, quote: Quote | None, feed: str | None) -> None:
        """The minichart under the ticket: the feed's bars with the ticket's own levels."""
        name = timeframe_name(self._timeframe)
        self.query_one("#dt-chart-head", Static).update(
            left_right(
                Text(f"CHART  {symbol or '—'} · {name}", style="bold"),
                Text("t changes the timeframe · drag the stop or target line", style=MUTED),
            )
        )
        target = self.query_one("#dt-chart", Static)
        self._chart_scale = None
        if not symbol:
            target.update(Text("type a symbol to chart it", style=MUTED))
            return
        rates = self.copier._engine.direct_rates(symbol, self._timeframe)
        if rates is None or rates.error is not None:
            if rates is not None and rates.error is not None:
                said, tone = rates.error, YELLOW
            elif feed is None:
                said, tone = "no feed: connect an MT5 slave to chart prices", f"bold {RED}"
            else:
                said, tone = f"waiting for {feed} to send bars…", YELLOW
            target.update(Text(said, style=tone))
            return
        width = max(30, target.content_size.width or self.size.width - 4)
        bid = quote.bid if quote is not None else None
        shown = window(rates.candles, bid, width)
        # Seeding measures off the bars alone; the drawing scale then stretches to the
        # levels, or defaults would compound off their own stretch.
        bars_scale = scale_of(shown, _CHART_HEIGHT)
        if bars_scale is not None and bid is not None:
            self._seed_levels(symbol, bars_scale, bid)
        sl = self._level("#dt-sl")
        tp = self._level("#dt-tp")
        entry = self._level("#dt-entry") if self._pending_kind() else 0.0
        self._chart_scale = scale_of(shown, _CHART_HEIGHT, (sl, tp, entry))
        target.update(
            chart(
                rates.candles,
                width=width,
                height=_CHART_HEIGHT,
                bid=bid,
                sl=sl,
                tp=tp,
                entry=entry,
                lots=self._level("#dt-lots"),
            )
        )

    def _pending_kind(self) -> bool:
        return str(self.query_one("#dt-kind", Choice).value) != "market"

    def _seed_levels(self, symbol: str, scale: Scale, bid: float) -> None:
        """A fresh ticket starts with its lines on the chart, ready to drag.

        Seeded once per (symbol, side, kind): the stop a quarter of the visible range on
        the losing side, the target the same on the winning one, a pending's entry nearer
        in on its own resting side. Only an empty field, or one still holding what was
        seeded before, is touched -- a number the person typed or dragged is theirs, and
        an emptied field stays empty until the ticket changes shape again."""
        side = str(self.query_one("#dt-side", Choice).value)
        kind = str(self.query_one("#dt-kind", Choice).value)
        ctx = (symbol, side, kind)
        if ctx == self._seeded_ctx:
            return
        self._seeded_ctx = ctx
        span = scale.hi - scale.lo
        way = 1.0 if side == "buy" else -1.0
        self._seed("sl", bid - way * 0.25 * span, scale)
        self._seed("tp", bid + way * 0.25 * span, scale)
        if kind != "market":
            # A limit rests on the pullback side of the market, a stop on the breakout
            # side: below the bid for a buy limit, above it for a buy stop, mirrored short.
            sign = -way if kind == "limit" else way
            self._seed("entry", bid + sign * 0.15 * span, scale)

    def _seed(self, name: str, price: float, scale: Scale) -> None:
        box = self.query_one(f"#dt-{name}", Input)
        current = box.value.strip()
        if box.has_focus or (current and current != self._auto.get(name)):
            return
        text = scale.fmt(price)
        box.value = text
        self._auto[name] = text

    def _level(self, selector: str) -> float:
        """What the ticket says for one level, 0.0 while it will not read as a number."""
        try:
            return float(self.query_one(selector, Input).value.strip() or 0.0)
        except ValueError:
            return 0.0

    def action_timeframe(self) -> None:
        self._timeframe = next_timeframe(self._timeframe)
        self.refresh_page()

    # --- dragging the chart's lines -----------------------------------------------------------

    @property
    def chart_dragging(self) -> bool:
        return self._dragging is not None

    def chart_grab(self, row: int) -> str | None:
        """Take hold of the line nearest a mouse press, if one is within reach."""
        scale = self._chart_scale
        if scale is None:
            return None
        held = [("sl", self._level("#dt-sl")), ("tp", self._level("#dt-tp"))]
        if self._pending_kind():
            held.append(("entry", self._level("#dt-entry")))
        lines = [
            (abs(row - scale.row(price)), what) for what, price in held if scale.grabbed(row, price)
        ]
        if not lines:
            return None
        self._dragging = min(lines)[1]
        return self._dragging

    def chart_drag(self, row: int) -> None:
        """The held line follows the mouse: the ticket's field is what actually moves."""
        scale = self._chart_scale
        if self._dragging is None or scale is None:
            return
        self.query_one(f"#dt-{self._dragging}", Input).value = scale.fmt(scale.price(row))
        self.refresh_page()

    def chart_release(self) -> None:
        if self._dragging is None:
            return
        name = {"sl": "stop", "tp": "target", "entry": "entry"}[self._dragging]
        value = self.query_one(f"#dt-{self._dragging}", Input).value
        self._dragging = None
        self._status(f"{name} dragged to {value}")

    def _refresh_keys(self) -> None:
        keys = [key_hint("esc", "back"), key_hint("enter", "place"), key_hint("t", "timeframe")]
        if self._selected_row() is not None:
            resting = self._ask_resting if self._asking else self._selected_row()[1]
            keys.append(key_hint("c", "cancel it" if resting else "close it"))
            if not resting:
                keys.append(key_hint("x", "partial close"))
            keys.append(key_hint("m", "modify"))
        if self.copier._engine.direct_positions() or self.copier._engine.direct_orders():
            keys.append(key_hint("F", "close everything"))
        self.query_one("#keybar", KeyBar).update(self.copier.keybar(keys))

    # --- the ticket -----------------------------------------------------------------------------

    def _show_kind(self) -> None:
        pending = self.query_one("#dt-kind", Choice).value != "market"
        for wid in ("#dt-entry-label", "#dt-entry", "#dt-expiry-label", "#dt-expiry"):
            self.query_one(wid).display = pending

    @on(Choice.Changed)
    def _on_choice(self) -> None:
        if self.is_mounted:
            self._show_kind()

    @on(Input.Changed, "#dt-symbol")
    def _on_symbol_typed(self) -> None:
        if self.is_mounted:
            self._watch_symbol()

    def _watch_symbol(self) -> None:
        symbol = self.query_one("#dt-symbol", Input).value.strip().upper()
        if symbol != self._watched:
            self._watched = symbol
            self.copier._engine.watch_direct(symbol or None)

    def _status(self, message: str, *, tone: str = MUTED) -> None:
        self.query_one("#dt-status", Static).update(
            Text(message, style=tone) if message else Text("")
        )

    def action_place(self) -> None:
        ticket = parse_ticket(
            self.query_one("#dt-symbol", Input).value,
            str(self.query_one("#dt-side", Choice).value),
            str(self.query_one("#dt-kind", Choice).value),
            self.query_one("#dt-lots", Input).value,
            self.query_one("#dt-entry", Input).value,
            self.query_one("#dt-sl", Input).value,
            self.query_one("#dt-tp", Input).value,
            self.query_one("#dt-expiry", Input).value,
            now=time.time(),
        )
        if isinstance(ticket, str):
            self._status(ticket, tone=f"bold {RED}")
            return
        engine = self.copier._engine
        if ticket.kind == "market":
            why = engine.place_direct_market(
                ticket.symbol, ticket.side, ticket.lots, ticket.sl, ticket.tp
            )
        else:
            why = engine.place_direct_pending(
                ticket.symbol,
                _ORDER_TYPES[(ticket.side.name.lower(), ticket.kind)],
                ticket.lots,
                ticket.entry,
                ticket.sl,
                ticket.tp,
                ticket.expiration_utc,
            )
        if why is not None:
            self._status(why, tone=f"bold {RED}")
            return
        what = "opened" if ticket.kind == "market" else "resting"
        self._status(
            f"{what}: {ticket.symbol} {ticket.side.name.lower()} {ticket.lots:g} lots; "
            "copies follow on the next tick",
            tone=GREEN,
        )
        self._drawn = None
        self.refresh_page()

    @on(Button.Pressed, "#dt-place")
    def _on_place(self) -> None:
        self.action_place()

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        if event.input.id == "dt-ask-box":
            return  # the ask row's own handler answers it
        event.stop()
        self.action_place()

    # --- managing the book ------------------------------------------------------------------

    def _selected_row(self) -> tuple[int, bool] | None:
        """The highlighted row as ``(ticket, resting)``, or None with nothing to act on."""
        table = self.query_one("#dt-book", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        if not 0 <= table.cursor_row < table.row_count:
            return None
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        if not key:
            return None
        return int(key[1:]), key[0] == "o"

    def action_close_row(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        ticket, resting = row
        engine = self.copier._engine
        why = engine.cancel_direct(ticket) if resting else engine.close_direct(ticket)
        if why is not None:
            self._status(why, tone=f"bold {RED}")
            return
        did = "cancelled" if resting else "closed"
        self._status(f"{did} #{ticket}; copies follow on the next tick", tone=GREEN)
        self._drawn = None
        self.refresh_page()

    def action_partial_row(self) -> None:
        row = self._selected_row()
        if row is None or row[1]:
            return
        self._open_ask("partial", row[0], False, f"close what % of #{row[0]}?", "50")

    def action_modify_row(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        ticket, resting = row
        engine = self.copier._engine
        if resting:
            order = next((o for o in engine.direct_orders() if o.ticket == ticket), None)
            if order is None:
                return
            prefill = f"{order.price_open:g} {order.sl:g}" + (f" {order.tp:g}" if order.tp else "")
            self._open_ask("modify", ticket, True, f"#{ticket}: entry, stop and target", prefill)
            return
        position = next((p for p in engine.direct_positions() if p.ticket == ticket), None)
        if position is None:
            return
        prefill = f"{position.sl:g}" + (f" {position.tp:g}" if position.tp else "")
        self._open_ask("modify", ticket, False, f"#{ticket}: stop and target", prefill)

    def action_flatten(self) -> None:
        engine = self.copier._engine
        if not engine.direct_positions() and not engine.direct_orders():
            return
        self._open_ask("flatten", None, False, "close every position and order? type ALL", "")

    def _open_ask(
        self, what: str, ticket: int | None, resting: bool, label: str, prefill: str
    ) -> None:
        self._asking = what
        self._ask_ticket = ticket
        self._ask_resting = resting
        self.query_one("#dt-ask-what", Static).update(Text(f"{label}  ", style=f"bold {FG}"))
        box = self.query_one("#dt-ask-box", QuickInput)
        box.value = prefill
        self.query_one("#dt-ask").display = True
        box.focus()

    def _close_ask(self) -> None:
        self._asking = None
        self._ask_ticket = None
        self.query_one("#dt-ask").display = False
        self.query_one("#dt-book", DataTable).focus()

    @on(QuickInput.Escaped, "#dt-ask-box")
    def _on_ask_escaped(self) -> None:
        self._close_ask()

    @on(Input.Submitted, "#dt-ask-box")
    def _on_ask_answered(self, event: Input.Submitted) -> None:
        event.stop()
        what, ticket = self._asking, self._ask_ticket
        resting = self._ask_resting
        typed = event.value.strip()
        engine = self.copier._engine
        why: str | None = None
        done = ""
        if what == "partial" and ticket is not None:
            numbers = parse_numbers(typed, 1)
            if numbers is None:
                self._status("give the percent to close, such as 50", tone=f"bold {RED}")
                return
            why = engine.partial_close_direct(ticket, numbers[0])
            done = f"closed {numbers[0]:g}% of #{ticket}"
        elif what == "modify" and ticket is not None and resting:
            numbers = parse_numbers(typed, 3)
            if numbers is None or len(numbers) < 2:
                self._status(
                    "give the entry and the stop, and the target if any: 1.0780 1.0730 1.0880",
                    tone=f"bold {RED}",
                )
                return
            order = next((o for o in engine.direct_orders() if o.ticket == ticket), None)
            expiry = order.expiration_utc if order is not None else 0
            why = engine.modify_direct_order(
                ticket, numbers[0], numbers[1], numbers[2] if len(numbers) > 2 else 0.0, expiry
            )
            done = f"moved #{ticket}"
        elif what == "modify" and ticket is not None:
            numbers = parse_numbers(typed, 2)
            if numbers is None:
                self._status(
                    "give the stop, and the target if any: 1.0820 1.0950", tone=f"bold {RED}"
                )
                return
            why = engine.modify_direct(ticket, numbers[0], numbers[1] if len(numbers) > 1 else 0.0)
            done = f"moved #{ticket}"
        elif what == "flatten":
            if typed.upper() != "ALL":
                self._status("that is not ALL: nothing was closed", tone=YELLOW)
                self._close_ask()
                return
            count = engine.flatten_direct()
            done = f"closed everything: {count} position(s) and order(s)"
        if why is not None:
            self._status(why, tone=f"bold {RED}")
            return
        self._close_ask()
        self._status(f"{done}; copies follow on the next tick", tone=GREEN)
        self._drawn = None
        self.refresh_page()

    def action_back(self) -> None:
        self.app.pop_screen()
