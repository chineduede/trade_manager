"""Copy-health statistics: the accumulator on its own, with no engine around it.

The engine wiring is tested beside the other dispatch tests in ``test_reconcile.py``.
"""

from __future__ import annotations

import pytest

from mt5copier.copy_stats import SAMPLE_HISTORY, StatsBook, slippage_in_points
from mt5copier.protocol import ActionKind, OrderResult, PositionType, WorkerState

TID = "B" * 32
OTHER = "C" * 32


def result(command_id: int = 1, **kw: object) -> OrderResult:
    base: dict[str, object] = {
        "command_id": command_id,
        "terminal_id": TID,
        "op": ActionKind.OPEN,
        "master_ticket": 11,
        "ok": True,
        "attempts": 1,
    }
    base.update(kw)
    return OrderResult(**base)  # type: ignore[arg-type]


def sent(book: StatsBook, command_id: int = 1, **kw: object) -> None:
    base: dict[str, object] = {
        "sent_at": 100.0,
        "op": ActionKind.OPEN,
        "direction": PositionType.BUY,
        "master_entry": 1.08500,
    }
    base.update(kw)
    book.on_sent(command_id, **base)  # type: ignore[arg-type]


# --- slippage ---------------------------------------------------------------------------------


def test_a_buy_filled_higher_slipped_against_us() -> None:
    assert slippage_in_points(PositionType.BUY, 1.08500, 1.08520, 0.00001) == pytest.approx(20)


def test_a_sell_filled_higher_slipped_in_our_favour() -> None:
    assert slippage_in_points(PositionType.SELL, 1.08500, 1.08520, 0.00001) == pytest.approx(-20)


@pytest.mark.parametrize(
    ("entry", "fill", "point"), [(1.0, 1.0, 0.0), (0.0, 1.0, 0.1), (1.0, 0.0, 0.1)]
)
def test_slippage_is_unknown_rather_than_invented(entry: float, fill: float, point: float) -> None:
    assert slippage_in_points(PositionType.BUY, entry, fill, point) is None


# --- latency and slippage samples ---------------------------------------------------------------


def test_latency_is_the_round_trip_from_send_to_answer() -> None:
    book = StatsBook()
    sent(book, sent_at=100.0)
    book.on_result(result(), now=100.25)
    stats = book.snapshot(TID, now=101.0)
    assert stats.latencies_ms == pytest.approx((250.0,))
    assert stats.latency_last == pytest.approx(250.0)


def test_latency_summary_is_last_median_and_max() -> None:
    book = StatsBook()
    for cid, took in enumerate([0.1, 0.5, 0.2], start=1):
        sent(book, cid, sent_at=0.0)
        book.on_result(result(cid), now=took)
    stats = book.snapshot(TID, now=1.0)
    assert stats.latency_last == pytest.approx(200.0)
    assert stats.latency_median == pytest.approx(200.0)
    assert stats.latency_max == pytest.approx(500.0)


def test_slippage_is_measured_on_a_filled_open_only() -> None:
    book = StatsBook()
    sent(book, 1, master_entry=1.08500)
    book.on_result(result(1, price=1.08503, point=0.00001), now=100.1)
    sent(book, 2, op=ActionKind.CLOSE)
    book.on_result(result(2, op=ActionKind.CLOSE, price=1.09, point=0.00001), now=100.1)
    sent(book, 3)
    book.on_result(result(3, ok=False, price=1.09, point=0.00001), now=100.1)
    assert book.snapshot(TID, now=101).slippage_points == pytest.approx((3.0,))


def test_a_fill_without_a_point_size_is_timed_but_not_measured() -> None:
    book = StatsBook()
    sent(book)
    book.on_result(result(price=1.08503), now=100.1)
    stats = book.snapshot(TID, now=101)
    assert stats.slippage_points == ()
    assert len(stats.latencies_ms) == 1


def test_samples_are_bounded() -> None:
    book = StatsBook()
    for cid in range(SAMPLE_HISTORY + 50):
        sent(book, cid, sent_at=float(cid))
        book.on_result(result(cid), now=float(cid) + 0.1)
    assert len(book.snapshot(TID, now=0).latencies_ms) == SAMPLE_HISTORY


