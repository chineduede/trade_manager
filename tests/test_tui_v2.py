"""The new UI (``--new-ui``): same actions as the classic app, laid out as a map.

The actions themselves are the classic app's and are tested in ``test_tui.py``; what is pinned
down here is that the new layout points them at the right terminal, and that the cards say
what the classic table said.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from test_tui import TID_BROKEN, TID_MASTER, TID_SLAVE, StubEngine, _wire_copying
from textual.widgets import Static

from mt5copier.config import AppConfig, Role
from mt5copier.day_book import MasterEvent, Outcome, SlaveOutcome
from mt5copier.engine import TerminalView
from mt5copier.presentation import RowInfo
from mt5copier.protocol import ActionKind
from mt5copier.tui_v2 import NewCopierApp
from mt5copier.tui_v2.cards import margin_bar, slave_state
from mt5copier.tui_v2.style import FAINT, GREEN, RED
from mt5copier.tui_v2.widgets import (
    Card,
    KeyBar,
    Rail,
    TopBar,
    chip_rows,
    line_color,
    rail_lines,
)


@pytest.fixture
def app(cfg: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NewCopierApp:
    monkeypatch.setattr("mt5copier.tui.app.Engine", StubEngine)
    saved = tmp_path / "config.json"
    monkeypatch.setattr("mt5copier.config.config_path", lambda: saved)
    monkeypatch.setattr("mt5copier.tui.app.config_path", lambda: saved)
    return NewCopierApp(cfg)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    from test_tui import cfg as classic_cfg

    return classic_cfg.__wrapped__(tmp_path)  # type: ignore[attr-defined]


def plain(renderable: object, width: int = 160) -> str:
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def info(**over: object) -> RowInfo:
    base: dict[str, object] = dict(
        terminal_id=TID_SLAVE,
        label="Beta",
        role="slave",
        status_text="connected",
        tier="good",
        hold_reason=None,
        login=1,
        server="s",
        balance=100.0,
        equity=90.0,
        floating_pl=-10.0,
        floating_pl_tier="critical",
        currency="USD",
        open_trades=1,
        paused=False,
        demo_guard_blocked=False,
    )
    base.update(over)
    return RowInfo(**base)  # type: ignore[arg-type]


# --- pure ----------------------------------------------------------------------------------


def test_a_link_that_is_down_outranks_everything_else() -> None:
    state = slave_state(
        info(tier="serious", status_text="disconnected", paused=True, hold_reason="x"),
        dry_run=False,
        copying=True,
    )
    assert state.kind == "down"


@pytest.mark.parametrize(
    ("over", "kind"),
    [
        ({"demo_guard_blocked": True, "paused": True}, "blocked"),
        ({"hold_reason": "orphans", "tier": "warning", "paused": True}, "held"),
        ({"paused": True}, "paused"),
        ({}, "copying"),
    ],
)
def test_slave_state_priority(over: dict[str, object], kind: str) -> None:
    assert slave_state(info(**over), dry_run=False, copying=True).kind == kind


def test_a_slave_is_not_called_copying_while_copying_is_off() -> None:
    assert slave_state(info(), dry_run=False, copying=False).kind == "idle"


def test_margin_bar() -> None:
    assert margin_bar(0.03) == "░" * 10
    assert margin_bar(0.3) == "█" * 3 + "░" * 7
    assert margin_bar(None) == "░" * 10


def test_the_first_row_hangs_off_the_masters_middle() -> None:
    stem, rail, drops = (
        t.plain
        for t in rail_lines(
            [(5, "copying"), (15, "paused"), (25, "blocked"), (35, "idle")],
            40,
            first=True,
            trunk=False,
            more=False,
        )
    )
    assert stem.index("│") == 20
    assert [i for i, ch in enumerate(rail) if ch in "┌┬┐"] == [5, 15, 25, 35]
    assert rail[20] == "┴"
    assert all(drops[x] == "│" for x in (5, 15, 25, 35))
    assert rail_lines([], 40, first=True, trunk=False, more=False) == []


def test_one_card_left_of_the_master_gets_a_rail_to_it() -> None:
    _, rail, _ = (
        t.plain for t in rail_lines([(5, "copying")], 40, first=True, trunk=False, more=False)
    )
    assert rail[5] == "┌" and rail[20] == "┘"
    assert set(rail[6:20]) == {"─"}


def _cells(line: Text, colour: str) -> set[int]:
    """Columns drawn in ``colour`` (later spans win, as when drawn)."""
    last: dict[int, str] = {}
    for span in line.spans:
        for x in range(span.start, span.end):
            last[x] = str(span.style)
    return {x for x, style in last.items() if colour in style}


def test_each_line_is_coloured_by_what_its_slave_is_doing() -> None:
    assert line_color(["copying"]) == GREEN
    assert line_color(["paused"]) == FAINT
    for kind in ("idle", "held", "blocked", "down"):
        assert line_color([kind]) == RED
    assert line_color([]) is None

    # Copying left of the master, paused right of it.
    stem, rail, down = rail_lines(
        [(5, "copying"), (35, "paused")], 40, first=True, trunk=False, more=False
    )
    assert _cells(rail, GREEN) == set(range(5, 21)) and _cells(down, GREEN) == {5}
    assert _cells(rail, FAINT) == set(range(21, 36)) and _cells(down, FAINT) == {35}
    # Where the paths split, and the stem above, lead to a slave being copied to: green.
    assert 20 in _cells(rail, GREEN) and _cells(stem, GREEN) == {20}
    # Solid lines, whatever the state.
    assert set(rail.plain[6:20]) == set(rail.plain[21:35]) == {"─"}


def test_a_line_to_a_slave_not_copying_is_red() -> None:
    stem, rail, down = rail_lines(
        [(5, "held"), (35, "idle")], 40, first=True, trunk=False, more=False
    )
    assert _cells(stem, RED) == {20}
    assert _cells(rail, RED) == set(range(5, 36))
    assert _cells(down, RED) == {5, 35}


def test_the_trunk_takes_the_colour_of_the_rows_below() -> None:
    stem, rail, down = rail_lines(
        [(10, "paused")], 40, first=True, trunk=True, more=True, below=["copying", "held"]
    )
    assert _cells(stem, GREEN) == {20}
    assert _cells(rail, GREEN) == set(range(0, 21))
    assert _cells(down, GREEN) == {0} and _cells(down, FAINT) == {10}


def test_later_rows_branch_off_a_trunk_down_the_left() -> None:
    _, first, down = (
        t.plain for t in rail_lines([(10, "copying")], 40, first=True, trunk=True, more=True)
    )
    assert first[0] == "┌" and down[0] == "│"
    rail, down = (
        t.plain for t in rail_lines([(10, "copying")], 40, first=False, trunk=True, more=False)
    )
    assert rail[0] == "└" and rail[10] == "┐" and down[0] == " "


def test_chips_wrap_whole() -> None:
    assert chip_rows([10, 10, 10], 21) == [[0, 1], [2]]
    assert chip_rows([30, 5], 20) == [[0], [1]]


# --- the app -------------------------------------------------------------------------------


async def test_the_map_starts_with_no_master_and_every_terminal_waiting(app: NewCopierApp) -> None:
    async with app.run_test(size=(160, 50)):
        assert app._order == [TID_MASTER, TID_SLAVE, TID_BROKEN]
        assert app._selected_id() == TID_MASTER
        assert "Not copying" in plain(app.query_one(TopBar).content)


async def test_keys_act_on_the_selected_card(app: NewCopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("m")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER
        await pilot.press("right", "s")
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE
        await pilot.pause()
        slave_cards = [c for c in app.query(Card) if c.has_class("slave")]
        assert len(app.query(Rail)) == 1
        assert [c.terminal_id for c in slave_cards] == [TID_SLAVE]
        # The selection followed the terminal onto its new card.
        assert app._selected_id() == TID_SLAVE
        assert slave_cards[0].has_class("-selected")


async def test_number_keys_jump_to_slave_cards(app: NewCopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("m", "right", "s")
        await pilot.press("a")
        assert app._selected_id() == TID_MASTER
        await pilot.press("1")
        assert app._selected_id() == TID_SLAVE
        await pilot.press("p")
        assert cfg.terminals[TID_SLAVE].paused


async def test_the_hint_only_offers_master_while_there_is_none(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        hint = app.query_one("#notyet-hint", Static)
        assert "made the master" in plain(hint.content)
        await pilot.press("m")
        await pilot.pause()
        said = plain(hint.content)
        assert "made the master" not in said
        assert "made a slave" in said and "first unassign" in said


async def test_live_cards_have_the_lines_green_border(app: NewCopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        _wire_copying(app, cfg)
        engine = app._engine
        stub_views = engine.views

        def connected_views() -> list[TerminalView]:
            views = stub_views()
            for v in views:
                v.process_alive = v.supervised = True
                v.received_at = time.monotonic()
            return views

        engine.views = connected_views  # type: ignore[method-assign]
        app._refresh()
        await pilot.pause()
        master = app.query_one(".card.master", Card)
        slave = next(c for c in app.query(Card) if c.terminal_id == TID_SLAVE)
        assert not master.has_class("-live") and not slave.has_class("-copying")

        cfg.copying_enabled = True
        app._refresh()
        await pilot.pause()
        assert slave.has_class("-copying")
        assert master.has_class("-live")
        cfg.copying_enabled = False
        app._refresh()
        await pilot.pause()
        assert not master.has_class("-live") and not slave.has_class("-copying")


async def test_tab_switches_to_the_log_and_back(app: NewCopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("tab")
        assert app._page == "log"
        await pilot.press("tab")
        assert app._page == "map"


async def test_live_and_dry_run_do_not_look_alike(app: NewCopierApp, cfg: AppConfig) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        assert "DRY RUN" in plain(app.query_one(TopBar).content)
        cfg.dry_run = False
        app._refresh()
        await pilot.pause()
        top = plain(app.query_one(TopBar).content)
        assert "LIVE" in top and "DRY RUN" not in top


async def test_the_key_bar_leads_with_the_selected_terminals_action_menu(
    app: NewCopierApp,
) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        bar = plain(app.query_one(KeyBar).content)
        assert bar.startswith(" space  actions for Alpha Broker")
        await pilot.press("right")
        assert "actions for Beta Broker" in plain(app.query_one(KeyBar).content)
        await pilot.press("m")
        bar = plain(app.query_one(KeyBar).content)
        # What one terminal can do is in its menu, not spelled out on the bar.
        assert " u " not in bar and " m " not in bar and " e " not in bar
        assert "F9  panic halt" in bar


async def test_number_keys_follow_the_numbers_on_the_cards(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    """Cards are numbered in the order they are drawn (by name), not the order the slaves were
    added: a key that selects a different card from the one it names acts on the wrong one."""
    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    cfg.terminals[TID_SLAVE].label = "Zulu Broker"  # added first, drawn second
    cfg.terminals[TID_BROKEN].role = Role.SLAVE
    async with app.run_test(size=(160, 50)) as pilot:
        numbered = [c.terminal_id for c in app.query(Card) if c.has_class("slave")]
        assert numbered == [TID_BROKEN, TID_SLAVE]
        await pilot.press("1")
        assert app._selected_id() == TID_BROKEN
        await pilot.press("2")
        assert app._selected_id() == TID_SLAVE


async def test_the_day_book_lists_each_master_trade_with_every_slaves_outcome(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("m", "right", "s")
        app._engine.events = [  # type: ignore[attr-defined]
            MasterEvent(
                id=1,
                ts=time.time(),
                kind=ActionKind.OPEN,
                preexisting=False,
                master_ticket=11,
                symbol="BTCUSD",
                label="buy",
                volume=0.78,
                volume_before=None,
                entry=84854.0,
                outcomes=(
                    SlaveOutcome(TID_SLAVE, Outcome.DONE, lots=3.11, latency_ms=360.0),
                    SlaveOutcome(TID_BROKEN, Outcome.REFUSED, reason="not a demo account"),
                ),
            )
        ]
        app._refresh()
        await pilot.pause()
        said = plain(app.query_one("#daybook", Static).content)
        assert "Master (Alpha Broker) opened BTCUSD buy 0.78 at 84,854" in said
        assert "Slave 1 (Beta Broker)" in said and "opened 3.11 lots in 360 ms" in said
        assert "refused: not a demo account" in said


async def test_the_log_page_merges_the_file_and_the_day_book_and_filters_them(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.paths import log_dir
    from mt5copier.tui.terminal_screen import TerminalScreen

    log_dir().mkdir(parents=True, exist_ok=True)
    (log_dir() / "copier.log").write_text(
        "2020-01-01 10:00:00 INFO    [MainProcess] mt5copier.engine: spawned worker pid=1 "
        "for Beta Broker\n"
        "2020-01-01 10:00:01 WARNING [MainProcess] mt5copier.config: something odd\n",
        encoding="utf-8",
    )
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("m", "right", "s")
        app._engine.events = [  # type: ignore[attr-defined]
            MasterEvent(
                id=1,
                ts=time.time(),
                kind=ActionKind.OPEN,
                preexisting=False,
                master_ticket=11,
                symbol="BTCUSD",
                label="buy",
                volume=0.78,
                volume_before=None,
                entry=84854.0,
                outcomes=(SlaveOutcome(TID_SLAVE, Outcome.DONE, lots=3.11, latency_ms=360.0),),
            )
        ]
        await pilot.press("tab")
        await pilot.pause()
        shown = app._log_shown or []
        assert [e.terminal_id for e in shown] == [TID_SLAVE, None, TID_MASTER, TID_SLAVE]
        assert shown[-1].text == "Opened 3.11 lots in 360 ms"
        assert "problems only · 1" in plain(app.query_one("#logbar", Static).content)
        who = [app._log_cells(e, app._terminal_numbers())[2].plain for e in shown]
        assert who[1] == "copier"
        assert who[2].startswith("Master (") and who[3].startswith("Slave 1 (")

        await pilot.press("f")
        assert [e.text for e in app._log_shown or []] == ["something odd"]
        await pilot.press("f", "1")
        assert {e.terminal_id for e in app._log_shown or []} == {TID_SLAVE}
        await pilot.press("1")
        assert len(app._log_shown or []) == 4, "the same key again shows every terminal"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, TerminalScreen)
        assert app._selected_id() == TID_SLAVE


def test_the_big_balance_is_drawn_in_solid_digits() -> None:
    from mt5copier.tui_v2.cards import big_amount, block_digits, block_width

    assert block_digits("10.5") == [
        "▄█  █▀█   █▀▀",
        " █  █ █   ▀▀█",
        "▀▀▀ ▀▀▀ ▀ ▀▀▀",
    ]
    assert block_width("10.5") == 13
    assert block_width("—") == 0
    assert big_amount(1234.5, 40).plain.count("\n") == 2
    assert big_amount(1234.5, 10).plain == "1,234.50", "too narrow: plain bold"


def test_a_slave_card_shows_its_margin_like_the_masters() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.protocol import Snapshot, WorkerState
    from mt5copier.tui_v2.cards import slave_card

    snap = Snapshot(terminal_id=TID_SLAVE, state=WorkerState.CONNECTED, equity=1000.0, margin=300.0)
    view = TerminalView(cfg=TerminalConfig(terminal_id=TID_SLAVE, role=Role.SLAVE), snapshot=snap)
    row = info()
    state = slave_state(row, dry_run=False, copying=True)
    said = plain(slave_card(1, view, row, state, refused=0))
    assert "margin ███░░░░░░░ 30%" in said


async def test_a_slave_card_with_big_digits_is_not_cut_off(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    """The row is a fixed height, so it has to hold the tallest card: the balance in big digits
    and the margin line under it."""
    from mt5copier.protocol import Snapshot, WorkerState

    async with app.run_test(size=(200, 55)) as pilot:
        await pilot.press("m", "right", "s")
        app._engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
            terminal_id=TID_SLAVE,
            state=WorkerState.CONNECTED,
            balance=97512.61,
            equity=97475.29,
            margin=8200.0,
            positions=1,
        )
        for _ in range(2):  # the first draw happens before the card knows its width
            app._refresh()
            await pilot.pause()
        card = next(c for c in app.query(Card) if c.has_class("slave"))
        drawn = plain(card.content, width=card.content_size.width).rstrip("\n").splitlines()
        assert any("margin" in line for line in drawn)
        assert len(drawn) <= card.content_size.height, "the card's last lines are cut off"


# --- the action menu -------------------------------------------------------------------------


def test_menu_offers_an_unassigned_terminal_only_the_role_it_can_take() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.tui_v2.menu import terminal_actions

    cfg = TerminalConfig(terminal_id=TID_SLAVE, exe_path=Path("t.exe"))
    keys = [i.key for i in terminal_actions(cfg, has_master=False, orphans_waiting=False)]
    assert keys == ["m", "enter", "e"]
    keys = [i.key for i in terminal_actions(cfg, has_master=True, orphans_waiting=False)]
    assert keys[0] == "s"
    uninstalled = TerminalConfig(terminal_id=TID_SLAVE)
    first = terminal_actions(uninstalled, has_master=True, orphans_waiting=False)[0]
    assert first.action is None, "explained, not offered"


def test_menu_for_a_slave_and_the_master() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.tui_v2.menu import terminal_actions

    slave = TerminalConfig(terminal_id=TID_SLAVE, role=Role.SLAVE, paused=True)
    items = terminal_actions(slave, has_master=True, orphans_waiting=True)
    assert [i.key for i in items] == ["enter", "o", "e", "p", "u"]
    assert next(i for i in items if i.key == "p").label == "resume"
    master = TerminalConfig(terminal_id=TID_MASTER, role=Role.MASTER)
    keys = [i.key for i in terminal_actions(master, has_master=True, orphans_waiting=False)]
    assert keys == ["enter", "u"], "no settings, no pause, and no live guard to lift"


def test_menu_offers_live_trading_only_where_the_guard_is_in_play() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.tui_v2.menu import terminal_actions

    def keys(cfg: TerminalConfig, **kw: bool) -> list[str]:
        return [i.key for i in terminal_actions(cfg, has_master=True, orphans_waiting=False, **kw)]

    slave = TerminalConfig(terminal_id=TID_SLAVE, role=Role.SLAVE)
    assert "l" not in keys(slave), "a demo account needs nothing lifted"
    assert "l" in keys(slave, not_a_demo=True)
    master = TerminalConfig(terminal_id=TID_MASTER, role=Role.MASTER)
    assert "l" not in keys(master, not_a_demo=True), "the master places no orders"
    lifted = TerminalConfig(terminal_id=TID_MASTER, role=Role.MASTER, allow_live_trading=True)
    assert "l" in keys(lifted), "where it was lifted, it can be put back"


def test_menu_offers_reconnect_and_restart_only_when_the_link_is_down() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.tui_v2.menu import terminal_actions

    slave = TerminalConfig(terminal_id=TID_SLAVE, role=Role.SLAVE)
    up = [i.key for i in terminal_actions(slave, has_master=True, orphans_waiting=False)]
    down = [
        i.key
        for i in terminal_actions(slave, has_master=True, orphans_waiting=False, link_ok=False)
    ]
    assert "c" not in up and "ctrl+r" not in up
    assert "c" in down and "ctrl+r" in down


def test_menu_is_placed_beside_its_terminal_and_kept_on_screen() -> None:
    from textual.geometry import Region, Size

    from mt5copier.tui_v2.menu import place

    screen = Size(100, 40)
    assert place(Region(10, 5, 20, 3), Size(30, 8), screen) == (10, 8)
    assert place(Region(90, 5, 10, 3), Size(30, 8), screen) == (70, 8), "pulled left"
    assert place(Region(10, 36, 20, 3), Size(30, 8), screen) == (10, 28), "opens upwards"


async def test_space_opens_the_menu_and_its_key_runs_the_action(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui_v2.menu import ActionMenu

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("space")
        assert isinstance(app.screen, ActionMenu)
        await pilot.press("m")
        await pilot.pause()
        assert not isinstance(app.screen, ActionMenu)
        assert cfg.terminals[TID_MASTER].role is Role.MASTER

        # An unassigned chip: the menu offers to make it a slave, and enter picks that.
        await pilot.press("right", "space")
        assert isinstance(app.screen, ActionMenu)
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].role is Role.SLAVE


async def test_escape_closes_the_menu_and_changes_nothing(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui_v2.menu import ActionMenu

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.press("space", "escape")
        assert not isinstance(app.screen, ActionMenu)
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_clicking_the_selected_terminal_opens_its_menu(app: NewCopierApp) -> None:
    from mt5copier.tui_v2.menu import ActionMenu

    async with app.run_test(size=(160, 50)) as pilot:
        app.action_pick(TID_SLAVE)
        assert app._selected_id() == TID_SLAVE
        assert not isinstance(app.screen, ActionMenu)
        app.action_pick(TID_SLAVE)
        await pilot.pause()
        assert isinstance(app.screen, ActionMenu)


# --- the details page ----------------------------------------------------------------------


def test_the_rule_column_says_how_a_slave_is_copied_in_words() -> None:
    from mt5copier.config import NoStopPolicy, SizingMode, TerminalConfig
    from mt5copier.tui_v2.detail import rule_rows

    t = TerminalConfig(terminal_id=TID_SLAVE, role=Role.SLAVE, symbol_suffix="p")
    said = {label: value.plain for label, value in rule_rows(t, "USD")}
    assert said["Risk per trade"] == "1% of balance"
    assert said["Master has no stop"] == "skip the trade"
    assert said["Direction"] == "same as master"
    assert said["Partials, pending orders"] == "partials only"
    assert said["Symbols copied"] == "all"
    assert said["Name on this broker"] == "EURUSD is traded as EURUSDp"
    assert "Live trading" not in said

    t = TerminalConfig(
        terminal_id=TID_SLAVE,
        role=Role.SLAVE,
        sizing_mode=SizingMode.FIXED_CASH_RISK,
        risk_cash=250,
        no_stop_policy=NoStopPolicy.LOT_MULTIPLE,
        no_stop_lot_multiple=0.5,
        invert=True,
        allow_live_trading=True,
    )
    said = {label: value.plain for label, value in rule_rows(t, "EUR")}
    assert said["Risk per trade"] == "250 EUR a trade"
    assert said["Master has no stop"] == "copy 0.5x its lots"
    assert said["Direction"] == "reversed"
    assert said["Live trading"] == "allowed"
    assert said["Name on this broker"] == "the same as the master's"


def test_the_rule_column_spells_out_renamed_symbols() -> None:
    from mt5copier.config import TerminalConfig
    from mt5copier.tui_v2.detail import symbol_rows

    t = TerminalConfig(
        terminal_id=TID_SLAVE,
        role=Role.SLAVE,
        symbol_suffix="p",
        symbol_blacklist=["XAUUSD"],
        symbol_map={"US30": "US30", "BTCUSD": "BTCUSD.x"},
    )
    said = [(label, value.plain) for label, value in symbol_rows(t, "BTCUSD")]
    assert said == [
        ("Symbols copied", "all except XAUUSD"),
        ("Name on this broker", "EURUSD is traded as EURUSDp"),
        ("Renamed", "BTCUSD is traded as BTCUSD.x"),
        ("", "US30 is traded as US30"),
    ]


def test_the_copying_column_reads_the_sessions_stats() -> None:
    from mt5copier.copy_stats import TerminalStats
    from mt5copier.tui_v2.detail import copying_rows

    stats = TerminalStats(
        latencies_ms=(360.0, 2443.0, 300.0),
        slippage_points=(-20.0, 1175.0),
        succeeded=3,
        refused=1,
        skipped=(("no stop on the master", 2),),
    )
    said = {label: value.plain for label, value in copying_rows(stats)}
    assert said["Orders copied"] == "3"
    assert said["Skipped"] == "2"
    assert said["  most often"] == "no stop on the master x2"
    assert said["Refused, retried, late"] == "1 / 0 / 0"
    assert said["Delay, last / median / max"] == "300 / 360 / 2,443 ms"
    assert said["Slippage, last"] == "1,175 pts worse"
    assert said["Slippage, average"] == "578 pts worse"
    assert said["Disconnects"] == "none"
    assert copying_rows(TerminalStats())[0][1].plain == "none"


async def test_enter_opens_the_details_page_and_arrows_walk_the_terminals(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui_v2.detail import DetailScreen

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("1", "enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        assert screen.terminal_id == TID_SLAVE
        band = plain(screen.query_one("#td-band", Static).content, width=200)
        assert "1 · SLAVE OF A" in band
        assert "RULE" in plain(screen.query_one("#td-rule-head", Static).content)
        assert "Beta Broker" in plain(screen.query_one(TopBar).content, width=200)

        await pilot.press("left")
        assert screen.terminal_id == TID_MASTER, "the arrows go to the next terminal in place"
        assert app._selected_id() == TID_MASTER, "and select it, so p, u and the rest act on it"
        await pilot.pause()
        assert "COPIED TO" in plain(screen.query_one("#td-rule-head", Static).content)
        await pilot.press("1")
        assert screen.terminal_id == TID_SLAVE

        await pilot.press("escape")
        assert not isinstance(app.screen, DetailScreen)


async def test_tab_on_the_details_page_goes_to_the_log(app: NewCopierApp, cfg: AppConfig) -> None:
    from mt5copier.tui_v2.detail import DetailScreen

    cfg.terminals[TID_MASTER].role = Role.MASTER
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("tab")
        assert not isinstance(app.screen, DetailScreen)
        assert app._page == "log"


async def test_the_menu_on_the_details_page_leaves_out_the_details(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui_v2.menu import ActionMenu

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("1", "enter", "space")
        menu = app.screen
        assert isinstance(menu, ActionMenu)
        actions = [i.action for i in menu._items]
        assert "open_terminal" not in actions and "toggle_pause" in actions
        await pilot.press("p")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].paused, "the shown terminal is the one acted on"


# --- the questions that guard money --------------------------------------------------------


def test_unassigning_the_master_says_its_slaves_go_too() -> None:
    from mt5copier.tui_v2.dialogs import letters_to_go, unassign_lead

    assert unassign_lead(Role.MASTER, 4, 2) == (
        "It is the master, so its 4 slaves are released too. Two of them hold open copies:"
    )
    assert unassign_lead(Role.MASTER, 1, 1) == (
        "It is the master, so its 1 slave is released too. It holds open copies:"
    )
    assert unassign_lead(Role.SLAVE, 3, 1) == "It holds open copies:"
    assert letters_to_go("VT Markets (P", "VT Markets (Pty)").plain == "3 letters to go"
    assert letters_to_go(" vt markets (pty) ", "VT Markets (Pty)").plain == "that is the name"
    assert letters_to_go("VX", "VT Markets (Pty)").plain == "not the name"


async def test_unassigning_with_open_copies_asks_in_the_new_dialog(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.reconcile import OpenCopy
    from mt5copier.tui_v2.dialogs import OrphanDialog

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(200, 50)) as pilot:
        app._engine.copies[TID_SLAVE] = [  # type: ignore[attr-defined]
            OpenCopy(
                master_ticket=11, slave_ticket=501, symbol="BTCUSD", volume=3.11, resting=False
            )
        ]
        await pilot.press("a", "u")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, OrphanDialog)
        said = " ".join(plain(s.content, width=200) for s in dialog.query(Static))
        assert "UNASSIGN ALPHA BROKER?" in said
        assert "1 Beta Broker" in said and "BTCUSD 3.11 lots" in said and "#501" in said
        assert dialog.focused is not None and dialog.focused.id == "cancel", "rests on keep"

        await pilot.press("escape")
        assert cfg.terminals[TID_MASTER].role is Role.MASTER, "esc keeps everything"
        await pilot.press("u")
        await pilot.pause()
        await pilot.click("#go")
        await pilot.pause()
        assert cfg.terminals[TID_MASTER].role is Role.UNASSIGNED


async def test_allowing_live_trading_counts_down_the_name(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from textual.widgets import Button

    from mt5copier.tui_v2.dialogs import LiveDialog

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("1", "l")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, LiveDialog)
        go = dialog.query_one("#go", Button)
        assert go.has_class("-waiting")
        await pilot.press(*"Beta")
        assert "letters to go" in plain(dialog.query_one("#to-go", Static).content)
        await pilot.press("enter")
        await pilot.pause()
        assert not cfg.terminals[TID_SLAVE].allow_live_trading, "half a name is refused"
        assert "not the name" in plain(dialog.query_one("#error", Static).content)
        await pilot.press(*" Broker")
        assert not go.has_class("-waiting")
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.terminals[TID_SLAVE].allow_live_trading
        assert not isinstance(app.screen, LiveDialog)


async def test_tab_inside_a_dialog_moves_between_its_buttons_not_the_pages(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.tui.settings_screen import SettingsScreen
    from mt5copier.tui_v2.dialogs import LiveDialog

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("1", "l")
        await pilot.pause()
        assert isinstance(app.screen, LiveDialog)
        before = app.screen.focused
        await pilot.press("tab")
        assert app._page == "map", "the page under the dialog stays put"
        assert app.screen.focused is not before

        await pilot.press("escape", "e")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        before = app.screen.focused
        await pilot.press("tab")
        assert app._page == "map"
        assert app.screen.focused is not before, "tab walks the settings form's fields"

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("tab")
        assert app._page == "log", "and on the page itself it still turns the page"


async def test_settings_are_written_as_sentences_with_a_worked_example(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from mt5copier.protocol import Position, PositionType, Snapshot, WorkerState
    from mt5copier.reconcile import OpenCopy
    from mt5copier.tui_v2.settings import SettingsForm

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    cfg.terminals[TID_SLAVE].commission_per_lot_round_turn = 3.5
    buy = Position(11, "BTCUSD", PositionType.BUY, 0.78, 84854.0, sl=84554.0)
    async with app.run_test(size=(140, 50)) as pilot:
        engine = app._engine
        engine.snapshots[TID_MASTER] = Snapshot(  # type: ignore[attr-defined]
            terminal_id=TID_MASTER, state=WorkerState.CONNECTED, open_positions=(buy,)
        )
        engine.snapshots[TID_SLAVE] = Snapshot(  # type: ignore[attr-defined]
            terminal_id=TID_SLAVE, state=WorkerState.CONNECTED, balance=97512.61, trade_mode=0
        )
        engine.copies[TID_SLAVE] = [  # type: ignore[attr-defined]
            OpenCopy(11, 501, "BTCUSD", 3.11, resting=False, price_value=1.0, point=0.01)
        ]
        await pilot.press("1", "e")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SettingsForm)
        assert form.focused is not None and form.focused.id == "f-risk_percent"

        def example() -> str:
            return plain(form.query_one("#ex-body", Static).content)

        assert "Master buys BTCUSD with its stop 300.00 away." in example()
        assert "this slave buys about 3.21 lots" in example()
        said = " ".join(plain(s.content, width=200) for s in form.query(Static))
        assert "slave 1 of A · demo account" in said
        assert "1 open copy on this slave is orphaned" in said

        await pilot.press("backspace", "2")
        await pilot.pause()
        assert "1,950.25 risk" in example() and "about 6.42 lots" in example(), "follows typing"
        await pilot.press("backspace", "x")
        await pilot.pause()
        assert "Correct the settings" in example()


async def test_the_settings_form_saves_direction_and_symbol_renames(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from textual.widgets import Input, Select

    from mt5copier.tui_v2.settings import SettingsForm

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("1", "e")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SettingsForm)
        form.query_one("#f-invert", Select).value = "opposite"
        form.query_one("#f-symbol_map", Input).value = "US30=US30.cash, ger40 = DE40"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsForm)
        saved = cfg.terminals[TID_SLAVE]
        assert saved.invert
        assert saved.symbol_map == {"US30": "US30.cash", "GER40": "DE40"}


async def test_the_settings_form_shows_only_the_values_its_choices_use(
    app: NewCopierApp, cfg: AppConfig
) -> None:
    from textual.widgets import Select

    from mt5copier.config import SizingMode
    from mt5copier.tui_v2.settings import SettingsForm

    cfg.terminals[TID_MASTER].role = Role.MASTER
    cfg.terminals[TID_SLAVE].role = Role.SLAVE
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press("1", "e")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SettingsForm)
        assert form.query_one("#f-risk_percent").display
        assert not form.query_one("#f-risk_cash").display
        assert not form.query_one("#multiple-part").display
        form.query_one("#f-sizing_mode", Select).value = SizingMode.FIXED_CASH_RISK.value
        form.query_one("#f-no_stop_policy", Select).value = "lot_multiple"
        await pilot.pause()
        assert form.query_one("#f-risk_cash").display
        assert not form.query_one("#f-risk_percent").display
        assert form.query_one("#multiple-part").display


def test_every_editable_setting_is_drawn_into_the_new_form() -> None:
    """A setting added to the editor has to be given a place in a sentence here too."""
    import inspect

    from mt5copier.settings_edit import EDITABLE
    from mt5copier.tui_v2 import settings

    source = inspect.getsource(settings.SettingsForm.compose)
    missing = [f.name for f in EDITABLE if f'"{f.name}"' not in source]
    assert not missing
