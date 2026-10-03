"""The terminal details page, in the new look: ``enter`` on a card or chip opens it.

A cream band says which terminal it is and how it is connected; under it three ruled columns
-- its ACCOUNT, the RULE it is copied by (on the master: the slaves it feeds), and how COPYING
to it has gone this session -- and then its open positions.

Every number comes from the same place the classic page reads it (:mod:`mt5copier.terminal_detail`
and the engine), and the breakeven keys and settings are the classic page's own: this is that
page, drawn again. The functions here only decide what each line says, so the tests can read
them without starting the app.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Region
from textual.widgets import DataTable, Sparkline, Static

from ..config import AppConfig, NoStopPolicy, Role, SizingMode, TerminalConfig
from ..copy_stats import TerminalStats
from ..engine import Engine, TerminalView
from ..presentation import RowInfo
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, Snapshot
from ..terminal_detail import (
    PositionRow,
    bar,
    breakeven_rule,
    exposure,
    format_duration,
    format_ping,
    format_price,
    held_by_counts,
    margin_gauge,
    position_rows,
)
from ..tui.terminal_screen import TerminalScreen
from . import cards
from .cards import big_amount
from .style import BLUE, FG, MUTED, RED, RULED, TIER_COLOR, YELLOW, amount, key_hint, left_right
from .widgets import KeyBar, TopBar

if TYPE_CHECKING:
    from .app import NewCopierApp

# On the cream band the page's own colours would wash out, so it has darker inks of them.
INK = "#1b1813"
INK_MUTED = "#6b6252"
INK_RED = "#b0352c"
INK_BLUE = "#2f5f9e"

Row = tuple[str, Text]
"""One line of a column: what it is, and its value."""


def _v(text: str, style: str = "") -> Text:
    return Text(text, style=style)


# --- the band ----------------------------------------------------------------------------------


def role_title(cfg: TerminalConfig, number: str | None) -> str:
    """``1 · SLAVE OF A``, ``A · MASTER``, or ``NOT YET OPENED``."""
    if cfg.role is Role.MASTER:
        return "A · MASTER"
    if cfg.role is Role.SLAVE:
        return f"{number} · SLAVE OF A" if number else "SLAVE OF A"
    return "NOT YET OPENED"


def state_word(cfg: TerminalConfig, info: RowInfo, *, dry_run: bool, copying: bool) -> Text:
    """The band's first word: what the terminal is doing, as its card says it."""
    if cfg.role is Role.SLAVE:
        state = cards.slave_state(info, dry_run=dry_run, copying=copying)
        words = {
            "held": "held",
            "blocked": "held back: not a demo account",
            "paused": "paused",
        }
        text = words.get(state.kind, state.status.plain.strip("[] ").lower())
        bad = state.kind in ("held", "blocked", "down")
        return Text(text, style=f"bold {INK_RED if bad else INK}")
    if info.tier in ("good", "warning"):
        return Text("connected" if info.tier == "good" else info.status_text.lower(), style="bold")
    bad = info.tier != "muted"
    return Text(info.status_text.lower(), style=f"bold {INK_RED if bad else INK_MUTED}")


def account_line(info: RowInfo, snap: Snapshot | None) -> Text:
    """``928374 · BlackBullMarkets-Demo · demo · 1:100``."""
    out = Text(style=INK_MUTED)
    bits: list[Text] = []
    if info.login:
        bits.append(Text(str(info.login)))
    if info.server:
        bits.append(Text(info.server))
    if snap is not None and snap.trade_mode is not None:
        if snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
            bits.append(Text("demo"))
        else:
            bits.append(Text("LIVE", style=f"bold {INK_RED}"))
    if snap is not None and snap.leverage:
        bits.append(Text(f"1:{snap.leverage}"))
    return out + Text(" · ").join(bits) if bits else Text("account not read yet", style=INK_MUTED)


