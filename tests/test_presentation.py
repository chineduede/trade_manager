"""Pure view-model logic: status tiers, row derivation, log-tail reading.

No Textual app needed here -- that is the point of keeping ``presentation.py`` separate from
``tui/app.py`` (see its module docstring).
"""

from __future__ import annotations

import time
from pathlib import Path

from mt5copier.config import Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.presentation import (
    badge_for_tier,
    format_money,
    group_rows,
    log_line_tier,
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


def test_badge_for_tier_covers_every_known_tier() -> None:
    assert badge_for_tier("good") == "[OK]"
    assert badge_for_tier("warning") == "[WARN]"
    assert badge_for_tier("serious") == "[ERR]"
    assert badge_for_tier("critical") == "[DEAD]"
    assert badge_for_tier("muted") == "[OFF]"


def test_badge_for_tier_falls_back_on_an_unknown_tier() -> None:
    assert badge_for_tier("something new") == "[??]"


def test_row_info_without_a_snapshot_shows_no_numbers() -> None:
    view = TerminalView(cfg=_cfg(), supervised=False)
    row = _info(view, timeout_s=15.0)
    assert row.status_text == "not connected"
    assert row.tier == "muted"
    assert row.balance is None
    assert row.floating_pl is None
    assert row.floating_pl_tier is None
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
    fallback the terminal table relies on."""
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


def test_format_money_on_none_is_an_em_dash() -> None:
    assert format_money(None, "USD") == "—"


def test_format_money_adds_thousands_separators() -> None:
    assert format_money(1234567.5, None) == "1,234,567.50"


def test_format_money_uses_a_known_currencys_symbol() -> None:
    assert format_money(1234.5, "usd") == "$1,234.50"
    assert format_money(1234.5, "EUR") == "€1,234.50"
    assert format_money(1234.5, "GBP") == "£1,234.50"


def test_format_money_falls_back_to_the_iso_code_for_an_unknown_currency() -> None:
    """Never a fabricated symbol for a currency this table doesn't know -- the bare code is
    honest, a guessed symbol could just be wrong."""
    assert format_money(1234.5, "XAU") == "1,234.50 XAU"


def test_format_money_with_no_currency_known_shows_the_bare_number() -> None:
    assert format_money(1234.5, None) == "1,234.50"
    assert format_money(1234.5, "") == "1,234.50"


def test_format_money_negative_amount_keeps_the_sign_before_the_symbol() -> None:
    assert format_money(-50.0, "USD") == "-$50.00"


def test_format_money_force_sign_adds_a_plus_on_a_non_negative_amount() -> None:
    assert format_money(12.5, "USD", force_sign=True) == "+$12.50"
    assert format_money(0.0, "USD", force_sign=True) == "+$0.00"


def test_format_money_force_sign_still_shows_minus_on_a_negative_amount() -> None:
    assert format_money(-50.0, "USD", force_sign=True) == "-$50.00"


def test_format_money_force_sign_with_no_currency_matches_the_old_plain_format() -> None:
    assert format_money(12.5, None, force_sign=True) == "+12.50"


def test_row_info_derives_positive_floating_pl_from_balance_and_equity() -> None:
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, balance=1000.0, equity=1012.5)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.status_text == "connected"
    assert row.tier == "good"
    assert row.floating_pl == 12.5
    assert row.floating_pl_tier == "good"


def test_row_info_derives_negative_floating_pl_as_critical() -> None:
    """P&L reuses the status good/critical tokens directly, not a second green/red pair kept in sync by hand."""
    snap = Snapshot(terminal_id=TID, state=WorkerState.CONNECTED, balance=1000.0, equity=950.0)
    view = TerminalView(
        cfg=_cfg(), snapshot=snap, supervised=True, process_alive=True, received_at=time.monotonic()
    )
    row = _info(view, timeout_s=999.0)
    assert row.floating_pl == -50.0
    assert row.floating_pl_tier == "critical"


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


def test_log_line_tier_colors_each_line_by_its_level() -> None:
    assert log_line_tier("2026-10-02 00:33:33 INFO    [MainProcess] x: OPEN filled") == "good"
    assert log_line_tier("2026-10-02 00:34:02 WARNING [MainProcess] x: stale") == "warning"
    assert log_line_tier("2026-10-02 00:34:05 ERROR   [MainProcess] x: failed") == "serious"


def test_log_line_tier_is_none_on_an_unrecognised_line() -> None:
    """A traceback continuation line has no level prefix at all -- it must not be misclassified
    as some arbitrary level."""
    assert log_line_tier('  File "engine.py", line 42, in _reconcile') is None


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


def test_group_rows_splits_master_slaves_and_unassigned() -> None:
    rows = [_row(TID, Role.MASTER), _row(TID_B, Role.SLAVE), _row(TID_C, Role.UNASSIGNED)]
    grouped = group_rows(rows)
    assert grouped.master is not None and grouped.master.terminal_id == TID
    assert [r.terminal_id for r in grouped.slaves] == [TID_B]
    assert [r.terminal_id for r in grouped.unassigned] == [TID_C]


def test_group_rows_with_no_master_is_none_not_a_crash() -> None:
    grouped = group_rows([_row(TID, Role.UNASSIGNED)])
    assert grouped.master is None
    assert grouped.slaves == []
    assert [r.terminal_id for r in grouped.unassigned] == [TID]


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
