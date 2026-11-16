"""Pure view-model logic: status tiers, row derivation, log-tail reading.

No UI needed here -- that is the point of ``presentation.py`` (see its module docstring).
"""

from __future__ import annotations

import time
from pathlib import Path

from mt5copier.config import Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.presentation import (
    read_log_tail,
    row_info,
    slave_badge,
    tier_for_status,
)
from mt5copier.protocol import Snapshot, WorkerState

TID = "A" * 32
TID_B = "B" * 32
TID_C = "C" * 32


def _cfg(terminal_id: str = TID, role: Role = Role.SLAVE) -> TerminalConfig:
    return TerminalConfig(terminal_id=terminal_id, label="Alpha Broker", role=role)


def _info(view: TerminalView, *, timeout_s: float = 15.0, hold_reason: str | None = None) -> object:
    """``row_info`` with a display_name supplied -- most tests here don't care what it is, only
    the real callers (``all_rows``, via ``AppConfig.display_name``) resolve it for real."""
    return row_info(view, timeout_s=timeout_s, hold_reason=hold_reason, display_name="Alpha Broker")


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
    row = _info(view, timeout_s=15.0)
    assert row.status_text == "not connected"
    assert row.tier == "muted"
    assert row.balance is None
    assert row.floating_pl is None
    assert row.currency is None


def test_row_info_uses_whatever_display_name_its_given() -> None:
    """``row_info`` takes the resolved name as a plain argument -- it has no access to
    ``AppConfig.account_names`` itself; resolving it from the account is ``all_rows``'s job
    (via ``AppConfig.display_name``), covered in ``test_config.py``."""
    row = row_info(
        TerminalView(cfg=_cfg(), supervised=False),
        timeout_s=15.0,
        hold_reason=None,
        display_name="My Funded Account",
    )
    assert row.label == "My Funded Account"


def test_row_info_falls_back_to_the_cached_login_and_server_when_disconnected() -> None:
    """Discovery's last-known account id should show before anything has connected -- the same
    fallback a terminal's card relies on."""
    cfg = _cfg()
    cfg.last_login = 12345678
    cfg.last_server = "ICMarketsSC-Live"
    row = _info(TerminalView(cfg=cfg, supervised=False), timeout_s=15.0)
    assert row.login == 12345678
    assert row.server == "ICMarketsSC-Live"


def test_row_info_prefers_the_live_snapshots_login_over_the_cached_one() -> None:
    cfg = _cfg()
    cfg.last_login = 11111111
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, login=22222222, server="Live-2")
    view = TerminalView(
        cfg=cfg, snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.login == 22222222
    assert row.server == "Live-2"


def test_row_info_carries_the_live_accounts_currency() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, currency="EUR")
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.currency == "EUR"


# --- money formatting -----------------------------------------------------------------------


def test_row_info_derives_positive_floating_pl_from_balance_and_equity() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, balance=1000.0, equity=1012.5)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.status_text == "connected"
    assert row.tier == "good"
    assert row.floating_pl == 12.5


def test_a_held_but_connected_slave_is_downgraded_to_warning() -> None:
    """Holding is orthogonal to connection status -- the link is fine, copying to it isn't."""
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0, hold_reason="an unanswered command")
    assert row.tier == "warning"
    assert row.hold_reason == "an unanswered command"


def test_a_stale_held_slave_is_not_downgraded_from_serious_to_warning() -> None:
    """The hold override only ever lifts a *good* tier -- it must never mask a worse problem
    (stale, dead, failed) by softening it to warning."""
    view = TerminalView(cfg=_cfg(), supervised=True, process_alive=True, received_at=-10_000.0)
    row = _info(view, timeout_s=0.001, hold_reason="whatever")
    assert row.status_text == "stale"
    assert row.tier == "serious"


def test_read_log_tail_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_log_tail(tmp_path / "nope.log") == ""


def test_read_log_tail_keeps_only_the_last_n_lines(tmp_path: Path) -> None:
    # write_bytes, not write_text: Windows text mode translates "\n" to "\r\n" on write, which
    # would make this test about platform newline handling instead of the tail-trimming logic.
    path = tmp_path / "copier.log"
    path.write_bytes(("\n".join(f"line {i}" for i in range(10)) + "\n").encode())
    assert read_log_tail(path, max_lines=3) == "line 7\nline 8\nline 9"


def _row(terminal_id: str, role: Role) -> object:
    return _info(TerminalView(cfg=_cfg(terminal_id, role), supervised=False), timeout_s=15.0)


# --- open_trades / demo_guard_blocked ----------------------------------------------------


