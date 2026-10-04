"""A sentence of words and fields that wraps like text: the settings forms' lines.

Textual lays a row out in one line whatever its width, so a sentence such as "Risk [1] [% of
balance] on each trade, over the master's stop distance" ran off the right of a narrow
terminal. :class:`Flow` places its children left to right and starts a new line when the next
one does not fit, as words do; :func:`flow_words` splits text into one widget per word, so a
long run of words breaks between them rather than as a whole.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from textual.geometry import NULL_OFFSET, NULL_SPACING, Region, Size
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.layouts.factory import LAYOUT_MAP
from textual.widget import Widget
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.css.scalar import Scalar


class FlowLayout(Layout):
    """Left to right, a new line whenever the next child would not fit."""

    name = "flow"

    def arrange(
        self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True
    ) -> ArrangeResult:
        parent.pre_layout(self)
        viewport = parent.app.viewport_size
        width = max(size.width, 1)
        placements: list[WidgetPlacement] = []
        x = y = line = 0

        def box(child: Widget, room: int) -> tuple[int, int, int]:
            # A fraction width (``1fr``) takes the room left on the line.
            model = child._get_box_model(
                Size(width, size.height),
                viewport,
                Fraction(max(room, 0)),
                Fraction(size.height),
                greedy=greedy,
            )
            return (
                min(int(model.width), width),
                int(model.height),
                model.margin.right,
            )

        shown = [child for child in children if child.display]

        def stretches(child: Widget) -> bool:
            scalar = child.styles.width
            return scalar is not None and scalar.is_fraction

        # Fields that stretch (``1fr``) share what the rest of the sentence leaves, so two of
        # them on one line get half each, rather than the first taking it all.
        fixed = sum(sum(box(c, 0)[::2]) for c in shown if not stretches(c))
        share = max(width - fixed, 0) // max(sum(1 for c in shown if stretches(c)), 1)

        for child in shown:
            room = share if stretches(child) else width - x
            w, h, gap = box(child, room)
            minimum = _min_width(child.styles.min_width, width)
            if x and (x + w > width or w < minimum):
                y += line
                x = line = 0
                w, h, gap = box(child, share if stretches(child) else width)
            placements.append(
                WidgetPlacement(
                    Region(x, y, w, h), NULL_OFFSET, NULL_SPACING, child, 0, False, False, False
                )
            )
            x += w + gap
            line = max(line, h)
        return placements


def _min_width(scalar: Scalar | None, width: int) -> int:
    if scalar is None:
        return 0
    return int(scalar.resolve(Size(width, 0), Size(width, 0)))


LAYOUT_MAP.setdefault(FlowLayout.name, FlowLayout)


class Flow(Widget):
    """A line of a form: its words and fields, wrapped to the width it has."""

    DEFAULT_CSS = """
    Flow {
        height: auto;
    }
    """

    def __init__(
        self, *children: Widget, id: str | None = None, classes: str | None = None
    ) -> None:
        super().__init__(*children, id=id, classes=classes)
        self.styles.layout = FlowLayout.name


class Word(Static):
    """One word of a sentence in a :class:`Flow`, with the space after it."""

    DEFAULT_CSS = """
    Word {
        width: auto;
        height: 1;
    }
    """


def flow_words(text: str, *, before: bool = False) -> list[Word]:
    """``text`` as one :class:`Word` per word. ``before`` puts a space ahead of the first, as
    between a field and the words after it."""
    parts = text.split()
    return [
        Word(f"{' ' if before and i == 0 else ''}{part} ", classes="w")
        for i, part in enumerate(parts)
    ]
