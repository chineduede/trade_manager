"""The new UI's app: the classic :class:`CopierApp`, laid out as a map.

Only presentation is replaced here. The classic app's actions all find their terminal through
:meth:`_selected_id` and redraw through :meth:`_refresh`, so overriding those two (and the
compose/mount that build the screen) is enough for every key to keep doing exactly what it
did -- with the selected card standing in for the selected table row.

Pages: MAP (the master's card, a card per slave joined to it, the day book and the terminals
not yet given a role) and LOG. ``tab`` switches between them, because on the Map the number
keys jump to slave cards.
"""

from __future__ import annotations

import time

from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ContentSwitcher, DataTable, Input, Static

from .. import config as config_mod
from ..config import AppConfig, Role
from ..day_book import MasterEvent, Outcome, SlaveOutcome, describe_master, describe_outcome
from ..discovery import discover_terminals
from ..engine import TerminalView
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
from ..presentation import read_log_tail
from ..protocol import WorkerState
from ..tui.app import CopierApp
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
    badge,
    key_hint,
    left_right,
    spaced,
)
from .widgets import Card, ChipItem, Chips, KeyBar, Picked, Rail, TopBar, Trunk

log = get_logger(__name__)

_DAYBOOK_ROWS = 12
"""Lines the day book stops adding events at. An event's slaves are never cut off part-way."""
_DAYBOOK_EVENTS = 12

_PAGES = ("map", "log")

_LOG_LINES = 500
"""How much of ``copier.log`` the log page reads, as the classic page does."""


