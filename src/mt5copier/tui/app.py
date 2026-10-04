"""Textual UI: the master and its slaves drawn as a map, the log, and a page per terminal.

The UI owns no MT5 state of its own. It polls :meth:`Engine.views` on a timer and renders
whatever it finds, which keeps the render path synchronous and free of cross-thread message
passing -- the engine's drain thread writes state, the timer reads it. Textual's
``post_message`` from a background thread would also work, but polling a latest-wins state map
is simpler and cannot queue up a backlog of stale frames when many terminals report at once.

Discovery runs on a Textual thread worker rather than inline: ``psutil.process_iter`` walks
every process on the machine and can take a second or two under load, which would visibly
freeze the event loop.

Every action finds its terminal through :meth:`_selected_id` -- the selected card or chip --
and redraws through :meth:`_refresh`.

Pages: MAP (the master's card, a card per slave joined to it, the day book and the terminals
not yet given a role) and LOG. ``tab`` switches between them, because on the Map the number
keys jump to slave cards.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Region
from textual.widget import Widget
from textual.widgets import ContentSwitcher, DataTable, Input, Static

from .. import config as config_mod
from .. import role_actions
from ..config import AppConfig, Role, TerminalConfig
from ..day_book import MasterEvent, Outcome, SlaveOutcome, describe_master, describe_outcome
from ..discovery import discover_terminals
from ..engine import Engine, TerminalView
from ..log_entries import (
    Level,
    LogEntry,
    Tone,
    event_entries,
    file_entries,
    merge,
    search,
    search_words,
    select,
    terminal_names,
)
from ..logging_setup import get_logger
from ..paths import config_path, log_dir
from ..presentation import RowInfo, read_log_tail, row_info
from ..protocol import WorkerState
from ..reconcile import OpenCopy
from ..settings_edit import (
    LIVE_OVERRIDE,
    EditOutcome,
    advisories,
    copies_word,
    describe_changes,
)
from . import cards
from .detail import DetailScreen
from .dialogs import LiveDialog, OrphanDialog
from .general import GeneralForm
from .menu import ActionMenu, terminal_actions
from .settings import SettingsForm
from .style import (
    BG,
    BLUE,
    CHIP_BG,
    FAINT,
    FG,
    MUTED,
    RED,
    RULED,
    THEME,
    YELLOW,
    Fitted,
    Piece,
    badge,
    key_hint,
    spaced,
)
from .widgets import Card, ChipItem, Chips, KeyBar, Picked, QuickInput, Rail, TopBar, Trunk

log = get_logger(__name__)

_DAYBOOK_ROWS = 12
"""Lines the day book stops adding events at. An event's slaves are never cut off part-way."""
_DAYBOOK_EVENTS = 12

_PAGES = ("map", "log")

_LOG_LINES = 500
"""How much of ``copier.log`` the log page reads."""


