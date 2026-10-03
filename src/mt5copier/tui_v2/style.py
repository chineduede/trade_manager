"""The new UI's palette and the small text helpers every screen of it shares.

Cream on a warm near-black, with three accents that each mean one thing: red is a loss, a
refusal or anything that can cost money; blue is the copier doing its job (copying, fills);
yellow is a warning. Green is kept for the live line from the master to a slave. Rich styles inside widgets cannot read CSS variables, so the colours live
here once and the Textual theme is built from the same values.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.theme import Theme

BG = "#1b1813"
BACKDROP = "#0c0b09"
FG = "#ece4d0"
MUTED = "#a39a87"
FAINT = "#5c5547"
CHIP_BG = "#3a3329"
RED = "#e5645a"
BLUE = "#7ea6e0"
YELLOW = "#e8b84a"
GREEN = "#7fbf6e"
"""Live: the master's border while it is being copied, and the line and border of every slave
it is copying to."""
SELECTED_BG = "#45413a"
"""The selected card or chip: the cream at about a fifth, over the background, so it shows
through rather than covering it (the stylesheet's ``$foreground 20%``)."""

THEME = Theme(
    name="copier-cream",
    primary=FG,
    secondary=BLUE,
    accent=BLUE,
    warning=YELLOW,
    error=RED,
    success=BLUE,
    foreground=FG,
    background=BG,
    surface=BG,
    panel=FAINT,
    dark=True,
)

TIER_COLOR: dict[str, str] = {
    "good": BLUE,
    "warning": YELLOW,
    "serious": RED,
    "critical": RED,
    "muted": MUTED,
}


def spaced(text: str) -> str:
    """A heading with its letters spaced out, which is the nearest a terminal gets to the
    design's wide-tracked capitals."""
    return " ".join(text.upper())


def amount(value: float | None) -> str:
    """Money the way the design writes it: no currency mark, a loss in parentheses."""
    if value is None:
        return "—"
    return f"({abs(value):,.2f})" if value < 0 else f"{value:,.2f}"


def floating(value: float | None) -> Text:
    """``(37.32) floating`` in red, or the plain amount when it is not a loss."""
    if value is None:
        return Text("— floating", style=MUTED)
    return Text(f"{amount(value)} floating", style=RED if value < 0 else "")


def key_hint(key: str, label: str, *, color: str = FG) -> Text:
    """One key of a key bar: the key underlined and bold, then what it does."""
    out = Text()
    out.append(key, style=f"bold underline {color}")
    out.append(f" {label}", style=color)
    return out


def left_right(left: Text | str, right: Text | str) -> Table:
    """A line with one part flush left and one flush right, at whatever width it is given."""
    grid = Table.grid(expand=True)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(left, right)
    return grid


def badge(text: str, color: str) -> Text:
    """A boxed word such as PAUSED or HELD BACK. A terminal cannot draw a box inside a line, so
    it is brackets around bold capitals."""
    return Text(f"[ {text} ]", style=f"bold {color}")
