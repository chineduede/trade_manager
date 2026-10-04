"""Textual UI: the terminal list, role assignment, and live status.

The UI owns no MT5 state of its own. It polls :meth:`Engine.views` on a timer and renders
whatever it finds, which keeps the render path synchronous and free of cross-thread message
passing -- the engine's drain thread writes state, the timer reads it. Textual's
``post_message`` from a background thread would also work, but polling a latest-wins state map
is simpler and cannot queue up a backlog of stale frames when many terminals report at once.

Discovery runs on a Textual thread worker rather than inline: ``psutil.process_iter`` walks
every process on the machine and can take a second or two under load, which would visibly
freeze the event loop.

Layout: a sidebar (brand, mode, page links, the handful of keys worth memorising) beside one of
two pages -- TERMINALS, the table grouped into master / slaves / unassigned with a panel for the
selected terminal under it, and LOG, the tail of the log file. The table's grouping is built
from the same :mod:`mt5copier.presentation` view-model the tests pin down, so what a row says
is decided there and only drawn here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.theme import Theme
from textual.widgets import ContentSwitcher, DataTable, Footer, RichLog, Static
from textual.widgets.data_table import CellDoesNotExist

from .. import config as config_mod
from .. import role_actions
from ..config import AppConfig, Role, TerminalConfig
from ..discovery import discover_terminals
from ..engine import Engine, TerminalView
from ..logging_setup import get_logger
from ..paths import config_path, log_dir
from ..presentation import (
    RowInfo,
    Tier,
    badge_for_tier,
    format_money,
    log_line_tier,
    read_log_tail,
    row_info,
    slave_badge,
)
from ..reconcile import OpenCopy
from ..settings_edit import (
    LIVE_OVERRIDE,
    EditOutcome,
    advisories,
    copies_word,
    describe_changes,
    summarize_slave,
)
from .live_override_screen import LiveOverrideScreen
from .orphan_screen import OrphanConfirmScreen
from .settings_screen import SettingsScreen
from .terminal_screen import TerminalScreen

log = get_logger(__name__)

# --- palette ---------------------------------------------------------------------------------
# One set of colors, used both by the Textual theme (for the CSS) and by the Rich styles inside
# table cells, which cannot read CSS variables.

_BG = "#0a0e13"
_SURFACE = "#0f141a"
_PANEL = "#151c24"
_FG = "#c9d1d9"
_MUTED = "#6e7681"
_ACCENT = "#39c5cf"
_GOOD = "#3fb950"
_WARN = "#d29922"
_SERIOUS = "#f0883e"
_CRITICAL = "#f85149"
_BUTTON = "#1c2632"

_TIER_COLOR: dict[Tier, str] = {
    "good": _GOOD,
    "warning": _WARN,
    "serious": _SERIOUS,
    "critical": _CRITICAL,
    "muted": _MUTED,
}

THEME = Theme(
    name="copier",
    primary=_ACCENT,
    secondary="#5b8db8",
    accent=_ACCENT,
    warning=_WARN,
    error=_CRITICAL,
    success=_GOOD,
    foreground=_FG,
    background=_BG,
    surface=_SURFACE,
    panel=_PANEL,
    dark=True,
)

# --- the table -------------------------------------------------------------------------------

_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("terminal", "TERMINAL", 29),
    ("role", "ROLE", 12),
    ("status", "STATUS", 19),
    ("balance", "BALANCE", 13),
    ("equity", "EQUITY", 13),
    ("pl", "FLOATING P/L", 13),
    ("risk", "RISK", 8),
    ("action", "ACTIONS", 11),
    ("pause", "", 4),
    ("settings", "", 4),
)

# Emoji with the U+FE0F variation selector where the base character is text-presentation by
# default (the gear): without it, terminals disagree on whether the glyph takes one cell or two
# and the table's columns drift. With it, Rich and Windows Terminal both say two.
_ICON_MASTER = "\U0001f451"  # crown
_ICON_SLAVE = "\U0001f517"  # link
_ICON_BLOCKED = "\U0001f512"  # lock
_ICON_SETTINGS = "⚙️"  # gear
_ICON_PAUSE = "\u23f8\ufe0f"  # pause
_ICON_RESUME = "\u25b6\ufe0f"  # play

_SECTION = "§"
"""Prefix of every row key that is not a terminal: section headings and their hints. The
cursor steps over them, and a click on one does nothing."""

_ACTION_ROWS = 6
"""Rows of detection history on screen. The full history is in the engine and the log file."""

_RESULT_ROWS = 3
"""Order results shown for the selected terminal. Few, because the detail panel is short and
the status bar carries the running failure count."""

_LOG_LINES = 500


def _right(text: str, style: str = "") -> Text:
    return Text(text, style=style, justify="right")


def _button(label: str, *, color: str = _FG, enabled: bool = True) -> Text:
    """A cell drawn as a small button."""
    return Text(f" {label} ", style=f"{color if enabled else _MUTED} on {_BUTTON}")


def row_action(cfg: AppConfig, terminal: TerminalConfig) -> str:
    """What the ACTIONS button on this terminal's row does: "master", "slave" or "unassign".

    Roles are set explicitly (see :mod:`mt5copier.role_actions`), so a row only ever offers the
    one step that can come next: while there is no master every unassigned terminal offers to
    become it; once there is one they offer to become slaves; anything with a role offers to
    give it up. Replacing the master is therefore unassign, then assign -- never one click.
    """
    if terminal.role is not Role.UNASSIGNED:
        return "unassign"
    return "master" if cfg.master is None else "slave"


class TerminalTable(DataTable[Text]):
    """A row-cursor table that also says *which cell* was clicked.

    A row-cursor :class:`DataTable` only ever reports whole rows: a click moves the cursor, a
    second click on the same row selects it, and ``CellSelected`` is never sent at all. The
    ACTIONS and settings cells are buttons, so the column has to come from the click itself.
    """

    class Clicked(Message):
        def __init__(self, row_key: str, column_key: str) -> None:
            super().__init__()
            self.row_key = row_key
            self.column_key = column_key

    async def _on_click(self, event: events.Click) -> None:
        # Not a replacement for DataTable's own handler: Textual dispatches to every class in
        # the MRO, so the base one still runs afterwards and moves the cursor. Calling super()
        # here would run it twice.
        meta = event.style.meta
        row, column = meta.get("row"), meta.get("column")
        if not isinstance(row, int) or not isinstance(column, int) or row < 0 or column < 0:
            return
        try:
            row_key = self.ordered_rows[row].key.value
            column_key = self.ordered_columns[column].key.value
        except IndexError:
            return
        if row_key is not None and column_key is not None:
            self.post_message(self.Clicked(row_key, column_key))

    BINDINGS = [
        # DataTable already binds Enter, but hidden. Shown here so the footer says what it does.
        Binding("enter", "select_cursor", "Details"),
    ]

    class OpenRequested(Message):
        """Enter was pressed on a row: open that terminal's detail page."""

    def action_select_cursor(self) -> None:
        # Only the key reaches here. A mouse click selects through its own path, so clicking
        # a button cell twice can never open the page by accident.
        super().action_select_cursor()
        self.post_message(self.OpenRequested())


