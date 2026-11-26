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
        "0.1.10",
        "2026-11-25",
        (
            "A symbol prefix or suffix typed into the wrong field no longer breaks direct"
            " trading. The value is checked against the broker's own symbol list and honored"
            " on the side its names actually carry it — “.fx” entered as a prefix still"
            " strips GBPCAD.FX down to GBPCAD and still finds the broker's .FX name when"
            " placing there. A value no name carries at all is set aside (the log says so),"
            " and the automatic catalog reading takes over.",
            "Terminals can be hidden. The eye icon on a card disconnects the terminal"
            " entirely — no copies, never the direct-trading price feed, and the terminal"
            " and its account are freed for other profiles — while its role and every"
            " setting are kept. A"
            " hidden terminal leaves the dashboard completely: its one home is the Hidden"
            " button in the DETECTED zone's header, which carries the count, lists each"
            " one with an Unhide, and only shows while something is hidden. Hiding a"
            " slave that holds open copies asks first, exactly as unassigning does,"
            " because those copies stop being managed. The master cannot be hidden —"
            " unassign it instead.",
            "A card's everyday actions are icons now, not a right-click menu. Hover a"
            " card (or tab to it) and the corner's state word gives way to pause, hide"
            " and unassign — on a detected terminal, log in and hide. The rare actions"
            " (rename the account, reconnect, restart the worker, allow live trading,"
            " acknowledge orphans) sit under the ··· icon, and right-click still opens"
            " that same menu.",
            "The Max open trades guard can now look past risk-free trades. A new option,"
            " off by default, stops counting a trade once its stop loss sits at or past"
            " its entry — the cap is there to limit risk, and a trade that can no longer"
            " lose has none left to limit — so moving a stop to breakeven frees that"
            " place for another trade. A trade with no stop is always counted, and the"
            " rule's readout says how many risk-free trades it is looking past.",
            "The Max trades a day guard now counts a pending order's fill. A resting order"
            " set to not count at placement moves into the day's count the moment it fills"
            " — on the day it fills — so a trade entered through a limit order no longer"
            " slips past the daily cap. A rule set to count pendings at placement is"
            " unchanged: the fill moves the count, it never adds a second one.",
            "The Setups page is gone. Profiles already keep an arrangement — every"
            " terminal's role, account and settings — so saved setups were a second copy"
            " of the same thing. A setups.json left on disk is no longer read; make a"
            " profile per arrangement instead.",
            "Risk settings now follow the account, not the terminal — in every profile."
            " Switching a terminal to another saved account banks the settings tuned on"
            " the account it leaves — sizing, breakeven, no-stop policy, filters and the"
            " rest of the trading policy — and switching back restores them, so nothing"
            " has to be remembered by hand. The memory is shared by all profiles, live:"
            " saving a terminal's settings banks them for its account on the spot, any"
            " other open window holding that account folds the change in within a moment,"
            " and a brand new profile finds every known account already tuned. The same"
            " account is never tuned two ways in two windows. The toast says which"
            " happened; a first login to an account the copier has no memory of keeps the"
            " current settings and says they came along.",
            "The memory also banks when a switch made in MT5 itself is noticed, so what"
            " an account ran with is never lost. The broker's symbol decoration stays the"
            " terminal's own, and allowing live trading still has to be re-confirmed"
            " after every switch — that consent is never brought back by machinery.",
            "The data every window shares — account names, guard rules and templates, the"
            " guards' memory, the prop account chains, and now each account's remembered"
            " settings — lives in one small database file (shared\\shared.db) instead of"
            " four JSON files. Two windows saving in the same instant can no longer catch"
            " each other mid-write, and a change that touches two of these stores lands"
            " whole or not at all.",
            "The move is automatic and one-time: the first start gathers what the JSON"
            " files hold, and the files stay on disk untouched as a fallback copy.",
            "One rule now governs accounts and terminals across profiles: the profile"
            " using them owns them, and owns them even while its window is closed."
            " Assigning a terminal takes the terminal itself and the account it holds;"
            " unassigning or hiding it releases both. Everywhere else the account is"
            " read-only — its settings, guard rules, name and prop link can be seen but"
            " not changed, and no terminal there can be assigned to it or logged into it"
            " — and the assigned terminal is out of reach entirely: another profile"
            " cannot assign it, nor even switch it to a different account, because that"
            " would pull the terminal out from under the profile running it. Its card"
            " says so at a glance — dimmed and dashed, with an ASSIGNED pill naming the"
            " profile — and a drag that could only be refused says so the moment it"
            " starts instead of at the drop.",
            "An account owned by a profile whose terminal is not assigned right now"
            " still shows an OWNED pill instead: that terminal may be logged into a"
            " different account and used here freely — only the account is theirs.",
            "If a closed profile sits on a terminal or account you need, its card's ···"
            " menu offers Take ownership, behind a confirmation that says exactly what"
            " it costs: nothing is interrupted now, but when that profile next opens it"
            " will not reconnect, and any copies it left stay open there, unmanaged."
            " The account picker offers the same way through: an account a closed"
            " profile owns is listed and says so, and picking it asks to take it over"
            " before logging in, instead of a flat refusal. While the owning profile's"
            " window is open, nothing is ever taken from it — the card and the picker"
            " both say so and point at that window, where the unassign, hide or switch"
            " belongs.",
            "Ownership also tidies itself now. Every few seconds each open profile"
            " re-checks that what it owns is what it actually uses, so an account row"
            " left behind — say the terminal was switched to another account in MT5"
            " itself and then unassigned — frees itself within a minute instead of"
            " locking other profiles out until someone notices.",
        ),
    ),
    (
        "0.1.9",
        "2026-11-24",
        (
            "A feed broker that decorates its symbol names — EURUSDp, EURUSD.r, mEURUSD —"
            " no longer needs its prefix or suffix typed into its settings for direct"
            " trading. The app reads the decoration off the broker's own symbol list, the"
            " ticket's search and anything typed into it land on the plain name, and every"
            " slave is sent EURUSD instead of erroring on a name its broker does not list."
            " A configured prefix or suffix still outranks the automatic reading, and a"
            " broker listing both spellings of a name is left alone rather than guessed at.",
            "The same reading covers a copied master: a master whose broker decorates its"
            " names now has them stripped for the slaves even when its prefix/suffix"
            " settings were never filled in.",
        ),
    ),
    (
        "0.1.8",
        "2026-11-24",
        (
            "A direct ticket's target stays commission-padded for the whole trade. The pad"
            " is worked out once, when the trade is first written, and every later stop"
            " move re-sends that exact target — moving the stop to breakeven used to"
            " re-send the ticket's raw target and quietly rub the pad back off, so a 1:2"
            " that ran to its target paid out short of 2R. And a target you move yourself"
            " is yours: it goes out exactly as written, never padded again.",
            "The pad is now also a per-terminal choice: “Keep direct targets' R:R net of"
            " costs” in a slave's settings, on by default. Off, the ticket's target goes"
            " out exactly as written and commission comes out of the win.",
            "A margin rule no longer refuses a trade because its symbol's very first quote"
            " was a beat late. The guard waits the quote out the same way the open itself"
            " already did, instead of declining what it could not yet measure.",
            "Choosing a symbol on the direct ticket now selects it on every connected MT5"
            " slave straight away, so by the time the trade is placed the symbol is already"
            " in Market Watch and ticking.",
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
