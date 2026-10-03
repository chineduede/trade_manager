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
        assert "the master (m)" in plain(hint.content)
        await pilot.press("m")
        await pilot.pause()
        said = plain(hint.content)
        assert "(m)" not in said
        assert "slave (s)" in said and "first unassign" in said


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


async def test_the_key_bar_offers_what_the_selected_terminal_can_do(app: NewCopierApp) -> None:
    async with app.run_test(size=(160, 50)) as pilot:
        assert "m master" in plain(app.query_one(KeyBar).content)
        await pilot.press("m")
        bar = plain(app.query_one(KeyBar).content)
        assert "u unassign" in bar and "m master" not in bar
        assert "F9 panic halt" in bar


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
        assert "A opened BTCUSD buy 0.78 at 84,854" in said
        assert "1 Beta" in said and "opened 3.11 lots in 360 ms" in said
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