def test_row_info_reads_open_trades_from_the_snapshots_position_count() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, positions=3)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.open_trades == 3


def test_row_info_open_trades_is_none_without_a_snapshot() -> None:
    row = _info(TerminalView(cfg=_cfg(), supervised=False), timeout_s=15.0)
    assert row.open_trades is None


def test_row_info_flags_the_demo_guard_on_a_live_account_with_no_override() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, trade_mode=2)  # real
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.demo_guard_blocked is True


def test_row_info_demo_guard_is_clear_on_a_readable_demo_account() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, trade_mode=0)  # DEMO
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.demo_guard_blocked is False


def test_row_info_demo_guard_treats_an_unreadable_trade_mode_as_not_demo() -> None:
    """Mirrors the executor's own policy: "we could not tell" is not a reason to trade someone's
    funded account."""
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, trade_mode=None)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.demo_guard_blocked is True


def test_row_info_demo_guard_is_clear_when_the_override_is_on() -> None:
    cfg = _cfg()
    cfg.allow_live_trading = True
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, trade_mode=2)
    view = TerminalView(
        cfg=cfg, snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = row_info(view, timeout_s=999.0, hold_reason=None, display_name="Alpha Broker")
    assert row.demo_guard_blocked is False


def test_row_info_demo_guard_never_applies_to_a_master() -> None:
    cfg = _cfg(role=Role.MASTER)
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, trade_mode=2)
    view = TerminalView(
        cfg=cfg, snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = row_info(view, timeout_s=999.0, hold_reason=None, display_name="Alpha Broker")
    assert row.demo_guard_blocked is False


def test_row_info_demo_guard_does_not_apply_while_disconnected() -> None:
    """A link that is down is reported as that, not as a demo-guard refusal -- nothing would be
    sent to it regardless of the account type right now."""
    row = _info(TerminalView(cfg=_cfg(), supervised=False), timeout_s=15.0)
    assert row.demo_guard_blocked is False


# --- slave_badge (the one-line slave state, in priority order) --------------------------------


def _slave_row(
    *,
    supervised: bool = True,
    process_alive: bool = True,
    state: WorkerState = WorkerState.CONNECTED,
    trade_mode: int | None = 0,
    allow_live: bool = False,
    hold_reason: str | None = None,
) -> object:
    cfg = _cfg()
    cfg.allow_live_trading = allow_live
    snap = Snapshot(terminal_id=TID, state=state, trade_mode=trade_mode) if supervised else None
    view = TerminalView(
        cfg=cfg,
        snapshot=snap,
        supervised=supervised,
        process_alive=process_alive,
        received_at=time.monotonic(),
    )
    return row_info(view, timeout_s=999.0, hold_reason=hold_reason, display_name="Alpha Broker")


def test_slave_badge_prioritises_connectivity_over_everything_else() -> None:
    row = _slave_row(supervised=False)
    assert slave_badge(row, True) == ("Not connected", "muted")


def test_slave_badge_describes_a_broken_broker_link() -> None:
    row = _slave_row(state=WorkerState.DISCONNECTED)
    assert row.tier == "serious"
    assert slave_badge(row, True) == ("Broker link lost", "serious")


def test_slave_badge_describes_a_dead_worker_distinctly_from_a_broken_link() -> None:
    row = _slave_row(process_alive=False)
    assert row.tier == "critical"
    assert slave_badge(row, True) == ("Terminal not responding", "critical")


def test_slave_badge_shows_the_demo_guard_before_the_dry_run_label() -> None:
    row = _slave_row(trade_mode=2)
    assert slave_badge(row, True) == ("Held back: not a demo account", "critical")


def test_slave_badge_shows_a_hold_reason_before_the_dry_run_label() -> None:
    row = _slave_row(hold_reason="an unanswered command")
    assert slave_badge(row, True) == ("an unanswered command", "warning")


def test_slave_badge_falls_back_to_the_ambient_dry_run_or_live_label() -> None:
    row = _slave_row()
    assert slave_badge(row, True) == ("Dry run", "warning")
    assert slave_badge(row, False) == ("Live", "good")


def test_read_log_tail_reflects_whatever_is_in_the_file_right_now(tmp_path: Path) -> None:
    """Every poll re-reads the file fresh and the panel is fully replaced -- there is no
    byte-offset bookkeeping here (unlike the push-based design this replaced), so a later call
    simply sees whatever the file holds at that moment."""
    path = tmp_path / "copier.log"
    path.write_bytes(b"first\n")
    assert read_log_tail(path) == "first"
    with path.open("ab") as f:
        f.write(b"second\n")
    assert read_log_tail(path) == "first\nsecond"
