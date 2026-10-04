"""The action menu: what can be done to the selected terminal, beside it.

Every action here already has its own key on the Map; the menu is how a person finds them.
So it offers each terminal only what will work on it as it stands -- a terminal waiting under
NOT YET OPENED can be made the master or a slave, a held slave can have its untracked positions
signed off -- and picking an entry runs the very same app action its key does.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Region, Size
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..config import Role, TerminalConfig
from .style import MUTED


@dataclass(frozen=True, slots=True)
class MenuItem:
    key: str
    """The key that does the same from the Map, shown beside the entry and live in the menu."""
    label: str
    action: str | None
    """The app action it runs (``set_slave`` runs ``action_set_slave``), or None for a line
    that only explains why something is not on offer."""


def terminal_actions(
    cfg: TerminalConfig,
    *,
    has_master: bool,
    orphans_waiting: bool,
    not_a_demo: bool = False,
    link_ok: bool = True,
) -> list[MenuItem]:
    """What can be done to this terminal now, most likely first.

    ``orphans_waiting`` is whether it is held for untracked positions nobody has signed off.
    ``not_a_demo`` is whether it is connected to an account that is not a demo, so the
    demo-only guard is holding it back. ``link_ok`` is whether its worker is connected and
    reporting; reconnect and restart are only offered when it is not.
    """
    items: list[MenuItem] = []
    if cfg.role is Role.UNASSIGNED:
        if cfg.exe_path is None:
            items.append(MenuItem("", "not installed here, so it cannot be launched", None))
        elif has_master:
            items.append(MenuItem("s", "make it a slave", "set_slave"))
        else:
            items.append(MenuItem("m", "make it the master", "set_master"))
        items.append(MenuItem("enter", "details", "open_terminal"))
        items.append(MenuItem("e", "settings", "edit_settings"))
        return items

    items.append(MenuItem("enter", "details", "open_terminal"))
    if cfg.role is Role.SLAVE:
        if orphans_waiting:
            items.append(MenuItem("o", "sign off its untracked positions", "acknowledge_orphans"))
        items.append(MenuItem("e", "settings", "edit_settings"))
        items.append(
            MenuItem("p", "resume" if cfg.paused else "pause: take no new trades", "toggle_pause")
        )
        items.append(MenuItem("k", "guards: prop-firm rules", "guards"))
    # The demo-only guard stops orders, so it only ever holds back a slave on a real account.
    # Where it has been lifted, the way to put it back is offered on any terminal.
    if cfg.allow_live_trading:
        items.append(MenuItem("l", "live trading is allowed: turn it off", "live_override"))
    elif cfg.role is Role.SLAVE and not_a_demo:
        items.append(MenuItem("l", "allow live trading: not a demo account", "live_override"))
    if not link_ok:
        items.append(MenuItem("c", "reconnect to the terminal", "reconnect"))
        items.append(MenuItem("ctrl+r", "restart its worker process", "restart_worker"))
    unassign = "unassign: stop copying to it" if cfg.role is Role.SLAVE else "unassign the master"
    items.append(MenuItem("u", unassign, "unassign"))
    return items


def place(anchor: Region, size: Size, screen: Size) -> tuple[int, int]:
    """Where the menu's top-left corner goes: under the anchor, or over it if there is no room
    below, and pulled left if it would run off the right edge."""
    x = max(0, min(anchor.x, screen.width - size.width))
    below = anchor.bottom
    y = below if below + size.height <= screen.height else anchor.y - size.height
    return x, max(0, y)


class ActionMenu(ModalScreen[str | None]):
    """The menu itself. Returns the chosen action's name, or None if it was closed."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("space", "close", "Close", show=False),
    ]

    def __init__(self, title: str, items: list[MenuItem], anchor: Region) -> None:
        super().__init__()
        self._title = title
        self._items = items
        self._anchor = anchor
        self._keys = {i.key: i.action for i in items if i.action and i.key != "enter"}

    def compose(self) -> ComposeResult:
        width = max(len(i.key) for i in self._items) if self._items else 0
        options: list[Option] = []
        for n, item in enumerate(self._items):
            # No colour on an entry that can be picked: the list colours it, so the highlighted
            # one can turn dark on cream.
            line = Text()
            line.append(item.key, style="bold underline")
            line.append(" " * (width - len(item.key) + 2))
            line.append(item.label, style="" if item.action else MUTED)
            options.append(Option(line, id=str(n), disabled=item.action is None))
        with Vertical(id="menu") as box:
            box.border_title = self._title
            yield OptionList(*options)

    def on_mount(self) -> None:
        menu = self.query_one("#menu")
        options = self.query_one(OptionList)
        keys = max((len(i.key) for i in self._items), default=0)
        widest = keys + 2 + max((len(i.label) for i in self._items), default=10)
        # Border and the list's padding, two columns each side, and one spare.
        size = Size(max(widest, len(self._title) + 4) + 5, len(self._items) + 2)
        menu.styles.width = size.width
        x, y = place(self._anchor, size, self.app.size)
        menu.styles.offset = (x, y)
        first = next((n for n, i in enumerate(self._items) if i.action), None)
        options.highlighted = first
        options.focus()

    def on_key(self, event: events.Key) -> None:
        action = self._keys.get(event.key)
        if action is not None:
            event.stop()
            self.dismiss(action)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        item = self._items[int(event.option.id or 0)]
        if item.action is not None:
            self.dismiss(item.action)

    def on_click(self, event: events.Click) -> None:
        """A click anywhere outside the menu closes it, as an open menu should."""
        if not self.query_one("#menu").region.contains(event.screen_x, event.screen_y):
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
