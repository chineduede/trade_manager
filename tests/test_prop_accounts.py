"""Prop account containers: the store, and the rules resolving through them.

What the feature promises: a linked account reads and writes its guard *rules* under its
container's identity, so a new stage is held to the same rules object as the old one --
while everything stateful (latches, day anchors, trade counts) stays the raw account's
own. The store half is exercised directly; the resolution half through a GuardBook with
two stages linked into one chain.
"""

from __future__ import annotations

import json
from pathlib import Path

from mt5copier.guards import AccountGuards, GuardBook, GuardRule, GuardsDoc
from mt5copier.paths import prop_accounts_path
from mt5copier.prop_accounts import PROP_PREFIX, PropStore, check_name
from mt5copier.protocol import Snapshot, WorkerState

STAGE1 = "1001@Prop-Demo"
STAGE2 = "2002@Prop-Live"
LOOSE = "3003@Elsewhere"


def store() -> PropStore:
    return PropStore()  # the suite isolates APPDATA, so the default path is a tmp dir


# --- the store ---------------------------------------------------------------------------------


def test_create_link_and_resolve() -> None:
    s = store()
    cid, why = s.create("FTMO 100k")
    assert why is None and cid
    assert s.link(cid, STAGE1) is None
    doc = s.current()
    assert doc.rules_key(STAGE1) == PROP_PREFIX + cid
    assert doc.rules_key(LOOSE) == LOOSE
    assert doc.rules_key(None) is None
    assert doc.containers[cid].current == STAGE1


def test_linking_again_appends_and_moves_current() -> None:
    s = store()
    cid, _ = s.create("Chain")
    assert cid is not None
    s.link(cid, STAGE1)
    assert s.link(cid, STAGE2) is None
    c = s.current().containers[cid]
    assert c.stages == (STAGE1, STAGE2)
    assert c.current == STAGE2
    # Both stages resolve to the one identity: the old stage is kept, not forgotten.
    assert s.current().rules_key(STAGE1) == s.current().rules_key(STAGE2)


def test_one_account_lives_in_one_chain() -> None:
    s = store()
    a, _ = s.create("A")
    b, _ = s.create("B")
    assert a is not None and b is not None
    s.link(a, STAGE1)
    why = s.link(b, STAGE1)
    assert why is not None and "'A'" in why
    why = s.link(a, STAGE1)
    assert why is not None and "stage 1" in why


def test_link_refuses_non_accounts() -> None:
    s = store()
    cid, _ = s.create("A")
    assert cid is not None
    assert s.link(cid, "") is not None
    assert s.link(cid, PROP_PREFIX + "deadbeef") is not None
    assert s.link("nope", STAGE1) == "that prop account is gone"


def test_unlink_falls_back_to_the_raw_key() -> None:
    s = store()
    cid, _ = s.create("A")
    assert cid is not None
    s.link(cid, STAGE1)
    s.link(cid, STAGE2)
    assert s.unlink(cid, STAGE1) is None
    doc = s.current()
    assert doc.rules_key(STAGE1) == STAGE1
    assert doc.containers[cid].stages == (STAGE2,)
    assert s.unlink(cid, STAGE1) == "that account is not in this chain"


def test_delete_releases_every_stage() -> None:
    s = store()
    cid, _ = s.create("A")
    assert cid is not None
    s.link(cid, STAGE1)
    assert s.delete(cid) is None
    assert s.current().containers == {}
    assert s.current().rules_key(STAGE1) == STAGE1
    assert s.delete(cid) == "that prop account is gone"


def test_names_are_bounded_and_unique() -> None:
    s = store()
    _, why = s.create("  ")
    assert why == "the name is empty"
    _, why = s.create("x" * 33)
    assert why is not None and "32" in why
    cid, _ = s.create("FTMO 100k")
    assert cid is not None
    _, why = s.create("ftmo 100K")  # case-insensitive clash
    assert why is not None and "already exists" in why
    assert s.rename(cid, "FTMO 200k") is None
    assert s.current().containers[cid].name == "FTMO 200k"
    other, _ = s.create("Second")
    assert other is not None
    assert s.rename(other, "ftmo 200K") is not None
    assert s.rename(cid, "FTMO 200k") is None  # renaming to its own name is fine


