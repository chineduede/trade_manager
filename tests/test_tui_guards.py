"""The guards page, driven by keys against the stub engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static
from tui_support import TID_MASTER, TID_SLAVE, copy_of, make_app, make_cfg, plain

from mt5copier.config import AppConfig, Role
from mt5copier.guards import AccountGuards, GuardReport, GuardRule, Level, RuleStatus
from mt5copier.protocol import Snapshot, WorkerState
from mt5copier.tui import CopierApp
from mt5copier.tui.cards import guard_badge
from mt5copier.tui.dialogs import OrphanDialog
from mt5copier.tui.guards import GuardConfirm, GuardPick, GuardsScreen, RuleForm, standing
from mt5copier.tui.settings import Choice

KEY = "222@"
"""The slave's account: make_cfg logs it in as 222, on no server."""


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    cfg = make_cfg(tmp_path)
    cfg.set_role(TID_MASTER, Role.MASTER)
    cfg.set_role(TID_SLAVE, Role.SLAVE)
    return cfg


@pytest.fixture
def app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CopierApp:
    app = make_app(cfg, tmp_path, monkeypatch)
    app._engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
        terminal_id=TID_SLAVE,
        state=WorkerState.CONNECTED,
        login=222,
        server="",
        balance=9_600.0,
        equity=9_500.0,
        currency="USD",
        open_positions=(),
        live_positions=(),
        deals=(),
        deals_since=0.0,
    )
    return app


def saved(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "guards.json").read_text(encoding="utf-8"))


async def _open(app: CopierApp, pilot: object) -> GuardsScreen:
    app._select(TID_SLAVE)
    await pilot.press("k")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    assert isinstance(app.screen, GuardsScreen)
    return app.screen


# --- pure ------------------------------------------------------------------------------------


def _status(level: Level, used: float | None = 50.0, limit: float | None = 100.0) -> RuleStatus:
    return RuleStatus("r", "max_drawdown", "Max drawdown", "", level, used=used, limit=limit)


def test_badge_says_nothing_while_comfortable() -> None:
    assert guard_badge(None).plain == ""
    report = GuardReport(KEY, True, None, "USD", (_status(Level.OK),))
    assert guard_badge(report).plain == ""


def test_badge_near_and_breached() -> None:
    near = GuardReport(KEY, True, None, "USD", (_status(Level.NEAR, used=82.0),))
    assert guard_badge(near).plain == "GUARD 82%"
    breached = GuardReport(KEY, True, None, "USD", (_status(Level.BREACHED, used=120.0),))
    assert guard_badge(breached).plain == "GUARD BREACHED"


def test_standing_is_a_bar_or_a_word() -> None:
    assert "50%" in standing(_status(Level.OK)).plain
    assert standing(_status(Level.UNKNOWN)).plain == "unknown"
    assert standing(_status(Level.NEAR, used=None, limit=None)).plain == "check"


# --- the page --------------------------------------------------------------------------------


async def test_k_opens_a_slaves_guards_and_not_the_masters(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        app._select(TID_MASTER)
        await pilot.press("k")
        assert not isinstance(app.screen, GuardsScreen)
        screen = await _open(app, pilot)
        assert "account 222@" in plain(screen.query_one("#gd-band", Static).content)
        assert "No rules yet" in plain(screen.query_one("#gd-empty", Static).content)


async def test_adding_a_rule_saves_it_and_shows_how_it_stands(
    app: CopierApp, tmp_path: Path
) -> None:
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("i")
        screen.query_one("#gd-ask-box", Input).value = "10000"
        await pilot.press("enter")
        await pilot.pause()
        assert app._guards.for_account(KEY).initial_balance == 10_000.0

        await pilot.press("a")
        await pilot.pause()
        pick = app.screen
        assert isinstance(pick, GuardPick)
        pick.dismiss("max_drawdown")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, RuleForm)
        form.query_one("#f-limit", Input).value = "8"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, GuardsScreen)
        rules = app._guards.for_account(KEY).rules
        assert [(r.type, r.params["limit"]) for r in rules] == [("max_drawdown", 8.0)]
        on_disk = saved(tmp_path)["accounts"][KEY]  # type: ignore[index]
        assert on_disk["initial_balance"] == 10_000.0
        assert on_disk["rules"][0]["type"] == "max_drawdown"

        table = screen.query_one("#gd-rules", DataTable)
        assert table.row_count == 1
        row = " ".join(plain(cell) for cell in table.get_row_at(0))
        assert "Max drawdown" in row and "500.00 of 800.00" in row


