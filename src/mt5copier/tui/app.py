"""Textual UI: the terminal list, role assignment, and live status.

The UI owns no MT5 state of its own. It polls :meth:`Engine.views` on a timer and renders
whatever it finds, which keeps the render path synchronous and free of cross-thread message
passing -- the engine's drain thread writes state, the timer reads it. Textual's
``post_message`` from a background thread would also work, but polling a latest-wins state map
is simpler and cannot queue up a backlog of stale frames when many terminals report at once.

Discovery runs on a Textual thread worker rather than inline: ``psutil.process_iter`` walks
every process on the machine and can take a second or two under load, which would visibly
freeze the event loop.
"""

from __future__ import annotations

import time

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Static

from .. import config as config_mod
from ..config import AppConfig, Role
from ..discovery import discover_terminals
from ..engine import Engine, TerminalView
from ..logging_setup import get_logger
from ..paths import config_path
from ..settings_edit import EditOutcome, advisories, describe_changes
from .settings_screen import SettingsScreen

log = get_logger(__name__)

_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("m", "M", 3),
    ("s", "S", 3),
    ("broker", "BROKER", 24),
    ("login", "LOGIN", 12),
    ("server", "SERVER", 24),
    ("equity", "EQUITY", 14),
    ("risk", "RISK", 10),
    ("status", "STATUS", 16),
)

_ACTION_ROWS = 6

_RESULT_ROWS = 3
"""Order results shown for the selected terminal. Few, because the detail panel is short and
the status bar carries the running failure count."""
"""Rows of detection history on screen. The full history is in the engine and the log file."""

_STATE_STYLE = {
    "connected": "bold green",
    "connecting": "yellow",
    "disconnected": "bold yellow",
    "failed": "bold red",
    "stale": "bold red",
    "worker dead": "bold red",
    "starting": "dim",
    "stopped": "dim",
    "not connected": "dim",
    "-": "dim",
}