def test_two_windows_fold_into_each_other() -> None:
    a, b = store(), store()
    cid, _ = a.create("Shared")
    assert cid is not None
    assert cid in b.current().containers  # b re-reads on mtime
    b.link(cid, STAGE1)
    assert a.current().containers[cid].current == STAGE1


def test_a_bad_file_reads_as_empty(tmp_path: Path) -> None:
    target = prop_accounts_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"containers": "what"}), encoding="utf-8")
    assert store().current().containers == {}
    target.write_text(
        json.dumps(
            {
                "containers": {
                    "good1234": {"name": "Kept", "stages": [STAGE1, "", 7, STAGE1]},
                    "bad": {"name": "", "stages": []},
                    "worse": "not a dict",
                }
            }
        ),
        encoding="utf-8",
    )
    doc = store().current()
    assert list(doc.containers) == ["good1234"]
    assert doc.containers["good1234"].stages == (STAGE1,)  # blanks and repeats dropped


def test_check_name_is_the_one_rulebook() -> None:
    s = store()
    cid, _ = s.create("Taken")
    assert cid is not None
    doc = s.current()
    assert check_name("Fresh", doc) is None
    assert check_name("taken", doc) is not None
    assert check_name("taken", doc, but=cid) is None


# --- rules resolving through the chain ----------------------------------------------------------


def snap() -> Snapshot:
    return Snapshot(
        terminal_id="T",
        state=WorkerState.CONNECTED,
        balance=100_000.0,
        equity=100_000.0,
        currency="USD",
    )


def chained_book(tmp: Path) -> tuple[GuardBook, str]:
    """A container holding both stages, with one daily-trades rule kept under the
    container's identity and a cap of one trade a day."""
    s = store()
    cid, _ = s.create("FTMO 100k")
    assert cid is not None
    s.link(cid, STAGE1)
    s.link(cid, STAGE2)
    rule = GuardRule(type="daily_trades", params={"limit": 1, "pendings": "counts"})
    doc = GuardsDoc(
        accounts={PROP_PREFIX + cid: AccountGuards(initial_balance=100_000.0, rules=(rule,))}
    )
    return GuardBook(doc, state_path=tmp / "state.json", props=s), rule.id


def test_every_stage_reads_the_containers_rules(tmp_path: Path) -> None:
    book, rule_id = chained_book(tmp_path)
    for key in (STAGE1, STAGE2):
        report = book.report(key, snap(), now=1000.0)
        assert [r.rule_id for r in report.rules] == [rule_id]
        assert report.initial_balance == 100_000.0
        assert book.title(key, rule_id) == "Max trades a day"
    # An account outside the chain reads nothing.
    assert book.report(LOOSE, snap(), now=1000.0).rules == ()


def test_guard_state_stays_each_stages_own(tmp_path: Path) -> None:
    """Stage 1 at its cap holds stage 1 alone: the rules are the chain's, the counts are
    not -- a closed stage's day cannot hold back the login that replaced it."""
    book, _ = chained_book(tmp_path)
    book.record_open(STAGE1, pending=False, now=1000.0)
    assert book.verdict(STAGE1, snap(), now=1000.0).held_back
    assert not book.verdict(STAGE2, snap(), now=1000.0).held_back


def test_without_a_store_nothing_resolves(tmp_path: Path) -> None:
    doc = GuardsDoc(accounts={STAGE1: AccountGuards(rules=(GuardRule(type="daily_trades"),))})
    book = GuardBook(doc, state_path=tmp_path / "state.json")
    assert len(book.report(STAGE1, snap(), now=1000.0).rules) == 1
