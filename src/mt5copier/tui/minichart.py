"""The minichart: the feed's candles, drawn in box characters under the order ticket.

Pure, like :mod:`.cards`: everything here turns candles and levels into a
:class:`rich.text.Text`, so the tests can pin the picture down without starting the app.
The last candle is live -- the current bid moves its close (and its high or low when it
goes past them), so the chart breathes with the quote line between bar refreshes.

Each candle is one slim column -- a heavy line for the body, a light one for the wick --
with a blank column between neighbours, so candles read as candles instead of fusing into
a solid mass. The body/wick boundary lands on half a cell: ``╽`` is a wick becoming a body
mid-cell, which doubles the vertical resolution the way the cards' half-block digits do.

What is drawn besides the candles: the bid as a dotted blue line, and the ticket's stop and
target as red and green ones, each labelled on the price axis -- so the person sees where
their levels sit against the bars as they type them. A level outside the bars' range is not
drawn (the range belongs to the price action); its number is still on the ticket.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.text import Text

from ..protocol import CHART_TIMEFRAMES, Candle
from .style import BLUE, GREEN, MUTED, RED

_AXIS = 10
"""Columns kept for the price labels on the right."""

_LEVEL = "┈"

BODY_GLYPHS = frozenset("┃╹╻╿╽")
"""Every character a candle's body can put in a cell (heavy strokes)."""
WICK_GLYPHS = frozenset("│╵╷")
"""Every character a wick alone can put in a cell (light strokes)."""


def _glyph(body_upper: bool, body_lower: bool, wick_upper: bool, wick_lower: bool) -> str:
    """One cell of one candle, from what covers its upper and lower half."""
    if body_upper and body_lower:
        return "┃"
    if body_upper:
        return "╿" if wick_lower else "╹"
    if body_lower:
        return "╽" if wick_upper else "╻"
    if wick_upper and wick_lower:
        return "│"
    if wick_upper:
        return "╵"
    if wick_lower:
        return "╷"
    return ""


def timeframe_name(timeframe: int) -> str:
    return next((name for name, value in CHART_TIMEFRAMES if value == timeframe), "?")


def next_timeframe(timeframe: int) -> int:
    """The one after this, wrapping: what the t key cycles through."""
    values = [value for _, value in CHART_TIMEFRAMES]
    try:
        return values[(values.index(timeframe) + 1) % len(values)]
    except ValueError:
        return values[0]


def live_last(candles: Sequence[Candle], bid: float | None) -> list[Candle]:
    """The candles with the current bid folded into the last one, as the terminal's own
    chart does between ticks of the bar clock."""
    out = list(candles)
    if not out or bid is None:
        return out
    last = out[-1]
    out[-1] = Candle(
        ts=last.ts,
        open=last.open,
        high=max(last.high, bid),
        low=min(last.low, bid),
        close=bid,
    )
    return out


def _decimals(span: float, hi: float) -> int:
    """Enough decimal places that neighbouring labels differ, within reason."""
    if hi >= 2_000:
        return 2
    if hi >= 50:
        return 3
    return 5 if span < 0.2 else 4