def test_a_command_never_sent_has_no_latency() -> None:
    """An answer the book has no send time for is counted but not timed -- a made-up zero
    would drag the median down."""
    book = StatsBook()
    book.on_result(result(), now=5.0)
    stats = book.snapshot(TID, now=5.0)
    assert stats.succeeded == 1
    assert stats.latencies_ms == ()


def test_old_sends_are_forgotten() -> None:
    book = StatsBook()
    sent(book, 1, sent_at=10.0)
    sent(book, 2, sent_at=50.0)
    book.forget_sent_before(20.0)
    book.on_result(result(1), now=60.0)
    book.on_result(result(2), now=60.0)
    assert book.snapshot(TID, now=60).latencies_ms == pytest.approx((10_000.0,))


# --- counters -----------------------------------------------------------------------------------


def test_outcomes_are_counted_by_kind() -> None:
    book = StatsBook()
    book.on_result(result(1), now=0)
    book.on_result(result(2, ok=False), now=0)
    book.on_result(result(3, ok=False, retryable=True), now=0)
    book.on_result(result(4, attempts=3), now=0)
    book.on_late(TID)
    book.on_expired(TID)
    stats = book.snapshot(TID, now=0)
    assert (stats.succeeded, stats.refused, stats.failed_transient) == (2, 1, 1)
    assert stats.answered == 4
    assert stats.retried == 1
    assert stats.dropped_late == 1
    assert stats.expired == 1


def test_an_abandoned_command_is_a_transient_failure_without_a_time() -> None:
    book = StatsBook()
    sent(book)
    book.on_abandoned(result(ok=False, retryable=True))
    book.on_result(result(), now=200.0)  # cannot be timed any more either
    stats = book.snapshot(TID, now=200)
    assert stats.failed_transient == 1
    assert stats.latencies_ms == ()


def test_skips_are_grouped_by_reason_most_common_first() -> None:
    book = StatsBook()
    book.on_skipped(TID, "no stop")
    book.on_skipped(TID, "blacklisted")
    book.on_skipped(TID, "no stop")
    stats = book.snapshot(TID, now=0)
    assert stats.skipped == (("no stop", 2), ("blacklisted", 1))
    assert stats.skipped_total == 3


def test_terminals_are_kept_apart() -> None:
    book = StatsBook()
    book.on_result(result(), now=0)
    book.on_skipped(TID, "x")
    other = book.snapshot(OTHER, now=0)
    assert other.answered == 0
    assert other.skipped == ()


# --- connection uptime ----------------------------------------------------------------------------


def test_uptime_runs_from_the_moment_of_connecting() -> None:
    book = StatsBook()
    book.on_state(TID, WorkerState.CONNECTING, now=0.0)
    assert book.snapshot(TID, now=1.0).connected_for_s is None
    book.on_state(TID, WorkerState.CONNECTED, now=10.0)
    book.on_state(TID, WorkerState.CONNECTED, now=20.0)  # still the same connection
    assert book.snapshot(TID, now=25.0).connected_for_s == pytest.approx(15.0)


def test_losing_the_connection_counts_and_restarts_the_clock() -> None:
    book = StatsBook()
    book.on_state(TID, WorkerState.CONNECTED, now=0.0)
    book.on_state(TID, WorkerState.DISCONNECTED, now=5.0)
    book.on_state(TID, WorkerState.DISCONNECTED, now=6.0)
    stats = book.snapshot(TID, now=7.0)
    assert stats.connected_for_s is None
    assert stats.disconnects == 1

    book.on_state(TID, WorkerState.CONNECTED, now=8.0)
    assert book.snapshot(TID, now=9.0).connected_for_s == pytest.approx(1.0)


def test_stopping_a_worker_on_purpose_is_not_a_disconnect() -> None:
    book = StatsBook()
    book.on_state(TID, WorkerState.CONNECTED, now=0.0)
    book.on_worker_gone(TID)
    stats = book.snapshot(TID, now=1.0)
    assert stats.connected_for_s is None
    assert stats.disconnects == 0


def test_an_empty_book_reads_as_blank_not_zero() -> None:
    stats = StatsBook().snapshot(TID, now=0)
    assert stats.latency_median is None
    assert stats.slippage_mean is None
    assert stats.slippage_worst is None
    assert stats.connected_for_s is None