class CopierApp(App[None]):
    """Discover, designate, observe -- and, from Stage 2c, actually copy.

    **Whether an order can leave this process now depends on one setting.** With ``dry_run``
    true, the default, the panel reports what *would* be copied and nothing is sent. With it
    false, the same decisions are dispatched to the slaves. The header and status bar say which
    of the two is happening, because a copier that looks identical in both modes is one nobody
    can trust.
    """

    CSS_PATH = "app.tcss"
    TITLE = "MT5 Trade Copier"
    SUB_TITLE = "Stage 2c - live copying"

    BINDINGS = [
        Binding("m", "set_master", "Master"),
        Binding("s", "toggle_slave", "Slave"),
        Binding("u", "unassign", "Unassign"),
        Binding("e", "edit_settings", "Settings"),
        Binding("r", "rescan", "Rescan"),
        Binding("c", "reconnect", "Reconnect"),
        Binding("d", "toggle_detection", "Detect"),
        Binding("ctrl+r", "restart_worker", "Restart worker"),
        Binding("ctrl+l", "clear_detection", "Clear ledger"),
        Binding("o", "acknowledge_orphans", "Ack orphans"),
        Binding("f9", "panic", "PANIC halt"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self._engine = Engine(cfg)
        self._row_ids: list[str] = []

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("", id="statusbar")
            table: DataTable[str] = DataTable(id="terminals", cursor_type="row", zebra_stripes=True)
            yield table
            yield Static("", id="actions")
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#terminals", DataTable)
        for key, label, width in _COLUMNS:
            table.add_column(label, key=key, width=width)
        self._engine.start()
        self._rebuild_rows()
        self.set_interval(0.5, self._on_timer)
        self._refresh()

    def on_unmount(self) -> None:
        self._engine.stop()

    # --- rendering --------------------------------------------------------------------------

    def _views(self) -> list[TerminalView]:
        return self._engine.views()

    def _rebuild_rows(self) -> None:
        """Rebuild the table from scratch. Only called when the set of terminals changes."""
        table = self.query_one("#terminals", DataTable)
        cursor = table.cursor_row
        table.clear()
        views = self._views()
        self._row_ids = [v.cfg.terminal_id for v in views]
        for v in views:
            table.add_row(*self._cells(v), key=v.cfg.terminal_id)
        if 0 <= cursor < len(self._row_ids):
            table.move_cursor(row=cursor)

    def _cells(self, v: TerminalView) -> tuple[str, ...]:
        cfg = v.cfg
        snap = v.snapshot
        timeout = self._cfg.heartbeat_timeout_s

        master_mark = "[b]O[/b]" if cfg.role is Role.MASTER else "."
        slave_mark = "[b]O[/b]" if cfg.role is Role.SLAVE else "."

        # Prefer live account data; fall back to what discovery read off disk so the table is
        # populated before anything is connected.
        login = snap.login if snap and snap.login else cfg.last_login
        server = snap.server if snap and snap.server else cfg.last_server

        if snap and snap.equity is not None:
            equity = f"{snap.equity:,.2f}"
        else:
            equity = "-"

        status = v.status(timeout)
        style = _STATE_STYLE.get(status, "")
        status_cell = f"[{style}]{status}[/]" if style else status

        label = cfg.label if cfg.exe_path else f"[strike]{cfg.label}[/strike]"

        return (
            master_mark,
            slave_mark,
            label,
            str(login) if login else "-",
            server or "-",
            equity,
            cfg.risk_label(),
            status_cell,
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
        except (NoMatches, ScreenStackError):
            log.debug("refresh tick landed during teardown; skipped")

    def _refresh(self) -> None:
        views = self._views()
        if [v.cfg.terminal_id for v in views] != self._row_ids:
            self._rebuild_rows()
            views = self._views()

        table = self.query_one("#terminals", DataTable)
        for row, v in enumerate(views):
            for col, cell in enumerate(self._cells(v)):
                try:
                    table.update_cell_at(Coordinate(row, col), cell, update_width=False)
                except Exception:  # row vanished between listing and update
                    self._rebuild_rows()
                    return

        self._refresh_statusbar()
        self._refresh_actions()
        self._refresh_detail()

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
                panel.update("[dim]detecting... no master activity seen yet[/dim]")
            else:
                panel.update("[dim]detection off - press d to start the dry run[/dim]")
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

        dry = self._cfg.dry_run
        # The subtitle carries the mode too, because the status bar scrolls out of a small
        # terminal and "is this live?" must never need a scroll to answer.
        self.sub_title = "Stage 2c - dry run" if dry else "Stage 2c - LIVE, orders are sent"

        if self._cfg.copying_enabled:
            copy_state = "DETECTING (dry run - nothing sent)" if dry else "COPYING LIVE"
            bar.set_classes("copying")
        else:
            copy_state = "detection off"
            bar.set_classes("halted" if master else "")

        failed = self._engine.failed_total
        bar.update(
            f" {copy_state}  |  master: {master.label if master else '(none)'}"
            f"  |  slaves: {len(self._cfg.slaves)}"
            f"  |  live {h['live']}/{h['supervised']}"
            + (f"  |  STALE {h['stale']}" if h["stale"] else "")
            + (f"  |  HELD {len(self._engine.holds())}" if self._engine.holds() else "")
            + (f"  |  FAILED {failed}" if failed else "")
            + f"  |  intents {self._engine.action_total}"
            f" / tracking {self._engine.believed_copies()}" + f"  |  {h['total']} terminals known"
        )

    def _refresh_detail(self) -> None:
        v = self._selected()
        detail = self.query_one("#detail", Static)
        if v is None:
            detail.update("")
            return
        cfg, snap = v.cfg, v.snapshot
        lines = [f"{cfg.label}   id={cfg.terminal_id[:8]}   exe={cfg.exe_path or '(missing)'}"]
        if snap is not None:
            bits = []
            if snap.company:
                bits.append(snap.company)
            if snap.currency:
                bits.append(snap.currency)
            if snap.balance is not None:
                bits.append(f"balance {snap.balance:,.2f}")
            if snap.positions is not None:
                bits.append(f"{snap.positions} open")
            if snap.leverage:
                bits.append(f"1:{snap.leverage}")
            if snap.trade_allowed is False:
                bits.append("[red]algo trading disabled in terminal[/red]")
            if bits:
                lines.append("   ".join(bits))
            if snap.error:
                lines.append(f"[yellow]last error:[/yellow] {snap.error}")
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
        detail.update("\n".join(lines))

    # --- selection helpers ------------------------------------------------------------------

    def _selected(self) -> TerminalView | None:
        table = self.query_one("#terminals", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self._row_ids)):
            return None
        tid = self._row_ids[row]
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
        cfg = self._cfg.terminals[tid]
        if cfg.exe_path is None:
            self.notify(f"{cfg.label} has no terminal executable", severity="error")
            return
        self._cfg.set_role(tid, Role.MASTER)
        self.notify(f"master: {cfg.label}")
        self._persist_and_sync()

    def action_toggle_slave(self) -> None:
        tid = self._selected_id()
        if tid is None:
            return
        cfg = self._cfg.terminals[tid]
        if cfg.role is Role.MASTER:
            self.notify(
                "that terminal is the master; press u to unassign first", severity="warning"
            )
            return
        if cfg.exe_path is None:
            self.notify(f"{cfg.label} has no terminal executable", severity="error")
            return
        new = Role.UNASSIGNED if cfg.role is Role.SLAVE else Role.SLAVE
        self._cfg.set_role(tid, new)
        self._persist_and_sync()

    def action_unassign(self) -> None:
        tid = self._selected_id()
        if tid is None:
            return
        self._cfg.set_role(tid, Role.UNASSIGNED)
        self._persist_and_sync()

    def action_edit_settings(self) -> None:
        """Open the settings form for the selected terminal."""
        tid = self._selected_id()
        if tid is None:
            return
        self.push_screen(
            SettingsScreen(
                # The live object, looked up when asked: an edit is built on what is true at the
                # moment of saving, not on what was true when the form opened.
                current=lambda: self._cfg.terminals[tid],
                apply=self._apply_settings,
                halt=self.action_panic,
                live=not self._cfg.dry_run,
            )
        )

    def _apply_settings(self, outcome: EditOutcome) -> None:
        """Take a validated edit: swap it in, record it, save it, and say what changed.

        The change goes in the log as well as on screen. A risk setting altered while copying is
        live is the kind of thing that has to be findable afterwards.
        """
        new = outcome.config
        if new is None:
            return
        if not outcome.changes:
            self.notify(f"{new.label}: no changes")
            return
        summary = describe_changes(outcome.changes)
        self._cfg.replace_terminal(new)
        log.info("settings changed for %s (%s): %s", new.label, new.terminal_id[:8], summary)
        self._persist_and_sync()
        self.notify(f"{new.label}: {summary}")
        for warning in advisories(outcome):
            self.notify(warning, severity="warning", timeout=12)

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

        Useful when switching master: the old beliefs describe positions that were never ours,
        and leaving them in would produce a CLOSE for each one.
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

    # --- mouse ------------------------------------------------------------------------------

    @on(DataTable.CellSelected)
    def _on_cell(self, event: DataTable.CellSelected) -> None:
        """Clicking the M or S column assigns the role directly."""
        column = event.cell_key.column_key.value
        if column == "m":
            self.action_set_master()
        elif column == "s":
            self.action_toggle_slave()

    @on(DataTable.RowHighlighted)
    def _on_row(self, event: DataTable.RowHighlighted) -> None:
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
