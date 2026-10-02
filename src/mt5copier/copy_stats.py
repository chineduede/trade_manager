"""Per-terminal copy-health statistics, kept for this session only.

Display only, like :class:`~mt5copier.protocol.PositionLive`: nothing that decides what to
send may read these. They answer "is this slave copying well?" -- how long the round trip to
the broker takes, how far fills land from the master's entry, how often orders are refused,
retried or lost -- which the action and result logs can only answer one line at a time.

Nothing here is persisted. A restart starts from zero, which is what "session" means and is
honest: a median carried over from yesterday would describe a connection that no longer exists.
"""

from __future__ import annotations

import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

from .protocol import ActionKind, OrderResult, PositionType, WorkerState

SAMPLE_HISTORY = 200
"""How many latency and slippage samples to keep per terminal. Enough for a sparkline and a
stable median; bounded because a busy master would otherwise grow this all session."""


@dataclass(frozen=True, slots=True)
class TerminalStats:
    """A read-only copy of one terminal's statistics, safe to hand to the UI thread."""

    latencies_ms: tuple[float, ...] = ()
    """Round trip per answered command, oldest first: from putting it on the slave's queue to
    its result arriving. Includes the worker's retries, because that is the delay the copy
    actually suffered."""

    slippage_points: tuple[float, ...] = ()
    """Per filled market OPEN, oldest first: the slave's fill against the master's entry, in
    the slave symbol's points. Positive is against us -- a buy filled higher, a sell lower."""

    succeeded: int = 0
    refused: int = 0
    """Failures the broker (or the executor) called final. Each one is a trade not copied."""
    failed_transient: int = 0
    """Failures that described a link problem rather than a refusal."""
    retried: int = 0
    """Answered commands that needed more than one send. Counted whatever the outcome."""
    expired: int = 0
    """Commands never answered, which the ledger gave up on."""
    dropped_late: int = 0
    """Answers that arrived for a command already given up on, and were refused."""
    skipped: tuple[tuple[str, int], ...] = ()
    """(reason, count), most common first: decisions deliberately not executed."""

    connected_for_s: float | None = None
    """How long the current connection has lasted, or None while not connected."""
    disconnects: int = 0
    """Times a connected terminal stopped being connected this session."""

    @property
    def latency_last(self) -> float | None:
        return self.latencies_ms[-1] if self.latencies_ms else None

    @property
    def latency_median(self) -> float | None:
        return statistics.median(self.latencies_ms) if self.latencies_ms else None

    @property
    def latency_max(self) -> float | None:
        return max(self.latencies_ms) if self.latencies_ms else None

    @property
    def slippage_last(self) -> float | None:
        return self.slippage_points[-1] if self.slippage_points else None

    @property
    def slippage_mean(self) -> float | None:
        return statistics.fmean(self.slippage_points) if self.slippage_points else None

    @property
    def slippage_worst(self) -> float | None:
        return max(self.slippage_points) if self.slippage_points else None

    @property
    def answered(self) -> int:
        return self.succeeded + self.refused + self.failed_transient

    @property
    def skipped_total(self) -> int:
        return sum(n for _, n in self.skipped)


def slippage_in_points(
    direction: PositionType, master_entry: float, fill: float, point: float
) -> float | None:
    """Signed distance from the master's entry to the slave's fill, positive when worse.

    None when it cannot be measured honestly: no point size, or no prices to compare.
    """
    if point <= 0 or master_entry <= 0 or fill <= 0:
        return None
    raw = (fill - master_entry) / point
    return raw if direction is PositionType.BUY else -raw


@dataclass
class _Sent:
    """What the engine remembers about a dispatched command until its answer arrives."""

    sent_at: float
    op: ActionKind
    direction: PositionType
    master_entry: float