def band(
    title: str,
    info: RowInfo,
    snap: Snapshot | None,
    stats: TerminalStats,
    word: Text,
    *,
    width: int = 0,
) -> RenderableType:
    """The cream band across the top. ``width`` is its inside width, which decides whether the
    equity fits in big digits."""
    grid = Table.grid(expand=True, padding=(0, 3))
    grid.add_column(no_wrap=True, vertical="middle")
    grid.add_column(no_wrap=True, vertical="middle")
    grid.add_column(no_wrap=True, ratio=1, vertical="middle")
    grid.add_column(justify="right", no_wrap=True, vertical="middle")

    pl = info.floating_pl
    if pl is None:
        floating = Text("— floating", style=INK_MUTED)
    else:
        floating = Text(f"{amount(pl)} floating", style=INK_RED if pl < 0 else INK)

    status = word.copy()
    if info.tier == "good" and stats.connected_for_s is not None:
        status.append(f" · connected {format_duration(stats.connected_for_s)}", style="bold")
    ping = format_ping(snap.ping_last_us if snap else None)
    if ping != "—":
        status.append(f" · ping {ping}", style="bold")

    grid.add_row(
        Group(Text(title, style="bold"), Text(info.label, no_wrap=True)),
        big_amount(info.equity, max(0, width - 110)),
        Group(Text("equity", style=INK_MUTED), floating),
        Group(status, account_line(info, snap)),
    )
    return grid


def notes(cfg: TerminalConfig, info: RowInfo) -> Text:
    """What is wrong or unusual about it, said under the band: holds, a pause, the demo guard."""
    lines: list[Text] = []
    if info.hold_reason:
        lines.append(Text(f"held: {info.hold_reason}", style=RED))
    if info.demo_guard_blocked:
        lines.append(
            Text(
                "held back: this is not a demo account, so nothing is sent to it. "
                "l allows live trading here.",
                style=RED,
            )
        )
    if cfg.allow_live_trading:
        lines.append(Text("live trading is allowed here. l turns it off.", style=f"bold {RED}"))
    if info.paused:
        lines.append(
            Text(
                "paused: no new trades are copied here; its open copies are still managed. "
                "p resumes it.",
                style=BLUE,
            )
        )
    return Text("\n").join(lines)


# --- the columns -------------------------------------------------------------------------------


def ruled(rows: Sequence[Row]) -> Table:
    """A column's lines, a rule between each, label left and value right."""
    table = Table(
        box=RULED,
        show_header=False,
        show_edge=False,
        show_lines=True,
        pad_edge=False,
        padding=(0, 0),
        border_style=MUTED,
        expand=True,
    )
    table.add_column(style=MUTED, no_wrap=True)
    table.add_column(justify="right", no_wrap=True, overflow="ellipsis")
    for label, value in rows:
        table.add_row(label, value)
    return table


def account_rows(snap: Snapshot | None) -> list[Row]:
    if snap is None:
        return [("Account", _v("not connected yet", MUTED))]
    gauge = margin_gauge(snap)
    color = TIER_COLOR[gauge.level_tier]
    pl = (
        snap.equity - snap.balance if snap.equity is not None and snap.balance is not None else None
    )
    used = Text(bar(gauge.used_fraction, 10), style=color)
    if gauge.used_fraction is not None:
        used.append(f" {gauge.used_fraction:.0%}")
    if gauge.level_pct is None:
        level = _v("no margin in use" if snap.margin == 0 else "—", MUTED)
    else:
        level = _v(f"{gauge.level_pct:,.0f}%", color)
    return [
        ("Balance", _v(amount(snap.balance))),
        ("Floating", _v(amount(pl), RED if pl is not None and pl < 0 else "")),
        ("Equity", _v(amount(snap.equity), "bold")),
        ("Margin in use", _v(amount(snap.margin))),
        ("Margin free", _v(amount(gauge.free))),
        ("Margin used", used),
        ("Margin level", level),
    ]


