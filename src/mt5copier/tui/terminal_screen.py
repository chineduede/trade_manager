"""The per-terminal detail page: Enter on a terminal row opens it, Esc goes back.

What the one-line table row cannot hold: every open position with its live P/L and the room
left to its stop and target, how the account's margin is holding up, net exposure per symbol,
and -- on a slave -- how well copying to it is going this session.

It reads the engine the same way the main screen does, polling on a timer, and only while it
is open: the timer belongs to this screen and goes with it. All of the deciding is done in
:mod:`mt5copier.terminal_detail`; this file only draws.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Sparkline, Static

from ..config import AppConfig, Role
from ..copy_stats import TerminalStats
from ..engine import Engine, TerminalView
from ..logging_setup import get_logger
from ..presentation import Tier, badge_for_tier, format_money, row_info, tier_for_status
from ..protocol import ACCOUNT_TRADE_MODE_DEMO, Snapshot
from ..terminal_detail import (
    PositionRow,
    bar,
    exposure,
    format_duration,
    format_ms,
    format_ping,
    format_price,
    held_by_counts,
    margin_gauge,
    position_rows,
)

log = get_logger(__name__)

_TIER_COLOR: dict[Tier, str] = {
    "good": "#3fb950",
    "warning": "#d29922",
    "serious": "#f0883e",
    "critical": "#f85149",
    "muted": "#6e7681",
}
_MUTED = _TIER_COLOR["muted"]
_GOOD = _TIER_COLOR["good"]
_BAD = _TIER_COLOR["critical"]

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("symbol", "SYMBOL"),
    ("side", "SIDE"),
    ("lots", "LOTS"),
    ("open", "OPEN"),
    ("current", "CURRENT"),
    ("sl", "SL"),
    ("sl_room", "TO SL"),
    ("tp", "TP"),
    ("tp_room", "TO TP"),
    ("swap", "SWAP"),
    ("pl", "P/L"),
    ("link", "COPY"),
)
_RIGHT = {"lots", "open", "current", "sl", "sl_room", "tp", "tp_room", "swap", "pl"}

_BAR_WIDTH = 20
_EXPOSURE_ROWS = 5


def _r(text: str, style: str = "") -> Text:
    return Text(text, style=style, justify="right")


def _signed(value: float | None, digits: int) -> str:
    return "—" if value is None else f"{value:+,.{digits}f}"


class TerminalScreen(Screen[None]):
    """One terminal, in full. Holds only the terminal id; everything else is read each tick."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("backspace", "app.pop_screen", "Back", show=False),
        Binding("e", "edit_settings", "Settings"),
    ]

    DEFAULT_CSS = """
    TerminalScreen {
        padding: 0 1;
    }
    TerminalScreen #td-header {
        height: auto;
        padding: 0 1;
        margin-top: 1;
        background: $panel;
    }
    TerminalScreen #td-top {
        height: auto;
        margin-top: 1;
    }
    TerminalScreen .td-box {
        height: auto;
        min-height: 8;
        padding: 0 1;
        background: $surface;
        border: round $panel;
        border-title-color: $text-muted;
        border-title-style: bold;
    }
    TerminalScreen #td-account {
        width: 1fr;
    }
    TerminalScreen #td-health-box {
        width: 1fr;
    }
    TerminalScreen #td-latency {
        height: 3;
        margin-top: 1;
    }
    TerminalScreen #td-positions {
        height: 1fr;
        background: $surface;
        border: round $panel;
        border-title-color: $text-muted;
        border-title-style: bold;
    }
    """

    def __init__(self, engine: Engine, cfg: AppConfig, terminal_id: str) -> None:
        super().__init__()
        self._engine = engine
        self._cfg = cfg
        self.terminal_id = terminal_id

    def compose(self) -> ComposeResult:
        yield Static("", id="td-header")
        with Horizontal(id="td-top"):
            yield Static("", id="td-account", classes="td-box")
            with Vertical(id="td-health-box", classes="td-box"):
                yield Static("", id="td-health")
                yield Sparkline([], id="td-latency")
        yield DataTable(id="td-positions", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#td-positions", DataTable)
        table.border_title = "POSITIONS"
        for key, label in _COLUMNS:
            table.add_column(Text(label, justify="right") if key in _RIGHT else label, key=key)
        self.query_one("#td-account", Static).border_title = "ACCOUNT & RISK"
        self.query_one("#td-health-box").border_title = "COPY HEALTH (this session)"
        self.set_interval(0.5, self._on_timer)
        self.refresh_page()
        table.focus()

    def action_edit_settings(self) -> None:
        """This terminal's settings form, on top of this page; closing it comes back here."""
        self.app.open_settings(self.terminal_id)  # type: ignore[attr-defined]

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            log.debug("detail refresh landed during teardown; skipped")

    # --- data -----------------------------------------------------------------------------

    def refresh_page(self) -> None:
        views = self._engine.views()
        view = next((v for v in views if v.cfg.terminal_id == self.terminal_id), None)
        if view is None:
            # Forgotten while open (a rescan dropped it). Nothing to show; say so.
            self.query_one("#td-header", Static).update(
                "[b]this terminal is no longer known[/b] -- press Esc to go back"
            )
            return
        snap = view.snapshot
        is_master = view.cfg.role is Role.MASTER
        is_slave = view.cfg.role is Role.SLAVE

        self.query_one("#td-header", Static).update(self._header(view))
        self.query_one("#td-account", Static).update(self._account(snap))

        health = self.query_one("#td-health", Static)
        spark = self.query_one("#td-latency", Sparkline)
        stats = self._engine.terminal_stats(self.terminal_id)
        if is_slave:
            health.update(self._health(stats))
            spark.data = list(stats.latencies_ms)
            spark.display = bool(stats.latencies_ms)
        else:
            health.update(self._connection_only(stats, is_master))
            spark.display = False

        rows: list[PositionRow] | None = None
        if snap is not None:
            if is_master:
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
                rows = position_rows(
                    snap,
                    is_master=False,
                    copies=self._engine.open_copies(self.terminal_id) if is_slave else None,
                    master_positions=master_snap.open_positions if master_snap else None,
                    orphan_tickets=(o.slave_ticket for o in self._engine.orphans(self.terminal_id)),
                )
        self._fill_positions(rows, snap)

    # --- drawing --------------------------------------------------------------------------

    def _header(self, view: TerminalView) -> str:
        info = row_info(
            view,
            timeout_s=self._cfg.heartbeat_timeout_s,
            hold_reason=self._engine.holds().get(view.cfg.terminal_id),
            display_name=self._cfg.display_name(view.cfg.terminal_id),
        )
        snap = view.snapshot
        tier = tier_for_status(info.status_text)
        color = _TIER_COLOR[tier]
        if snap is None or snap.trade_mode is None:
            account_kind = f"[{_MUTED}]account type unknown[/]"
        elif snap.trade_mode == ACCOUNT_TRADE_MODE_DEMO:
            account_kind = f"[{_GOOD}]DEMO[/]"
        else:
            account_kind = f"[bold {_BAD}]LIVE[/]"
        first = "   ".join(
            [
                f"[b]{escape(info.label)}[/b]",
                f"[b]{escape(info.role.upper())}[/b]",
                account_kind,
                f"[{color}]{badge_for_tier(tier)} {escape(info.status_text)}[/]",
                f"ping {format_ping(snap.ping_last_us if snap else None)}",
            ]
        )
        cur = info.currency
        pl = info.floating_pl
        pl_color = _MUTED if pl is None else (_GOOD if pl >= 0 else _BAD)
        second = (
            f"balance {format_money(info.balance, cur)}  →  "
            f"equity {format_money(info.equity, cur)}   "
            f"floating [{pl_color}]{format_money(pl, cur, force_sign=True)}[/]"
        )
        bits = [first, second]
        account = [str(info.login) if info.login else "", info.server or ""]
        if snap is not None and snap.leverage:
            account.append(f"1:{snap.leverage}")
        line = "  ·  ".join(a for a in account if a)
        if line:
            bits.append(f"[{_MUTED}]{escape(line)}[/]")
        if info.hold_reason:
            bits.append(f"[{_TIER_COLOR['warning']}]held: {escape(info.hold_reason)}[/]")
        if info.paused:
            bits.append(
                f"[bold {_TIER_COLOR['warning']}]PAUSED[/] [{_MUTED}]no new trades are copied "
                "here; its open copies are still managed. p on the terminal list resumes it.[/]"
            )
        return "\n".join(bits)

    def _account(self, snap: Snapshot | None) -> str:
        if snap is None:
            return f"[{_MUTED}]not connected -- nothing to show yet[/]"
        cur = snap.currency
        gauge = margin_gauge(snap)
        level_color = _TIER_COLOR[gauge.level_tier]
        used = "—" if gauge.used_fraction is None else f"{gauge.used_fraction:.0%}"
        level = "no margin in use" if gauge.level_pct is None else f"{gauge.level_pct:,.0f}%"
        lines = [
            f"margin used  [{level_color}]{bar(gauge.used_fraction, _BAR_WIDTH)}[/] {used}"
            f"  {format_money(snap.margin, cur)} of {format_money(snap.equity, cur)} equity",
            f"margin level [{level_color}]{level}[/]   free {format_money(gauge.free, cur)}",
            "",
            "[b]net exposure[/b]",
        ]
        exp = exposure(snap.open_positions)
        if snap.open_positions is None:
            lines.append(f"[{_MUTED}]positions could not be read[/]")
        elif not exp:
            lines.append(f"[{_MUTED}]flat[/]")
        for e in exp[:_EXPOSURE_ROWS]:
            side = "long " if e.net_lots > 0 else ("short" if e.net_lots < 0 else "flat ")
            color = _GOOD if e.net_lots > 0 else (_BAD if e.net_lots < 0 else _MUTED)
            hedged = (
                f"  [{_MUTED}](gross {e.gross_lots:g})[/]"
                if e.gross_lots != abs(e.net_lots)
                else ""
            )
            lines.append(
                f"  {escape(e.symbol):<12} [{color}]{side} {abs(e.net_lots):g}[/] lots{hedged}"
            )
        if len(exp) > _EXPOSURE_ROWS:
            lines.append(f"  [{_MUTED}]+{len(exp) - _EXPOSURE_ROWS} more symbols[/]")
        return "\n".join(lines)

    @staticmethod
    def _connection_line(stats: TerminalStats) -> str:
        drops = stats.disconnects
        drop_color = _MUTED if drops == 0 else _TIER_COLOR["warning"]
        return (
            f"connected for {format_duration(stats.connected_for_s)}   "
            f"[{drop_color}]{drops} disconnect{'s' if drops != 1 else ''}[/]"
        )

    def _connection_only(self, stats: TerminalStats, is_master: bool) -> str:
        why = (
            "the master sends nothing, so there is nothing to time"
            if is_master
            else "copy health is measured on slaves only"
        )
        return f"{self._connection_line(stats)}\n\n[{_MUTED}]{why}[/]"

    def _health(self, stats: TerminalStats) -> str:
        lines = [self._connection_line(stats)]
        lines.append(
            f"latency  last {format_ms(stats.latency_last)}  "
            f"median {format_ms(stats.latency_median)}  max {format_ms(stats.latency_max)}"
        )
        if stats.slippage_points:
            worst = stats.slippage_worst or 0.0
            worst_color = _BAD if worst > 0 else _GOOD
            lines.append(
                f"slippage last {stats.slippage_last:+.1f}  avg {stats.slippage_mean:+.1f}  "
                f"worst [{worst_color}]{worst:+.1f}[/] pts  [{_MUTED}](+ is against us)[/]"
            )
        else:
            lines.append(f"slippage [{_MUTED}]no market fills yet[/]")
        refused_color = _BAD if stats.refused else _MUTED
        lines.append(
            f"[{_GOOD}]{stats.succeeded} ok[/]  [{refused_color}]{stats.refused} refused[/]  "
            f"{stats.failed_transient} transient  {stats.retried} retried  "
            f"{stats.expired} unanswered  {stats.dropped_late} late"
        )
        if stats.skipped:
            top = ", ".join(f"{escape(reason)} ×{n}" for reason, n in stats.skipped[:2])
            more = len(stats.skipped) - 2
            tail = f" +{more} more" if more > 0 else ""
            lines.append(f"[{_MUTED}]skipped {stats.skipped_total}: {top}{tail}[/]")
        if stats.latencies_ms:
            lines.append(f"[{_MUTED}]round trip per order, oldest → newest:[/]")
        return "\n".join(lines)

    def _fill_positions(self, rows: list[PositionRow] | None, snap: Snapshot | None) -> None:
        table = self.query_one("#td-positions", DataTable)
        cursor = table.cursor_row
        table.clear()
        cur = snap.currency if snap else None
        if rows is None:
            table.border_title = "POSITIONS -- could not be read"
            return
        total = sum(r.profit for r in rows if r.profit is not None)
        table.border_title = f"POSITIONS ({len(rows)})" if rows else "POSITIONS -- none open"
        if rows:
            table.border_subtitle = f"total {format_money(total, cur, force_sign=True)}"
        else:
            table.border_subtitle = None
        for r in rows:
            side_color = _GOOD if r.side == "BUY" else _BAD
            pl_color = _MUTED if r.profit is None else (_GOOD if r.profit >= 0 else _BAD)
            sl_color = _BAD if r.sl_room is not None and r.sl_room <= 0 else ""
            table.add_row(
                Text(r.symbol),
                Text(r.side, style=side_color),
                _r(f"{r.volume:g}"),
                _r(format_price(r.price_open, r.digits)),
                _r(format_price(r.price_current, r.digits)),
                _r(format_price(r.sl, r.digits), "" if r.sl else _MUTED),
                _r(_signed(r.sl_room, r.digits), sl_color),
                _r(format_price(r.tp, r.digits), "" if r.tp else _MUTED),
                _r(_signed(r.tp_room, r.digits)),
                _r(format_money(r.swap, None)),
                _r(format_money(r.profit, cur, force_sign=True), pl_color),
                Text(r.link, style=_TIER_COLOR[r.link_tier]),
                key=str(r.ticket),
            )
        if rows and cursor is not None:
            table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)
