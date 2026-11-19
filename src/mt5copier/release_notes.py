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
from .paths import version_seen_path

NOTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
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
    """Record that the running version's notes were shown and dismissed."""
    path = version_seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last": __version__}), encoding="utf-8")
