"""Filling-mode and retcode tests.

Both are pure lookups, and both are places the obvious implementation is wrong in a way that
only shows up against a real broker -- so they are worth pinning down here.
"""

from __future__ import annotations

from mt5copier.execution import (
    OrderFilling,
    Outcome,
    Retcode,
    SymbolFilling,
    alternative_filling,
    choose_filling,
    classify,
    describe_retcode,
)

FOK_ONLY = SymbolFilling.FOK
IOC_ONLY = SymbolFilling.IOC
BOTH = SymbolFilling.FOK | SymbolFilling.IOC


def test_the_mask_and_the_request_enum_are_different_numbers() -> None:
    """The easy conflation: SYMBOL_FILLING_FOK is 1 but ORDER_FILLING_FOK is 0. Sending the
    mask value as the mode asks for IOC when you meant FOK."""
    assert SymbolFilling.FOK == 1 and OrderFilling.FOK == 0
    assert SymbolFilling.IOC == 2 and OrderFilling.IOC == 1


def test_fok_is_preferred_when_permitted() -> None:
    """All-or-nothing keeps the copy's size equal to the size that was computed."""
    assert choose_filling(BOTH, is_pending=False) is OrderFilling.FOK
    assert choose_filling(FOK_ONLY, is_pending=False) is OrderFilling.FOK


def test_ioc_is_used_when_fok_is_not_permitted() -> None:
    """The case zele-mt5 gets right by accident and the FOK-only case it gets wrong."""
    assert choose_filling(IOC_ONLY, is_pending=False) is OrderFilling.IOC


def test_a_fok_only_symbol_never_gets_an_ioc_request() -> None:
    """Hardcoding IOC -- what zele-mt5 does in all four order paths -- rejects every order on
    these symbols with INVALID_FILL."""
    assert choose_filling(FOK_ONLY, is_pending=False) is not OrderFilling.IOC


def test_pending_orders_use_return_regardless_of_the_mask() -> None:
    """The mask describes market execution; a resting order is not executed at placement."""
    for mask in (0, FOK_ONLY, IOC_ONLY, BOTH):
        assert choose_filling(mask, is_pending=True) is OrderFilling.RETURN


def test_an_empty_mask_guesses_ioc_and_stays_recoverable() -> None:
    assert choose_filling(0, is_pending=False) is OrderFilling.IOC
    assert alternative_filling(OrderFilling.IOC, 0) is None


def test_the_alternative_after_invalid_fill_is_the_other_permitted_mode() -> None:
    assert alternative_filling(OrderFilling.FOK, BOTH) is OrderFilling.IOC
    assert alternative_filling(OrderFilling.IOC, BOTH) is OrderFilling.FOK


def test_no_alternative_is_offered_that_the_mask_forbids() -> None:
    """Otherwise a rejection loop bounces between two modes the symbol refuses equally."""
    assert alternative_filling(OrderFilling.FOK, FOK_ONLY) is None
    assert alternative_filling(OrderFilling.IOC, IOC_ONLY) is None


# --- retcodes -------------------------------------------------------------------------------


def test_success_codes() -> None:
    assert classify(Retcode.DONE) is Outcome.SUCCEEDED
    assert classify(Retcode.PLACED) is Outcome.SUCCEEDED, "pending orders report PLACED"


def test_a_partial_fill_is_its_own_outcome() -> None:
    """The volume actually filled has to be recorded, never the volume requested."""
    assert classify(Retcode.DONE_PARTIAL) is Outcome.PARTIAL


def test_price_problems_are_repriced_not_abandoned() -> None:
    for code in (Retcode.REQUOTE, Retcode.PRICE_CHANGED, Retcode.PRICE_OFF):
        assert classify(code) is Outcome.REPRICE


def test_invalid_fill_asks_for_the_other_filling_mode() -> None:
    assert classify(Retcode.INVALID_FILL) is Outcome.REFILL


def test_permanently_wrong_requests_are_fatal() -> None:
    """Retrying changes nothing and only delays the report."""
    for code in (
        Retcode.NO_MONEY,
        Retcode.INVALID_VOLUME,
        Retcode.INVALID_STOPS,
        Retcode.TRADE_DISABLED,
        Retcode.MARKET_CLOSED,
    ):
        assert classify(code) is Outcome.FATAL


def test_connection_problems_are_left_to_the_reconnect_loop() -> None:
    assert classify(Retcode.CONNECTION) is Outcome.CONNECTION
    assert classify(Retcode.TIMEOUT) is Outcome.CONNECTION


def test_a_none_result_is_a_connection_problem_not_a_rejection() -> None:
    """order_send returns None when the call itself failed, not the trade."""
    assert classify(None) is Outcome.CONNECTION


def test_a_busy_server_backs_off() -> None:
    assert classify(Retcode.TOO_MANY_REQUESTS) is Outcome.BACKOFF


def test_an_unrecognised_code_is_fatal_not_retryable() -> None:
    """A copy still retrying after seconds is no longer the trade the master took, so the
    bias is toward abandoning and surfacing."""
    assert classify(99999) is Outcome.FATAL


def test_describe_names_known_codes_and_survives_unknown_ones() -> None:
    assert "NO_MONEY" in describe_retcode(Retcode.NO_MONEY)
    assert "10019" in describe_retcode(Retcode.NO_MONEY)
    assert "99999" in describe_retcode(99999)
    assert "call failed" in describe_retcode(None)
