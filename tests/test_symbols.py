"""Symbol resolution tests, against real broker naming conventions.

An unresolved symbol fails loudly; a *wrongly* resolved one copies the trade onto a different
instrument and says nothing. So the interesting cases here are the refusals.
"""

from __future__ import annotations

from mt5copier.catalog import DEFAULT
from mt5copier.config import TerminalConfig
from mt5copier.symbols import (
    Resolution,
    SymbolCache,
    asserted_decoration,
    base_name,
    configured_name,
    infer_decoration,
    resolve,
    undecorate,
)

TID_A = "A" * 32
TID_B = "B" * 32


def slave(terminal_id: str = TID_A, **kw: object) -> TerminalConfig:
    base: dict[str, object] = {"terminal_id": terminal_id}
    base.update(kw)
    return TerminalConfig(**base)  # type: ignore[arg-type]


def test_an_exact_name_wins() -> None:
    got = resolve("EURUSD", ["EURUSD", "EURUSD.r"])
    assert got.name == "EURUSD"
    assert got.how is Resolution.EXACT


def test_matching_is_case_insensitive_but_returns_the_brokers_spelling() -> None:
    got = resolve("eurusd", ["EURUSD"])
    assert got.name == "EURUSD"


def test_the_configured_suffix_resolves_a_decorated_broker() -> None:
    got = resolve("EURUSD", ["EURUSD.r", "GBPUSD.r"], suffix=".r")
    assert got.name == "EURUSD.r"
    assert got.how is Resolution.CONFIGURED


def test_a_prefix_works_too() -> None:
    got = resolve("EURUSD", ["mEURUSD"], prefix="m")
    assert got.name == "mEURUSD"


def test_prefix_and_suffix_together() -> None:
    got = resolve("EURUSD", ["mEURUSD.pro"], prefix="m", suffix=".pro")
    assert got.name == "mEURUSD.pro"


def test_a_decorated_master_name_maps_onto_a_differently_decorated_slave() -> None:
    """The master is on a .r broker and the slave is on an m broker."""
    got = resolve("EURUSD.r", ["mEURUSD"], prefix="m")
    assert got.name == "mEURUSD"


def test_the_scan_finds_a_decoration_nobody_configured() -> None:
    got = resolve("EURUSD", ["EURUSD.pro", "GBPUSD.pro"])
    assert got.name == "EURUSD.pro"
    assert got.how is Resolution.SCAN


def test_an_unknown_symbol_is_refused_rather_than_guessed() -> None:
    got = resolve("EURUSD", ["GBPUSD", "USDJPY"])
    assert not got.ok
    assert got.how is Resolution.UNRESOLVED


def test_an_ambiguous_scan_refuses_rather_than_picking_the_first() -> None:
    """Two names sharing a base can be different instruments -- a CFD and its cash
    equivalent, say -- and choosing wrongly copies onto the wrong market."""
    got = resolve("EURUSD", ["EURUSD.r", "EURUSD.pro"])
    assert not got.ok


def test_stripping_is_conservative_about_short_names() -> None:
    """Over-stripping is how two genuinely different instruments start looking alike."""
    assert base_name("XAUUSD") == "XAUUSD"
    assert base_name("EURUSD.r") == "EURUSD"
    assert base_name("EURUSDm") == "EURUSD"
    assert base_name("GER40") == "GER40", "too short to strip a trailing 0"


def test_an_exact_match_beats_a_scan_candidate() -> None:
    got = resolve("EURUSD", ["EURUSD", "EURUSDm"])
    assert got.name == "EURUSD"
    assert got.how is Resolution.EXACT


def test_the_configured_mapping_beats_a_scan() -> None:
    got = resolve("EURUSD", ["EURUSDm", "EURUSD.r"], suffix=".r")
    assert got.name == "EURUSD.r"
    assert got.how is Resolution.CONFIGURED


# --- explicit overrides ---------------------------------------------------------------------