@dataclass
class StatsBook:
    """Accumulates :class:`TerminalStats` for every terminal. Not thread-safe: the engine
    calls it under its own lock."""

    _latencies: dict[str, deque[float]] = field(default_factory=dict)
    _slippage: dict[str, deque[float]] = field(default_factory=dict)
    _counts: dict[str, Counter[str]] = field(default_factory=dict)
    _skips: dict[str, Counter[str]] = field(default_factory=dict)
    _connected_since: dict[str, float] = field(default_factory=dict)
    _disconnects: dict[str, int] = field(default_factory=dict)
    _sent: dict[int, _Sent] = field(default_factory=dict)

    # --- recording --------------------------------------------------------------------------

    def on_sent(
        self,
        command_id: int,
        *,
        sent_at: float,
        op: ActionKind,
        direction: PositionType,
        master_entry: float,
    ) -> None:
        self._sent[command_id] = _Sent(sent_at, op, direction, master_entry)

    def on_result(self, result: OrderResult, *, now: float) -> None:
        """An answer to the command we were waiting for. Late answers go to :meth:`on_late`."""
        tid = result.terminal_id
        counts = self._counts.setdefault(tid, Counter())
        if result.ok:
            counts["succeeded"] += 1
        elif result.retryable:
            counts["failed_transient"] += 1
        else:
            counts["refused"] += 1
        if result.attempts > 1:
            counts["retried"] += 1

        sent = self._sent.pop(result.command_id, None)
        if sent is None:
            return
        self._samples(self._latencies, tid).append((now - sent.sent_at) * 1000.0)
        if (
            result.ok
            and sent.op is ActionKind.OPEN
            and result.price is not None
            and result.point is not None
        ):
            slip = slippage_in_points(sent.direction, sent.master_entry, result.price, result.point)
            if slip is not None:
                self._samples(self._slippage, tid).append(slip)

    def on_abandoned(self, result: OrderResult) -> None:
        """A command that never left the engine. A failure, but not one the broker timed."""
        self._sent.pop(result.command_id, None)
        self._counts.setdefault(result.terminal_id, Counter())["failed_transient"] += 1

    def on_late(self, terminal_id: str) -> None:
        self._counts.setdefault(terminal_id, Counter())["dropped_late"] += 1

    def on_expired(self, terminal_id: str) -> None:
        self._counts.setdefault(terminal_id, Counter())["expired"] += 1

    def on_skipped(self, terminal_id: str, reason: str) -> None:
        self._skips.setdefault(terminal_id, Counter())[reason] += 1

    def on_state(self, terminal_id: str, state: WorkerState, *, now: float) -> None:
        """Track connection uptime from each snapshot's state. Only transitions count."""
        connected = state is WorkerState.CONNECTED
        was = terminal_id in self._connected_since
        if connected and not was:
            self._connected_since[terminal_id] = now
        elif was and not connected:
            del self._connected_since[terminal_id]
            self._disconnects[terminal_id] = self._disconnects.get(terminal_id, 0) + 1

    def on_worker_gone(self, terminal_id: str) -> None:
        """The worker was stopped on purpose: not connected any more, but not a disconnect."""
        self._connected_since.pop(terminal_id, None)

    def forget_sent_before(self, cutoff: float) -> None:
        """Drop commands too old to be answered, alongside the engine's own sweep."""
        for cid in [c for c, s in self._sent.items() if s.sent_at < cutoff]:
            del self._sent[cid]

    # --- reading ----------------------------------------------------------------------------

    def snapshot(self, terminal_id: str, *, now: float) -> TerminalStats:
        counts = self._counts.get(terminal_id, Counter())
        since = self._connected_since.get(terminal_id)
        return TerminalStats(
            latencies_ms=tuple(self._latencies.get(terminal_id, ())),
            slippage_points=tuple(self._slippage.get(terminal_id, ())),
            succeeded=counts["succeeded"],
            refused=counts["refused"],
            failed_transient=counts["failed_transient"],
            retried=counts["retried"],
            expired=counts["expired"],
            dropped_late=counts["dropped_late"],
            skipped=tuple(self._skips.get(terminal_id, Counter()).most_common()),
            connected_for_s=None if since is None else max(0.0, now - since),
            disconnects=self._disconnects.get(terminal_id, 0),
        )

    @staticmethod
    def _samples(store: dict[str, deque[float]], terminal_id: str) -> deque[float]:
        return store.setdefault(terminal_id, deque(maxlen=SAMPLE_HISTORY))
