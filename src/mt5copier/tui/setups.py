"""The setups page: ``w``. Saved setups -- a master, its slaves and whether copying was on -- to
save the one in place now as, and to load another in its place.

What a setup holds, and what loading one does, are :mod:`mt5copier.setups`' and the engine's;
loading goes through the app (:meth:`CopierApp.load_setup`), which asks first if it would
strand open copies. The functions here decide what the page says, so the tests can read them
without starting the app.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from ..config import AppConfig
from ..setups import Setup, capture, current_name, name_of, problems
from .guards import GuardConfirm
from .style import FG, GREEN, MUTED, RED, YELLOW, key_hint, left_right, spaced
from .widgets import KeyBar, QuickInput, TopBar

if TYPE_CHECKING:
    from .app import CopierApp

ABOUT = (
    "A setup is who copies from whom: the master, its slaves, and whether copying was on. Each "
    "terminal keeps its own settings and each account its own guards, whichever setup is loaded."
)

_COLUMNS = ("SETUP", "MASTER", "SLAVES", "COPYING")


def slaves_cell(setup: Setup, cfg: AppConfig) -> str:
    if not setup.slaves:
        return "none"
    return ", ".join(name_of(cfg, tid) for tid in setup.slaves)


def setup_cells(name: str, setup: Setup, cfg: AppConfig, *, current: bool) -> list[Text]:
    title = Text("● " if current else "  ", style=GREEN)
    title.append(name, style=f"bold {FG}")
    copying = Text("on", style=GREEN) if setup.copying else Text("off", style=MUTED)
    return [
        title,
        Text(name_of(cfg, setup.master)),
        Text(slaves_cell(setup, cfg)),
        copying,
    ]


def describe(setup: Setup, cfg: AppConfig) -> str:
    """The roles a setup puts in place, as the change a confirmation names."""
    n = len(setup.slaves)
    said = f"master {name_of(cfg, setup.master)}, "
    said += f"{n} slave{'' if n == 1 else 's'}" + (f" ({slaves_cell(setup, cfg)})" if n else "")
    said += ", copying " + ("on" if setup.copying else "off")
    return said


def saved_when(ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    here = time.localtime(ts)
    if time.strftime("%Y%m%d", here) == time.strftime("%Y%m%d", time.localtime(now)):
        return f"today {time.strftime('%H:%M', here)}"
    return time.strftime("%d %b %Y %H:%M", here)


class SetupsScreen(Screen[None]):
    """The saved setups. Holds nothing of its own; the app's copy of the file is read each tick."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("backspace", "app.pop_screen", "Back", show=False),
        Binding("a", "save_as", "Save current as"),
        Binding("enter", "load", "Load"),
        Binding("u", "update", "Update with current"),
        Binding("n", "rename", "Rename"),
        Binding("x", "remove", "Delete"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._names: list[str] = []
        """The setup names, in the order the table lists them."""
        self._drawn: list[list[str]] | None = None
        self._asking: str | None = None
        """What the line under the table is asking for: "new", or the setup being renamed."""

    @property
    def copier(self) -> CopierApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield TopBar("", id="topbar")
        with VerticalScroll(id="st-page"):
            yield Static("", id="st-band")
            yield Static(Text(ABOUT, style=MUTED), id="st-note")
            yield Static("", id="st-now")
            yield Static("", id="st-head", classes="heading")
            yield DataTable(id="st-table", cursor_type="row", zebra_stripes=False)
            yield Static("", id="st-said")
            yield Static("", id="st-empty")
            with Horizontal(id="st-ask"):
                yield Static("", id="st-ask-what")
                yield QuickInput(id="st-ask-box", compact=True)
                yield Static(Text("  enter keep it · esc leave it", style=MUTED), classes="w")
            yield Static("", id="st-ask-error")
        yield KeyBar("", id="keybar")

    def on_mount(self) -> None:
        table = self.query_one("#st-table", DataTable)
        for label in _COLUMNS:
            table.add_column(Text(label, style=f"bold {FG}"), key=label)
        self.set_interval(0.5, self._on_timer)
        self.refresh_page()
        table.focus()

    def _on_timer(self) -> None:
        try:
            self.refresh_page()
        except NoMatches:  # the screen is being torn down under the tick
            pass

    # --- reading -------------------------------------------------------------------------

    def selected(self) -> str | None:
        table = self.query_one("#st-table", DataTable)
        if not self._names or not table.row_count:
            return None
        return self._names[min(table.cursor_row, len(self._names) - 1)]

    def refresh_page(self) -> None:
        app = self.copier
        cfg = app._cfg
        doc = app._setups
        self.query_one("#topbar", TopBar).update(app.topbar(crumb="setups"))
        current = current_name(doc, cfg)

        band = Text(spaced("setups"), style="bold")
        self.query_one("#st-band", Static).update(
            left_right(band, Text(f"{len(doc.setups)} saved"))
        )

        now = Text("Now  ", style=MUTED)
        live = capture(cfg)
        if live is None:
            now.append("no master assigned", style=YELLOW)
        else:
            now.append(describe(live, cfg), style=FG)
            now.append("  ·  ", style=MUTED)
            if current is None:
                now.append("not saved as a setup", style=YELLOW)
            else:
                now.append(f"setup {current}", style=f"bold {GREEN}")
        self.query_one("#st-now", Static).update(now)
        self.query_one("#st-head", Static).update(
            left_right(Text("SAVED SETUPS", style="bold"), Text("● the roles in place now"))
        )

        table = self.query_one("#st-table", DataTable)
        names = sorted(doc.setups)
        rows = [setup_cells(n, doc.setups[n], cfg, current=n == current) for n in names]
        wanted = [[c.plain for c in row] for row in rows]
        if wanted != self._drawn:
            cursor = table.cursor_row
            table.clear()
            for name, row in zip(names, rows, strict=True):
                table.add_row(*row, key=name)
            self._names = names
            self._drawn = wanted
            if names:
                table.move_cursor(row=min(cursor, len(names) - 1), animate=False)
        table.display = bool(names)
        self._show_said()

        empty = self.query_one("#st-empty", Static)
        empty.display = not names
        said = Text("No setups saved yet. ", style=MUTED)
        said.append("a", style=f"bold underline {FG}")
        said.append(" save the roles in place now as one", style=MUTED)
        empty.update(said)

        keys = [key_hint("a", "save current as", primary=True), key_hint("esc", "back")]
        if names:
            keys += [
                key_hint("enter", "load"),
                key_hint("u", "update with current"),
                key_hint("n", "rename"),
                key_hint("x", "delete"),
            ]
        keys.append(key_hint("?", "all keys"))
        self.query_one("#keybar", KeyBar).update(app.keybar(keys))

    @on(DataTable.RowHighlighted, "#st-table")
    def _show_said(self) -> None:
        """When the selected setup was saved, and anything stopping it loading here."""
        said = self.query_one("#st-said", Static)
        name = self.selected()
        said.display = name is not None
        if name is None:
            return
        setup = self.copier._setups.setups[name]
        line = Text(f"{name}: ", style=f"bold {FG}")
        line.append(f"saved {saved_when(setup.saved_at)}", style=MUTED)
        for problem in problems(setup, self.copier._cfg):
            line.append(f"\ncannot load: {problem}", style=RED)
        said.update(line)

    # --- writing -------------------------------------------------------------------------

    def _now_or_say(self) -> Setup | None:
        live = capture(self.copier._cfg)
        if live is None:
            self.notify("no master is assigned: assign one (m) before saving a setup")
        return live

    def action_save_as(self) -> None:
        if self._now_or_say() is not None:
            self._ask("new", "Save the roles in place now as: ", "")

    @on(DataTable.RowSelected, "#st-table")
    def _on_row_chosen(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_load()

    def action_load(self) -> None:
        name = self.selected()
        if name is not None:
            self.copier.load_setup(name, done=self.refresh_page)

    def action_update(self) -> None:
        name = self.selected()
        live = self._now_or_say() if name is not None else None
        if name is None or live is None:
            return
        cfg = self.copier._cfg

        def answered(yes: bool | None) -> None:
            if yes:
                self._put(name, live, f"setup {name} updated")

        self.app.push_screen(
            GuardConfirm(
                f"Update {name}?",
                f"{name} is saved again as the roles in place now: {describe(live, cfg)}.",
                "Update it",
            ),
            answered,
        )

    def action_rename(self) -> None:
        name = self.selected()
        if name is not None:
            self._ask(name, f"Rename {name} to: ", name)

    def action_remove(self) -> None:
        name = self.selected()
        if name is None:
            return

        def answered(yes: bool | None) -> None:
            if yes:
                setups = self.copier._setups.setups
                setups.pop(name, None)
                if self.copier.save_setups():
                    self.notify(f"setup {name} deleted")
                self.refresh_page()

        self.app.push_screen(
            GuardConfirm(
                f"Delete {name}?",
                "The saved setup is forgotten. The roles in place now do not change.",
                "Delete it",
            ),
            answered,
        )

    def _put(self, name: str, setup: Setup, said: str) -> bool:
        self.copier._setups.setups[name] = setup
        done = self.copier.save_setups()
        if done:
            self.notify(said)
        self.refresh_page()
        return done

    # --- the line under the table ------------------------------------------------------------

    def _ask(self, what: str, label: str, value: str) -> None:
        self._asking = what
        self.query_one("#st-ask-what", Static).update(Text(label, style=MUTED))
        box = self.query_one("#st-ask-box", QuickInput)
        box.value = value
        self._ask_error("")
        line = self.query_one("#st-ask")
        line.display = True
        box.focus()
        box.cursor_position = len(value)
        self.call_after_refresh(line.scroll_visible, animate=False)

    @on(QuickInput.Escaped, "#st-ask-box")
    def _on_ask_left(self, event: QuickInput.Escaped) -> None:
        event.stop()
        self._close_ask()

    @on(Input.Submitted, "#st-ask-box")
    def _on_ask_given(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        setups = self.copier._setups.setups
        if not text:
            self._ask_error("give the setup a name")
            return
        if self._asking == "new":
            if text in setups:
                self._ask_error(f"{text} is already saved: pick it and press u to update it")
                return
            live = self._now_or_say()
            if live is None or not self._put(text, live, f"setup {text} saved"):
                return
        elif self._asking is not None:
            old = self._asking
            if text != old:
                if text in setups:
                    self._ask_error(f"a setup called {text} is already saved")
                    return
                setups[text] = setups.pop(old)
                if self.copier.save_setups():
                    self.notify(f"setup {old} renamed {text}")
                self.refresh_page()
        self._close_ask()

    def _ask_error(self, message: str) -> None:
        line = self.query_one("#st-ask-error", Static)
        line.update(Text(message, style=RED))
        line.display = bool(message)

    def _close_ask(self) -> None:
        self._asking = None
        self.query_one("#st-ask").display = False
        self._ask_error("")
        self.query_one("#st-table", DataTable).focus()