def test_an_explicit_mapping_beats_a_configured_suffix() -> None:
    got = resolve("XAUUSD", ["XAUUSDp", "XAUUSD.s"], suffix="p", symbol_map={"XAUUSD": "XAUUSD.s"})
    assert got.name == "XAUUSD.s"
    assert got.how is Resolution.MAPPED


def test_an_explicit_mapping_beats_an_exact_match_too() -> None:
    """The point of an override is to disambiguate; one that loses to a coincidence of naming
    could not do that."""
    got = resolve("GOLD", ["GOLD", "XAUUSD"], symbol_map={"GOLD": "XAUUSD"})
    assert got.name == "XAUUSD"
    assert got.how is Resolution.MAPPED


def test_a_mapping_key_is_matched_case_insensitively() -> None:
    got = resolve("eurgbp", ["EURGBPp"], symbol_map={"EURGBP": "EURGBPp"})
    assert got.name == "EURGBPp"


def test_a_mapping_returns_the_brokers_spelling_not_the_users() -> None:
    """Folding the value against the real list means a mistyped case still resolves, and what
    goes into the request is what ``symbol_info`` will accept."""
    got = resolve("EURGBP", ["EURGBPp"], symbol_map={"EURGBP": "eurgbpp"})
    assert got.name == "EURGBPp"


def test_a_mapping_onto_a_name_the_broker_does_not_list_is_refused_not_guessed() -> None:
    """The entry asserts these two names are the same instrument. If the named one is absent
    the assertion is false, and falling through would place the trade on a name the user never
    wrote while the log reported a match -- this module's cardinal sin."""
    got = resolve("EURGBP", ["EURGBPp", "EURGBP.r"], symbol_map={"EURGBP": "NOPE"})
    assert not got.ok
    assert got.how is Resolution.MAPPED_MISSING, "and distinctly, so the tester can say why"


def test_an_unmapped_symbol_is_unaffected_by_other_peoples_overrides() -> None:
    got = resolve("EURUSD", ["EURUSDp"], suffix="p", symbol_map={"XAUUSD": "XAUUSD.s"})
    assert got.name == "EURUSDp"
    assert got.how is Resolution.CONFIGURED


# --- the decoration now outranks an exact match ----------------------------------------------


def test_a_configured_suffix_outranks_a_bare_exact_match() -> None:
    """BlackBull lists EURGBPp. A bare EURGBP beside it is more likely a different product --
    a standard account's symbol next to a raw one -- than the same instrument, and the user
    setting a suffix is what says so."""
    got = resolve("EURGBP", ["EURGBP", "EURGBPp"], suffix="p")
    assert got.name == "EURGBPp"
    assert got.how is Resolution.CONFIGURED


def test_a_configured_suffix_that_misses_still_falls_back_to_an_exact_match() -> None:
    """BlackBull suffixes 1978 symbols with p and leaves US30 and NAS100 bare. If a miss were
    fatal, every undecorated index would need an override that says nothing."""
    got = resolve("US30", ["US30", "EURGBPp"], suffix="p")
    assert got.name == "US30"
    assert got.how is Resolution.EXACT


def test_the_reorder_changes_nothing_when_no_decoration_is_configured() -> None:
    """candidates()[1:] is empty without a prefix or suffix, so a terminal that has not opted
    in behaves exactly as it did before."""
    got = resolve("EURUSD", ["EURUSD", "EURUSDm"])
    assert got.name == "EURUSD"
    assert got.how is Resolution.EXACT


# --- the connectionless approximation --------------------------------------------------------


def test_configured_name_prefers_an_override() -> None:
    assert configured_name("XAUUSD", "", "p", {"XAUUSD": "XAUUSD.s"}) == "XAUUSD.s"


def test_configured_name_falls_back_to_the_decoration() -> None:
    assert configured_name("EURGBP", "", "p") == "EURGBPp"
    assert configured_name("EURUSD", "m", ".r") == "mEURUSD.r"


# --- the cache ------------------------------------------------------------------------------