def risk_text(cfg: TerminalConfig, currency: str | None) -> str:
    if cfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
        return f"{cfg.risk_cash:,g} {currency or ''}".rstrip() + " a trade"
    return f"{cfg.risk_percent:g}% of balance"


def _few(names: Sequence[str]) -> str:
    shown = ", ".join(names[:3])
    return shown + (f" +{len(names) - 3}" if len(names) > 3 else "")


def symbol_rows(cfg: TerminalConfig, example: str = "EURUSD") -> list[Row]:
    """Which of the master's symbols it takes, and the name each is traded under here: the
    prefix and suffix shown on a real symbol, ``example`` (one the master holds, if any), and
    each renamed symbol with its own line."""
    if cfg.symbol_whitelist:
        which = f"only {_few(cfg.symbol_whitelist)}"
    elif cfg.symbol_blacklist:
        which = f"all except {_few(cfg.symbol_blacklist)}"
    else:
        which = "all"
    rows: list[Row] = [("Symbols copied", _v(which))]
    if cfg.symbol_whitelist and cfg.symbol_blacklist:
        rows.append(("  never", _v(_few(cfg.symbol_blacklist))))
    # A renamed symbol keeps its own name, so the example is one the decoration applies to.
    example = example.upper()
    if example in cfg.symbol_map:
        example = next((s for s in ("EURUSD", "XAUUSD") if s not in cfg.symbol_map), example)
    if cfg.symbol_prefix or cfg.symbol_suffix:
        here = f"{cfg.symbol_prefix}{example}{cfg.symbol_suffix}"
        rows.append(("Name on this broker", _v(f"{example} is traded as {here}")))
    else:
        rows.append(("Name on this broker", _v("the same as the master's")))
    renamed = sorted(cfg.symbol_map.items())
    for n, (master, here) in enumerate(renamed[:3]):
        rows.append(("Renamed" if n == 0 else "", _v(f"{master} is traded as {here}")))
    if len(renamed) > 3:
        rows.append(("", _v(f"+{len(renamed) - 3} more (e settings)", MUTED)))
    return rows


def rule_rows(cfg: TerminalConfig, currency: str | None, example: str = "EURUSD") -> list[Row]:
    """How trades are copied to it, in the words of its settings form."""
    if cfg.no_stop_policy is NoStopPolicy.SKIP:
        no_stop = "skip the trade"
    else:
        no_stop = f"copy {cfg.no_stop_lot_multiple:g}x its lots"
    if cfg.copy_partials and cfg.copy_pending:
        partials = "followed"
    elif cfg.copy_partials:
        partials = "partials only"
    elif cfg.copy_pending:
        partials = "pending orders only"
    else:
        partials = "not followed"
    rule = breakeven_rule(cfg)
    rows: list[Row] = [
        ("Risk per trade", _v(risk_text(cfg, currency))),
    ]
    if cfg.max_lot is not None:
        rows.append(("Largest lot", _v(f"{cfg.max_lot:g}")))
    rows += [
        ("Master has no stop", _v(no_stop)),
        (
            "Direction",
            _v("reversed" if cfg.invert else "same as master", YELLOW if cfg.invert else ""),
        ),
        ("Stop and target moves", _v("followed" if cfg.copy_sltp else "not followed")),
        ("Partials, pending orders", _v(partials)),
        ("Breakeven", _v("off" if rule == "off" else f"at {rule}")),
    ]
    rows += symbol_rows(cfg, example)
    if cfg.allow_live_trading:
        rows.append(("Live trading", _v("allowed", f"bold {RED}")))
    return rows


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def slippage(value: float | None, *, colour: bool = True) -> Text:
    """Points from the master's price: ``1,175 pts worse`` in red, or better in blue."""
    if value is None:
        return _v("—", MUTED)
    if round(value) == 0:
        return _v("none")
    if value > 0:
        return _v(f"{value:,.0f} pts worse", RED if colour else "")
    return _v(f"{-value:,.0f} pts better", BLUE if colour else "")