class NavItem(Static):
    """One sidebar link. Clicking it does what its number key does."""

    def __init__(self, label: str, key: str, page: str) -> None:
        super().__init__(f"{label}  [dim]{key}[/dim]", id=f"nav-{page}", classes="nav")
        self.page = page

    def on_click(self) -> None:
        self.app.action_show_page(self.page)  # type: ignore[attr-defined]


class CopierApp(App[None]):
    """Discover, designate, observe -- and, from Stage 2c, actually copy.

    **Whether an order can leave this process now depends on one setting.** With ``dry_run``
    true, the default, the panel reports what *would* be copied and nothing is sent. With it
    false, the same decisions are dispatched to the slaves. The sidebar and status bar say which
    of the two is happening, because a copier that looks identical in both modes is one nobody
    can trust.
    """

    CSS_PATH = "app.tcss"
    TITLE = "MT5 Trade Copier"
    SUB_TITLE = "Stage 2c - live copying"

    # The dialogs that guard open copies and the demo-only guard, and the settings form. Named
    # here so the new UI can draw them its own way; what they ask, and what is done with the
    # answer, stays theirs.
    ORPHAN_SCREEN: type[OrphanConfirmScreen] = OrphanConfirmScreen
    LIVE_OVERRIDE_SCREEN: type[LiveOverrideScreen] = LiveOverrideScreen
    SETTINGS_SCREEN: type[SettingsScreen] = SettingsScreen

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    """Below 110 columns the sidebar goes, so the table keeps the width."""

    BINDINGS = [
        Binding("m", "set_master", "Master"),
        Binding("s", "set_slave", "Slave"),
        Binding("u", "unassign", "Unassign"),
        Binding("e", "edit_settings", "Settings"),
        Binding("d", "toggle_detection", "Start/stop"),
        Binding("f9", "panic", "PANIC"),
        Binding("question_mark", "toggle_help", "All keys"),
        Binding("escape", "hide_help_panel", "Close keys", show=False),
        Binding("q", "quit", "Quit"),
        Binding("l", "live_override", "Live override", show=False),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("c", "reconnect", "Reconnect", show=False),
        Binding("ctrl+r", "restart_worker", "Restart worker", show=False),
        Binding("ctrl+l", "clear_detection", "Clear ledger", show=False),
        Binding("o", "acknowledge_orphans", "Ack orphans", show=False),
        Binding("p", "toggle_pause", "Pause/resume slave", show=False),
        Binding("1", "show_page('terminals')", "Terminals page", show=False),
        Binding("2", "show_page('log')", "Log page", show=False),
    ]

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self._engine = Engine(cfg)
        self._row_ids: list[str] = []
        """Terminal ids, in the order they appear in the table."""
        self._row_keys: list[str] = []
        """Every table row's key, section rows included -- index is the table row."""
        self._last_row = 0
        self._log_stamp: tuple[int, float] | None = None

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("MT5 COPIER", id="brand")
                yield Static("", id="mode")
                yield NavItem("TERMINALS", "1", "terminals")
                yield NavItem("LOG", "2", "log")
                yield Static("", id="spacer")
                yield Static(
                    "[b]m[/b] master   [b]s[/b] slave\n"
                    "[b]u[/b] unassign [b]e[/b] settings\n"
                    "[b]p[/b] pause/resume a slave\n"
                    "[b]d[/b] start/stop copying\n"
                    "[b]Enter[/b] terminal details\n"
                    "[b]F9[/b] panic halt\n"
                    "[b]?[/b] every key",
                    id="keys",
                )
            with Vertical(id="main"):
                yield Static("", id="statusbar")
                with ContentSwitcher(initial="page-terminals", id="pages"):
                    with Vertical(id="page-terminals"):
                        yield TerminalTable(id="terminals", cursor_type="row", cell_padding=1)
                        yield Static("", id="actions")
                        yield Static("", id="detail")
                    with Vertical(id="page-log"):
                        yield RichLog(id="log", wrap=False, markup=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = THEME.name
        table = self.query_one("#terminals", TerminalTable)
        table.border_title = "TERMINALS"
        self.query_one("#actions", Static).border_title = "ACTIVITY"
        self.query_one("#log", RichLog).border_title = "LOG"
        for key, label, width in _COLUMNS:
            justified: str | Text = (
                Text(label, justify="right") if key in ("balance", "equity", "pl") else label
            )
            table.add_column(justified, key=key, width=width)
        self._engine.start()
        self._rebuild_rows()
        self.set_interval(0.5, self._on_timer)
        self._refresh()
        self._mark_nav("terminals")

    def on_unmount(self) -> None:
        self._engine.stop()

    # --- pages ------------------------------------------------------------------------------

    def action_show_page(self, page: str) -> None:
        self.query_one("#pages", ContentSwitcher).current = f"page-{page}"
        self._mark_nav(page)
        if page == "log":
            self._log_stamp = None
            self._refresh_log()
            self.query_one("#log", RichLog).focus()
        else:
            self.query_one("#terminals", TerminalTable).focus()

    def _mark_nav(self, page: str) -> None:
        for item in self.query(NavItem):
            item.set_class(item.page == page, "-active")

    # --- rendering --------------------------------------------------------------------------

    def _views(self) -> list[TerminalView]:
        return self._engine.views()

    def _info(self, v: TerminalView, holds: dict[str, str]) -> RowInfo:
        return row_info(
            v,
            timeout_s=self._cfg.heartbeat_timeout_s,
            hold_reason=holds.get(v.cfg.terminal_id),
            display_name=self._cfg.display_name(v.cfg.terminal_id),
        )

    @staticmethod
    def _layout(views: list[TerminalView]) -> list[tuple[str, TerminalView | None, str]]:
        """The table's rows, top to bottom: (key, view or None for a section row, section text).

        Every group is shown even when empty, with a hint in place of rows, so the next step is
        always written on the screen rather than remembered.
        """
        master = [v for v in views if v.cfg.role is Role.MASTER]
        slaves = [v for v in views if v.cfg.role is Role.SLAVE]
        unassigned = [v for v in views if v.cfg.role is Role.UNASSIGNED]
        rows: list[tuple[str, TerminalView | None, str]] = [(f"{_SECTION}master", None, "MASTER")]
        if master:
            rows += [(v.cfg.terminal_id, v, "") for v in master]
        else:
            rows.append((f"{_SECTION}master-hint", None, "none yet: select a terminal, press m"))
        rows.append((f"{_SECTION}slaves", None, f"SLAVES ({len(slaves)})"))
        if slaves:
            rows += [(v.cfg.terminal_id, v, "") for v in slaves]
        else:
            hint = "none yet: select a terminal, press s"
            rows.append(
                (f"{_SECTION}slaves-hint", None, hint if master else "assign a master first")
            )
        rows.append((f"{_SECTION}unassigned", None, f"UNASSIGNED ({len(unassigned)})"))
        rows += [(v.cfg.terminal_id, v, "") for v in unassigned]
        return rows

    def _rebuild_rows(self) -> None:
        """Rebuild the table from scratch. Only called when the rows or their grouping change.

        The cursor follows the *terminal* it was on, not the row number: assigning a role moves
        a terminal into another group, and the cursor going with it is what makes the next key
        press act on the terminal the person was just looking at.
        """
        table = self.query_one("#terminals", TerminalTable)
        selected = self._selected_id()
        old_row = table.cursor_row
        table.clear()
        views = self._views()
        holds = self._engine.holds()
        layout = self._layout(views)
        self._row_keys = [key for key, _, _ in layout]
        self._row_ids = [key for key, v, _ in layout if v is not None]
        for key, v, text in layout:
            if v is None:
                table.add_row(*self._section_cells(key, text), key=key)
            else:
                table.add_row(*self._cells(v, self._info(v, holds)), key=key)
        if selected in self._row_keys:
            target: int | None = self._row_keys.index(selected)
        else:
            target = self._nearest_terminal_row(max(old_row, 0), forward=True)
        if target is not None:
            self._last_row = target
            table.move_cursor(row=target)

    def _section_cells(self, key: str, text: str) -> tuple[Text, ...]:
        hint = key.endswith("-hint")
        style = f"italic {_MUTED}" if hint else f"bold {_ACCENT}"
        first = Text(("  " if hint else "") + text, style=style, no_wrap=True, overflow="ellipsis")
        return (first, *(Text("") for _ in _COLUMNS[1:]))

    def _cells(self, v: TerminalView, row: RowInfo) -> tuple[Text, ...]:
        cfg = v.cfg
        role = cfg.role

        # A slave on a real-money account the demo-only guard is holding back: connected, looks
        # healthy, and copies nothing. It gets a lock by the name and its own status, so it can
        # never pass for a working slave -- and the status says the key that fixes it.
        blocked = row.demo_guard_blocked
        name = Text("  " if role is Role.SLAVE else "", no_wrap=True)
        if blocked:
            name.append(f"{_ICON_BLOCKED} ")
        name.append(row.label)
        name.overflow = "ellipsis"
        if not cfg.exe_path:
            name.stylize(f"strike {_MUTED}")
        elif blocked:
            name.stylize(f"bold {_SERIOUS}")
        elif role is not Role.UNASSIGNED:
            name.stylize("bold")

        if role is Role.MASTER:
            role_cell = Text(" MASTER ", style=f"bold {_BG} on {_FG}")
        elif role is Role.SLAVE and cfg.paused:
            role_cell = Text(" PAUSED ", style=f"bold {_BG} on {_TIER_COLOR['warning']}")
        elif role is Role.SLAVE:
            role_cell = Text(" SLAVE ", style=f"bold {_FG} on {_BUTTON}")
        else:
            role_cell = Text(" UNASSIGNED ", style=f"{_MUTED}")

        color = _TIER_COLOR.get(row.tier, _MUTED)
        status = Text()
        if blocked:
            status.append("[BLOCKED]", style=f"bold {_BG} on {_SERIOUS}")
            status.append(" press l", style=f"bold {_SERIOUS}")
        else:
            status.append(badge_for_tier(row.tier), style=f"bold {_BG} on {color}")
            status.append(" ")
            status.append(row.status_text, style=color)

        pl_color = _TIER_COLOR.get(row.floating_pl_tier or "muted", _MUTED)
        action = row_action(self._cfg, cfg)
        if action == "master":
            button = _button(f"{_ICON_MASTER} Master", color=_ACCENT, enabled=bool(cfg.exe_path))
        elif action == "slave":
            button = _button(f"{_ICON_SLAVE} Slave", enabled=bool(cfg.exe_path))
        else:
            button = _button("Unassign", color=_CRITICAL)
        return (
            name,
            role_cell,
            status,
            _right(format_money(row.balance, row.currency)),
            _right(format_money(row.equity, row.currency)),
            _right(format_money(row.floating_pl, row.currency, force_sign=True), pl_color),
            Text(cfg.risk_label() if role is Role.SLAVE else ""),
            button,
            # Only a slave can be paused, and the master has nothing to set: both cells are
            # simply absent where they would do nothing.
            _button(_ICON_RESUME if cfg.paused else _ICON_PAUSE) if role is Role.SLAVE else Text(),
            Text() if role is Role.MASTER else _button(_ICON_SETTINGS),
        )

    def _on_timer(self) -> None:
        """The periodic redraw, which has to survive the app being torn down beneath it.

        On the way out Textual removes the screens' widgets before it stops the timers, and a
        tick that lands in that gap finds nothing to draw into. Left alone the query raises
        inside the timer and takes the exit with it. Only this path is forgiving: every direct
        call of :meth:`_refresh` still fails loudly, so a mistyped widget id cannot hide here.
        """
        try:
            self._refresh()
            self._refresh_log()
        except (NoMatches, ScreenStackError):
            log.debug("refresh tick landed during teardown; skipped")

    def _refresh(self) -> None:
        views = self._views()
        if [key for key, _, _ in self._layout(views)] != self._row_keys:
            self._rebuild_rows()
            views = self._views()

        table = self.query_one("#terminals", TerminalTable)
        holds = self._engine.holds()
        for v in views:
            cells = self._cells(v, self._info(v, holds))
            for (column, _, _), cell in zip(_COLUMNS, cells, strict=True):
                try:
                    table.update_cell(v.cfg.terminal_id, column, cell, update_width=False)
                except CellDoesNotExist:  # row vanished between listing and update
                    self._rebuild_rows()
                    return

        self._refresh_statusbar()
        self._refresh_actions()
        self._refresh_detail()

    def _refresh_log(self) -> None:
        """Re-read the log tail, but only when the page is showing and the file has changed."""
        if self.query_one("#pages", ContentSwitcher).current != "page-log":
            return
        path = log_dir() / "copier.log"
        try:
            stat = path.stat()
            stamp: tuple[int, float] | None = (stat.st_size, stat.st_mtime)
        except OSError:
            stamp = None
        if stamp == self._log_stamp and self._log_stamp is not None:
            return
        self._log_stamp = stamp
        panel = self.query_one("#log", RichLog)
        panel.clear()
        text = read_log_tail(path, max_lines=_LOG_LINES)
        if not text:
            panel.write(Text("nothing logged yet", style=f"italic {_MUTED}"))
            return
        for line in text.splitlines():
            tier = log_line_tier(line)
            panel.write(Text(line, style=_TIER_COLOR[tier] if tier and tier != "good" else ""))

    def _refresh_actions(self) -> None:
        """Render the newest intended actions, newest last.

        Skipped actions are shown too, dimmed, with their reason. A copier that silently does
        nothing is indistinguishable from one that is broken, so a decision not to copy has to
        be as visible as a decision to copy.
        """
        panel = self.query_one("#actions", Static)
        recorded = self._engine.recent_actions(limit=_ACTION_ROWS)
        blocked = self._engine.blocked_reason
        if blocked is not None and self._cfg.copying_enabled:
            # Paused detection looks exactly like an idle master unless it is said out loud.
            panel.update(f"[bold yellow]DETECTION PAUSED:[/] {escape(blocked)}")
            return

        labels = {t.terminal_id: t.label for t in self._cfg.terminals.values()}
        # A hold is one slave going quiet while the others carry on, which is easy to miss --
        # so it is prepended rather than replacing the panel the way a global pause does.
        held = [
            f"[bold red]HELD[/] {escape(labels.get(tid, tid[:8]))}: {escape(reason)}"
            f"  [dim](press o to acknowledge)[/dim]"
            for tid, reason in sorted(self._engine.holds().items())
        ]

        if not recorded:
            if held:
                panel.update("\n".join(held))
            elif self._cfg.copying_enabled:
                panel.update("[dim]watching the master... no activity seen yet[/dim]")
            elif self._cfg.master is None:
                panel.update("[dim]1. select a terminal and press m to make it the master[/dim]")
            elif not self._cfg.slaves:
                panel.update("[dim]2. select a terminal and press s to make it a slave[/dim]")
            else:
                panel.update("[dim]3. press d to start copying[/dim]")
            return

        lines = list(held)
        for rec in recorded:
            a = rec.action
            stamp = time.strftime("%H:%M:%S", time.localtime(rec.ts))
            who = labels.get(a.terminal_id, a.terminal_id[:8])[:16]
            text = escape(f"{stamp}  {who:<16}  {a.describe()}")
            if not a.executable:
                lines.append(f"[dim]{text}[/dim]")
            elif a.unprotected:
                lines.append(f"[yellow]{text}[/yellow]")
            else:
                lines.append(text)
        panel.update("\n".join(lines))

    def _refresh_statusbar(self) -> None:
        h = self._engine.health()
        master = self._cfg.master
        bar = self.query_one("#statusbar", Static)
        mode = self.query_one("#mode", Static)

        dry = self._cfg.dry_run
        # The subtitle carries the mode too, because the status bar scrolls out of a small
        # terminal and "is this live?" must never need a scroll to answer.
        self.sub_title = "Stage 2c - dry run" if dry else "Stage 2c - LIVE, orders are sent"
        mode.update(
            "DRY RUN\n[dim]nothing is sent[/dim]" if dry else "LIVE\n[dim]orders are sent[/dim]"
        )
        mode.set_classes("dry" if dry else "live")

        if self._cfg.copying_enabled:
            copy_state = "DETECTING (dry run - nothing sent)" if dry else "COPYING LIVE"
            bar.set_classes("copying" if dry else "copying live")
        else:
            copy_state = "detection off"
            bar.set_classes("halted" if master else "")

        # Said in the bar, not only in the dialog: a terminal with the guard lifted is the one
        # that can trade a funded account, and it stays lifted across a role change.
        lifted = [
            t.label or t.terminal_id[:8]
            for t in self._cfg.terminals.values()
            if t.allow_live_trading
        ]
        holds = self._engine.holds()
        blocked = [
            info.label
            for info in (self._info(v, holds) for v in self._views())
            if info.demo_guard_blocked
        ]
        failed = self._engine.failed_total
        sep = "  [dim]│[/dim]  "
        bar.update(
            f"[b]{copy_state}[/b]"
            + (f"{sep}[bold red]NON-DEMO ALLOWED: {escape(', '.join(lifted))}[/]" if lifted else "")
            + f"{sep}master: {escape(master.label) if master else '(none)'}"
            f"{sep}slaves: {len(self._cfg.slaves)}"
            f"{sep}live {h['live']}/{h['supervised']}"
            + (f"{sep}[bold red]STALE {h['stale']}[/]" if h["stale"] else "")
            + (
                f"{sep}[bold red]HELD {len(self._engine.holds())}[/]"
                if self._engine.holds()
                else ""
            )
            + (f"{sep}[bold red]FAILED {failed}[/]" if failed else "")
            + (
                f"{sep}[bold {_SERIOUS}]{_ICON_BLOCKED} BLOCKED {len(blocked)}: "
                f"live account, press l[/]"
                if blocked
                else ""
            )
            + f"{sep}intents {self._engine.action_total}"
            f" / tracking {self._engine.believed_copies()}"
        )

    def _refresh_detail(self) -> None:
        v = self._selected()
        detail = self.query_one("#detail", Static)
        if v is None:
            detail.border_title = None
            detail.update("[dim]select a terminal to see its details[/dim]")
            return
        cfg, snap = v.cfg, v.snapshot
        info = self._info(v, self._engine.holds())
        detail.border_title = escape(info.label)

        account = [f"{info.login}" if info.login else "no login yet"]
        if info.server:
            account.append(info.server)
        if snap is not None:
            if snap.company:
                account.append(snap.company)
            if snap.leverage:
                account.append(f"1:{snap.leverage}")
            if snap.positions is not None:
                account.append(f"{snap.positions} open")
        lines = [escape("  ·  ".join(account))]
        if not cfg.exe_path:
            lines.append("[red]terminal executable missing: it cannot be assigned[/red]")

        if cfg.role is Role.SLAVE:
            summary = f"[dim]{escape(summarize_slave(cfg))}[/dim]"
            if not info.demo_guard_blocked:
                text, tier = slave_badge(info, self._cfg.dry_run)
                lines.append(f"[{_TIER_COLOR[tier]}]{escape(text)}[/]   {summary}")
            else:
                lines.append(summary)
                lines.append(
                    f"[bold {_SERIOUS}]{_ICON_BLOCKED} Blocked: this is a live (non-demo) account, "
                    "so nothing is copied to it.[/] Press [b]l[/b] and type its name to allow "
                    "live trading on it."
                )
        if snap is not None:
            if snap.trade_allowed is False:
                lines.append("[red]algo trading disabled in terminal[/red]")
            if snap.error:
                lines.append(f"[yellow]last error:[/yellow] {escape(snap.error)}")
        results = [
            r for r in self._engine.recent_results() if r.result.terminal_id == cfg.terminal_id
        ][-_RESULT_ROWS:]
        for r in results:
            stamp = time.strftime("%H:%M:%S", time.localtime(r.ts))
            text = escape(f"{stamp}  {r.result.describe()}")
            lines.append(text if r.result.ok else f"[red]{text}[/red]")

        orphans = self._engine.orphans(cfg.terminal_id)
        if orphans:
            lines.append(
                f"[bold red]{len(orphans)} untracked position(s) of ours[/] "
                "-- open, unmanaged, and not closed by the master:"
            )
            for o in orphans:
                lines.append(f"  #{o.slave_ticket}  {escape(o.symbol)}  {o.volume} lots")
        if cfg.role is Role.UNASSIGNED and snap is None:
            lines.append("[dim]assign a role to connect (nothing is launched until you do)[/dim]")
        keys = [f"[{_ACCENT}]Enter[/] [dim]positions, risk and copy health[/dim]"]
        if cfg.role is Role.SLAVE:
            keys.append(f"[{_ACCENT}]p[/] [dim]{'resume' if cfg.paused else 'pause'}[/dim]")
        if cfg.role is not Role.MASTER:
            keys.append(f"[{_ACCENT}]e[/] [dim]settings[/dim]")
        lines.append("   ".join(keys))
        detail.update("\n".join(lines))

    # --- selection helpers ------------------------------------------------------------------

    def _nearest_terminal_row(self, start: int, *, forward: bool) -> int | None:
        """The closest terminal row from ``start`` in one direction, else the other."""
        n = len(self._row_keys)
        ahead = range(start, n) if forward else range(start, -1, -1)
        behind = range(start - 1, -1, -1) if forward else range(start + 1, n)
        candidates: list[int] = [*ahead, *behind]
        for r in candidates:
            if 0 <= r < n and not self._row_keys[r].startswith(_SECTION):
                return r
        return None

    def _selected(self) -> TerminalView | None:
        table = self.query_one("#terminals", TerminalTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._row_keys)):
            return None
        tid = self._row_keys[row]
        if tid.startswith(_SECTION):
            return None
        return next((v for v in self._views() if v.cfg.terminal_id == tid), None)

    def _selected_id(self) -> str | None:
        v = self._selected()
        return v.cfg.terminal_id if v else None

    def _persist_and_sync(self) -> None:
        try:
            config_mod.save(self._cfg)
        except OSError as exc:
            self.notify(f"could not save config: {exc}", severity="error", timeout=8)
            log.exception("config save failed")
        self._engine.sync()
        self._refresh()

    # --- actions ----------------------------------------------------------------------------

    def action_set_master(self) -> None:
        tid = self._selected_id()
        if tid is None:
            return
        refusal = role_actions.set_master(self._cfg, tid)
        if refusal is not None:
            self.notify(refusal, severity="error")
            return
        self.notify(f"master: {self._cfg.terminals[tid].label}")
        self._persist_and_sync()

    def action_toggle_help(self) -> None:
        """``?`` opens the key list and ``?`` (or escape) closes it again."""
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_set_slave(self) -> None:
        tid = self._selected_id()
        if tid is None:
            return
        refusal = role_actions.set_slave(self._cfg, tid)
        if refusal is not None:
            self.notify(refusal, severity="error")
            return
        self._persist_and_sync()

    def action_unassign(self) -> None:
        """Take the selected terminal's role away -- asking first if that strands open copies.

        Unassigning a slave stops its copies being managed, and unassigning the master also
        unassigns every slave, so all of theirs go. Either way the person is shown the copies
        and has to agree, exactly as for a settings edit (:meth:`_apply_settings`).
        """
        tid = self._selected_id()
        if tid is None:
            return
        # Unreadable reads as nothing to ask about; the engine reads again and refuses.
        stake = self._engine.copies_at_stake(tid) or {}
        if not stake:
            self._commit_unassign(tid, {})
            return

        cfg = self._cfg.terminals[tid]
        slaves = [self._cfg.display_name(t.terminal_id) for t in self._cfg.slaves]
        change = f"unassign {self._cfg.display_name(tid)}"
        if cfg.role is Role.MASTER and slaves:
            change += f" (the master), and with it its slaves {', '.join(slaves)}"

        def decided(agreed: bool | None) -> None:
            if agreed:
                self._commit_unassign(tid, stake)

        self.push_screen(
            self.ORPHAN_SCREEN(
                copies=self._by_name(stake),
                changes=change,
                live=not self._cfg.dry_run,
                halt=self.action_panic,
                unassigning=True,
            ),
            decided,
        )

    def _commit_unassign(self, tid: str, given_up: Mapping[str, Sequence[OpenCopy]]) -> None:
        was_master = self._cfg.terminals[tid].role is Role.MASTER
        swept = [t.terminal_id for t in self._cfg.slaves] if was_master else []
        refusal = self._engine.unassign(tid, orphan=self._agreed(given_up))
        if refusal is not None:
            self.notify(
                f"{self._cfg.display_name(tid)} was not unassigned: {refusal}", severity="error"
            )
            return
        if swept:
            names = ", ".join(self._cfg.display_name(t) for t in swept)
            self.notify(f"master unassigned; also unassigned slave(s) left with no master: {names}")
        self._persist_and_sync()
        self._report_given_up(given_up)

    def _by_name(self, stake: Mapping[str, Sequence[OpenCopy]]) -> dict[str, list[OpenCopy]]:
        return {self._cfg.display_name(t): list(held) for t, held in stake.items()}

    @staticmethod
    def _agreed(given_up: Mapping[str, Sequence[OpenCopy]]) -> dict[str, list[int]] | None:
        """What the person agreed to give up, in the form the engine checks it against."""
        if not given_up:
            return None
        return {t: [c.master_ticket for c in held] for t, held in given_up.items()}

    def _report_given_up(self, given_up: Mapping[str, Sequence[OpenCopy]]) -> None:
        """Say, per slave, which copies were orphaned and are now the person's to close."""
        for t, held in given_up.items():
            name = self._cfg.display_name(t)
            if self._cfg.dry_run:
                said = f"{len(held)} simulated copies let go; they are no longer managed."
            else:
                tickets = [f"#{c.slave_ticket}" for c in held if c.slave_ticket is not None]
                named = f" ({', '.join(tickets[:5])}{', ...' if len(tickets) > 5 else ''})"
                said = (
                    f"{copies_word(len(held))} orphaned{named if tickets else ''}. "
                    "They stay open and are no longer managed: close them by hand."
                )
            self.notify(f"{name}: {said}", severity="warning", timeout=20)

    def action_edit_settings(self) -> None:
        """Open the settings form for the selected terminal."""
        tid = self._selected_id()
        if tid is not None:
            self.open_settings(tid)

    def open_settings(self, tid: str) -> None:
        """Open the settings form for one terminal: from the table, or from its detail page."""
        if self._cfg.terminals[tid].role is Role.MASTER:
            self.notify(
                "The master has no settings: it is only read from. Sizing, symbols and what "
                "gets copied are set on each slave."
            )
            return
        self.push_screen(
            self.SETTINGS_SCREEN(
                # The live object, looked up when asked: an edit is built on what is true at the
                # moment of saving, not on what was true when the form opened.
                current=lambda: self._cfg.terminals[tid],
                apply=self._apply_settings,
                halt=self.action_panic,
                live=not self._cfg.dry_run,
            )
        )

    def action_toggle_pause(self) -> None:
        """Pause or resume the selected slave: it stops (or starts again) taking new trades.

        Nothing is asked first, because nothing is given up. Its open copies go on being
        managed either way, and it stays connected.
        """
        tid = self._selected_id()
        if tid is None:
            return
        cfg = self._cfg.terminals[tid]
        name = self._cfg.display_name(tid)
        if cfg.role is not Role.SLAVE:
            self.notify("Only a slave can be paused.", severity="warning")
            return
        paused = not cfg.paused
        self._engine.set_paused(tid, paused)
        if paused:
            log.warning("%s paused: no new trades will be copied to it", name)
            self.notify(
                f"{name} paused: no new trades are copied to it. Copies it already holds are "
                "still closed, reduced and kept in sync.",
                severity="warning",
                timeout=10,
            )
        else:
            log.warning("%s resumed", name)
            self.notify(
                f"{name} resumed: trades the master opens from now on are copied. Any it "
                "opened while paused are not.",
                timeout=10,
            )
        self._persist_and_sync()

    def action_live_override(self) -> None:
        """Open the demo-only guard's override for the selected terminal."""
        tid = self._selected_id()
        if tid is None:
            return
        self.push_screen(
            self.LIVE_OVERRIDE_SCREEN(
                current=lambda: self._cfg.terminals[tid],
                apply=self._apply_settings,
                halt=self.action_panic,
            )
        )

    def _apply_settings(self, outcome: EditOutcome, done: Callable[[str | None], None]) -> None:
        """Take a validated edit -- asking first if it would give up open copies -- and apply it.

        ``done`` is told how it ended: None if the edit went in (or there was nothing to do),
        otherwise why it was refused. It is *not* called when the person declines to give the
        copies up: the form is still open and nothing has happened, so there is nothing to say.

        The question exists for every setting alike. What each one does to a copy that is already
        open is not something a person should have to know, and not something this has to decide.
        """
        new = outcome.config
        if new is None:
            done(None)
            return
        if not outcome.changes:
            self.notify(f"{new.label}: no changes")
            done(None)
            return

        # An unreadable store reads as "nothing to ask about" here, and that is safe: the engine
        # reads it again under its lock and refuses an edit it cannot account for. What is asked
        # about is only ever a courtesy; the engine is the one that cannot be raced. For the
        # master this is every slave's copies when the edit disables it, and nothing otherwise.
        stake = self._engine.copies_at_stake(new.terminal_id, new) or {}
        if not stake:
            done(self._commit_settings(new, outcome, {}))
            return

        def decided(agreed: bool | None) -> None:
            if agreed:
                done(self._commit_settings(new, outcome, stake))

        self.push_screen(
            self.ORPHAN_SCREEN(
                copies=self._by_name(stake),
                changes=describe_changes(outcome.changes, limit=160),
                live=not self._cfg.dry_run,
                halt=self.action_panic,
            ),
            decided,
        )

    def _commit_settings(
        self,
        new: TerminalConfig,
        outcome: EditOutcome,
        given_up: Mapping[str, Sequence[OpenCopy]],
    ) -> str | None:
        """Swap the edit in, record it, save it, and say what changed and what was given up.

        Returns why the engine refused it, or None if it went in. The engine is the one that
        swaps, because it can check and swap under the lock the drain thread dispatches under,
        and ``given_up`` is what it checks the person agreed to against what is held now.

        The change goes in the log in full as well as on screen. A risk setting altered while
        copying is live is the kind of thing that has to be findable afterwards -- and a change
        to the demo-only guard's override is logged as a warning, so it stands out from one.
        """
        refusal = self._engine.apply_settings(new, orphan=self._agreed(given_up))
        if refusal is not None:
            return refusal
        guard_moved = any(c.name == LIVE_OVERRIDE for c in outcome.changes)
        log.log(
            logging.WARNING if guard_moved else logging.INFO,
            "settings changed for %s (%s): %s",
            new.label,
            new.terminal_id[:8],
            describe_changes(outcome.changes),
        )
        self._persist_and_sync()
        self.notify(f"{new.label}: {describe_changes(outcome.changes, limit=160)}")
        self._report_given_up(given_up)
        for warning in advisories(outcome):
            self.notify(warning, severity="warning", timeout=12)
        return None

    def action_reconnect(self) -> None:
        tid = self._selected_id()
        if tid is not None:
            self._engine.reconnect(tid)
            self.notify("reconnect requested")

    def action_restart_worker(self) -> None:
        tid = self._selected_id()
        if tid is not None:
            self._engine.restart(tid)
            self.notify("worker restarted")

    def action_toggle_detection(self) -> None:
        """Start or stop the dry-run diff. Sends nothing either way."""
        if not self._cfg.copying_enabled:
            if self._cfg.master is None:
                self.notify("designate a master first (press m)", severity="warning")
                return
            if not self._cfg.slaves:
                self.notify("designate at least one slave first (press s)", severity="warning")
                return
            self._cfg.copying_enabled = True
            self._persist_and_sync()
            if self._cfg.dry_run:
                self.notify("detection on: dry run, nothing will be sent")
            else:
                self.notify(
                    "COPYING LIVE: orders will be sent to every connected slave",
                    severity="warning",
                    timeout=10,
                )
        else:
            self._cfg.copying_enabled = False
            self._persist_and_sync()
            self.notify("detection off")

    def action_clear_detection(self) -> None:
        """Forget every believed copy, so the diff starts from nothing.

        Switching master no longer needs this: unassigning the old one gives up every slave's
        copies (see :meth:`action_unassign`), so no record of its tickets is left to close.
        """
        self._engine.clear_detection_state()
        self._refresh()
        self.notify("detection ledger cleared")

    def action_acknowledge_orphans(self) -> None:
        """Sign off the selected slave's untracked positions and let it copy again.

        This closes nothing and modifies nothing -- it records that a human has looked. The
        positions stay open and stay untracked, which is why they remain listed afterwards.
        """
        tid = self._selected_id()
        if tid is None:
            return
        cfg = self._cfg.terminals[tid]
        count = self._engine.acknowledge_orphans(tid)
        if count == 0:
            self.notify(f"{cfg.label} has no unacknowledged orphans")
            return
        self._refresh()
        self.notify(
            f"acknowledged {count} orphan(s) on {cfg.label}. They stay open and untracked; "
            "anything the master opened while it was held will not be copied to it.",
            severity="warning",
            timeout=10,
        )

    def action_panic(self) -> None:
        """Halt copying. Deliberately does not close or modify any position.

        In Stage 2a there is nothing to halt but the diff, since no order is ever sent -- but
        the semantics are fixed now so that turning execution on later cannot change what panic
        means. It clears the flag and touches no position, ever.
        """
        self._cfg.copying_enabled = False
        self._persist_and_sync()
        self.notify("PANIC: copying halted. Positions untouched.", severity="warning", timeout=10)

    def action_rescan(self) -> None:
        self.notify("rescanning...")
        self._rescan_worker()

    @work(thread=True, exclusive=True)
    def _rescan_worker(self) -> None:
        """Passive discovery on a thread. Launches no terminals and opens no connections."""
        try:
            found = discover_terminals()
        except Exception as exc:
            log.exception("discovery failed")
            self.call_from_thread(
                self.notify, f"discovery failed: {exc}", severity="error", timeout=8
            )
            return
        self.call_from_thread(self._apply_discovery, found)

    def _apply_discovery(self, found: list[object]) -> None:
        before = set(self._cfg.terminals)
        config_mod.merge_discovery(self._cfg, found)  # type: ignore[arg-type]
        new = set(self._cfg.terminals) - before
        self._rebuild_rows()
        self._persist_and_sync()
        msg = f"{len(found)} terminals found"
        if new:
            msg += f", {len(new)} new"
        self.notify(msg)

    # --- table events -----------------------------------------------------------------------

    @on(TerminalTable.Clicked)
    def _on_cell_clicked(self, event: TerminalTable.Clicked) -> None:
        """The ACTIONS and settings cells are buttons: see :func:`row_action` for which.

        The cursor is moved there first, so the action reads the clicked terminal as selected
        -- which is also what lets one action method serve both the key and the click.
        """
        if event.column_key not in ("action", "pause", "settings"):
            return
        if event.row_key not in self._row_ids:
            return
        role = self._cfg.terminals[event.row_key].role
        if event.column_key == "pause" and role is not Role.SLAVE:
            return  # an empty cell, not a button
        if event.column_key == "settings" and role is Role.MASTER:
            return
        self.query_one("#terminals", TerminalTable).move_cursor(
            row=self._row_keys.index(event.row_key)
        )
        if event.column_key == "pause":
            self.action_toggle_pause()
            return
        if event.column_key == "settings":
            self.action_edit_settings()
            return
        {
            "master": self.action_set_master,
            "slave": self.action_set_slave,
            "unassign": self.action_unassign,
        }[row_action(self._cfg, self._cfg.terminals[event.row_key])]()

    @on(TerminalTable.OpenRequested)
    def action_open_terminal(self) -> None:
        """Open the selected terminal's detail page. Esc on that page comes back here."""
        tid = self._selected_id()
        if tid is None:
            return
        self.push_screen(TerminalScreen(self._engine, self._cfg, tid))

    @on(DataTable.RowHighlighted)
    def _on_row(self, event: DataTable.RowHighlighted) -> None:
        """Step the cursor over section rows, in the direction it was already moving."""
        row = event.cursor_row
        if 0 <= row < len(self._row_keys) and self._row_keys[row].startswith(_SECTION):
            table = self.query_one("#terminals", TerminalTable)
            if table.cursor_row != row:  # already moved on by the time this arrived
                return
            target = self._nearest_terminal_row(row, forward=row >= self._last_row)
            if target is not None and target != row:
                table.move_cursor(row=target)
            return
        self._last_row = row
        self._refresh_detail()


def run() -> None:
    """Entry point: load config, fold in a discovery sweep, then hand over to the UI."""
    cfg = config_mod.load()
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.save(cfg)
    except Exception:
        log.exception("initial discovery failed; starting with saved config only")
    log.info("config at %s", config_path())
    CopierApp(cfg).run()
