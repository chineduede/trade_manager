"""Pure view-model logic: status tiers, row derivation, log-tail reading.

No ASGI app or websocket needed here -- that is the point of keeping ``presentation.py``
separate from ``routes.py`` (see its module docstring).
"""

from __future__ import annotations

import time
from pathlib import Path

from mt5copier.config import Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.protocol import Snapshot, WorkerState
from mt5copier.web.presentation import read_log_tail, read_new_log_lines, row_info, tier_for_status

TID = "A" * 32


def _cfg() -> TerminalConfig:
    return TerminalConfig(terminal_id=TID, label="Alpha Broker", role=Role.SLAVE)


def test_tier_for_status_maps_the_known_states() -> None:
    assert tier_for_status("connected") == "good"
    assert tier_for_status("starting") == "warning"
    assert tier_for_status("stale") == "serious"
    assert tier_for_status("worker dead") == "critical"
    assert tier_for_status("-") == "muted"


def test_tier_for_status_defaults_unknown_text_to_muted() -> None:
    assert tier_for_status("something this table has never seen") == "muted"


def test_row_info_without_a_snapshot_shows_no_numbers() -> None:
    view = TerminalView(cfg=_cfg(), supervised=False)
    row = row_info(view, timeout_s=15.0, hold_reason=None)
    assert row.status_text == "not connected"
    assert row.tier == "muted"
    assert row.balance is None
    assert row.floating_pl is None
    assert row.floating_pl_tier is None


def test_row_info_derives_positive_floating_pl_from_balance_and_equity() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, balance=1000.0, equity=1012.5)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = row_info(view, timeout_s=999.0, hold_reason=None)
    assert row.status_text == "connected"
    assert row.tier == "good"
    assert row.floating_pl == 12.5
    assert row.floating_pl_tier == "good"


def test_row_info_derives_negative_floating_pl_as_critical() -> None:
    """P&L reuses the status good/critical tokens directly -- WEBUI_PLAN.md's collision rule,
    not a second green/red pair kept in sync by hand."""
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, balance=1000.0, equity=950.0)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = row_info(view, timeout_s=999.0, hold_reason=None)
    assert row.floating_pl == -50.0
    assert row.floating_pl_tier == "critical"


def test_a_held_but_connected_slave_is_downgraded_to_warning() -> None:
    """Holding is orthogonal to connection status -- the link is fine, copying to it isn't."""
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = row_info(view, timeout_s=999.0, hold_reason="an unanswered command")
    assert row.tier == "warning"
    assert row.hold_reason == "an unanswered command"


def test_a_stale_held_slave_is_not_downgraded_from_serious_to_warning() -> None:
    """The hold override only ever lifts a *good* tier -- it must never mask a worse problem
    (stale, dead, failed) by softening it to warning."""
    view = TerminalView(cfg=_cfg(), supervised=True, process_alive=True, received_at=-10_000.0)
    row = row_info(view, timeout_s=0.001, hold_reason="whatever")
    assert row.status_text == "stale"
    assert row.tier == "serious"


def test_read_log_tail_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    tail = read_log_tail(tmp_path / "nope.log")
    assert tail.text == ""
    assert tail.offset == 0


def test_read_log_tail_keeps_only_the_last_n_lines(tmp_path: Path) -> None:
    # write_bytes, not write_text: Windows text mode translates "\n" to "\r\n" on write, which
    # would make this test about platform newline handling instead of the tail-trimming logic.
    path = tmp_path / "copier.log"
    path.write_bytes(("\n".join(f"line {i}" for i in range(10)) + "\n").encode())
    tail = read_log_tail(path, max_lines=3)
    assert tail.text == "line 7\nline 8\nline 9"
    assert tail.offset == path.stat().st_size


def test_read_new_log_lines_returns_only_what_was_appended(tmp_path: Path) -> None:
    path = tmp_path / "copier.log"
    path.write_bytes(b"first\n")
    offset = path.stat().st_size
    with path.open("ab") as f:
        f.write(b"second\n")
    tail = read_new_log_lines(path, offset)
    assert tail.text == "second\n"
    assert tail.offset == path.stat().st_size


def test_read_new_log_lines_with_nothing_new_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "copier.log"
    path.write_bytes(b"first\n")
    offset = path.stat().st_size
    tail = read_new_log_lines(path, offset)
    assert tail.text == ""
    assert tail.offset == offset


def test_read_new_log_lines_restarts_from_zero_after_rotation(tmp_path: Path) -> None:
    """A file smaller than the tracked offset means the log rotated underneath us -- read the
    whole fresh file as new rather than silently going quiet or raising."""
    path = tmp_path / "copier.log"
    path.write_bytes(b"a" * 500)
    stale_offset = 500
    path.write_bytes(b"fresh after rotation\n")
    tail = read_new_log_lines(path, stale_offset)
    assert tail.text == "fresh after rotation\n"
