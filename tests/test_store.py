"""Mapping store tests.

This table is the copier's only record of what it owns. Losing it orphans live positions on
real accounts, so the tests care about durability and about the reconciliation queries a
restart depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.protocol import OrderType, PositionType
from mt5copier.store import (
    Copy,
    CopyKind,
    MappingStore,
    find_missing,
    find_missing_orders,
    find_orphans,
)

TID_A = "A" * 32
TID_B = "B" * 32
MAGIC = 770_101


def position_copy(
    master_ticket: int = 1,
    slave_ticket: int | None = 77,
    terminal_id: str = TID_A,
    master_volume: float = 1.0,
    **kw: object,
) -> Copy:
    base: dict[str, object] = {
        "terminal_id": terminal_id,
        "master_ticket": master_ticket,
        "kind": CopyKind.POSITION,
        "slave_symbol": "EURUSD",
        "direction": PositionType.BUY,
        "master_volume": master_volume,
        "slave_ticket": slave_ticket,
        "slave_volume": 0.5,
    }
    base.update(kw)
    return Copy(**base)  # type: ignore[arg-type]


@pytest.fixture
def store() -> MappingStore:
    return MappingStore()


# --- round trips ----------------------------------------------------------------------------


def test_a_recorded_copy_comes_back() -> None:
    store = MappingStore()
    store.record(position_copy())
    got = store.get(TID_A, 1)
    assert got is not None
    assert got.slave_ticket == 77
    assert got.slave_symbol == "EURUSD"
    assert got.direction is PositionType.BUY


def test_recording_stamps_the_times() -> None:
    store = MappingStore()
    saved = store.record(position_copy())
    assert saved.opened_at > 0
    assert saved.updated_at >= saved.opened_at


def test_rerecording_keeps_the_original_open_time() -> None:
    """updated_at moves; opened_at is when the copy came into existence."""
    store = MappingStore()
    first = store.record(position_copy())
    again = store.record(position_copy(slave_volume=0.25, opened_at=first.opened_at))
    assert again.opened_at == first.opened_at


def test_the_same_master_ticket_on_two_slaves_is_two_rows() -> None:
    store = MappingStore()
    store.record(position_copy(terminal_id=TID_A, slave_ticket=77))
    store.record(position_copy(terminal_id=TID_B, slave_ticket=88))
    assert store.get(TID_A, 1) is not None
    assert store.get(TID_B, 1) is not None
    assert len(store.all()) == 2


def test_recording_the_same_copy_twice_replaces_rather_than_duplicates() -> None:
    store = MappingStore()
    store.record(position_copy())
    store.record(position_copy(slave_volume=0.9))
    rows = store.for_terminal(TID_A)
    assert len(rows) == 1
    assert rows[0].slave_volume == pytest.approx(0.9)


def test_an_order_round_trips_with_its_type() -> None:
    store = MappingStore()
    store.record(position_copy(kind=CopyKind.ORDER, order_type=OrderType.SELL_STOP, entry=1.07))
    got = store.get(TID_A, 1)
    assert got is not None and got.order_type is OrderType.SELL_STOP
    assert got.entry == pytest.approx(1.07)


def test_forgetting_removes_only_that_copy() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=1))
    store.record(position_copy(master_ticket=2, slave_ticket=78))
    store.forget(TID_A, 1)
    assert store.get(TID_A, 1) is None
    assert store.get(TID_A, 2) is not None


# --- the views the diff engine wants ----------------------------------------------------------


def test_positions_and_orders_are_kept_apart() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=1))
    store.record(position_copy(master_ticket=2, slave_ticket=78, kind=CopyKind.ORDER))
    assert [c.master_ticket for c in store.positions(TID_A)] == [1]
    assert [c.master_ticket for c in store.orders(TID_A)] == [2]


def test_a_stored_copy_becomes_what_the_reconciler_expects() -> None:
    store = MappingStore()
    store.record(position_copy(master_volume=2.5, synced_sl=1.08, synced_tp=1.09))
    believed = store.positions(TID_A)[0]
    assert believed.master_volume == pytest.approx(2.5), "the volume it was sized against"
    assert believed.synced_sl == pytest.approx(1.08)
    assert believed.slave_ticket == 77


# --- reverse lookup and reconciliation --------------------------------------------------------


def test_a_slave_ticket_can_be_traced_back_to_its_master() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=5, slave_ticket=99))
    found = store.by_slave_ticket(TID_A, 99)
    assert found is not None and found.master_ticket == 5


def test_the_reverse_lookup_is_per_terminal() -> None:
    """Two brokers hand out overlapping ticket numbers; they are not globally unique."""
    store = MappingStore()
    store.record(position_copy(terminal_id=TID_A, master_ticket=1, slave_ticket=100))
    store.record(position_copy(terminal_id=TID_B, master_ticket=2, slave_ticket=100))
    a = store.by_slave_ticket(TID_A, 100)
    b = store.by_slave_ticket(TID_B, 100)
    assert a is not None and a.master_ticket == 1
    assert b is not None and b.master_ticket == 2


def test_an_orphan_is_one_of_ours_that_we_have_no_record_of() -> None:
    store = MappingStore()
    store.record(position_copy(slave_ticket=77))
    live = [(77, "EURUSD", 0.5, MAGIC), (78, "GBPUSD", 1.0, MAGIC)]
    orphans = find_orphans(store, TID_A, live, MAGIC)
    assert [o.slave_ticket for o in orphans] == [78]


def test_the_users_own_trades_are_not_orphans() -> None:
    """Anything without our magic is their trading and none of our business."""
    store = MappingStore()
    live = [(500, "EURUSD", 1.0, 0), (501, "GBPUSD", 1.0, 999)]
    assert find_orphans(store, TID_A, live, MAGIC) == []


def test_a_missing_copy_is_one_whose_slave_position_is_gone() -> None:
    """Closed by hand, stopped out, or hit its target while we were not running."""
    store = MappingStore()
    store.record(position_copy(master_ticket=1, slave_ticket=77))
    store.record(position_copy(master_ticket=2, slave_ticket=78))
    missing = find_missing(store, TID_A, [77])
    assert [c.master_ticket for c in missing] == [2]


def test_orders_are_not_counted_as_missing_positions() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=3, slave_ticket=79, kind=CopyKind.ORDER))
    assert find_missing(store, TID_A, []) == []


def test_a_filled_pending_order_is_not_a_stale_order_row() -> None:
    """The trap this query exists to avoid.

    An MT5 pending order that fills becomes a position carrying the same ticket. Reading that
    as "the order is gone" and forgetting the row would lose the handover and copy the trade a
    second time as a fresh open.
    """
    store = MappingStore()
    store.record(position_copy(master_ticket=3, slave_ticket=79, kind=CopyKind.ORDER))
    assert (
        find_missing_orders(store, TID_A, slave_order_tickets=[], slave_position_tickets=[79]) == []
    )


def test_an_order_still_resting_is_not_stale() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=3, slave_ticket=79, kind=CopyKind.ORDER))
    assert (
        find_missing_orders(store, TID_A, slave_order_tickets=[79], slave_position_tickets=[]) == []
    )


def test_an_order_that_is_neither_resting_nor_filled_is_stale() -> None:
    """Cancelled, expired or rejected while we were not running."""
    store = MappingStore()
    store.record(position_copy(master_ticket=3, slave_ticket=79, kind=CopyKind.ORDER))
    stale = find_missing_orders(store, TID_A, slave_order_tickets=[], slave_position_tickets=[])
    assert [c.master_ticket for c in stale] == [3]


def test_positions_are_not_counted_as_missing_orders() -> None:
    store = MappingStore()
    store.record(position_copy(master_ticket=1, slave_ticket=77))
    assert (
        find_missing_orders(store, TID_A, slave_order_tickets=[], slave_position_tickets=[]) == []
    )


# --- durability -----------------------------------------------------------------------------


def test_a_copy_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "mapping.db"
    with MappingStore(path) as store:
        store.record(position_copy())

    with MappingStore(path) as revived:
        got = revived.get(TID_A, 1)
        assert got is not None and got.slave_ticket == 77


def test_the_file_is_created_with_its_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "mapping.db"
    MappingStore(path).close()
    assert path.exists()


def test_writes_are_committed_immediately_not_at_close(tmp_path: Path) -> None:
    """The window between a fill and the fact being durable is a window in which a crash
    orphans a real position."""
    path = tmp_path / "mapping.db"
    store = MappingStore(path)
    store.record(position_copy())

    # A second connection, without the first having closed.
    other = MappingStore(path)
    assert other.get(TID_A, 1) is not None
    store.close()
    other.close()


# --- who manages a copy ---------------------------------------------------------------------


def test_who_manages_a_copy_is_kept(store: MappingStore) -> None:
    store.record(position_copy(1, master_managed=False))
    store.record(position_copy(2, master_managed=True))
    assert [c.master_managed for c in store.positions(TID_A)] == [False, True]


def test_a_copy_from_before_management_is_settled_once(store: MappingStore) -> None:
    """A row written before who manages it was kept is decided once, per slave, and then left
    alone: a later call, or a different answer, changes nothing already decided."""
    store.record(position_copy(1, terminal_id=TID_A))
    store.record(position_copy(2, terminal_id=TID_B))
    store.record(position_copy(3, terminal_id=TID_B, master_managed=True))
    assert store.get(TID_A, 1).master_managed is None  # type: ignore[union-attr]

    asked: list[str] = []

    def decide(terminal_id: str) -> bool:
        asked.append(terminal_id)
        return terminal_id == TID_B

    assert store.settle_management(decide) == 2
    assert sorted(asked) == [TID_A, TID_B]
    assert store.get(TID_A, 1).master_managed is False  # type: ignore[union-attr]
    assert store.get(TID_B, 2).master_managed is True  # type: ignore[union-attr]
    assert store.settle_management(lambda _: False) == 0
    assert store.get(TID_B, 3).master_managed is True  # type: ignore[union-attr]
