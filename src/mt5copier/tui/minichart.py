"""The minichart: the feed's candles, drawn in block characters under the order ticket.

Pure, like :mod:`.cards`: everything here turns candles and levels into a
:class:`rich.text.Text`, so the tests can pin the picture down without starting the app.
The last candle is live -- the current bid moves its close (and its high or low when it
goes past them), so the chart breathes with the quote line between bar refreshes.

What is drawn besides the candles: the bid as a dotted blue line, and the ticket's stop and
target as red and green ones, each labelled on the price axis -- so the person sees where
their levels sit against the bars as they type them. A level outside the bars' range is not
drawn (the range belongs to the price action); its number is still on the ticket.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text

from ..protocol import CHART_TIMEFRAMES, Candle
from .style import BLUE, GREEN, MUTED, RED

_AXIS = 10
"""Columns kept for the price labels on the right."""

_BODY = "█"
_WICK = "│"
_LEVEL = "┈"


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


def chart(
    candles: Sequence[Candle],
    *,
    width: int,
    height: int,
    bid: float | None = None,
    sl: float = 0.0,
    tp: float = 0.0,
) -> Text:
    """The picture: one column per candle, newest at the right, price labels on the axis.

    ``width`` and ``height`` are the whole drawing, axis included. Levels (the bid in blue,
    the stop in red, the target in green) are dotted lines under the candles, drawn only
    inside the bars' own price range.
    """
    columns = width - _AXIS
    if columns < 10 or height < 4:
        return Text("the chart needs more room", style=MUTED)
    shown = live_last(candles, bid)[-columns:]
    if not shown:
        return Text("")

    lo = min(c.low for c in shown)
    hi = max(c.high for c in shown)
    pad = (hi - lo) * 0.05 or max(abs(hi) * 0.0001, 1e-9)
    lo -= pad
    hi += pad

    def row(price: float) -> int:
        return round((hi - price) / (hi - lo) * (height - 1))

    grid = [[(" ", "")] * columns for _ in range(height)]
    labels: list[tuple[str, str]] = [("", MUTED)] * height

    decimals = _decimals(hi - lo, hi)
    labels[0] = (f"{hi:.{decimals}f}", MUTED)
    labels[height - 1] = (f"{lo:.{decimals}f}", MUTED)
    labels[(height - 1) // 2] = (f"{(hi + lo) / 2:.{decimals}f}", MUTED)

    # Levels first, so the candles draw over them: the price action owns its pixels.
    for price, color in ((tp, GREEN), (sl, RED), (bid, BLUE)):
        if not price or not lo <= price <= hi:
            continue
        r = row(price)
        grid[r] = [(_LEVEL, color)] * columns
        labels[r] = (f"{price:.{decimals}f}", color)

    for i, c in enumerate(shown):
        color = GREEN if c.close >= c.open else RED
        for r in range(row(c.high), row(c.low) + 1):
            grid[r][i] = (_WICK, color)
        for r in range(row(max(c.open, c.close)), row(min(c.open, c.close)) + 1):
            grid[r][i] = (_BODY, color)

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