def copying_rows(stats: TerminalStats) -> list[Row]:
    """How copying to a slave has gone since the copier started."""
    ok = stats.succeeded
    rows: list[Row] = [("Orders copied", _v(f"{ok:,}", BLUE) if ok else _v("none", MUTED))]
    if stats.skipped_total:
        rows.append(("Skipped", _v(f"{stats.skipped_total:,}")))
        reason, n = stats.skipped[0]
        rows.append(("  most often", _v(f"{reason} x{n}", MUTED)))
    else:
        rows.append(("Skipped", _v("none", MUTED)))
    trouble = (stats.refused, stats.retried, stats.dropped_late)
    if any(trouble):
        rows.append(
            (
                "Refused, retried, late",
                _v(" / ".join(f"{n:,}" for n in trouble), RED if stats.refused else ""),
            )
        )
    else:
        rows.append(("Refused, retried, late", _v("none", MUTED)))
    lost = (stats.failed_transient, stats.expired)
    rows.append(
        (
            "Failed, unanswered",
            _v(" / ".join(f"{n:,}" for n in lost), YELLOW) if any(lost) else _v("none", MUTED),
        )
    )
    delays = (stats.latency_last, stats.latency_median, stats.latency_max)
    rows.append(
        (
            "Delay, last / median / max",
            _v(" / ".join(_ms(d) for d in delays) + " ms")
            if delays[0] is not None
            else _v("—", MUTED),
        )
    )
    rows.append(("Slippage, last", slippage(stats.slippage_last)))
    rows.append(("Slippage, average", slippage(stats.slippage_mean, colour=False)))
    rows.append(("Slippage, worst", slippage(stats.slippage_worst)))
    rows.append(("Disconnects", disconnects(stats)))
    return rows


def disconnects(stats: TerminalStats) -> Text:
    n = stats.disconnects
    return _v("none", MUTED) if n == 0 else _v(f"{n:,}", YELLOW)


def connection_rows(stats: TerminalStats, snap: Snapshot | None, *, is_master: bool) -> list[Row]:
    """The master and terminals with no role send nothing, so there is no copying to report:
    only how their link is holding."""
    return [
        ("Connected for", _v(format_duration(stats.connected_for_s))),
        ("Disconnects", disconnects(stats)),
        ("Ping", _v(format_ping(snap.ping_last_us if snap else None))),
    ]


def connection_note(*, is_master: bool) -> Text:
    """Why there is no COPYING column."""
    if is_master:
        return Text(
            "The master is only read from, so there is no copying to time here.", style=MUTED
        )
    return Text("Copying is measured on slaves only.", style=MUTED)


def exposure_text(snap: Snapshot | None) -> Text:
    """``net exposure: BTCUSD long 3.11 lots``, for the positions heading."""
    out = Text("net exposure: ", style=MUTED)
    if snap is None or snap.open_positions is None:
        return out + Text("—")
    exp = exposure(snap.open_positions)
    if not exp:
        return out + Text("flat")
    bits: list[Text] = []
    for e in exp[:3]:
        side = "long" if e.net_lots > 0 else ("short" if e.net_lots < 0 else "flat")
        bit = Text(f"{e.symbol} {side} {abs(e.net_lots):g} lots")
        if e.gross_lots != abs(e.net_lots):
            bit.append(f" (gross {e.gross_lots:g})", style=MUTED)
        bits.append(bit)
    if len(exp) > 3:
        bits.append(Text(f"+{len(exp) - 3} more", style=MUTED))
    return out + Text(", ").join(bits)


# --- positions -----------------------------------------------------------------------------------


