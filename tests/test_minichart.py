"""The minichart's picture, pinned down as text: candles, wicks, levels and the axis."""

from __future__ import annotations

from mt5copier.protocol import CHART_TIMEFRAMES, Candle
from mt5copier.tui.minichart import chart, live_last, next_timeframe, timeframe_name


def candle(o: float, h: float, lo: float, c: float, ts: int = 0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c)


def lines(text: object) -> list[str]:
    return str(getattr(text, "plain", text)).split("\n")


def test_every_line_is_exactly_the_asked_width() -> None:
    drawn = lines(chart([candle(1.0, 2.0, 1.0, 2.0)], width=30, height=5))
    assert len(drawn) == 5
    assert all(len(line) == 30 for line in drawn)


def test_a_candles_body_fills_its_column() -> None:
    # One candle whose body is its whole range: every row of column 0 is body.
    drawn = lines(chart([candle(1.0, 2.0, 1.0, 2.0)], width=30, height=5))
    assert all(line[0] == "█" for line in drawn)


def test_wicks_poke_out_of_the_body() -> None:
    # A small body low in its range: wick above and below, body between.
    drawn = lines(chart([candle(1.05, 2.0, 1.0, 1.1)], width=30, height=7))
    column = [line[0] for line in drawn]
    assert column[0] == "│", "the high is wick"
    assert column[-1] == "│", "so is the low, below the body"
    assert "█" in column, "the body sits between them"


def test_newest_candles_win_when_the_chart_is_narrow() -> None:
    # 25 candles into 10 columns (width 20 - axis 10): the last 10 are drawn. The oldest
    # shown is flat at 1.0; anything older was at 9.0 and would stretch the axis to it.
    old = [candle(9.0, 9.0, 9.0, 9.0)] * 15
    new = [candle(1.0, 2.0, 1.0, 2.0)] * 10
    drawn = lines(chart(old + new, width=20, height=5))
    assert "9." not in drawn[0], "the axis only spans what is shown"


def test_the_bid_is_a_dotted_line_with_its_own_label() -> None:
    drawn = lines(chart([candle(1.0, 2.0, 1.0, 2.0)] * 2, width=30, height=5, bid=1.5))
    middle = drawn[2]
    assert "┈" in middle, "the bid is drawn across the empty columns"
    assert middle.rstrip().endswith("1.5000"), "and labelled on the axis"
    assert middle[0] == "█", "the candles draw over it"


def test_levels_inside_the_range_are_drawn_and_outside_are_not() -> None:
    bars = [candle(1.0, 2.0, 1.0, 2.0)] * 2
    with_sl = lines(chart(bars, width=30, height=9, sl=1.5))
    assert any("┈" in line and line.rstrip().endswith("1.5000") for line in with_sl)
    without = lines(chart(bars, width=30, height=9, sl=5.0))
    assert not any("┈" in line for line in without), "a level outside the bars is not drawn"


def test_the_axis_reads_high_middle_and_low() -> None:
    drawn = lines(chart([candle(1.0, 2.0, 1.0, 2.0)], width=30, height=5))
    assert drawn[0].rstrip().endswith("2.0500"), "the top label is the padded high"
    assert drawn[-1].rstrip().endswith("0.9500")
    assert drawn[2].rstrip().endswith("1.5000")


def test_too_small_a_chart_says_so() -> None:
    assert "more room" in str(chart([candle(1, 2, 1, 2)], width=15, height=5).plain)


def test_no_candles_draw_nothing() -> None:
    assert chart([], width=30, height=5).plain == ""


def test_the_live_bid_moves_the_last_candle() -> None:
    bars = [candle(1.0, 1.2, 0.9, 1.1), candle(1.1, 1.2, 1.0, 1.15)]
    moved = live_last(bars, 1.3)
    assert moved[0] == bars[0], "only the last candle is live"
    assert (moved[-1].close, moved[-1].high) == (1.3, 1.3), "a new high stretches it"
    assert live_last(bars, None) == bars
    assert live_last([], 1.3) == []


def test_the_timeframes_cycle_and_name_themselves() -> None:
    values = [value for _, value in CHART_TIMEFRAMES]
    walked = [values[0]]
    for _ in range(len(values)):
        walked.append(next_timeframe(walked[-1]))
    assert walked == [*values, values[0]], "t walks M1 through H1 and wraps"
    assert timeframe_name(16385) == "H1"
    assert next_timeframe(999) == values[0], "an unknown timeframe resets to the first"