def test_the_cache_is_keyed_per_terminal_not_per_symbol() -> None:
    """The symbol-keyed-cache bug: two brokers mapping GOLD differently collide, and the
    second silently gets the first one's answer."""
    cache = SymbolCache()
    a = cache.resolve(TID_A, "GOLD", ["GOLD.r"])
    b = cache.resolve(TID_B, "GOLD", ["XAUUSD"])

    assert a.name == "GOLD.r"
    assert b.name != "GOLD.r"


def test_a_cached_answer_is_reused_without_rescanning() -> None:
    cache = SymbolCache()
    assert cache.resolve(TID_A, "EURUSD", ["EURUSD.r"]).name == "EURUSD.r"
    # The symbol list is now empty; a cache miss would fail to resolve.
    assert cache.resolve(TID_A, "EURUSD", []).name == "EURUSD.r"


def test_failures_are_cached_too() -> None:
    """A symbol this broker does not list will not start existing, and rescanning the whole
    list every tick is wasteful."""
    cache = SymbolCache()
    assert not cache.resolve(TID_A, "NOPE", ["EURUSD"]).ok
    assert cache.get(TID_A, "NOPE") is not None


def test_a_changed_suffix_is_not_answered_from_the_cache() -> None:
    """The 2026-09-21 failure. The suffix was corrected and the worker went on refusing,
    because the answer was served from the key alone and the new decoration never reached
    resolve(). It needed a worker restart, which the editor cannot ask a user to do."""
    cache = SymbolCache()
    assert not cache.resolve(TID_A, "EURGBP", ["EURGBPp"]).ok, "no suffix yet"

    got = cache.resolve(TID_A, "EURGBP", ["EURGBPp"], suffix="p")
    assert got.name == "EURGBPp"
    assert got.how is Resolution.CONFIGURED


def test_a_changed_override_is_not_answered_from_the_cache_either() -> None:
    cache = SymbolCache()
    assert cache.resolve(TID_A, "GOLD", ["GOLD", "XAUUSD"]).name == "GOLD"
    got = cache.resolve(TID_A, "GOLD", ["GOLD", "XAUUSD"], symbol_map={"GOLD": "XAUUSD"})
    assert got.name == "XAUUSD"


def test_changing_one_override_leaves_the_other_answers_alone() -> None:
    """The entry remembers the mapping for *its own* symbol, not a fingerprint of the whole
    map, so correcting one override does not throw away every scan already paid for."""
    cache = SymbolCache()
    cache.resolve(TID_A, "EURUSD", ["EURUSD.pro"])
    cache.resolve(TID_A, "GBPUSD", ["GBPUSD.pro"])

    # A map that says nothing about EURUSD or GBPUSD.
    assert cache.resolve(TID_A, "EURUSD", [], symbol_map={"XAUUSD": "XAUUSD.s"}).name == (
        "EURUSD.pro"
    ), "served from cache despite an empty symbol list"
    assert cache.resolve(TID_A, "GBPUSD", [], symbol_map={"XAUUSD": "XAUUSD.s"}).ok


def test_identical_settings_still_hit_the_cache_without_rescanning() -> None:
    cache = SymbolCache()
    assert cache.resolve(TID_A, "EURGBP", ["EURGBPp"], suffix="p").name == "EURGBPp"
    # An empty list would fail to resolve, so answering proves nothing was re-scanned.
    assert cache.resolve(TID_A, "EURGBP", [], suffix="p").name == "EURGBPp"


def test_forgetting_one_terminal_leaves_the_others() -> None:
    cache = SymbolCache()
    cache.resolve(TID_A, "EURUSD", ["EURUSD"])
    cache.resolve(TID_B, "EURUSD", ["EURUSD"])
    cache.forget(TID_A)
    assert cache.get(TID_A, "EURUSD") is None
    assert cache.get(TID_B, "EURUSD") is not None