class NewCopierApp(CopierApp):
    """:class:`CopierApp` with the redesigned layout. Launched by ``mt5copier --new-ui``."""

    CSS_PATH = "app.tcss"
    ORPHAN_SCREEN = OrphanDialog
    LIVE_OVERRIDE_SCREEN = LiveDialog
    SETTINGS_SCREEN = SettingsForm
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (120, "-wide")]

    BINDINGS = [
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
        super().__init__(cfg)
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
        self._log_drawn: tuple[list[LogEntry], tuple[str, ...]] | None = None
        """What the table holds: the lines, and the words lit up in them."""

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
                    yield SearchBox(
                        placeholder="words a line must hold", id="log-search", compact=True
                    )
                    yield Static(Text("  enter keep it · esc clear it", style=MUTED), classes="w")
                yield DataTable(id="log", cursor_type="row")
        yield KeyBar("", id="keybar")

    def on_mount(self, event: events.Mount | None = None) -> None:
        # Textual calls ``on_mount`` on every class up the MRO; the classic one builds its table.
        if event is not None:
            event.prevent_default()
        self.register_theme(THEME)
        self.theme = THEME.name
        log_table = self.query_one("#log", DataTable)
        for label in ("Time", "Level", "Terminal", "Entry"):
            log_table.add_column(Text(label, style=f"bold {FG}"), key=label.lower())
        self._engine.start()
        self._rebuild_rows()
        self.set_interval(0.5, self._on_timer)
        self._refresh()

    # The classic app's panels, which this layout does not have. Its handlers still call them
    # -- a row highlighted in a detail page's table bubbles up to the classic ``_on_row`` --
    # so they are made to do nothing rather than look for widgets that are not there.

    def _refresh_detail(self) -> None:
        pass

    def _refresh_actions(self) -> None:
        pass

    def _refresh_statusbar(self) -> None:
        pass

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

    def _selected(self) -> TerminalView | None:
        if self._sel is None:
            return None
        return next((v for v in self._views() if v.cfg.terminal_id == self._sel), None)

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
        group to another leaves it selected where it landed -- as the classic table does.
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

    def topbar(self, crumb: str | None = None) -> Table:
        """MT5 COPIER, the page tabs, and what the copier is doing. ``crumb`` is the terminal
        whose details page is open, shown after the Map tab it was opened from."""
        left = Text()
        left.append(spaced("MT5") + "   " + spaced("Copier"), style=f"bold {FG}")
        left.append("    ")
        for page, label in (("map", "Map"), ("log", "Log")):
            if (page == "map") if crumb is not None else (page == self._page):
                left.append(f" {label} ", style=f"bold {BG} on {FG}")
            else:
                left.append(f" {label} ", style=MUTED)
            left.append(" ")
            if page == "map" and crumb is not None:
                left.append(f"› {crumb}", style=f"bold {FG}")
                left.append("   ")

        right = Text()
        for alert in self._alerts():
            right.append(alert, style=f"bold {RED}")
            right.append("   ")
        slaves = len(self._cfg.slaves)
        if self._cfg.copying_enabled:
            verb = "Dry run to" if self._cfg.dry_run else "Copying to"
            right.append(f"{verb} {self._copying_to} of {slaves} slaves")
        else:
            right.append("Not copying", style=MUTED)
        right.append(f"   {time.strftime('%H:%M:%S')}   ", style=MUTED)
        right.append_text(badge("DRY RUN", YELLOW) if self._cfg.dry_run else badge("LIVE", RED))
        return left_right(left, right)

    def _alerts(self) -> list[str]:
        """What the classic status bar shouts about, kept: none of it may go unsaid."""
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
        keys: list[Text] = []
        if self._page == "map":
            # What can be done to one terminal is in its action menu, so the bar keeps to the
            # keys that find a terminal and those that act on the whole copier -- and leads
            # with the key that opens the menu, named for the terminal it will act on.
            sel = self._sel if self._sel in self._cfg.terminals else None
            what = f"actions for {self._cfg.display_name(sel)}" if sel else "actions"
            keys.append(key_hint("space", what, primary=True))
            keys.append(key_hint("←→", "choose"))
            if self._cfg.slaves:
                count = min(len(self._cfg.slaves), 9)
                keys.append(key_hint("a 1" if count == 1 else f"a 1-{count}", "go to"))
            keys.append(key_hint("tab", "log"))
            keys.append(
                key_hint("d", "stop copying" if self._cfg.copying_enabled else "start copying")
            )
            keys.append(key_hint("g", "copier settings"))
        else:
            keys.append(key_hint("↑↓", "line"))
            keys.append(key_hint("f", "everything" if self._log_problems else "problems only"))
            count = min(len(self._cfg.slaves), 9)
            which = "a" + ("" if not count else " 1" if count == 1 else f" 1-{count}")
            keys.append(key_hint(which, "one terminal"))
            keys.append(key_hint("/", "search"))
            keys.append(key_hint("end", "newest"))
            keys.append(key_hint("enter", "open that terminal"))
            keys.append(key_hint("tab", "map"))
        keys.append(key_hint("?", "all keys"))
        self.query_one("#keybar", KeyBar).update(self.keybar(keys))

    @staticmethod
    def keybar(keys: list[Text]) -> Table:
        """A page's keys on the left, with panic and quit kept to the right on every page."""
        left = Text("  ").join(keys)
        right = Text("  ").join([key_hint("F9", "panic halt", color=RED), key_hint("q", "quit")])
        return left_right(left, right)

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
        box = self.query_one("#log-search", SearchBox)
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

    def on_search_box_cleared(self, _message: SearchBox.Cleared) -> None:
        self.query_one("#log-search", SearchBox).value = ""
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
        drawn = (shown, tuple(words))
        if drawn == self._log_drawn:
            return
        old = self._log_shown or []
        at = old[table.cursor_row] if 0 <= table.cursor_row < len(old) else None
        self._log_shown = shown
        self._log_drawn = drawn
        table.clear()
        for entry in shown:
            table.add_row(*self._log_cells(entry, numbers, words))
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

    def _who(self, entry: LogEntry, numbers: dict[str, str]) -> str:
        """The Terminal column, as the day book names them: "Master (name)", "Slave 2 (name)"."""
        if entry.terminal_id is None:
            return "copier"
        number = numbers.get(entry.terminal_id)
        if number == "A":
            role = "Master"
        elif number:
            role = f"Slave {number}"
        else:
            role = "Terminal"
        return f"{role} ({self._cfg.display_name(entry.terminal_id)[:24]})"

    def _log_cells(
        self, entry: LogEntry, numbers: dict[str, str], words: list[str] | None = None
    ) -> tuple[Text, ...]:
        when = Text(time.strftime("%H:%M:%S", time.localtime(entry.ts)), style=MUTED)
        level = Text(entry.level.value, style=_LEVEL_STYLE[entry.level])
        if entry.terminal_id is None:
            who = Text("copier", style=MUTED)
        else:
            style = f"bold {FG}" if entry.tone is Tone.MASTER else FG
            who = Text(self._who(entry, numbers), style=style)
        first, *rest = entry.text.splitlines() or [""]
        text = Text(first, style=_TONE_STYLE[entry.tone])
        if rest:
            text.append(f"  (+{len(rest)} more lines in copier.log)", style=FAINT)
        if words:
            # What was searched for, lit up where it is in the line.
            for cell in (level, who, text):
                cell.highlight_words(words, style=_FOUND, case_sensitive=False)
        return when, level, who, text

    def _refresh_logbar(self, problems: int, following: bool) -> None:
        bar = Text()
        bar.append("Show  ", style=MUTED)
        bar.append_text(_choice("everything", not self._log_problems))
        bar.append(" ")
        bar.append_text(_choice(f"problems only · {problems}", self._log_problems))
        bar.append("     From  ", style=MUTED)
        bar.append_text(_choice("all", self._log_from is None))
        numbers = self._terminal_numbers()
        for tid in sorted(numbers, key=lambda t: (numbers[t] != "A", numbers[t].zfill(2))):
            bar.append(" ")
            bar.append_text(_choice(numbers[tid], self._log_from == tid))
        if self._log_query.strip():
            bar.append("     Search  ", style=MUTED)
            bar.append_text(_choice(self._log_query.strip(), True))
        where = "following the newest line" if following else "end for the newest line"
        right = Text(f"copier.log · {where}", style=MUTED)
        self.query_one("#logbar", Static).update(left_right(bar, right))

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


class SearchBox(Input):
    """The log page's search. Esc here clears the search, rather than reaching the app."""

    BINDINGS = [Binding("escape", "clear", "Clear the search", show=False)]

    class Cleared(Message):
        pass

    def action_clear(self) -> None:
        self.post_message(self.Cleared())


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
    """Entry point for ``--new-ui``: the same start-up as the classic UI, then this app."""
    cfg = config_mod.load()
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.save(cfg)
    except Exception:
        log.exception("initial discovery failed; starting with saved config only")
    log.info("config at %s (new UI)", config_path())
    NewCopierApp(cfg).run()
