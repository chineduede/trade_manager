"""Symbol resolution tests, against real broker naming conventions.

An unresolved symbol fails loudly; a *wrongly* resolved one copies the trade onto a different
instrument and says nothing. So the interesting cases here are the refusals.
"""

from __future__ import annotations

from mt5copier.config import TerminalConfig
from mt5copier.symbols import Resolution, SymbolCache, base_name, resolve

TID_A = "A" * 32
TID_B = "B" * 32


def slave(terminal_id: str = TID_A, **kw: object) -> TerminalConfig:
    base: dict[str, object] = {"terminal_id": terminal_id}
    base.update(kw)
    return TerminalConfig(**base)  # type: ignore[arg-type]


def test_an_exact_name_wins() -> None:
    got = resolve("EURUSD", ["EURUSD", "EURUSD.r"], slave())
    assert got.name == "EURUSD"
    assert got.how is Resolution.EXACT


def test_matching_is_case_insensitive_but_returns_the_brokers_spelling() -> None:
    got = resolve("eurusd", ["EURUSD"], slave())
    assert got.name == "EURUSD"


def test_the_configured_suffix_is_tried_next() -> None:
    got = resolve("EURUSD", ["EURUSD.r", "GBPUSD.r"], slave(symbol_suffix=".r"))
    assert got.name == "EURUSD.r"
    assert got.how is Resolution.CONFIGURED


def test_a_prefix_works_too_which_zele_mt5_could_not_do() -> None:
    got = resolve("EURUSD", ["mEURUSD"], slave(symbol_prefix="m"))
    assert got.name == "mEURUSD"


def test_prefix_and_suffix_together() -> None:
    got = resolve("EURUSD", ["mEURUSD.pro"], slave(symbol_prefix="m", symbol_suffix=".pro"))
    assert got.name == "mEURUSD.pro"


def test_a_decorated_master_name_maps_onto_a_differently_decorated_slave() -> None:
    """The master is on a .r broker and the slave is on an m broker."""
    got = resolve("EURUSD.r", ["mEURUSD"], slave(symbol_prefix="m"))
    assert got.name == "mEURUSD"


def test_the_scan_finds_a_decoration_nobody_configured() -> None:
    got = resolve("EURUSD", ["EURUSD.pro", "GBPUSD.pro"], slave())
    assert got.name == "EURUSD.pro"
    assert got.how is Resolution.SCAN


def test_an_unknown_symbol_is_refused_rather_than_guessed() -> None:
    got = resolve("EURUSD", ["GBPUSD", "USDJPY"], slave())
    assert not got.ok
    assert got.how is Resolution.UNRESOLVED


def test_an_ambiguous_scan_refuses_rather_than_picking_the_first() -> None:
    """Two names sharing a base can be different instruments -- a CFD and its cash
    equivalent, say -- and choosing wrongly copies onto the wrong market."""
    got = resolve("EURUSD", ["EURUSD.r", "EURUSD.pro"], slave())
    assert not got.ok


def test_stripping_is_conservative_about_short_names() -> None:
    """Over-stripping is how two genuinely different instruments start looking alike."""
    assert base_name("XAUUSD") == "XAUUSD"
    assert base_name("EURUSD.r") == "EURUSD"
    assert base_name("EURUSDm") == "EURUSD"
    assert base_name("GER40") == "GER40", "too short to strip a trailing 0"


def test_an_exact_match_beats_a_scan_candidate() -> None:
    got = resolve("EURUSD", ["EURUSD", "EURUSDm"], slave())
    assert got.name == "EURUSD"
    assert got.how is Resolution.EXACT


def test_the_configured_mapping_beats_a_scan() -> None:
    got = resolve("EURUSD", ["EURUSDm", "EURUSD.r"], slave(symbol_suffix=".r"))
    assert got.name == "EURUSD.r"
    assert got.how is Resolution.CONFIGURED


# --- the cache ------------------------------------------------------------------------------


def test_the_cache_is_keyed_per_terminal_not_per_symbol() -> None:
    """The zele-mt5 SymbolMapper bug: two brokers mapping GOLD differently collide, and the
    second silently gets the first one's answer."""
    cache = SymbolCache()
    a = cache.resolve(TID_A, "GOLD", ["GOLD.r"], slave(TID_A))
    b = cache.resolve(TID_B, "GOLD", ["XAUUSD"], slave(TID_B, symbol_suffix=""))

    assert a.name == "GOLD.r"
    assert b.name != "GOLD.r"


def test_a_cached_answer_is_reused_without_rescanning() -> None:
    cache = SymbolCache()
    assert cache.resolve(TID_A, "EURUSD", ["EURUSD.r"], slave()).name == "EURUSD.r"
    # The symbol list is now empty; a cache miss would fail to resolve.
    assert cache.resolve(TID_A, "EURUSD", [], slave()).name == "EURUSD.r"


def test_failures_are_cached_too() -> None:
    """A symbol this broker does not list will not start existing, and rescanning the whole
    list every tick is wasteful."""
    cache = SymbolCache()
    assert not cache.resolve(TID_A, "NOPE", ["EURUSD"], slave()).ok
    assert cache.get(TID_A, "NOPE") is not None


def test_forgetting_one_terminal_leaves_the_others() -> None:
    cache = SymbolCache()
    cache.resolve(TID_A, "EURUSD", ["EURUSD"], slave(TID_A))
    cache.resolve(TID_B, "EURUSD", ["EURUSD"], slave(TID_B))
    cache.forget(TID_A)
    assert cache.get(TID_A, "EURUSD") is None
    assert cache.get(TID_B, "EURUSD") is not None
