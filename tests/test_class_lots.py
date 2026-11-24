"""The lot cap by asset class: the catalog's reading, the rule's arithmetic, the verdict's
table, the engine's reduction to one number, and the executor-side fit."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mt5copier.catalog import DEFAULT, CatalogStore
from mt5copier.engine import Engine
from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc, Level
from mt5copier.guards.book import ClassLotsCap, Verdict
from mt5copier.protocol import Position, PositionType, Snapshot, WorkerState
from mt5copier.sizing import SymbolFacts, fit_lots

KEY = "1001@Prop-Demo"
NOW = 1_780_000_000.0


def snap(positions: tuple[tuple[str, float], ...] = ()) -> Snapshot:
    """``positions`` are ``(symbol, lots)`` pairs."""
    opened = tuple(
        Position(ticket=10 + i, symbol=s, type=PositionType.BUY, volume=v, price_open=1.0, sl=0.0)
        for i, (s, v) in enumerate(positions)
    )
    return Snapshot(
        terminal_id="T",
        state=WorkerState.CONNECTED,
        balance=100_000.0,
        equity=100_000.0,
        margin_free=90_000.0,
        currency="USD",
        open_positions=opened,
        live_positions=(),
        deals=(),
        deals_since=NOW - 3600,
    )


def rule(scope: str = "each", on_breach: str = "shrink", **caps: float) -> GuardRule:
    return GuardRule(
        type="class_lots",
        params={"scope": scope, "on_breach": on_breach, **caps},
    )


def book_with(r: GuardRule, tmp: Path) -> GuardBook:
    doc = GuardsDoc(accounts={KEY: AccountGuards(initial_balance=100_000.0, rules=(r,))})
    return GuardBook(doc, state_path=tmp / "state.json", catalog=CatalogStore(tmp / "catalog.json"))


FACTS = SymbolFacts(
    name="US30", volume_min=0.01, volume_max=100.0, volume_step=0.01, point=0.01, digits=2
)


# --- the catalog's reading ---------------------------------------------------------------------


def test_the_seeds_read_the_common_names() -> None:
    assert DEFAULT.classify("EURUSD") == "forex"
    assert DEFAULT.classify("AUDCAD") == "forex", "crosses included"
    assert DEFAULT.classify("US30") == "indices"
    assert DEFAULT.classify("XAUUSD") == "metals"
    assert DEFAULT.classify("USOIL") == "energies"
    assert DEFAULT.classify("BTCUSD") == "crypto"
    assert DEFAULT.classify("AAPL") == "other", "stocks ship empty, so an equity is 'other'"


def test_decoration_washes_out_in_the_matching() -> None:
    assert DEFAULT.classify("EURUSD.r") == "forex"
    assert DEFAULT.classify("m.EURUSDp") == "forex"
    assert DEFAULT.classify("US30.cash") == "indices", "a listed name inside a longer one counts"
    assert DEFAULT.classify("GOLD#") == "metals"


def test_an_explicit_listing_outranks_the_pair_reading(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog.json")
    store.save(["USD", "EUR"], {"stocks": ["EURUSD"], "indices": ["US30"]})
    assert store.current().classify("EURUSD") == "stocks"


def test_the_saved_file_is_the_whole_truth(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog.json")
    assert store.current().classify("SGDJPY") == "other", "SGD is not seeded"
    store.save(["USD", "EUR", "JPY", "SGD"], {})
    assert store.current().classify("SGDJPY") == "forex"
    assert store.current().classify("US30") == "other", "a removed seed stays removed"


def test_another_window_saving_is_seen_by_mtime(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    ours, theirs = CatalogStore(path), CatalogStore(path)
    assert ours.current().classify("US30") == "indices"
    theirs.save(["USD"], {"energies": ["US30"]})
    assert ours.current().classify("US30") == "energies"


# --- the rule ----------------------------------------------------------------------------------


def test_zero_means_no_cap_and_negatives_are_refused(tmp_path: Path) -> None:
    r = rule(forex=2.0, indices=0.0)
    params, problems = r.resolved()
    assert not problems
    bad = GuardRule(type="class_lots", params={"forex": -1.0})
    _, complaints = bad.resolved()
    assert complaints and "at least 0" in complaints[0]


def test_each_scope_reads_ok_with_the_caps_as_detail(tmp_path: Path) -> None:
    book = book_with(rule(forex=2.0, indices=1.0), tmp_path)
    row = book.report(KEY, snap(), NOW).rules[0]
    assert row.level is Level.OK
    assert "forex ≤ 2" in row.detail and "indices ≤ 1" in row.detail
    assert row.per_trade


def test_total_scope_measures_the_fullest_class(tmp_path: Path) -> None:
    book = book_with(rule(scope="total", forex=2.0, indices=1.0), tmp_path)
    taken = snap(positions=(("US30.cash", 0.75), ("EURUSD.r", 0.5), ("XAUUSD", 5.0)))
    row = book.report(KEY, taken, NOW, decor=("", ".r", ())).rules[0]
    assert (row.used, row.limit) == (0.75, 1.0), "indices are fuller than forex; metals uncapped"
    assert "indices 0.75 of 1 lots open" in row.detail
    assert row.level is Level.OK


def test_a_rename_is_reversed_before_the_class_is_read(tmp_path: Path) -> None:
    book = book_with(rule(scope="total", indices=1.0), tmp_path)
    taken = snap(positions=(("Dow30", 1.0),))
    row = book.report(KEY, taken, NOW, decor=("", "", (("US30", "Dow30"),))).rules[0]
    assert (row.used, row.limit) == (1.0, 1.0)
    assert row.level is Level.BREACHED


# --- the verdict -------------------------------------------------------------------------------


def test_the_verdict_carries_the_table_and_never_holds_back(tmp_path: Path) -> None:
    book = book_with(rule(scope="total", on_breach="decline", indices=1.0), tmp_path)
    verdict = book.verdict(KEY, snap(positions=(("US30", 3.0),)), NOW)
    assert verdict.class_lots == (
        ClassLotsCap(
            verdict.class_lots[0].rule_id,
            "Max lots by asset class",
            "decline",
            "total",
            (("indices", 1.0),),
        ),
    )
    assert not verdict.held_back, "the cap acts on each trade at dispatch, never as a hold"


def test_a_rule_with_every_ceiling_zero_sends_no_table(tmp_path: Path) -> None:
    book = book_with(rule(), tmp_path)
    assert book.verdict(KEY, snap(), NOW).class_lots == ()


# --- the engine's reduction --------------------------------------------------------------------


def engine_like(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _catalog=CatalogStore(tmp_path / "catalog.json"),
        _class_open_lots=Engine._class_open_lots,
    )


def cfg_like(
    prefix: str = "", suffix: str = "", symbol_map: dict[str, str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(symbol_prefix=prefix, symbol_suffix=suffix, symbol_map=symbol_map or {})


def caps(*, scope: str = "each", mode: str = "shrink", **table: float) -> Verdict:
    return Verdict(
        class_lots=(
            ClassLotsCap("r1", "Max lots by asset class", mode, scope, tuple(table.items())),
        )
    )


def test_each_scope_sends_the_class_cap(tmp_path: Path) -> None:
    got = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(),
        caps(indices=1.0, forex=2.0),
        SimpleNamespace(master_symbol="US30"),
        snap(),
    )
    assert got == (
        1.0,
        "shrink",
        "Max lots by asset class",
        "r1",
        "US30 → indices, capped at 1 lots a trade",
    )


def test_an_uncapped_class_sends_nothing(tmp_path: Path) -> None:
    got = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(),
        caps(indices=1.0),
        SimpleNamespace(master_symbol="EURUSD"),
        snap(),
    )
    assert got is None


def test_total_scope_sends_the_room_left(tmp_path: Path) -> None:
    got = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(suffix=".cash"),
        caps(scope="total", indices=2.0),
        SimpleNamespace(master_symbol="US30"),
        snap(positions=(("US30.cash", 0.75), ("EURUSD", 1.0))),
    )
    assert got is not None
    ceiling, _mode, _rule, _id, allowed = got
    assert ceiling == 1.25
    assert allowed == "US30 → indices: 0.75 of 2 lots already open"


def test_unreadable_open_lots_refuse_unless_warn_only(tmp_path: Path) -> None:
    unread = Snapshot(terminal_id="T", state=WorkerState.CONNECTED, open_positions=None)
    strict = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(),
        caps(scope="total", indices=2.0),
        SimpleNamespace(master_symbol="US30"),
        unread,
    )
    assert strict is not None and strict[0] == 0.0
    soft = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(),
        caps(scope="total", mode="warn", indices=2.0),
        SimpleNamespace(master_symbol="US30"),
        unread,
    )
    assert soft is not None and soft[0] == 2.0


def test_the_tightest_rule_wins(tmp_path: Path) -> None:
    verdict = Verdict(
        class_lots=(
            ClassLotsCap("a", "A", "decline", "each", (("indices", 2.0),)),
            ClassLotsCap("b", "B", "shrink", "each", (("indices", 0.5),)),
        )
    )
    got = Engine._class_lots_limit(
        engine_like(tmp_path),
        cfg_like(),
        verdict,
        SimpleNamespace(master_symbol="US30"),
        snap(),
    )
    assert got is not None and (got[0], got[1], got[3]) == (0.5, "shrink", "b")


# --- the executor-side fit ---------------------------------------------------------------------


def test_fit_lots_under_the_cap_changes_nothing() -> None:
    assert fit_lots(lots=0.5, cap=1.0, mode="shrink", symbol="US30", facts=FACTS).lots == 0.5


def test_fit_lots_shrinks_to_the_cap_and_says_which_class() -> None:
    fit = fit_lots(
        lots=3.0,
        cap=1.0,
        mode="shrink",
        symbol="US30.cash",
        facts=FACTS,
        rule="Max lots by asset class",
        rule_id="r1",
        allowed="US30 → indices, capped at 1 lots a trade",
    )
    assert fit.lots == 1.0
    assert fit.note is not None and fit.note.action == "shrunk"
    assert "US30 → indices" in fit.note.detail


def test_fit_lots_declines_and_warns_by_mode() -> None:
    declined = fit_lots(lots=3.0, cap=1.0, mode="decline", symbol="US30", facts=FACTS)
    assert declined.lots is None and declined.note is not None
    warned = fit_lots(lots=3.0, cap=1.0, mode="warn", symbol="US30", facts=FACTS)
    assert warned.lots == 3.0 and warned.note is not None and warned.note.action == "warned"


def test_a_cap_below_one_step_declines_even_when_shrinking() -> None:
    fit = fit_lots(lots=1.0, cap=0.0, mode="shrink", symbol="US30", facts=FACTS)
    assert fit.lots is None
    assert fit.note is not None and fit.note.action == "declined"


# --- base_of: the known instrument inside a decorated broker name -----------------------------


def test_base_of_reads_through_decoration() -> None:
    assert DEFAULT.base_of("EURUSDp") == "EURUSD"
    assert DEFAULT.base_of("mGBPJPY") == "GBPJPY"
    assert DEFAULT.base_of("EURUSD.r") == "EURUSD"
    assert DEFAULT.base_of("US500.cash") == "US500"
    assert DEFAULT.base_of("XAUUSD+") == "XAUUSD"


def test_base_of_takes_a_listed_name_whole() -> None:
    assert DEFAULT.base_of("USTEC") == "USTEC", "not USTE with a c bolted on"
    assert DEFAULT.base_of("GOLD") == "GOLD"
    assert DEFAULT.base_of("EURUSD") == "EURUSD"


def test_base_of_refuses_what_it_does_not_know() -> None:
    assert DEFAULT.base_of("AAPL") is None
    assert DEFAULT.base_of("") is None
    assert DEFAULT.base_of("EURUSDQUARTERLY") is None, "too much leftover to be decoration"
