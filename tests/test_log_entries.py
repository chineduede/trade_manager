"""The new UI's log page: copier.log and the day book as one filtered list."""

from __future__ import annotations

from datetime import datetime

from mt5copier.day_book import MasterEvent, Outcome, SlaveOutcome
from mt5copier.log_entries import (
    Level,
    LogEntry,
    Tone,
    event_entries,
    file_entries,
    merge,
    outcome_text,
    parse_log,
    search,
    search_words,
    select,
    terminal_names,
)
from mt5copier.protocol import ActionKind

TID_M = "0E0471AA" + "0" * 24
TID_1 = "26C8E26B" + "0" * 24

NAMES = terminal_names({TID_M: ("IC Markets Global",), TID_1: ("BlackBull Markets",)})

LOG = """\
2026-10-03 15:36:20 INFO    [MainProcess] mt5copier.engine: spawned worker pid=1 for BlackBull Markets
2026-10-03 15:36:21 INFO    [MainProcess] mt5copier.engine: [live] BlackBull Markets: OPEN         #11 BTCUSD buy  @84767.3
2026-10-03 15:36:21 INFO    [MainProcess] mt5copier.engine: -> BlackBull Markets open #11 (cmd 4)
2026-10-03 15:36:22 INFO    [MainProcess] mt5copier.engine: OPEN         #11 BTCUSD filled 2.74 @84768
2026-10-03 15:36:22 ERROR   [MainProcess] mt5copier.reconcile: open for #11 on 26C8E26B failed, and will not be retried
2026-10-03 15:36:23 WARNING [MainProcess] mt5copier.config: config could not be read
  Extra inputs are not permitted
2026-10-03 15:36:24 INFO    [MainProcess] mt5copier.engine: stopping worker for 0E0471AA
"""  # noqa: E501


def ts(clock: str) -> float:
    return datetime.strptime(f"2026-10-03 {clock}", "%Y-%m-%d %H:%M:%S").timestamp()


def test_parse_keeps_continuation_lines_with_their_record() -> None:
    records = parse_log(LOG)
    assert len(records) == 7
    _, level, msg, _ = records[5]
    assert level is Level.WARN
    assert msg.endswith("Extra inputs are not permitted")


def test_parse_marks_the_engines_copying_lines() -> None:
    copying = [c for _, _, _, c in parse_log(LOG)]
    assert copying == [False, True, True, True, False, False, False]


def test_lines_are_put_down_to_the_terminal_they_name() -> None:
    entries = file_entries(LOG, NAMES, covered_from=None)
    assert entries[0].terminal_id == TID_1
    assert entries[4].terminal_id == TID_1, "by the first eight characters of its id"
    assert entries[5].terminal_id is None
    assert entries[6].terminal_id == TID_M


def test_the_longest_name_wins() -> None:
    names = terminal_names({TID_M: ("IC Markets Global",), TID_1: ("IC Markets Global #2",)})
    [entry] = file_entries(
        "2026-10-03 15:00:00 INFO    [MainProcess] x.engine: spawned worker for "
        "IC Markets Global #2\n",
        names,
        covered_from=None,
    )
    assert entry.terminal_id == TID_1


def test_copying_lines_the_day_book_covers_are_left_out() -> None:
    kept = file_entries(LOG, NAMES, covered_from=ts("15:36:21"))
    assert [e.text.split()[0] for e in kept] == ["spawned", "open", "config", "stopping"]


def test_copying_lines_from_before_the_day_book_are_kept() -> None:
    assert len(file_entries(LOG, NAMES, covered_from=ts("23:00:00"))) == 7


def event() -> MasterEvent:
    return MasterEvent(
        id=1,
        ts=ts("15:41:07"),
        kind=ActionKind.OPEN,
        preexisting=False,
        master_ticket=11,
        symbol="BTCUSD",
        label="buy",
        volume=0.78,
        volume_before=None,
        entry=84854.0,
        sl=84532.7,
        tp=85165.81,
        outcomes=(
            SlaveOutcome(
                TID_1,
                Outcome.DONE,
                lots=3.11,
                latency_ms=360,
                slippage_points=1175.2,
                ts=ts("15:41:08"),
            ),
            SlaveOutcome("X" * 32, Outcome.REFUSED, reason="account is not a demo"),
        ),
    )


def test_day_book_entries_read_as_the_design_has_them() -> None:
    master, filled, refused = event_entries([event()], TID_M)
    assert master.terminal_id == TID_M
    assert master.tone is Tone.MASTER
    assert master.text == "Opened BTCUSD buy 0.78 at 84854, stop 84532.7, target 85165.81"
    assert filled.text == ("Opened 3.11 lots in 360 ms, filled 1,175 points worse than the master")
    assert filled.ts == ts("15:41:08"), "at the time it was settled"
    assert refused.level is Level.ERROR
    assert refused.ts == master.ts, "still settling: shown at the master's time"


def test_a_fill_better_than_the_master_says_nothing_about_slippage() -> None:
    out = SlaveOutcome(TID_1, Outcome.DONE, lots=1.0, latency_ms=10, slippage_points=-40)
    assert outcome_text(ActionKind.OPEN, out) == "opened 1 lots in 10 ms"


def test_merge_is_in_time_order() -> None:
    a = LogEntry(2.0, Level.INFO, None, "b")
    b = LogEntry(1.0, Level.INFO, None, "a")
    assert [e.text for e in merge([a], [b])] == ["a", "b"]


def test_filters() -> None:
    entries = [
        LogEntry(1.0, Level.INFO, TID_1, "fine"),
        LogEntry(2.0, Level.ERROR, TID_1, "bad"),
        LogEntry(3.0, Level.WARN, None, "hmm"),
    ]
    assert [e.text for e in select(entries, problems_only=True, terminal_id=None)] == [
        "bad",
        "hmm",
    ]
    assert [e.text for e in select(entries, problems_only=False, terminal_id=TID_1)] == [
        "fine",
        "bad",
    ]


def test_search_wants_every_word_in_any_case_and_order() -> None:
    a = LogEntry(1.0, Level.INFO, "s1", "Opened 3.11 lots in 360 ms")
    b = LogEntry(2.0, Level.ERROR, "s2", "Refused: account is not a demo")
    c = LogEntry(3.0, Level.WARN, None, "Something odd")
    names = {"s1": "Slave 1 (BlackBull)", "s2": "Slave 2 (VT Markets)"}

    def said(e: LogEntry) -> str:
        return names.get(e.terminal_id or "", "copier")

    entries = [a, b, c]
    assert search_words("  LOTS  opened ") == ["lots", "opened"]
    assert search(entries, search_words("LOTS opened"), said) == [a]
    assert search(entries, search_words("opened refused"), said) == []
    assert search(entries, search_words("vt markets"), said) == [b], "the terminal's name"
    assert search(entries, search_words("error"), said) == [b], "the level"
    assert search(entries, search_words("copier odd"), said) == [c]
    assert search(entries, [], said) == entries
