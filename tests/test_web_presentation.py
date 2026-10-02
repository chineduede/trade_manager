"""Pure view-model logic: status tiers, row derivation, log-tail reading.

No ASGI app needed here -- that is the point of keeping ``presentation.py`` separate from
``routes.py`` (see its module docstring).
"""

from __future__ import annotations

import time
from pathlib import Path

from mt5copier.config import Role, TerminalConfig
from mt5copier.engine import TerminalView
from mt5copier.protocol import Snapshot, WorkerState
from mt5copier.web.presentation import (
    badge_for_tier,
    read_log_tail,
    render_log_lines,
    row_info,
    tier_for_status,
)

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


def test_render_log_lines_on_empty_text_is_empty() -> None:
    assert render_log_lines("") == ""


def test_render_log_lines_colors_each_line_by_its_level() -> None:
    text = (
        "2026-10-02 00:33:33 INFO    [MainProcess] mt5copier.engine: OPEN filled\n"
        "2026-10-02 00:34:02 WARNING [MainProcess] mt5copier.engine: stale slave\n"
        "2026-10-02 00:34:05 ERROR   [MainProcess] mt5copier.engine: send failed\n"
    )
    html = render_log_lines(text)
    assert '<span class="log-line log-info">' in html
    assert '<span class="log-line log-warning">' in html
    assert '<span class="log-line log-error">' in html


def test_render_log_lines_falls_back_on_an_unrecognised_line() -> None:
    """A traceback continuation line has no level prefix at all -- it must not be misclassified
    as some arbitrary level, and must not crash the renderer."""
    html = render_log_lines('  File "engine.py", line 42, in _reconcile\n')
    assert '<span class="log-line log-line-plain">' in html


def test_render_log_lines_escapes_html_in_the_message() -> None:
    """Log messages can contain anything a broker or a symbol name throws at them -- this must
    never be interpreted as markup."""
    html = render_log_lines(
        "2026-10-02 00:33:33 INFO    [MainProcess] mt5copier.engine: <script>alert(1)</script>\n"
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_read_log_tail_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_log_tail(tmp_path / "nope.log") == ""


def test_read_log_tail_keeps_only_the_last_n_lines(tmp_path: Path) -> None:
    # write_bytes, not write_text: Windows text mode translates "\n" to "\r\n" on write, which
    # would make this test about platform newline handling instead of the tail-trimming logic.
    path = tmp_path / "copier.log"
    path.write_bytes(("\n".join(f"line {i}" for i in range(10)) + "\n").encode())
    assert read_log_tail(path, max_lines=3) == "line 7\nline 8\nline 9"


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