class CopierApp(App[None]):
    """Discover, designate, observe, and copy.

    **Whether an order can leave this process depends on one setting.** With ``dry_run`` true,
    the default, the copier reports what *would* be copied and nothing is sent. With it false,
    the same decisions are dispatched to the slaves. The top bar says which of the two is
    happening, because a copier that looks identical in both modes is one nobody can trust.
    """

    CSS_PATH = "app.tcss"
    TITLE = "MT5 Trade Copier"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (120, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (32, "-tall")]

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
        Binding("tab", "next_page", "Map / Log", show=False, priority=True),
        Binding("shift+tab", "next_page", "Map / Log", show=False, priority=True),
        Binding("left", "step(-1)", "Previous card", show=False),
        Binding("right", "step(1)", "Next card", show=False),
        Binding("up", "step(-1)", "Previous card", show=False),
        Binding("down", "step(1)", "Next card", show=False),
        Binding("enter", "open_terminal", "Details", show=False),
        Binding("f", "log_problems", "Problems only", show=False),
        Binding("slash", "log_search", "Search the log", show=False),
        Binding("space", "menu", "Actions", show=False),
        Binding("g", "general_settings", "Copier settings", show=False),
        Binding("a", "jump(0)", "Master card", show=False),
        *(Binding(str(n), f"jump({n})", f"Slave {n}", show=False) for n in range(1, 10)),
    ]

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self._cfg = cfg
        self._engine = Engine(cfg)
        self._page = "map"
        self._sel: str | None = None
        self._order: list[str] = []
        """Terminal ids in the order the arrow keys walk them: master, slaves, unassigned."""
        self._shape: tuple[object, ...] | None = None
        self._cards: dict[str, Card] = {}
        self._copying_to = 0
        """Slaves copying as of the last redraw, for the top bar."""
        self._kinds: dict[str, str] = {}
        """Each slave's state as of the last redraw, for its drop in the tree."""
        self._rails: list[Rail] = []
        self._trunks: list[Trunk] = []
        """One beside each row when there is more than one row, in order."""
        self._per_row = 4
        self._log_problems = False
        """The log page's Show filter: problems only, or everything."""
        self._log_from: str | None = None
        """The log page's From filter: one terminal's entries, or None for all of them."""
        self._log_file: tuple[object, list[LogEntry]] = (None, [])
        """``copier.log`` as entries, with what the file looked like when it was read."""
        self._log_shown: list[LogEntry] | None = None
        self._log_query = ""
        """The log page's search, as typed: only lines holding all of its words are shown."""
        self._log_drawn: tuple[list[LogEntry], tuple[str, ...], LogLayout] | None = None
        """What the table holds: the lines, the words lit up in them, and its columns."""
        self._log_layout: LogLayout | None = None

    # --- composition ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with ContentSwitcher(initial="page-map", id="pages"):
            with VerticalScroll(id="page-map"):
                yield Card("", classes="master")
                yield Vertical(id="slaves")
                with Horizontal(id="lower"):
                    with Vertical(id="daybook-col"):
                        yield Static("DAY BOOK", classes="heading")
                        yield Static("", id="daybook")
                    with Vertical(id="notyet-col"):
                        yield Static("", id="notyet-title", classes="heading")
                        yield Chips(id="chips")
                        yield Static("", id="notyet-hint")
            with Vertical(id="page-log"):
                yield Static("", id="logbar")
                with Horizontal(id="searchbar"):
                    yield Static(Text("Search  ", style=MUTED), classes="w")
                    yield QuickInput(
                        placeholder="words a line must hold", id="log-search", compact=True
                    )
                    yield Static(Text("  enter keep it · esc clear it", style=MUTED), classes="w")
                yield DataTable(id="log", cursor_type="row")
        yield KeyBar("", id="keybar")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = THEME.name
        self._engine.start()
        self._rebuild_rows()
        self.set_interval(0.5, self._on_timer)
        self._refresh()

    def on_unmount(self) -> None:
        self._engine.stop()

    # --- pages ------------------------------------------------------------------------------

    def action_general_settings(self) -> None:
        """The copier's own settings: its mode, connection timing and join grace."""
        self.push_screen(GeneralForm(), lambda _saved: self._refresh())

    def action_show_page(self, page: str) -> None:
        self._page = page
        self.query_one("#pages", ContentSwitcher).current = f"page-{page}"
        if page == "log":
            self._log_shown = None
            self._log_drawn = None
            self._refresh_log()
            self.query_one("#log", DataTable).focus()
        else:
            self.screen.set_focus(None)
        self._refresh_frame()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Tab turns the page only on the pages themselves. Over a dialog or the settings form
        it is theirs, to move between fields and buttons."""
        if action == "next_page":
            return self.screen is self.screen_stack[0] or self._detail() is not None
        if action == "step" and isinstance(self.focused, Input):
            # The arrows move the cursor in a box being typed into, not to another terminal.
            return False
        return True

    def action_next_page(self) -> None:
        if self._detail() is not None:
            # From a terminal's details, tab goes on to the log, as the tabs over it read.
            self.pop_screen()
            self.action_show_page("log")
            return
        self.action_show_page(_PAGES[(_PAGES.index(self._page) + 1) % len(_PAGES)])

    # --- the details page -------------------------------------------------------------------

    def action_open_terminal(self) -> None:
        """Open the selected terminal's details page. Esc on that page comes back here."""
        tid = self._selected_id()
        if tid is None:
            return
        detail = self._detail()
        if detail is not None:
            detail.show(tid)
            return
        self.push_screen(DetailScreen(self._engine, self._cfg, tid))

    def _detail(self) -> DetailScreen | None:
        """The details page, if it is the screen showing."""
        return self.screen if isinstance(self.screen, DetailScreen) else None

    # --- selection --------------------------------------------------------------------------

    def _views(self) -> list[TerminalView]:
        return self._engine.views()

    def _info(self, v: TerminalView, holds: dict[str, str]) -> RowInfo:
        return row_info(
            v,
            timeout_s=self._cfg.heartbeat_timeout_s,
            hold_reason=holds.get(v.cfg.terminal_id),
            display_name=self._cfg.display_name(v.cfg.terminal_id),
        )

    def _selected(self) -> TerminalView | None:
        if self._sel is None:
            return None
        return next((v for v in self._views() if v.cfg.terminal_id == self._sel), None)

    def _selected_id(self) -> str | None:
        v = self._selected()
        return v.cfg.terminal_id if v else None

    def _select(self, tid: str | None) -> None:
        self._sel = tid
        if self._cards:
            self._refresh()
        else:
            self._refresh_chips()
            self._refresh_frame()

    def action_step(self, delta: int) -> None:
        """The next terminal along: on the Map, the next card; on a details page, the next
        terminal's details."""
        detail = self._detail()
        if (self._page != "map" and detail is None) or not self._order:
            return
        at = self._order.index(self._sel) if self._sel in self._order else -delta
        self._select(self._order[(at + delta) % len(self._order)])
        if detail is not None and self._sel is not None:
            detail.show(self._sel)

    def action_jump(self, number: int) -> None:
        """``a`` is the master's card and 1-9 the slaves', by the number each card shows. On
        the log page the same keys pick the one terminal to show, and again to show them all;
        on a details page, they show that terminal's details."""
        detail = self._detail()
        if detail is not None:
            self._jump_to(number)
            if self._sel is not None:
                detail.show(self._sel)
            return
        if self._page == "log":
            self._filter_log_to(number)
            return
        if self._page != "map":
            return
        self._jump_to(number)

    def _jump_to(self, number: int) -> None:
        if number == 0:
            master = self._cfg.master
            if master is not None:
                self._select(master.terminal_id)
            return
        slaves = self._slave_ids()
        if 1 <= number <= len(slaves):
            self._select(slaves[number - 1])

    def action_pick(self, tid: str) -> None:
        """A click on a card or chip selects it; a click on the one already selected opens its
        action menu."""
        if tid not in self._cfg.terminals:
            return
        if tid == self._sel:
            self.action_menu()
        else:
            self._select(tid)

    # --- the action menu --------------------------------------------------------------------

    def action_menu(self) -> None:
        """Open the selected terminal's action menu beside its card or chip."""
        tid = self._sel
        detail = self._detail()
        if (self._page != "map" and detail is None) or tid is None:
            return
        if tid not in self._cfg.terminals:
            return
        # On a details page the menu hangs off its band, and leaves out the details themselves.
        anchor = detail.menu_anchor() if detail is not None else self._anchor(tid)
        if anchor is None:
            return
        cfg = self._cfg.terminals[tid]
        holds = self._engine.holds()
        view = next((v for v in self._views() if v.cfg.terminal_id == tid), None)
        info = self._info(view, holds) if view is not None else None
        status = view.status(self._cfg.heartbeat_timeout_s) if view is not None else ""
        items = terminal_actions(
            cfg,
            has_master=self._cfg.master is not None,
            # Held, and with untracked positions on record: signing them off is what lifts it.
            orphans_waiting=tid in holds and bool(self._engine.orphans(tid)),
            not_a_demo=info is not None and info.demo_guard_blocked,
            link_ok=status == str(WorkerState.CONNECTED),
        )
        if detail is not None:
            items = [i for i in items if i.action != "open_terminal"]

        def chosen(action: str | None) -> None:
            if action is not None:
                getattr(self, f"action_{action}")()

        self.push_screen(ActionMenu(self._cfg.display_name(tid), items, anchor), chosen)

    def _anchor(self, tid: str) -> Region | None:
        """Where the terminal is drawn on screen: its card, or its chip."""
        card = self._cards.get(tid)
        if card is not None:
            return card.region
        return self.query_one("#chips", Chips).chip_region(tid)

    @on(Picked)
    def _on_picked(self, event: Picked) -> None:
        self.action_pick(event.terminal_id)

    # --- actions ----------------------------------------------------------------------------

    def _persist_and_sync(self) -> None:
        try:
            config_mod.save(self._cfg)
        except OSError as exc:
            self.notify(f"could not save config: {exc}", severity="error", timeout=8)
            log.exception("config save failed")
        self._engine.sync()
        self._refresh()

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

        def decided(agreed: bool | None) -> None:
            if agreed:
                self._commit_unassign(tid, stake)

        self.push_screen(
            OrphanDialog(
                copies=self._by_name(stake),
                changes=f"unassign {self._cfg.display_name(tid)}",
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
        """Open the settings form for one terminal: from the map, or from its details page."""
        if self._cfg.terminals[tid].role is Role.MASTER:
            self.notify(
                "The master has no settings: it is only read from. Sizing, symbols and what "
                "gets copied are set on each slave."
            )
            return
        self.push_screen(
            SettingsForm(
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
            LiveDialog(
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
            OrphanDialog(
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
        """Start or stop copying (in a dry run, the simulation of it)."""
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

        Switching master does not need this: unassigning the old one gives up every slave's
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

        It clears the flag and touches no position, ever, so what panic means cannot change
        with whatever else the copier learns to do.
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

    # --- building the map -------------------------------------------------------------------

    def _groups(self) -> tuple[TerminalView | None, list[TerminalView], list[TerminalView]]:
        views = self._views()
        master = next((v for v in views if v.cfg.role is Role.MASTER), None)
        slaves = [v for v in views if v.cfg.role is Role.SLAVE]
        unassigned = [v for v in views if v.cfg.role is Role.UNASSIGNED]
        return master, slaves, unassigned

    def _rebuild_rows(self) -> None:
        """Rebuild the cards. Only when who is master, slave or unassigned has changed.

        The selection stays on the terminal it was on, so a key that moves a terminal from one
        group to another leaves it selected where it landed.
        """
        master, slaves, unassigned = self._groups()
        self._shape = self._shape_of(master, slaves, unassigned)
        self._order = (
            ([master.cfg.terminal_id] if master else [])
            + [v.cfg.terminal_id for v in slaves]
            + [v.cfg.terminal_id for v in unassigned]
        )

        master_card = self.query_one(".card.master", Card)
        master_card.terminal_id = master.cfg.terminal_id if master else ""
        area = self.query_one("#slaves", Vertical)
        area.remove_children()
        self._cards = {master_card.terminal_id: master_card} if master else {}
        self._rails = []
        self._trunks = []
        self._per_row = self._cards_per_row()
        rows = [slaves[i : i + self._per_row] for i in range(0, len(slaves), self._per_row)]
        trunk = len(rows) > 1
        widgets: list[Widget] = []
        for n, chunk in enumerate(rows):
            more = n < len(rows) - 1
            cells: list[Widget] = []
            if trunk:
                beside = Trunk(more=more)
                self._trunks.append(beside)
                cells.append(beside)
            for v in chunk:
                card = Card(v.cfg.terminal_id, classes="slave")
                self._cards[card.terminal_id] = card
                cells.append(card)
            # Empty slots keep a lone card a quarter wide rather than stretching it.
            cells += [Static(classes="slot") for _ in range(self._per_row - len(chunk))]
            row = Horizontal(*cells, classes="slave-row")
            rail = Rail(row, first=n == 0, trunk=trunk, more=more)
            self._rails.append(rail)
            widgets += [rail, row]
        if not rows:
            widgets.append(Static(self._no_slaves_text(master is not None), classes="hint"))
        area.mount(*widgets)

        keep = self._sel if self._sel in self._order else (self._order[0] if self._order else None)
        self._select(keep)

    def _slave_ids(self) -> list[str]:
        """Slaves in the order their cards are drawn and numbered -- which the 1-9 keys and the
        day book must follow too. Not ``cfg.slaves``, which is in the order they were added."""
        return [v.cfg.terminal_id for v in self._groups()[1]]

    def _cards_per_row(self) -> int:
        """Four slave cards to a row, as the design has them; two on a narrow terminal."""
        return 2 if 0 < self.size.width < 120 else 4

    @staticmethod
    def _shape_of(
        master: TerminalView | None, slaves: list[TerminalView], unassigned: list[TerminalView]
    ) -> tuple[object, ...]:
        return (
            master.cfg.terminal_id if master else None,
            tuple(v.cfg.terminal_id for v in slaves),
            tuple(v.cfg.terminal_id for v in unassigned),
        )

    @staticmethod
    def _no_slaves_text(has_master: bool) -> Text:
        if has_master:
            return Text(
                "No slaves yet: select a terminal under NOT YET OPENED and press space.",
                style=MUTED,
            )
        return Text("Assign a master first.", style=MUTED)

    # --- drawing ----------------------------------------------------------------------------

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
        master, slaves, unassigned = self._groups()
        if self._shape_of(master, slaves, unassigned) != self._shape:
            self._rebuild_rows()
        holds = self._engine.holds()
        dry_run = self._cfg.dry_run
        copying = self._cfg.copying_enabled

        master_card = self.query_one(".card.master", Card)
        master_up = False
        if master is None:
            master_card.update(cards.no_master_card())
        else:
            master_info = self._info(master, holds)
            master_up = master.snapshot is not None and master_info.tier == "good"
            master_card.update(
                cards.master_card(
                    master,
                    master_info,
                    slaves=len(slaves),
                    width=master_card.content_size.width,
                )
            )
        master_card.set_class(
            master is not None and master.cfg.terminal_id == self._sel, "-selected"
        )

        if self._cards_per_row() != self._per_row:
            self._rebuild_rows()
        self._copying_to = 0
        self._kinds = {}
        for number, v in enumerate(slaves, start=1):
            card = self._cards.get(v.cfg.terminal_id)
            if card is None:
                continue
            info = self._info(v, holds)
            state = cards.slave_state(info, dry_run=dry_run, copying=copying)
            selected = v.cfg.terminal_id == self._sel
            self._kinds[v.cfg.terminal_id] = state.kind
            if state.kind == "copying":
                self._copying_to += 1
            refused = self._engine.terminal_stats(v.cfg.terminal_id).refused
            card.update(
                cards.slave_card(
                    number, v, info, state, refused=refused, width=card.content_size.width
                )
            )
            card.set_classes(f"card slave -{state.kind}" + (" -selected" if selected else ""))

        # The master is live while it is up and at least one slave is taking its trades: its
        # border then matches the lines running from it.
        master_card.set_class(master_up and self._copying_to > 0, "-live")

        self._refresh_daybook()
        self._refresh_chips(unassigned)
        self._refresh_frame()
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        """Hand each rail and trunk the slaves' states: its own row's, and those of the rows
        below, which the trunk beside and under it leads on to."""
        rows = [[self._kinds.get(t, "") for t in rail.terminal_ids()] for rail in self._rails]
        for n, rail in enumerate(self._rails):
            rail.kinds = self._kinds
            rail.below = [k for row in rows[n + 1 :] for k in row]
            rail.refresh()
        for n, beside in enumerate(self._trunks):
            beside.below = [k for row in rows[n + 1 :] for k in row]
            beside.refresh()

    def _refresh_frame(self) -> None:
        self._refresh_topbar()
        self._refresh_keybar()

    def _refresh_topbar(self) -> None:
        self.query_one("#topbar", TopBar).update(self.topbar())

    def topbar(self, crumb: str | None = None) -> Fitted:
        """MT5 COPIER, the page tabs, and what the copier is doing. ``crumb`` is the terminal
        whose details page is open, shown after the Map tab it was opened from.

        On a narrow terminal the clock goes first, then the words shorten; the alerts and the
        LIVE / DRY RUN badge are always shown."""

        def tabs(crumb: str | None) -> Text:
            out = Text()
            for page, label in (("map", "Map"), ("log", "Log")):
                if (page == "map") if crumb is not None else (page == self._page):
                    out.append(f" {label} ", style=f"bold {BG} on {FG}")
                else:
                    out.append(f" {label} ", style=MUTED)
                out.append(" ")
                if page == "map" and crumb is not None:
                    out.append(f"› {crumb}", style=f"bold {FG}")
                    out.append("   ")
            out.rstrip()
            return out

        title = Text(spaced("MT5") + "   " + spaced("Copier"), style=f"bold {FG}")
        left = [
            Piece(title, short=Text("MT5 COPIER", style=f"bold {FG}")),
            Piece(
                tabs(crumb),
                short=tabs(crumb[:14] + "…" if crumb and len(crumb) > 15 else crumb),
            ),
        ]

        right = [Piece(Text(alert, style=f"bold {RED}")) for alert in self._alerts()]
        slaves = len(self._cfg.slaves)
        if self._cfg.copying_enabled:
            verb = "Dry run to" if self._cfg.dry_run else "Copying to"
            doing = Text(f"{verb} {self._copying_to} of {slaves} slaves")
            short = Text(f"{'dry' if self._cfg.dry_run else 'copying'} {self._copying_to}/{slaves}")
            right.append(Piece(doing, short=short))
        else:
            right.append(Piece(Text("Not copying", style=MUTED)))
        right.append(Piece(Text(time.strftime("%H:%M:%S"), style=MUTED), drop=1))
        right.append(Piece(badge("DRY RUN", YELLOW) if self._cfg.dry_run else badge("LIVE", RED)))
        return Fitted(left, right, joiner="   ")

    def _alerts(self) -> list[str]:
        """What the top bar shouts about: none of it may go unsaid."""
        out: list[str] = []
        blocked = self._engine.blocked_reason
        if blocked is not None and self._cfg.copying_enabled:
            out.append("DETECTION PAUSED")
        lifted = [
            t.label or t.terminal_id[:8]
            for t in self._cfg.terminals.values()
            if t.allow_live_trading
        ]
        if lifted:
            out.append(f"NON-DEMO ALLOWED: {', '.join(lifted)}")
        h = self._engine.health()
        if h["stale"]:
            out.append(f"STALE {h['stale']}")
        if self._engine.holds():
            out.append(f"HELD {len(self._engine.holds())}")
        if self._engine.failed_total:
            out.append(f"FAILED {self._engine.failed_total}")
        return out

    def _refresh_keybar(self) -> None:
        # Each key's ``drop`` says how soon it goes when the terminal is too narrow for them
        # all: the highest first. What is never dropped -- the first key, ``?`` (which lists
        # every key), panic and quit -- is what a person cannot do without.
        keys: list[Piece] = []
        if self._page == "map":
            # What can be done to one terminal is in its action menu, so the bar keeps to the
            # keys that find a terminal and those that act on the whole copier -- and leads
            # with the key that opens the menu, named for the terminal it will act on.
            sel = self._sel if self._sel in self._cfg.terminals else None
            what = f"actions for {self._cfg.display_name(sel)}" if sel else "actions"
            keys.append(
                Piece(
                    key_hint("space", what, primary=True),
                    short=key_hint("space", "actions", primary=True),
                    shorten=10,
                )
            )
            keys.append(Piece(key_hint("←→", "choose"), drop=4))
            if self._cfg.slaves:
                count = min(len(self._cfg.slaves), 9)
                go = key_hint("a 1" if count == 1 else f"a 1-{count}", "go to")
                keys.append(Piece(go, drop=3))
            keys.append(Piece(key_hint("tab", "log"), drop=5))
            copying = self._cfg.copying_enabled
            keys.append(
                Piece(
                    key_hint("d", "stop copying" if copying else "start copying"),
                    drop=1,
                    short=key_hint("d", "stop" if copying else "start"),
                )
            )
            keys.append(
                Piece(key_hint("g", "copier settings"), drop=6, short=key_hint("g", "settings"))
            )
        else:
            keys.append(Piece(key_hint("↑↓", "line"), drop=7))
            problems = "everything" if self._log_problems else "problems only"
            keys.append(
                Piece(key_hint("f", problems), drop=2, short=key_hint("f", problems.split()[0]))
            )
            count = min(len(self._cfg.slaves), 9)
            which = "a" + ("" if not count else " 1" if count == 1 else f" 1-{count}")
            keys.append(
                Piece(key_hint(which, "one terminal"), drop=3, short=key_hint(which, "one"))
            )
            keys.append(Piece(key_hint("/", "search"), drop=4))
            keys.append(Piece(key_hint("end", "newest"), drop=6))
            keys.append(
                Piece(
                    key_hint("enter", "open that terminal"), drop=5, short=key_hint("enter", "open")
                )
            )
            keys.append(Piece(key_hint("tab", "map"), drop=1))
        keys.append(Piece(key_hint("?", "all keys")))
        self.query_one("#keybar", KeyBar).update(self.keybar(keys))

    @staticmethod
    def keybar(keys: Sequence[Piece | Text]) -> Fitted:
        """A page's keys on the left, with panic and quit kept to the right on every page. A
        key given as plain text keeps its place by order: the first and the last (``?``) are
        never dropped, and the others go from the right."""
        last = len(keys) - 1
        pieces = [
            k if isinstance(k, Piece) else Piece(k, drop=0 if i in (0, last) else i)
            for i, k in enumerate(keys)
        ]
        right = [
            Piece(
                key_hint("F9", "panic halt", color=RED), short=key_hint("F9", "panic", color=RED)
            ),
            Piece(key_hint("q", "quit")),
        ]
        return Fitted(pieces, right)

    def _refresh_daybook(self) -> None:
        """The master's newest trades, newest first, each with what every slave made of it.
        Skips and refusals are shown with their reason: a copier that quietly does nothing
        looks the same as one that is broken."""
        events = self._engine.day_book(limit=_DAYBOOK_EVENTS)
        panel = self.query_one("#daybook", Static)
        if not events:
            if self._cfg.copying_enabled:
                hint = "Watching the master: nothing has happened yet."
            elif self._cfg.master is None:
                hint = "1. Select a terminal under NOT YET OPENED, press space, and make it the master."
            elif not self._cfg.slaves:
                hint = "2. Select another, press space, and make it a slave."
            else:
                hint = "3. Press d to start copying."
            panel.update(Text(hint, style=MUTED))
            return
        numbers = {tid: n for n, tid in enumerate(self._slave_ids(), start=1)}
        table = Table(
            box=RULED,
            show_header=False,
            show_edge=False,
            show_lines=True,
            pad_edge=False,
            padding=(0, 2, 0, 0),
            border_style=FAINT,
            expand=True,
        )
        table.add_column(no_wrap=True)
        table.add_column(ratio=1)
        master = self._cfg.master
        master_name = (
            f"Master ({self._cfg.display_name(master.terminal_id)})" if master else "Master"
        )

        def slave_name(tid: str) -> str:
            # A terminal that is no longer a slave has no number, but still had this outcome.
            number = numbers.get(tid)
            role = f"Slave {number}" if number else "Slave"
            return f"{role} ({self._cfg.display_name(tid)[:24]})"

        shown: list[tuple[MasterEvent, list[tuple[str, SlaveOutcome]]]] = []
        lines = 0
        for event in reversed(events):
            if lines >= _DAYBOOK_ROWS:
                break
            ordered = sorted(event.outcomes, key=lambda o: numbers.get(o.terminal_id, 99))
            shown.append((event, [(slave_name(o.terminal_id), o) for o in ordered]))
            lines += 1 + len(ordered)
        # Wide enough for the longest slave name shown, so the outcomes line up.
        width = max((len(name) for _, outs in shown for name, _ in outs), default=0)

        for event, outs in shown:
            when = Text(time.strftime("%H:%M:%S", time.localtime(event.ts)), style=MUTED)
            style = MUTED if event.preexisting else f"bold {FG}"
            head = Text(f"{master_name} ", style=style)
            head.append(describe_master(event), style=style)
            table.add_row(when, head)
            for name, out in outs:
                line = Text(f"{name:<{width}}  ", style=FG)
                line.append(describe_outcome(event.kind, out), style=_tone(out))
                table.add_row("", line)
        panel.update(table)

    # --- the log page -----------------------------------------------------------------------

    def action_log_problems(self) -> None:
        if self._page != "log":
            return
        self._log_problems = not self._log_problems
        self._refresh_log()
        self._refresh_keybar()

    def action_log_search(self) -> None:
        """Open the search line under the filters, holding what was searched for last."""
        if self._page != "log" or self._detail() is not None:
            return
        self.query_one("#searchbar").display = True
        box = self.query_one("#log-search", QuickInput)
        box.focus()
        box.cursor_position = len(box.value)

    @on(Input.Changed, "#log-search")
    def _on_search_typed(self, event: Input.Changed) -> None:
        """The lines narrow as each letter is typed."""
        self._log_query = event.value
        self._refresh_log()

    @on(Input.Submitted, "#log-search")
    def _on_search_kept(self, event: Input.Submitted) -> None:
        """Enter keeps the search and goes back to the lines; the bar above still says what
        is being searched for."""
        event.stop()
        self._close_search()

    @on(QuickInput.Escaped, "#log-search")
    def _on_search_cleared(self, event: QuickInput.Escaped) -> None:
        """Esc in the search clears it."""
        event.stop()
        self.query_one("#log-search", QuickInput).value = ""
        self._log_query = ""
        self._close_search()

    def _close_search(self) -> None:
        self.query_one("#searchbar").display = False
        self.query_one("#log", DataTable).focus()
        self._refresh_log()
        self._refresh_keybar()

    def _filter_log_to(self, number: int) -> None:
        """0 is the master, 1-9 the slaves by their card numbers. Picking the terminal already
        shown goes back to showing all of them."""
        if number == 0:
            master = self._cfg.master
            tid = master.terminal_id if master else None
        else:
            slaves = self._slave_ids()
            tid = slaves[number - 1] if 1 <= number <= len(slaves) else None
        if tid is None:
            return
        self._log_from = None if self._log_from == tid else tid
        self._refresh_log()

    @on(DataTable.RowSelected, "#log")
    def _on_log_row(self, event: DataTable.RowSelected) -> None:
        """Enter on a line opens the terminal it is about."""
        shown = self._log_shown or []
        row = event.cursor_row
        tid = shown[row].terminal_id if 0 <= row < len(shown) else None
        if tid is None or tid not in self._cfg.terminals:
            self.notify("That line is not about one terminal.", severity="warning")
            return
        self._select(tid)
        self.action_open_terminal()

    def _log_entries(self) -> list[LogEntry]:
        """``copier.log`` and the day book, merged. The file is parsed again only when it has
        changed; the day book is cheap to read every time."""
        path = log_dir() / "copier.log"
        try:
            stat = path.stat()
            seen: object = (stat.st_size, stat.st_mtime)
        except OSError:
            seen = None
        since = self._engine.day_book_since
        stamp = (seen, since, tuple((t, c.label) for t, c in self._cfg.terminals.items()))
        if stamp != self._log_file[0]:
            names = terminal_names(
                {
                    tid: (t.label, self._cfg.display_name(tid))
                    for tid, t in self._cfg.terminals.items()
                }
            )
            text = read_log_tail(path, max_lines=_LOG_LINES)
            self._log_file = (stamp, file_entries(text, names, covered_from=since))
        master = self._cfg.master
        events = event_entries(self._engine.day_book(), master.terminal_id if master else None)
        return merge(self._log_file[1], events)

    def _refresh_log(self) -> None:
        """Redraw the log page when what it shows has changed, keeping the cursor on the line
        it was on -- or on the newest line, if that is where it was."""
        if self._page != "log":
            return
        scoped = select(self._log_entries(), problems_only=False, terminal_id=self._log_from)
        numbers = self._terminal_numbers()
        words = search_words(self._log_query)
        scoped = search(scoped, words, lambda e: self._who(e, numbers))
        shown = select(scoped, problems_only=self._log_problems, terminal_id=None)
        table = self.query_one("#log", DataTable)
        following = table.row_count == 0 or table.cursor_row >= table.row_count - 1
        self._refresh_logbar(sum(1 for e in scoped if e.level.problem), following)
        if not table.size.width:
            # Not laid out yet: its columns are cut to its width, so wait until it has one.
            self.call_after_refresh(self._refresh_log)
            return
        layout = log_layout(table.size.width, [self._who(e, numbers) for e in shown])
        drawn = (shown, tuple(words), layout)
        if drawn == self._log_drawn:
            return
        old = self._log_shown or []
        at = old[table.cursor_row] if 0 <= table.cursor_row < len(old) else None
        self._log_shown = shown
        self._log_drawn = drawn
        if layout != self._log_layout:
            self._log_layout = layout
            table.clear(columns=True)
            for key, label, size in layout.columns():
                table.add_column(Text(label, style=f"bold {FG}"), key=key, width=size)
        else:
            table.clear()
        for entry in shown:
            cells = self._log_cells(entry, numbers, words, layout)
            # A line longer than the Entry column wraps onto more rows, rather than running
            # off the right edge; most lines fit, and are not measured.
            tall = cells[-1].cell_len > layout.entry
            table.add_row(*cells, height=None if tall else 1)
        if not shown:
            return
        target = len(shown) - 1
        if not following and at is not None and at in shown:
            target = shown.index(at)
        table.move_cursor(row=target, scroll=True)

    def _terminal_numbers(self) -> dict[str, str]:
        """``A`` for the master and each slave's card number, as the map shows them."""
        out = {tid: str(n) for n, tid in enumerate(self._slave_ids(), start=1)}
        master = self._cfg.master
        if master is not None:
            out[master.terminal_id] = "A"
        return out

    def _who(self, entry: LogEntry, numbers: dict[str, str], *, short: bool = False) -> str:
        """The Terminal column, as the day book names them: "Master (name)", "Slave 2 (name)";
        ``short`` leaves the name out, for a narrow terminal."""
        if entry.terminal_id is None:
            return "copier"
        number = numbers.get(entry.terminal_id)
        if number == "A":
            role = "Master"
        elif number:
            role = f"Slave {number}"
        else:
            role = "Terminal"
        if short:
            return role
        return f"{role} ({self._cfg.display_name(entry.terminal_id)[:24]})"

    def _log_cells(
        self,
        entry: LogEntry,
        numbers: dict[str, str],
        words: list[str] | None = None,
        layout: LogLayout | None = None,
    ) -> tuple[Text, ...]:
        when = Text(time.strftime("%H:%M:%S", time.localtime(entry.ts)), style=MUTED)
        level = Text(entry.level.value, style=_LEVEL_STYLE[entry.level])
        if entry.terminal_id is None:
            who = Text("copier", style=MUTED)
        else:
            style = f"bold {FG}" if entry.tone is Tone.MASTER else FG
            short = layout is not None and layout.short_names
            who = Text(self._who(entry, numbers, short=short), style=style)
        first, *rest = entry.text.splitlines() or [""]
        text = Text(first, style=_TONE_STYLE[entry.tone])
        if rest:
            text.append(f"  (+{len(rest)} more lines in copier.log)", style=FAINT)
        if words:
            # What was searched for, lit up where it is in the line.
            for cell in (level, who, text):
                cell.highlight_words(words, style=_FOUND, case_sensitive=False)
        if layout is not None and not layout.level:
            return when, who, text
        return when, level, who, text

    def _refresh_logbar(self, problems: int, following: bool) -> None:
        """The filters, which are always shown, and where the page is in the file, which
        shortens and then goes when the terminal is narrow."""

        def show(label: str) -> Text:
            out = Text(label, style=MUTED)
            out.append_text(_choice("everything", not self._log_problems))
            out.append(" ")
            out.append_text(_choice(f"problems only · {problems}", self._log_problems))
            return out

        def where(label: str) -> Text:
            out = Text(label, style=MUTED)
            out.append_text(_choice("all", self._log_from is None))
            numbers = self._terminal_numbers()
            for tid in sorted(numbers, key=lambda t: (numbers[t] != "A", numbers[t].zfill(2))):
                out.append(" ")
                out.append_text(_choice(numbers[tid], self._log_from == tid))
            return out

        left = [Piece(show("Show  "), short=show("")), Piece(where("From  "), short=where(""))]
        query = self._log_query.strip()
        if query:
            found = Text("Search  ", style=MUTED)
            found.append_text(_choice(query, True))
            left.append(Piece(found, short=_choice(query[:12], True)))
        spot = "following the newest line" if following else "end for the newest line"
        right = [
            Piece(
                Text(f"copier.log · {spot}", style=MUTED),
                drop=1,
                short=Text("following" if following else "end: newest", style=MUTED),
            )
        ]
        self.query_one("#logbar", Static).update(Fitted(left, right, joiner="     "))

    def _refresh_chips(self, unassigned: list[TerminalView] | None = None) -> None:
        if unassigned is None:
            unassigned = self._groups()[2]
        self.query_one("#notyet-title", Static).update(f"NOT YET OPENED · {len(unassigned)}")
        self.query_one("#chips", Chips).set_items(
            [
                ChipItem(
                    terminal_id=v.cfg.terminal_id,
                    label=self._cfg.display_name(v.cfg.terminal_id),
                    usable=v.cfg.exe_path is not None,
                    selected=v.cfg.terminal_id == self._sel,
                )
                for v in unassigned
            ]
        )
        hint = self.query_one("#notyet-hint", Static)
        hint.display = bool(unassigned)
        hint.update(self._notyet_hint())

    def _notyet_hint(self) -> Text:
        """Only the role that will work: a second master is refused, and so is a slave while
        there is no master. The menu on space offers the same, so the hint points there."""
        master = self._cfg.master
        if master is None:
            return Text(
                "None is launched until made the master: select one and press space.", style=MUTED
            )
        out = Text("None is launched until made a slave: select one and press space. ", style=MUTED)
        out.append(
            f"To change the master, first unassign {self._cfg.display_name(master.terminal_id)}"
            " (press a, then space).",
            style=MUTED,
        )
        return out


@dataclass(frozen=True)
class LogLayout:
    """The log table's columns at one width: the Entry column takes what the others leave,
    and on a narrow terminal the Terminal column drops the names and then Level goes (an
    entry's colour still says it)."""

    level: bool
    short_names: bool
    who: int
    entry: int

    def columns(self) -> list[tuple[str, str, int]]:
        out = [("time", "Time", 8)]
        if self.level:
            out.append(("level", "Level", 5))
        out += [("terminal", "Terminal", self.who), ("entry", "Entry", self.entry)]
        return out


_LOG_PAD = 2
"""Each column's cell padding, one each side."""
_LOG_SCROLLBAR = 2
_SHORT_WHO = 8
"""The widest Terminal cell without names: "Terminal"."""


def log_layout(width: int, who: Sequence[str]) -> LogLayout:
    """The columns for a table ``width`` cells wide, whose Terminal cells would read ``who``."""
    room = width - _LOG_SCROLLBAR
    full = min(max((len(w) for w in who), default=_SHORT_WHO), 34)
    full = max(full, len("Terminal"))
    for level, short in ((True, False), (True, True), (False, True)):
        who_width = _SHORT_WHO if short else full
        used = 8 + _LOG_PAD + who_width + _LOG_PAD + (5 + _LOG_PAD if level else 0)
        entry = room - used - _LOG_PAD
        if entry >= (50 if (level, short) != (False, True) else 0):
            break
    return LogLayout(level, short, who_width, max(entry, 20))


_LEVEL_STYLE = {
    Level.DEBUG: FAINT,
    Level.INFO: MUTED,
    Level.WARN: f"bold {YELLOW}",
    Level.ERROR: f"bold {RED}",
}

_FOUND = f"bold {BG} on {YELLOW}"
"""A searched-for word, where it is in a line."""

_TONE_STYLE = {
    Tone.PLAIN: MUTED,
    Tone.MASTER: f"bold {FG}",
    Tone.DONE: BLUE,
    Tone.QUIET: MUTED,
    Tone.WARN: YELLOW,
    Tone.ERROR: RED,
}


def _choice(label: str, chosen: bool) -> Text:
    """One option of a filter, filled in cream when it is the one in force."""
    if chosen:
        return Text(f" {label} ", style=f"bold {BG} on {FG}")
    return Text(f" {label} ", style=f"{FG} on {CHIP_BG}")


def _tone(out: SlaveOutcome) -> str:
    """Blue when the copy was done, red when a slave fell short, muted when there was
    deliberately nothing to do."""
    if out.outcome is Outcome.DONE:
        return BLUE
    if out.outcome.problem:
        return RED
    return MUTED


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