async def test_a_bad_value_keeps_the_form_open_and_says_why(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await _open(app, pilot)
        app.push_screen(RuleForm(GuardRule(type="daily_loss"), "Beta"))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, RuleForm)
        form.query_one("#f-reset", Input).value = "25:00"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is form
        assert "00:00" in plain(form.query_one("#e-reset", Static).content)


async def test_what_a_percent_is_of_hides_for_money(app: CopierApp) -> None:
    async with app.run_test() as pilot:
        await _open(app, pilot)
        app.push_screen(RuleForm(GuardRule(type="daily_loss"), "Beta"))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, RuleForm)
        line = form.query_one(".p-percent_of")
        assert line.display
        form.query_one("#f-unit", Choice).value = "money"
        await pilot.pause()
        assert not line.display
        assert "in a day" in plain(form.query_one("#ex-body", Static).content)


async def test_space_switches_a_rule_off_and_x_removes_it_after_asking(app: CopierApp) -> None:
    app._guards.set_account(KEY, AccountGuards(rules=(GuardRule(type="stop_required"),)))
    async with app.run_test() as pilot:
        await _open(app, pilot)
        await pilot.press("space")
        assert app._guards.for_account(KEY).rules[0].enabled is False

        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, GuardConfirm)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app._guards.for_account(KEY).rules) == 1, "esc keeps it"

        await pilot.press("x")
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert app._guards.for_account(KEY).rules == ()


async def test_a_template_saved_and_applied_elsewhere(
    app: CopierApp, cfg: AppConfig, tmp_path: Path
) -> None:
    app._guards.set_account(
        KEY,
        AccountGuards(initial_balance=5_000.0, rules=(GuardRule(type="daily_loss"),)),
    )
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("t")
        await pilot.pause()
        app.screen.dismiss("\0save")
        await pilot.pause()
        screen.query_one("#gd-ask-box", Input).value = "2-step 5k"
        await pilot.press("enter")
        await pilot.pause()
        assert [r.type for r in app._guards.templates["2-step 5k"]] == ["daily_loss"]
        assert "2-step 5k" in saved(tmp_path)["templates"]  # type: ignore[operator]

        # Another account, applied the template: same rules, fresh ids, its own balance.
        other = "999@Other"
        app._engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
            terminal_id=TID_SLAVE, state=WorkerState.CONNECTED, login=999, server="Other"
        )
        await pilot.press("t")
        await pilot.pause()
        app.screen.dismiss("2-step 5k")
        await pilot.pause()
        got = app._guards.for_account(other)
        assert [r.type for r in got.rules] == ["daily_loss"]
        assert got.rules[0].id != app._guards.for_account(KEY).rules[0].id
        assert got.initial_balance is None


@pytest.mark.parametrize(
    ("on_breach", "badge"),
    [("block_new", "GUARD: NO NEW TRADES"), ("warn", "GUARD BREACHED")],
)
async def test_the_card_carries_the_badge(app: CopierApp, on_breach: str, badge: str) -> None:
    params = {"limit": 500, "unit": "money", "on_breach": on_breach}
    app._guards.set_account(
        KEY,
        AccountGuards(
            initial_balance=10_000.0,
            rules=(GuardRule(type="max_drawdown", params=params),),
        ),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app._cards[TID_SLAVE]
        assert badge in plain(card.content, width=40)


async def test_the_arrows_move_along_a_dialogs_buttons(app: CopierApp) -> None:
    async with app.run_test(size=(140, 50)) as pilot:
        app._select(TID_MASTER)
        for dialog in (
            GuardConfirm("Forget it?", "It goes.", "Forget it"),
            OrphanDialog(
                copies={"Beta Broker": [copy_of(100)]}, changes="", live=False, halt=print
            ),
        ):
            app.push_screen(dialog)
            await pilot.pause()
            focused = []
            for key in ("right", "right", "left"):
                await pilot.press(key)
                focused.append(app.focused.id if app.focused else None)
            assert focused == ["go", "cancel", "go"], "round from one end to the other"
            await pilot.press("escape")
            await pilot.pause()
            assert app._sel == TID_MASTER, "the arrows never reach the map under it"