def test_the_masters_decoration_comes_off_only_where_it_is() -> None:
    """BlackBull's p: too short for the built-in list, so only the master's setting strips it."""
    assert base_name("EURUSDp") == "EURUSDP", "the scan alone cannot get at this one"
    assert undecorate("EURUSDp", suffix="p") == "EURUSD"
    assert undecorate("EURUSDP", suffix="p") == "EURUSD", "case is folded for the comparison"
    assert undecorate("US30", suffix="p") == "US30", "undecorated indices stay as they are"
    assert undecorate("mGBPUSD.a", prefix="m", suffix=".a") == "GBPUSD"
    assert undecorate("p", suffix="p") == "p", "never stripped to nothing"
    assert undecorate("EURUSDp") == "EURUSDp"


def test_a_stripped_master_name_takes_the_slaves_own_decoration() -> None:
    plain = undecorate("EURUSDp", suffix="p")
    got = resolve(plain, ["EURUSD.r", "GBPUSD.r"], suffix=".r")
    assert got.name == "EURUSD.r"


# --- reading a broker's decoration off its own catalog ----------------------------------------


def test_a_dominant_suffix_is_inferred_from_the_catalog() -> None:
    names = ["EURUSDp", "GBPUSDp", "USDJPYp", "XAUUSDp", "US30"]
    assert infer_decoration(names, DEFAULT.base_of) == ("", "P")


def test_a_dominant_prefix_is_inferred_too() -> None:
    names = ["mEURUSD", "mGBPUSD", "mUSDJPY", "mXAUUSD"]
    assert infer_decoration(names, DEFAULT.base_of) == ("M", "")


def test_a_dotted_suffix_keeps_the_brokers_own_spelling() -> None:
    names = ["EURUSD.r", "GBPUSD.r", "USDJPY.r", "AUDUSD.r"]
    assert infer_decoration(names, DEFAULT.base_of) == ("", ".R")


def test_bare_names_winning_is_not_an_inference() -> None:
    names = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US30", "EURUSDp"]
    assert infer_decoration(names, DEFAULT.base_of) == ("", "")


def test_a_split_vote_infers_nothing() -> None:
    """Bare beside decorated: which p-name is which product is not this function's call."""
    names = ["EURUSD", "EURUSDp", "GBPUSD", "GBPUSDp", "USDJPY", "USDJPYp"]
    assert infer_decoration(names, DEFAULT.base_of) == ("", "")


def test_too_few_recognizable_names_are_not_believed() -> None:
    assert infer_decoration(["EURUSDp", "GBPUSDp", "USDJPYp"], DEFAULT.base_of) == ("", "")
    assert infer_decoration(["AAPL", "TSLA", "NVDA", "AMZN"], DEFAULT.base_of) == ("", "")


# --- the configured decoration, honored on the side the broker's names carry it ---------------

FX_NAMES = ["EURUSD.FX", "GBPUSD.FX", "USDJPY.FX", "GBPCAD.FX"]


def test_a_misplaced_suffix_is_moved_to_the_suffix_side() -> None:
    assert asserted_decoration(".fx", "", FX_NAMES) == ("", ".fx")


def test_a_correctly_placed_decoration_stays_where_it_is() -> None:
    assert asserted_decoration("", ".fx", FX_NAMES) == ("", ".fx")
    assert asserted_decoration("m", "", ["mEURUSD", "mGBPUSD", "mUSDJPY"]) == ("m", "")


def test_a_decoration_no_name_carries_is_dropped() -> None:
    assert asserted_decoration("", ".q", FX_NAMES) == ("", "")
    assert asserted_decoration(".q", ".r", FX_NAMES) == ("", "")


def test_counting_beats_an_accidental_one_letter_hit() -> None:
    """m typed into the prefix field: MSFT starts with it by accident, but the many names
    ending in m are the decoration."""
    names = ["EURUSDm", "GBPUSDm", "USDJPYm", "XAUUSDm", "MSFT"]
    assert asserted_decoration("m", "", names) == ("", "m")


def test_without_a_symbol_list_the_configuration_is_taken_at_its_word() -> None:
    assert asserted_decoration(".fx", "", []) == (".fx", "")
    assert asserted_decoration("", "p", []) == ("", "p")


def test_both_values_landing_on_one_side_keep_the_one_configured_for_it() -> None:
    assert asserted_decoration(".fx", ".FX", FX_NAMES) == ("", ".FX")