def columns(role: Role) -> list[tuple[str, str, bool]]:
    """(key, heading, right-aligned) for the positions table."""
    out = [
        ("symbol", "Symbol", False),
        ("side", "Side", False),
        ("lots", "Lots", True),
        ("open", "Open", True),
        ("now", "Now", True),
        ("stop", "Stop", False),
        ("target", "Target", False),
        ("swap", "Swap", True),
        ("pl", "P/L", True),
    ]
    if role is Role.SLAVE:
        out.append(("be", "Breakeven", False))
    out.append(("link", "On slaves" if role is Role.MASTER else "Copy of", False))
    return out


def _level(price: float, room: float | None, digits: int) -> Text:
    """``84,532.70 309.30 away``; red, and how far past, once the price has gone through it."""
    if not price:
        return _v("—", MUTED)
    out = Text(format_price(price, digits))
    if room is not None:
        if room > 0:
            out.append(f" {room:,.{digits}f} away", style=MUTED)
        else:
            out.append(f" {-room:,.{digits}f} past", style=RED)
    return out


def _right(text: str, style: str = "") -> Text:
    return Text(text, style=style, justify="right")


def position_cells(r: PositionRow, role: Role) -> list[Text]:
    pl = r.profit
    link_color = FG if r.link_tier == "good" else TIER_COLOR[r.link_tier]
    cells = [
        Text(r.symbol, style="bold"),
        Text(r.side.lower(), style=BLUE if r.side == "BUY" else RED),
        _right(f"{r.volume:g}"),
        _right(format_price(r.price_open, r.digits)),
        _right(format_price(r.price_current, r.digits)),
        _level(r.sl, r.sl_room, r.digits),
        _level(r.tp, r.tp_room, r.digits),
        _right(amount(r.swap), MUTED),
        _right(amount(pl), RED if pl is not None and pl < 0 else ""),
    ]
    if role is Role.SLAVE:
        cells.append(Text(r.breakeven, style=TIER_COLOR[r.breakeven_tier]))
    cells.append(Text(r.link, style=link_color))
    return cells


def total_cells(rows: Sequence[PositionRow], role: Role) -> list[Text]:
    total = sum(r.profit for r in rows if r.profit is not None)
    cells = [Text("") for _ in columns(role)]
    cells[0] = Text("Total", style="bold")
    at = [k for k, _, _ in columns(role)].index("pl")
    cells[at] = _right(amount(total), f"bold {RED}" if total < 0 else "bold")
    return cells


# --- the screen --------------------------------------------------------------------------------


