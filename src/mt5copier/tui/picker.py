"""The profile picker: the first thing a plain launch shows, before anything is locked.

Deliberately its own little app rather than a screen of :class:`CopierApp`: the main app's
construction already loads the chosen profile's config, guards and setups, so the choice has
to be made before that app can exist. Nothing here takes the single-instance lock, connects
to a terminal or configures logging -- a person can sit on this screen forever while three
other profiles run.

``--profile <name>`` skips this screen entirely; it exists so nobody has to remember a name.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Input, Static

from .. import profiles
from ..paths import DEFAULT_PROFILE
from .style import BG, FG, MUTED, RED, THEME, YELLOW, key_hint


def _age(ts: float | None) -> str:
    if ts is None:
        return "never opened"
    days = max(0, int((time.time() - ts) // 86400))
    if days == 0:
        return time.strftime("today %H:%M", time.localtime(ts))
    if days == 1:
        return "yesterday"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class PickerApp(App[str]):
    """Pick a profile; exits with its name, or ``None`` when the person quits instead."""

    CSS = f"""
    Screen {{ background: {BG}; }}
    #title {{ padding: 1 2 0 2; }}
    #table {{ margin: 1 2 0 2; height: 1fr; }}
    #status {{ padding: 0 2; height: 1; }}
    #ask {{ margin: 0 2; height: auto; display: none; }}
    #ask.shown {{ display: block; }}
    #keys {{ dock: bottom; padding: 0 2; height: 1; }}
    """

    BINDINGS = [
        Binding("enter", "open", "Open", show=False, priority=False),
        Binding("n", "new", "New profile", show=False),
        Binding("e", "rename", "Rename", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("y", "confirm_delete", "Confirm delete", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "quit_picker", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[profiles.ProfileInfo] = []
        self._asking: str | None = None
        """What the input row is for right now: ``"new"``, ``"rename"`` or ``None``."""
        self._rename_from: str | None = None
        self._deleting: str | None = None
        """A profile d was pressed on, awaiting its y."""

    def compose(self) -> ComposeResult:
        yield Static(Text("MT5COPIER — PROFILES", style=f"bold {FG}"), id="title")
        yield DataTable(id="table", cursor_type="row")
        with Horizontal(id="ask"):
            yield Input(placeholder="profile name", id="name", compact=True)
        yield Static("", id="status")
        keys = Text("")
        for i, (k, label) in enumerate(
            [
                ("enter", "open"),
                ("n", "new"),
                ("e", "rename"),
                ("d", "delete"),
                ("q", "quit"),
            ]
        ):
            if i:
                keys.append("  ·  ", style=MUTED)
            keys.append_text(key_hint(k, label))
        yield Static(keys, id="keys")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = THEME.name
        table = self.query_one("#table", DataTable)
        table.add_columns("PROFILE", "STATUS", "MASTER", "SLAVES", "LAST OPENED")
        self._reload()
        table.focus()

    # --- model ------------------------------------------------------------------------------

    def _reload(self, select: str | None = None) -> None:
        table = self.query_one("#table", DataTable)
        previous = self._selected()
        table.clear()
        self._rows = profiles.list_profiles()
        for info in self._rows:
            name = Text(info.name, style=f"bold {FG}" if info.is_default else FG)
            if info.running:
                status = Text(f"running (pid {info.pid})", style=YELLOW)
            else:
                status = Text("-", style=MUTED)
            table.add_row(
                name,
                status,
                Text(info.master or "-", style=FG if info.master else MUTED),
                Text(str(info.slaves) if info.slaves else "-", style=FG if info.slaves else MUTED),
                Text(_age(info.last_opened), style=MUTED),
                key=info.name,
            )
        wanted = select or (previous.name if previous else None)
        if wanted is not None:
            for i, info in enumerate(self._rows):
                if info.name == wanted:
                    table.move_cursor(row=i)
                    break

    def _selected(self) -> profiles.ProfileInfo | None:
        table = self.query_one("#table", DataTable)
        if not self._rows or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self._rows):
            return self._rows[table.cursor_row]
        return None

    def _say(self, message: str, *, tone: str = MUTED) -> None:
        self.query_one("#status", Static).update(Text(message, style=tone))

    # --- actions ----------------------------------------------------------------------------

    def _ask(self, what: str, *, prefill: str = "") -> None:
        self._asking = what
        box = self.query_one("#ask")
        box.add_class("shown")
        field = self.query_one("#name", Input)
        field.value = prefill
        field.focus()

    def _close_ask(self) -> None:
        self._asking = None
        self._rename_from = None
        self.query_one("#ask").remove_class("shown")
        self.query_one("#table", DataTable).focus()

    def action_open(self) -> None:
        if self._asking is not None:
            return
        info = self._selected()
        if info is None:
            return
        self._deleting = None
        if info.running:
            self._say(f"{info.name!r} is already running (pid {info.pid})", tone=RED)
            return
        self.exit(info.name)

    def action_new(self) -> None:
        self._deleting = None
        self._say("name the new profile · enter makes it · esc backs out")
        self._ask("new")

    def action_rename(self) -> None:
        self._deleting = None
        info = self._selected()
        if info is None:
            return
        if info.is_default:
            self._say("the default profile keeps its name", tone=RED)
            return
        if info.running:
            self._say(f"{info.name!r} is running (pid {info.pid}); close it first", tone=RED)
            return
        self._rename_from = info.name
        self._say(f"rename {info.name!r} · enter renames it · esc backs out")
        self._ask("rename", prefill=info.name)

    def action_delete(self) -> None:
        if self._asking is not None:
            return
        info = self._selected()
        if info is None:
            return
        if info.is_default:
            self._say("the default profile cannot be deleted", tone=RED)
            return
        if info.running:
            self._say(f"{info.name!r} is running (pid {info.pid}); close it first", tone=RED)
            return
        self._deleting = info.name
        self._say(
            f"delete {info.name!r} and every record it keeps? y deletes it · esc keeps it",
            tone=RED,
        )

    def action_confirm_delete(self) -> None:
        name, self._deleting = self._deleting, None
        if name is None:
            return
        try:
            profiles.delete(name)
        except profiles.ProfileError as exc:
            self._say(str(exc), tone=RED)
            return
        self._say(f"deleted {name!r}")
        self._reload(select=DEFAULT_PROFILE)

    def action_cancel(self) -> None:
        if self._asking is not None:
            self._close_ask()
        self._deleting = None
        self._say("")

    def action_quit_picker(self) -> None:
        if self._asking is not None:
            return  # q is just a letter while typing a name
        self.exit(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter lands here, not on the binding: the table consumes the key itself."""
        self.action_open()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        what, rename_from = self._asking, self._rename_from
        if what is None:
            return
        try:
            if what == "new":
                profiles.create(name)
            elif rename_from is not None:
                profiles.rename(rename_from, name)
        except profiles.ProfileError as exc:
            self._say(str(exc), tone=RED)
            return
        self._close_ask()
        self._say("")
        self._reload(select=name)


def pick_profile() -> str | None:
    """Show the picker and return the chosen profile's name, or ``None`` to quit."""
    return PickerApp().run()