def window(candles: Sequence[Candle], bid: float | None, width: int) -> list[Candle]:
    """The candles a chart of ``width`` shows: the newest that fit, the last one live."""
    columns = width - _AXIS
    if columns < 10:
        return []
    return live_last(candles, bid)[-max(1, (columns + 1) // 2) :]


@dataclass(frozen=True, slots=True)
class Scale:
    """How a drawn chart maps prices to rows and back.

    What makes a line draggable: the mouse hands the page a row, and only the scale the
    chart was *drawn with* can say which price line sits there and what price a row the
    line is dragged to means. Built by :func:`scale_of` from the same window the picture
    was built from, so the two can never disagree.
    """

    hi: float
    """The price at the top edge, padding included."""
    lo: float
    """The price at the bottom edge, padding included."""
    height: int

    def half(self, price: float) -> int:
        """The half-cell a price lands in: the drawing's own vertical unit."""
        return round((self.hi - price) / (self.hi - self.lo) * (self.height * 2 - 1))

    def row(self, price: float) -> int:
        return self.half(price) // 2

    def price(self, row: int) -> float:
        """The price at a row's centre, clamped to the drawing: what a drag there means."""
        row = max(0, min(self.height - 1, row))
        return self.hi - (row + 0.5) / self.height * (self.hi - self.lo)

    def fmt(self, price: float) -> str:
        """A price written as the axis writes it."""
        return f"{price:.{_decimals(self.hi - self.lo, self.hi)}f}"

    def grabbed(self, row: int, price: float) -> bool:
        """Whether a press on ``row`` takes hold of the line at ``price``: the line is
        drawn (inside the range) and the press is within one row of it -- a one-cell line
        is a thin thing to hit exactly."""
        if not price or not self.lo <= price <= self.hi:
            return False
        return abs(row - self.row(price)) <= 1


def scale_of(shown: Sequence[Candle], height: int) -> Scale | None:
    """The scale a chart of ``shown`` draws with: their range, padded 5% each side."""
    if not shown:
        return None
    lo = min(c.low for c in shown)
    hi = max(c.high for c in shown)
    pad = (hi - lo) * 0.05 or max(abs(hi) * 0.0001, 1e-9)
    return Scale(hi=hi + pad, lo=lo - pad, height=height)


def chart(
    candles: Sequence[Candle],
    *,
    width: int,
    height: int,
    bid: float | None = None,
    sl: float = 0.0,
    tp: float = 0.0,
) -> Text:
    """The picture: one slim candle every other column, newest against the axis, price
    labels on the right.

    ``width`` and ``height`` are the whole drawing, axis included. Levels (the bid in blue,
    the stop in red, the target in green) are dotted lines running behind the candles,
    drawn only inside the bars' own price range.
    """
    columns = width - _AXIS
    if columns < 10 or height < 4:
        return Text("the chart needs more room", style=MUTED)
    shown = window(candles, bid, width)
    scale = scale_of(shown, height)
    if scale is None:
        return Text("")

    grid = [[(" ", "")] * columns for _ in range(height)]
    labels: list[tuple[str, str]] = [("", MUTED)] * height

    labels[0] = (scale.fmt(scale.hi), MUTED)
    labels[height - 1] = (scale.fmt(scale.lo), MUTED)
    labels[(height - 1) // 2] = (scale.fmt((scale.hi + scale.lo) / 2), MUTED)

    # Levels first, so the candles draw over them: the price action owns its pixels.
    for price, color in ((tp, GREEN), (sl, RED), (bid, BLUE)):
        if not price or not scale.lo <= price <= scale.hi:
            continue
        r = scale.row(price)
        grid[r] = [(_LEVEL, color)] * columns
        labels[r] = (scale.fmt(price), color)

    half = scale.half
    for i, c in enumerate(shown):
        column = columns - 1 - 2 * (len(shown) - 1 - i)
        color = GREEN if c.close >= c.open else RED
        wick_top, body_top = half(c.high), half(max(c.open, c.close))
        body_bottom, wick_bottom = half(min(c.open, c.close)), half(c.low)
        for r in range(wick_top // 2, wick_bottom // 2 + 1):
            upper, lower = 2 * r, 2 * r + 1
            drawn = _glyph(
                body_top <= upper <= body_bottom,
                body_top <= lower <= body_bottom,
                wick_top <= upper <= wick_bottom,
                wick_top <= lower <= wick_bottom,
            )
            if drawn:
                grid[r][column] = (drawn, color)

    out = Text()
    for r in range(height):
        if r:
            out.append("\n")
        run = ""
        run_style = ""
        for char, style in grid[r]:
            if style != run_style and run:
                out.append(run, style=run_style or None)
                run = ""
            run_style = style
            run += char
        if run:
            out.append(run, style=run_style or None)
        label, label_style = labels[r]
        out.append(f" {label:>{_AXIS - 1}}", style=label_style)
    return out
