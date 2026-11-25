"""Cross-profile claims on terminal installs. Accounts are not claims any more: which
profile may use an account is persistent ownership (tests/test_ownership.py).

Like the single-instance lock's tests: ``msvcrt`` locks are per file handle, so two
:class:`ClaimBook`\\ s in this one process contend exactly as two real instances would.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5copier.claims import ClaimBook, ClaimRefusedError, claim_keys
from mt5copier.config import TerminalConfig, ctrader_server_name, ctrader_terminal_id


def test_a_terminal_claims_only_the_install_never_the_account() -> None:
    tcfg = TerminalConfig(terminal_id="abc123", last_login=555, last_server="Broker-Demo")
    assert claim_keys(tcfg) == ["terminal:abc123"]
    assert claim_keys(TerminalConfig(terminal_id="abc123")) == ["terminal:abc123"]


def test_a_ctrader_entry_claims_its_row_id_the_account_is_ownerships() -> None:
    tcfg = TerminalConfig(
        terminal_id=ctrader_terminal_id(42),
        ctrader_account_id=42,
        last_login=999,
        last_server=ctrader_server_name("IC Markets", False),
    )
    assert claim_keys(tcfg) == [f"terminal:{ctrader_terminal_id(42)}"]


def test_a_claim_held_by_one_book_is_refused_to_another(tmp_path: Path) -> None:
    ours = ClaimBook(tmp_path, "funded")
    theirs = ClaimBook(tmp_path, "scalp")
    ours.acquire(["terminal:abc"])
    try:
        with pytest.raises(ClaimRefusedError) as err:
            theirs.acquire(["terminal:abc"])
        assert err.value.owner_profile == "funded", "the refusal names who to blame"
        assert "funded" in str(err.value)
    finally:
        ours.release_all()
    theirs.acquire(["terminal:abc"])  # free once released
    theirs.release_all()


def test_a_refused_acquire_keeps_none_of_what_it_took(tmp_path: Path) -> None:
    """All or nothing: a half-claim would block the holder's own respawn for nothing."""
    ours = ClaimBook(tmp_path, "funded")
    theirs = ClaimBook(tmp_path, "scalp")
    ours.acquire(["terminal:held"])
    try:
        with pytest.raises(ClaimRefusedError):
            theirs.acquire(["terminal:fresh", "terminal:held"])
        # The terminal key it briefly took must be free again.
        ours.acquire(["terminal:fresh"])
    finally:
        ours.release_all()


def test_reacquiring_what_this_book_already_holds_is_fine(tmp_path: Path) -> None:
    """A worker restart re-claims its keys without ever having released them."""
    book = ClaimBook(tmp_path, "funded")
    book.acquire(["terminal:abc"])
    book.acquire(["terminal:abc"])  # must not raise
    book.release_all()


def test_releasing_what_was_never_held_is_a_no_op(tmp_path: Path) -> None:
    ClaimBook(tmp_path, "funded").release(["terminal:never"])


def test_two_distinct_keys_never_share_a_lock_file(tmp_path: Path) -> None:
    book = ClaimBook(tmp_path, "funded")
    other = ClaimBook(tmp_path, "scalp")
    book.acquire(["terminal:abc"])
    try:
        other.acquire(["terminal:abd"])  # similar name, different key: no contention
    finally:
        book.release_all()
        other.release_all()
