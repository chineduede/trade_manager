"""The worker's reads for the guards: the broker's clock offset and the recent deal history."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from test_worker import FakeMT5

from mt5copier.worker import _ServerClock, _Session

HOUR = 3600


@dataclass
class FakeTick:
    time: int
    time_msc: int


@dataclass
class FakeSymbol:
    name: str
    visible: bool = True


@dataclass
class FakeDeal:
    ticket: int
    time: int
    type: int = 1
    entry: int = 1
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    symbol: str = "EURUSD"
    time_msc: int = 0


class ClockMT5(FakeMT5):
    """Ticks stamped in a broker clock ``offset`` seconds ahead of UTC, and a deal history."""

    def __init__(self, offset: int = 3 * HOUR, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.offset = offset
        self.tick_at: dict[str, float] = {}
        self.history: list[FakeDeal] | None = []
        self.deal_reads: list[tuple[int, int]] = []

    def symbols_get(self) -> list[FakeSymbol]:
        return [FakeSymbol("EURUSD"), FakeSymbol("HIDDEN", visible=False), FakeSymbol("GBPUSD")]

    def symbol_info_tick(self, name: str) -> FakeTick | None:
        at = self.tick_at.get(name)
        if at is None:
            return None
        server = at + self.offset
        return FakeTick(time=int(server), time_msc=int(server * 1000))

    def history_deals_get(self, date_from: int, date_to: int) -> list[FakeDeal] | None:
        self.deal_reads.append((date_from, date_to))
        if self.history is None:
            return None
        return [d for d in self.history if date_from <= d.time <= date_to] or None

    def history_deals_total(self, date_from: int, date_to: int) -> int:
        if self.history is None:
            return -1
        return sum(1 for d in self.history if date_from <= d.time <= date_to)


# --- the clock ---------------------------------------------------------------------------------


def test_offset_is_unknown_until_a_tick_changes() -> None:
    mt5, clock = ClockMT5(), _ServerClock()
    mt5.tick_at["EURUSD"] = 1000.0
    assert clock.update(mt5, (), now=1000.5) is None, "one look cannot tell a fresh tick"
    mt5.tick_at["EURUSD"] = 1002.0
    assert clock.update(mt5, (), now=1002.3) == 3 * HOUR


def test_a_stale_tick_never_sets_the_offset() -> None:
    """A closed market's last tick is days old; rounding it would give a confident wrong answer."""
    mt5, clock = ClockMT5(), _ServerClock()
    mt5.tick_at["EURUSD"] = 1000.0
    clock.update(mt5, (), now=200_000.0)
    clock.update(mt5, (), now=200_002.0)
    assert clock.offset is None


def test_negative_and_half_hour_offsets() -> None:
    for offset in (-5 * HOUR, int(5.5 * HOUR)):
        mt5, clock = ClockMT5(offset=offset), _ServerClock()
        mt5.tick_at["EURUSD"] = 10.0
        clock.update(mt5, (), now=10.0)
        mt5.tick_at["EURUSD"] = 12.0
        assert clock.update(mt5, (), now=12.4) == offset


def test_hidden_symbols_are_not_probed() -> None:
    mt5, clock = ClockMT5(), _ServerClock()
    mt5.tick_at["HIDDEN"] = 10.0
    clock.update(mt5, (), now=10.0)
    mt5.tick_at["HIDDEN"] = 12.0
    assert clock.update(mt5, (), now=12.0) is None


# --- the deal history --------------------------------------------------------------------------


def _connected(mt5: ClockMT5) -> _Session:
    session = _Session(mt5, Path("x.exe"))
    session.connect()
    return session


def _know_offset(session: _Session, mt5: ClockMT5, now: float) -> None:
    mt5.tick_at["EURUSD"] = now - 2
    session._clock.update(mt5, (), now=now - 2)
    mt5.tick_at["EURUSD"] = now
    session._clock._checked_at = 0.0
    session._clock.update(mt5, (), now=now)


def test_deals_are_read_in_the_broker_clock_and_reported_in_utc() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    now = 1_000_000.0
    _know_offset(session, mt5, now)
    mt5.history = [FakeDeal(ticket=7, time=int(now - HOUR) + mt5.offset, profit=-50.0, fee=-1.0)]

    session._refresh_deals(mt5.offset, 1000.0, now=now)

    assert session._deals is not None and len(session._deals) == 1
    deal = session._deals[0]
    assert deal.time == now - HOUR
    assert deal.net == -51.0
    since, until = mt5.deal_reads[-1]
    assert since == int(session._deals_since or 0) + mt5.offset
    assert until > now + mt5.offset


def test_no_deals_is_an_empty_history_not_an_unknown_one() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    session._refresh_deals(mt5.offset, 1000.0, now=1_000_000.0)
    assert session._deals == ()


def test_a_failed_read_makes_the_history_unknown() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    session._refresh_deals(mt5.offset, 1000.0, now=1_000_000.0)
    mt5.history = None
    session._refresh_deals(mt5.offset, 1000.0, now=1_000_010.0)
    assert session._deals is None and session._deals_since is None


def test_history_is_reread_when_the_balance_moves_and_not_otherwise() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    session._refresh_deals(mt5.offset, 1000.0, now=1_000_000.0)
    session._refresh_deals(mt5.offset, 1000.0, now=1_000_001.0)
    assert len(mt5.deal_reads) == 1
    session._refresh_deals(mt5.offset, 950.0, now=1_000_002.0)
    assert len(mt5.deal_reads) == 2


def test_nothing_is_read_without_the_offset() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    session._refresh_deals(None, 1000.0, now=1_000_000.0)
    assert mt5.deal_reads == [] and session._deals is None


def test_poll_carries_offset_and_deals() -> None:
    mt5 = ClockMT5()
    session = _connected(mt5)
    snap = session.poll("TID", seq=1, attempts=0)
    assert snap.server_offset_s is None and snap.deals is None
