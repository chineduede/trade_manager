"""The new UI's palette and the small text helpers every screen of it shares.

Cream on a warm near-black, with three accents that each mean one thing: red is a loss, a
refusal or anything that can cost money; blue is the copier doing its job (copying, fills);
yellow is a warning. Green is kept for the live line from the master to a slave. Rich styles inside widgets cannot read CSS variables, so the colours live
here once and the Textual theme is built from the same values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rich.box import Box
from rich.console import Console, ConsoleOptions, RenderResult
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


def key_hint(key: str, label: str, *, color: str = FG, primary: bool = False) -> Text:
    """One key of a key bar, drawn as a keycap -- a small filled block -- so what to press stands
    out from what it does. ``primary`` fills it in cream, for the one key a person most needs."""
    out = Text()
    if primary:
        cap = f"bold {BG} on {FG}"
    elif color != FG:
        cap = f"bold {BG} on {color}"
    else:
        cap = f"bold {FG} on {CHIP_BG}"
    out.append(f" {key} ", style=cap)
    out.append(f" {label}", style=f"bold {color}" if primary else color)
    return out


def left_right(left: Text | str, right: Text | str) -> Table:
    """A line with one part flush left and one flush right, at whatever width it is given."""
    grid = Table.grid(expand=True)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(left, right)
    return grid


@dataclass(frozen=True)
class Piece:
    """One part of a :class:`Fitted` line: what it says, and what may be done to it when the
    line is too narrow to say everything."""

    text: Text
    drop: int = 0
    """0: always kept. Otherwise it may be left out, the highest number first."""
    short: Text | None = None
    """A shorter way to say it."""
    shorten: float | None = None
    """When the short form is used, on the same scale as :attr:`drop`. By default just before
    the part would be dropped; for a part that is never dropped, only once everything that
    can be has been."""


class Fitted:
    """A line with parts flush left and flush right, that fits itself to the width it is
    drawn at, on every redraw -- so a narrower terminal gets a shorter line rather than one
    cut off at the end, where panic and quit are.

    Too wide, it shortens and leaves out parts by their :attr:`Piece.shorten` and
    :attr:`Piece.drop`, highest first, and past that lets the left side end in an ellipsis."""

    def __init__(
        self,
        left: Sequence[Piece | Text],
        right: Sequence[Piece | Text] = (),
        *,
        joiner: str = "  ",
    ) -> None:
        self.left = [p if isinstance(p, Piece) else Piece(p) for p in left]
        self.right = [p if isinstance(p, Piece) else Piece(p) for p in right]
        self.joiner = joiner

    def _join(self, pieces: Iterable[tuple[Piece, bool]]) -> Text:
        texts = [p.short if short and p.short is not None else p.text for p, short in pieces]
        return Text(self.joiner).join(texts)

    def fit(self, width: int) -> tuple[Text, Text]:
        everything = [*self.left, *self.right]
        kept = {id(p) for p in everything}
        shortened: set[int] = set()

        def line() -> tuple[Text, Text]:
            def side(pieces: list[Piece]) -> Text:
                return self._join((p, id(p) in shortened) for p in pieces if id(p) in kept)

            return side(self.left), side(self.right)

        def fits() -> bool:
            left, right = line()
            gap = 2 if left.cell_len and right.cell_len else 0
            return left.cell_len + gap + right.cell_len <= width

        steps: list[tuple[float, int, str, Piece]] = []
        for i, p in enumerate(everything):
            if p.drop:
                steps.append((p.drop, i, "drop", p))
            if p.short is not None:
                when = p.shorten if p.shorten is not None else (p.drop + 0.5 if p.drop else 0.5)
                steps.append((when, i, "short", p))
        # Highest first; among equals, the rightmost.
        steps.sort(key=lambda step: (step[0], step[1]), reverse=True)
        for _, _, kind, piece in steps:
            if fits():
                break
            if kind == "short":
                shortened.add(id(piece))
            else:
                kept.discard(id(piece))
        return line()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield left_right(*self.fit(options.max_width))


def badge(text: str, color: str) -> Text:
    """A boxed word such as PAUSED or HELD BACK. A terminal cannot draw a box inside a line, so
    it is brackets around bold capitals."""
    return Text(f"[ {text} ]", style=f"bold {color}")


RULED = Box("    \n    \n    \n    \n────\n    \n    \n    \n")
"""No borders, only a rule between rows: the day book's and the details page's columns."""
