"""Widgets of the UI's frame and Map page: the top bar, the key bar, terminal cards and
chips, and the lines that join the master to its slaves."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rich.console import RenderableType
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.geometry import Region, Size
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from .style import FAINT, FG, GREEN, MUTED, RED, SELECTED_BG


class QuickInput(Input):
    """A one-line entry typed into in place, with no dialog: the log's search, a copy's
    breakeven price. Esc backs out of it, rather than reaching the page under it."""

    BINDINGS = [Binding("escape", "back_out", "Back out", show=False)]

    class Escaped(Message):
        def __init__(self, box: QuickInput) -> None:
            super().__init__()
            self.box = box

        @property
        def control(self) -> QuickInput:
            return self.box

    def action_back_out(self) -> None:
        self.post_message(self.Escaped(self))


class ButtonRow(Horizontal):
    """A dialog's row of buttons, that the left and right arrows move along, round from one end
    to the other. Tab still does too; a button alone answers only enter and space."""

    BINDINGS = [
        Binding("left", "along(-1)", "Previous button", show=False),
        Binding("right", "along(1)", "Next button", show=False),
    ]

    def action_along(self, step: int) -> None:
        buttons = [b for b in self.query(Button) if b.focusable]
        focused = self.screen.focused
        if focused in buttons:
            buttons[(buttons.index(focused) + step) % len(buttons)].focus()


class Picked(Message):
    """A card was clicked: select that terminal. (A chip calls ``app.pick`` directly.)"""

    def __init__(self, terminal_id: str) -> None:
        super().__init__()
        self.terminal_id = terminal_id


class Card(Static):
    """One terminal on the Map: the master's wide card, or one slave's.

    It draws whatever the app hands it. Which border it has (solid, dashed for paused, red and
    dashed for held back) and whether it is the selected one are CSS classes, so the look of a
    state lives in the stylesheet beside the others.
    """

    def __init__(self, terminal_id: str, *, classes: str = "") -> None:
        super().__init__("", classes=f"card {classes}".strip())
        self.terminal_id = terminal_id

    def on_click(self) -> None:
        self.post_message(Picked(self.terminal_id))


# --- chips -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChipItem:
    terminal_id: str
    label: str
    usable: bool
    """False when there is no program to launch: drawn dashed and struck through."""
    selected: bool


def chip_rows(widths: list[int], room: int, gap: int = 1) -> list[list[int]]:
    """Which chips go on which row: as many as fit, in order, never splitting one. A chip wider
    than the room gets a row to itself."""
    rows: list[list[int]] = []
    used = 0
    for i, w in enumerate(widths):
        if not rows or used + gap + w > room:
            rows.append([i])
            used = w
        else:
            rows[-1].append(i)
            used += gap + w
    return rows


class Chips(Widget):
    """The unassigned terminals under NOT YET OPENED, each in its own box.

    A terminal cannot wrap boxes the way a browser wraps inline elements, so this lays them out
    itself at its current width -- which is also what keeps a name from breaking across lines.
    """

    DEFAULT_CSS = """
    Chips {
        height: auto;
        link-style: none;
        link-color-hover: $foreground;
        link-background-hover: $panel 60%;
        link-style-hover: bold not underline;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.items: list[ChipItem] = []

    def set_items(self, items: list[ChipItem]) -> None:
        if items != self.items:
            self.items = items
            self.refresh(layout=True)

    def lines(self, width: int) -> list[Text]:
        widths = [len(c.label) + 4 for c in self.items]
        out: list[Text] = []
        for row in chip_rows(widths, max(width, 1)):
            top, mid, bottom = Text(), Text(), Text()
            for n, i in enumerate(row):
                chip = self.items[i]
                if n:
                    for line in (top, mid, bottom):
                        line.append(" ")
                h, v = ("─", "│") if chip.usable else ("╌", "╎")
                click = Style.from_meta({"@click": f"app.pick({chip.terminal_id!r})"})
                box = Style.parse(FG if chip.usable else MUTED) + click
                if chip.selected:
                    label = Style.parse(f"bold {FG} on {SELECTED_BG}") + click
                elif not chip.usable:
                    label = Style.parse(f"strike {MUTED}") + click
                else:
                    label = Style.parse(FG) + click
                inner = len(chip.label) + 2
                top.append(f"┌{h * inner}┐", style=box)
                mid.append(v, style=box)
                mid.append(f" {chip.label} ", style=label)
                mid.append(v, style=box)
                bottom.append(f"└{h * inner}┘", style=box)
            out += [top, mid, bottom]
        return out

    def chip_region(self, terminal_id: str) -> Region | None:
        """Where one chip is drawn, on screen, laid out as :meth:`lines` lays it out."""
        widths = [len(c.label) + 4 for c in self.items]
        for row_no, row in enumerate(chip_rows(widths, max(self.size.width, 1))):
            x = 0
            for i in row:
                if self.items[i].terminal_id == terminal_id:
                    # The content region: the widget's padding sits above the first row.
                    top = self.content_region
                    return Region(top.x + x, top.y + row_no * 3, widths[i], 3)
                x += widths[i] + 1
        return None

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.lines(width))

    def render(self) -> RenderableType:
        return Text("\n").join(self.lines(self.size.width))


