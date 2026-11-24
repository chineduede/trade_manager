"""The app's own changelog: what each release changed, in the user's words.

The one source the What's new modal reads, compiled into the exe with everything else.
``release.ps1`` refuses to cut a version whose notes are not the top entry here, so the words
are written while the work is fresh, never reconstructed at the next release.

The seen-state lives install-wide, not per profile: "has this person read what changed" is a
fact about the installation, and telling them once is enough however many profiles run.
"""

from __future__ import annotations

import json

from . import __version__
from .json_store import write_json
from .paths import version_seen_path

NOTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "0.1.8",
        "2026-11-24",
        (
            "A direct ticket's target stays commission-padded for the whole trade. Moving"
            " the stop — to breakeven especially — re-sent the ticket's raw target and"
            " quietly rubbed the pad back off, so a 1:2 that ran to its target paid out"
            " short of 2R. The pad is now measured from the risk the trade opened with,"
            " wherever the stop sits since.",
            "A margin rule no longer refuses a trade because its symbol's very first quote"
            " was a beat late. The guard waits the quote out the same way the open itself"
            " already did, instead of declining what it could not yet measure.",
        ),
    ),
    (
        "0.1.7",
        "2026-11-23",
        (
            "Prop accounts: a named chain for each challenge. When the firm closes a stage"
            " and hands you a new login, link it on the new Prop accounts page and it"
            " inherits the chain's guard rules and initial balance on the spot — the same"
            " rules, not a copy, so editing them once covers every stage. Breaches, day"
            " anchors and trade counts stay each login's own, and old stages are kept in"
            " the chain for the record.",
            "Linking can also carry the previous stage's terminal settings onto the new"
            " stage's terminal, when they sit on different ones.",
            "The counting rules say their limits as counts — “5 trades a day”, “3 open at"
            " once” — no longer dressed up as money.",
            "Templates can be replaced and deleted: saving under a name already taken asks,"
            " then overwrites, and the Templates window gained a Delete under its Apply list."
            " A deleted template goes for every account and profile; rules an account took"
            " from it stay.",
        ),
    ),
    (
        "0.1.6",
        "2026-11-22",
        (
            "Two guard rules against overtrading: Max trades a day caps what the copier"
            " places between one midnight and the next on your computer's clock, and Max"
            " open trades caps how many are open at once. Each says whether pending orders"
            " count, and each can block new trades or only warn.",
            "Adding a guard rule got a search box: type a word from a rule's name or what"
            " it does, and Enter takes the first match.",
        ),
    ),
    (
        "0.1.5",
        "2026-11-19",
        (
            "A trade's row keeps two buttons however many actions exist: Close, and a ⋯ menu"
            " holding the rest — BE, TP BE, Partial, Modify — each with a line saying what"
            " it does. An armed breakeven keeps its blue BE on the ⋯ button.",
            "Close and Cancel ask first, like every other action already did — a misclick"
            " on the red button can no longer take a trade and its copies out.",
            "Hover hints wear the app's own skin everywhere, instead of the system's plain"
            " yellow box.",
        ),
    ),
    (
        "0.1.4",
        "2026-11-19",
        (
            "Breakeven can wait for a price: the BE button takes one, wears it while armed,"
            " and moves the stop to the entry only when the market touches that price —"
            " every copy follows on the next tick.",
            "The Copies cell opens a menu: each copy named with its state, the live ones"
            " linking to the ticket they hold on their terminal's page — and a copy's"
            " “Copy of #…” there leads back to its row on the Trading page.",
            "The installed app's window can no longer go blank mid-run, and a development"
            " run can no longer sit on the installed app's port.",
            "A TP BE button beside BE, for a trade that has lost its reason: the target"
            " moves to the entry plus the copies' costs, so a price that comes back to"
            " your entry takes the trade out at nothing lost.",
        ),
    ),
    (
        "0.1.3",
        "2026-11-17",
        (
            "The app says what changed: this window, behind the version label at the bottom"
            " right and on Help — opened once by itself after an update, with a dot on the"
            " label until it has been read.",
            "Setups work in direct-trading profiles too: the slaves, their accounts and"
            " settings save under a name, with the order ticket as the master. A setup loads"
            " into a profile of the mode it was saved in.",
        ),
    ),
    (
        "0.1.2",
        "2026-11-17",
        (
            "Lot caps learn asset classes: a symbol catalog on Settings says what each symbol"
            " is — forex, an index, a metal, crypto — and a new guard rule caps lots per class.",
            "Direct trading enters symbols clean of the feed broker's decoration, so the"
            " catalog, the guards and the slaves all see the plain name.",
            "The guard rule form points the way to the catalog when a symbol is not yet"
            " classed, and the detour remembers the way back.",
        ),
    ),
    (
        "0.1.1",
        "2026-11-16",
        (
            "The installer brings WebView2 along where Windows is missing it, so the app"
            " opens on a machine that never had it.",
            "The app got its icon: the master candle and its green copy.",
            "Help says which build is running.",
            "Development and the installed app now keep separate data — nothing a source run"
            " does can touch the data of the app you actually use.",
        ),
    ),
    (
        "0.1.0",
        "2026-11-16",
        (
            "The first installed release. The desktop window is the whole app — the old"
            " terminal UI is gone.",
            "Everything the copier does, in pages: Terminals, Trading, Guards, News, History,"
            " Setups, Settings — with profiles, the log dock and the status bar always at hand.",
        ),
    ),
)
"""Newest first: (version, date, what changed). The modal shows these words verbatim."""


def unseen() -> bool:
    """Whether this build's notes still wait to be read.

    A fresh install has nothing to announce — the file is primed to the running version, so
    only a later *update* raises the modal. Any other mismatch, a downgrade included, counts
    as unseen: the person has not read what this build says about itself.
    """
    try:
        last = json.loads(version_seen_path().read_text(encoding="utf-8")).get("last")
    except (OSError, ValueError):
        last = None
    if last is None:
        mark_seen()
        return False
    return bool(last != __version__)


def mark_seen() -> None:
    """Record that the running version's notes were shown and dismissed. Atomically, like
    every other JSON file: a half-written file reads as a fresh install, which would mark
    this build's notes read without them ever being shown."""
    write_json(version_seen_path(), {"last": __version__})