class DetailScreen(TerminalScreen):
    """One terminal, in full. The classic page's actions (breakeven, settings) are inherited;
    the keys that act on a terminal from the Map -- p, u, l and the rest -- reach the app, which
    acts on the terminal shown here, since showing one selects it."""

    BINDINGS = [
        Binding("left", "app.step(-1)", "Other terminal", show=False, priority=True),
        Binding("right", "app.step(1)", "Other terminal", show=False, priority=True),
    ]

    def __init__(self, engine: Engine, cfg: AppConfig, terminal_id: str) -> None:
        super().__init__(engine, cfg, terminal_id)
        self._role: Role | None = None
        """The role the positions table's columns were laid out for."""

    @property
    def copier(self) -> NewCopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with VerticalScroll(id="td-page"):
            yield Static("", id="td-band")
            yield Static("", id="td-notes")
            with Horizontal(id="td-columns"):
                with Vertical(classes="td-col"):
                    yield Static("", id="td-account-head", classes="heading")
                    yield Static("", id="td-account")
                with Vertical(classes="td-col"):
                    yield Static("", id="td-rule-head", classes="heading")
                    yield Static("", id="td-rule")
                with Vertical(classes="td-col"):
                    yield Static("", id="td-copying-head", classes="heading")
                    yield Static("", id="td-copying")
                    yield Sparkline([], id="td-latency")
            yield Static("", id="td-positions-head", classes="heading")
            yield DataTable(id="td-positions", cursor_type="row")
        yield KeyBar("", id="keybar")

    def on_mount(self, event: events.Mount | None = None) -> None:
        # Textual runs ``on_mount`` up the class tree; the classic page's builds its own widgets.
        if event is not None:
            event.prevent_default()
        self.set_interval(0.5, self._on_timer)
        self.refresh_page()
        self.query_one("#td-positions", DataTable).focus()

    def show(self, terminal_id: str) -> None:
        """Show another terminal in place: what ←→ and the number keys do here."""
        if terminal_id == self.terminal_id:
            return
        self.terminal_id = terminal_id
        self._role = None
        self.refresh_page()

    def menu_anchor(self) -> Region:
        """Where the action menu hangs from: the band's left end."""
        band_region = self.query_one("#td-band").region
        return Region(band_region.x, band_region.y, 30, band_region.height)

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            pass

    def refresh_page(self) -> None:
        app = self.copier
        views = self._engine.views()
        view = next((v for v in views if v.cfg.terminal_id == self.terminal_id), None)
        self.query_one("#topbar", TopBar).update(
            app.topbar(crumb=self._cfg.display_name(self.terminal_id) if view else None)
        )
        if view is None:
            # Forgotten while open (a rescan dropped it). Nothing to show; say so.
            self.query_one("#td-band", Static).update(
                Text("This terminal is no longer known. Press esc to go back.", style="bold")
            )
            return
        cfg = view.cfg
        snap = view.snapshot
        holds = self._engine.holds()
        info = app._info(view, holds)
        stats = self._engine.terminal_stats(self.terminal_id)
        numbers = app._terminal_numbers()

        word = state_word(cfg, info, dry_run=self._cfg.dry_run, copying=self._cfg.copying_enabled)
        band_widget = self.query_one("#td-band", Static)
        band_widget.update(
            band(
                role_title(cfg, numbers.get(self.terminal_id)),
                info,
                snap,
                stats,
                word,
                width=band_widget.content_size.width,
            )
        )
        said = notes(cfg, info)
        notes_widget = self.query_one("#td-notes", Static)
        notes_widget.display = bool(said.plain)
        notes_widget.update(said)

        self._head("#td-account-head", "ACCOUNT", "")
        self.query_one("#td-account", Static).update(ruled(account_rows(snap)))

        if cfg.role is Role.MASTER:
            slaves = [v for v in views if v.cfg.role is Role.SLAVE]
            self._head("#td-rule-head", "COPIED TO", f"{len(slaves)} slaves")
            self.query_one("#td-rule", Static).update(ruled(self._slave_rows(slaves, numbers)))
        else:
            self._head("#td-rule-head", "RULE", "e change", key="e")
            self.query_one("#td-rule", Static).update(
                ruled(rule_rows(cfg, snap.currency if snap else None, self._example(views)))
            )

        spark = self.query_one("#td-latency", Sparkline)
        if cfg.role is Role.SLAVE:
            self._head("#td-copying-head", "COPYING", "this session")
            self.query_one("#td-copying", Static).update(ruled(copying_rows(stats)))
            spark.data = list(stats.latencies_ms)
            spark.display = bool(stats.latencies_ms)
            spark.border_title = "delay of each order, oldest first"
        else:
            self._head("#td-copying-head", "CONNECTION", "this session")
            is_master = cfg.role is Role.MASTER
            self.query_one("#td-copying", Static).update(
                Group(
                    ruled(connection_rows(stats, snap, is_master=is_master)),
                    Text(""),
                    connection_note(is_master=is_master),
                )
            )
            spark.display = False

        self._fill(cfg, snap, views)
        self._refresh_keybar(cfg)

    @staticmethod
    def _example(views: list[TerminalView]) -> str:
        """A symbol the master holds, to show how its name changes here; EURUSD if none."""
        master = next((v for v in views if v.cfg.role is Role.MASTER), None)
        held = master.snapshot.open_positions if master and master.snapshot else None
        return held[0].symbol if held else "EURUSD"

    def _head(self, widget: str, title: str, right: str, *, key: str | None = None) -> None:
        side = Text(style=MUTED)
        if key is not None and right.startswith(key + " "):
            side.append(key, style=f"bold underline {FG}")
            side.append(right[len(key) :])
        else:
            side.append(right)
        self.query_one(widget, Static).update(left_right(Text(title, style="bold"), side))

    def _slave_rows(self, slaves: list[TerminalView], numbers: dict[str, str]) -> list[Row]:
        holds = self._engine.holds()
        rows: list[Row] = []
        for v in slaves:
            tid = v.cfg.terminal_id
            state = cards.slave_state(
                self.copier._info(v, holds),
                dry_run=self._cfg.dry_run,
                copying=self._cfg.copying_enabled,
            )
            name = self._cfg.display_name(tid)[:24]
            rows.append((f"{numbers.get(tid, '')} {name}".strip(), state.status))
        return rows or [("", _v("no slaves yet", MUTED))]

    def _fill(self, cfg: TerminalConfig, snap: Snapshot | None, views: list[TerminalView]) -> None:
        table = self.query_one("#td-positions", DataTable)
        if self._role is not cfg.role:
            self._role = cfg.role
            table.clear(columns=True)
            for key, label, right in columns(cfg.role):
                table.add_column(
                    Text(label, style=f"bold {FG}", justify="right" if right else "left"), key=key
                )

        rows: list[PositionRow] | None = None
        if snap is not None:
            if cfg.role is Role.MASTER:
                slaves = [v for v in views if v.cfg.role is Role.SLAVE]
                rows = position_rows(
                    snap,
                    is_master=True,
                    held_by=held_by_counts(
                        self._engine.open_copies(v.cfg.terminal_id) for v in slaves
                    ),
                    slave_count=len(slaves),
                )
            else:
                master = next((v for v in views if v.cfg.role is Role.MASTER), None)
                master_snap = master.snapshot if master is not None else None
                is_slave = cfg.role is Role.SLAVE
                rows = position_rows(
                    snap,
                    is_master=False,
                    copies=self._engine.open_copies(self.terminal_id) if is_slave else None,
                    breakeven=breakeven_rule(cfg),
                    master_positions=master_snap.open_positions if master_snap else None,
                    orphan_tickets=(o.slave_ticket for o in self._engine.orphans(self.terminal_id)),
                )

        if rows is None:
            heading = "POSITIONS · could not be read" if snap is not None else "POSITIONS"
        else:
            heading = f"POSITIONS · {len(rows)}"
        self.query_one("#td-positions-head", Static).update(
            left_right(Text(heading, style="bold"), exposure_text(snap))
        )

        cursor = table.cursor_row
        table.clear()
        self._rows = rows or []
        for r in self._rows:
            table.add_row(*position_cells(r, cfg.role), key=str(r.ticket))
        if self._rows:
            table.add_row(*total_cells(self._rows, cfg.role), key="total")
            table.move_cursor(row=min(cursor, len(self._rows) - 1), animate=False)
        table.display = bool(self._rows)

    def _refresh_keybar(self, cfg: TerminalConfig) -> None:
        app = self.copier
        keys = [key_hint("space", "actions", primary=True), key_hint("esc", "back to map")]
        keys.append(key_hint("←→", "other terminal"))
        if cfg.role is not Role.MASTER:
            keys.append(key_hint("e", "settings"))
        if cfg.role is Role.SLAVE:
            keys.append(key_hint("p", "resume" if cfg.paused else "pause"))
            if any(r.master_ticket is not None for r in self._rows):
                keys.append(key_hint("b B", "breakeven"))
        if cfg.role is not Role.UNASSIGNED:
            keys.append(key_hint("u", "unassign"))
        keys.append(key_hint("tab", "log"))
        keys.append(key_hint("?", "all keys"))
        self.query_one("#keybar", KeyBar).update(app.keybar(keys))