# --- the tree --------------------------------------------------------------------------------
#
# Slave cards sit in rows of four under the master. The first row's rail hangs off a stem from
# the middle of the master's card; when there is more than one row, a trunk runs down the left
# edge and each later row's rail branches off it. The line to each slave is coloured by what
# it is doing: green while trades are copied to it, grey while it is paused, and red while
# nothing is copied to it for any other reason. A stretch shared by several slaves takes the
# best of their colours, so green always reaches every slave being copied to.


def line_color(kinds: Iterable[str]) -> str | None:
    """The colour of a stretch of line leading to slaves of these kinds; None for none."""
    kinds = set(kinds)
    if not kinds:
        return None
    if "copying" in kinds:
        return GREEN
    if "paused" in kinds:
        return FAINT
    return RED


def rail_lines(
    drops: list[tuple[int, str]],
    width: int,
    *,
    first: bool,
    trunk: bool,
    more: bool,
    below: Iterable[str] = (),
) -> list[Text]:
    """One row's connector, top to bottom.

    ``drops`` are (column, kind) for each card in the row. ``first`` adds the stem down from
    the master's middle. ``trunk`` means the tree has more than one row, so column 0 carries
    the trunk; ``more`` that it continues below this row, and ``below`` the kinds of the slaves
    in the rows further down, which the trunk leads on to.
    """
    drops = sorted((x, k) for x, k in drops if 0 <= x < width)
    if not drops or width < 4:
        return []
    below = list(below) if trunk and more else []
    middle = width // 2
    xs = [x for x, _ in drops]
    origin = middle if first else 0
    """Where the line enters this rail: down the stem, or off the trunk."""
    # What each column of the rail leads to: the drops on its far side from the origin.
    ends = [(x, [k]) for x, k in drops] + ([(0, below)] if below else [])
    leads: dict[int, list[str]] = {}
    for x, kinds in ends:
        for c in range(min(origin, x), max(origin, x) + 1):
            leads.setdefault(c, []).extend(kinds)
    lines: list[Text] = []

    if first:
        stem = [" "] * width
        stem[middle] = "│"
        line = Text("".join(stem))
        line.stylize(line_color([k for _, k in drops] + below) or FG, middle, middle + 1)
        lines.append(line)

    span = xs + ([middle] if first else []) + ([0] if trunk else [])
    lo, hi = min(span), max(span)
    rail = [" "] * width
    for x in range(lo, hi + 1):
        rail[x] = "─"
    for x in xs:
        rail[x] = "┬"
    if trunk:
        rail[0] = "┌" if first else ("├" if more else "└")
    elif lo != hi:
        rail[lo] = "┌" if lo in xs else "└"
    if lo != hi:
        rail[hi] = "┐" if hi in xs else "┘"
    if first:
        if lo == hi:
            rail[middle] = "│"
        elif lo < middle < hi:
            rail[middle] = "┼" if middle in xs else "┴"
    line = Text("".join(rail), style=FG)
    for c, kinds in leads.items():
        line.stylize(line_color(kinds) or FG, c, c + 1)
    lines.append(line)

    down = [" "] * width
    colors: dict[int, str | None] = {}
    if trunk and more:
        down[0] = "│"
        colors[0] = line_color(below)
    for x, kind in drops:
        down[x] = "│"
        colors[x] = line_color([kind])
    line = Text("".join(down), style=FG)
    for x, color in colors.items():
        line.stylize(color or FG, x, x + 1)
    lines.append(line)
    return lines


class Rail(Widget):
    """The connector above one row of slave cards. See :func:`rail_lines`.

    Drops are placed where the row's cards actually are, read from their regions after layout,
    so they land over each card's middle whatever the terminal's width.
    """

    DEFAULT_CSS = """
    Rail {
        height: auto;
    }
    """

    def __init__(self, row: Widget, *, first: bool, trunk: bool, more: bool) -> None:
        super().__init__()
        self.row = row
        self.first = first
        self.trunk = trunk
        self.more = more
        self.kinds: dict[str, str] = {}
        self.below: list[str] = []
        """The kinds of the slaves in the rows further down."""

    def terminal_ids(self) -> list[str]:
        return [c.terminal_id for c in self.row.query(Card)]

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return 3 if self.first else 2

    def render(self) -> RenderableType:
        drops = [
            (c.region.x + c.region.width // 2 - self.region.x, self.kinds.get(c.terminal_id, ""))
            for c in self.row.query(Card)
            if c.region.width > 0
        ]
        return Text("\n").join(
            rail_lines(
                drops,
                self.size.width,
                first=self.first,
                trunk=self.trunk,
                more=self.more,
                below=self.below,
            )
        )


class Trunk(Widget):
    """The trunk's column beside a row of cards: a line while the tree continues below,
    coloured by the slaves in the rows it leads on to (see :func:`line_color`)."""

    DEFAULT_CSS = """
    Trunk {
        width: 2;
        height: 100%;
    }
    """

    def __init__(self, *, more: bool) -> None:
        super().__init__()
        self.more = more
        self.below: list[str] = []
        """The kinds of the slaves in the rows further down."""

    def render(self) -> RenderableType:
        if not self.more:
            return Text("")
        color = line_color(self.below) or FG
        return Text("\n".join(["│ "] * self.size.height), style=color)


# --- frame -------------------------------------------------------------------------------------


class TopBar(Static):
    """MT5 COPIER, the page tabs, what is being copied, the clock and LIVE / DRY RUN."""


class KeyBar(Static):
    """The keys worth knowing on the page showing, with panic and quit kept to the right."""
