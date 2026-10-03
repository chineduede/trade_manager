"""The new UI's log page: ``copier.log`` and the day book, merged into one list of entries.

The file says everything, in the words a developer wrote; the day book says the part that
matters most -- what each slave made of each master trade -- in the words a person would use.
So the page takes the copying lines from the day book and everything else from the file, and
drops the file's own copying lines for the stretch the day book covers. Before that stretch
(an earlier run of the app, which the day book does not remember) the file's lines are kept.

Each entry is put down to a terminal where the line names one, so the page can be filtered to
a single terminal. Pure: no Textual, no engine.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .day_book import MasterEvent, Outcome, SlaveOutcome, describe_master_full, describe_outcome
from .protocol import ActionKind


class Level(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    @property
    def problem(self) -> bool:
        return self in (Level.WARN, Level.ERROR)


class Tone(StrEnum):
    """How an entry's text is coloured: what it says about the copier, beyond its level."""

    PLAIN = "plain"
    MASTER = "master"
    """What the master did: the heading each slave's line answers."""
    DONE = "done"
    QUIET = "quiet"
    """Deliberately nothing to do."""
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LogEntry:
    ts: float
    level: Level
    terminal_id: str | None
    """The terminal the entry is about, or None for the copier as a whole."""
    text: str
    tone: Tone = Tone.PLAIN


_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<level>[A-Z]+)\s+\[[^\]]*\] "
    r"(?P<logger>[\w.]+): (?P<msg>.*)$"
)

_LEVELS = {
    "DEBUG": Level.DEBUG,
    "INFO": Level.INFO,
    "WARNING": Level.WARN,
    "ERROR": Level.ERROR,
    "CRITICAL": Level.ERROR,
}

_KINDS = "|".join(k.value.upper() for k in ActionKind)
_COPYING_LINE = re.compile(
    r"^(?:\[(?:live|dry-run)\] .+: (?:" + _KINDS + r")\s"  # an intended action
    r"|-> .+ (?:" + "|".join(k.value for k in ActionKind) + r") #\d+ \(cmd \d+\)$"  # sent
    r"|(?:" + _KINDS + r")\s+#\d+ )"  # the broker's answer
)
"""The engine's own lines for what the day book records: each action as derived, each command
as sent, each result as answered."""


def parse_log(text: str) -> list[tuple[float, Level, str, bool]]:
    """Each record in a ``copier.log`` excerpt as (time, level, message, copying).

    A line that does not start a record -- a traceback, a validation error's detail -- is
    added to the record before it, so a filter never shows half of one. ``copying`` marks the
    engine's lines that the day book says better.
    """
    out: list[tuple[float, Level, str, bool]] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m is None:
            if out and line.strip():
                ts, level, msg, copying = out[-1]
                out[-1] = (ts, level, f"{msg}\n{line.rstrip()}", copying)
            continue
        try:
            ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        level = _LEVELS.get(m["level"], Level.INFO)
        msg = m["msg"]
        copying = m["logger"].endswith(".engine") and _COPYING_LINE.match(msg) is not None
        out.append((ts, level, msg, copying))
    return out


def attribute(message: str, names: Sequence[tuple[str, str]]) -> str | None:
    """The terminal a message is about, from the names it uses. ``names`` is (name, terminal
    id) pairs, longest name first, so ``IC Markets Global #2`` wins over ``IC Markets Global``.
    The engine names a terminal by its label, or by the first eight characters of its id."""
    for name, tid in names:
        if name and name in message:
            return tid
    return None


def terminal_names(labels: Mapping[str, Iterable[str]]) -> list[tuple[str, str]]:
    """``labels`` is each terminal id with the names it may go by. The id's own first eight
    characters are always one of them."""
    pairs: list[tuple[str, str]] = []
    for tid, names in labels.items():
        pairs += [(n, tid) for n in {*names, tid[:8]} if n]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def file_entries(
    text: str,
    names: Sequence[tuple[str, str]],
    *,
    covered_from: float | None,
) -> list[LogEntry]:
    """``copier.log`` as entries. Copying lines from ``covered_from`` on are left out: the day
    book has them, said better. None keeps them all."""
    out: list[LogEntry] = []
    for ts, level, msg, copying in parse_log(text):
        if copying and covered_from is not None and ts >= covered_from:
            continue
        tone = {Level.WARN: Tone.WARN, Level.ERROR: Tone.ERROR}.get(level, Tone.PLAIN)
        out.append(LogEntry(ts, level, attribute(msg, names), msg, tone))
    return out


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def outcome_level(outcome: Outcome) -> Level:
    if outcome in (Outcome.REFUSED, Outcome.FAILED, Outcome.NO_ANSWER):
        return Level.ERROR
    if outcome.problem:
        return Level.WARN
    return Level.INFO


def _outcome_tone(outcome: Outcome) -> Tone:
    if outcome is Outcome.DONE:
        return Tone.DONE
    level = outcome_level(outcome)
    if level is Level.ERROR:
        return Tone.ERROR
    if level is Level.WARN:
        return Tone.WARN
    return Tone.QUIET


def outcome_text(kind: ActionKind, outcome: SlaveOutcome) -> str:
    """:func:`describe_outcome`, plus how far a fill landed from the master's entry when it
    landed worse -- the one thing the day book's short line leaves out."""
    text = describe_outcome(kind, outcome)
    slip = outcome.slippage_points
    if slip is not None and round(slip) > 0:
        text += f", filled {round(slip):,} points worse than the master"
    return text


def event_entries(events: Iterable[MasterEvent], master_id: str | None) -> list[LogEntry]:
    """The day book as entries: the master's line, then one per slave, each at the time it
    was settled. A slave still waiting for its answer is shown at the master's time."""
    out: list[LogEntry] = []
    for event in events:
        out.append(
            LogEntry(
                event.ts,
                Level.INFO,
                master_id,
                _sentence(describe_master_full(event)),
                Tone.QUIET if event.preexisting else Tone.MASTER,
            )
        )
        for o in event.outcomes:
            out.append(
                LogEntry(
                    o.ts if o.ts is not None else event.ts,
                    outcome_level(o.outcome),
                    o.terminal_id,
                    _sentence(outcome_text(event.kind, o)),
                    _outcome_tone(o.outcome),
                )
            )
    return out


def merge(file: Sequence[LogEntry], events: Sequence[LogEntry]) -> list[LogEntry]:
    """Both, oldest first. Stable, so a master's line stays above its slaves' when they share
    a time, and the file's second-resolution times sort before the day book's in that second."""
    return sorted([*file, *events], key=lambda e: e.ts)


def select(
    entries: Sequence[LogEntry], *, problems_only: bool, terminal_id: str | None
) -> list[LogEntry]:
    """The entries the page's two filters let through."""
    return [
        e
        for e in entries
        if (not problems_only or e.level.problem)
        and (terminal_id is None or e.terminal_id == terminal_id)
    ]
