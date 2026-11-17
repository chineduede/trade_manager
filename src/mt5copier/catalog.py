"""The shared symbol catalog: which asset class a symbol belongs to.

Guard rules that treat forex differently from indices (a lot cap per class, and later the
no-hedging rule) need to say which class a symbol is in, and no heuristic baked into code can
be corrected when a broker's naming defeats it. So the catalog is data, shared by every
profile (``shared\\catalog.json``), edited as a general setting: a **currency list** -- any
pair of two known currencies is forex, crosses included -- and a **symbol list per other
class**, user-extendable. A symbol in no list falls into *everything else*, which a rule caps
on its own. Misfiling is visible by design: wherever a class decides something, the guard
note names it ("US30 -> indices"), so a wrong guess is seen, never silent.

Matching is forgiving about decoration: names are compared stripped of anything but letters
and digits, an entry found *inside* a longer name counts (``US30.cash`` is ``US30``), and the
longest listed entry wins. The engine still translates a slave's configured decoration off
before summing its open lots, because a rename (``symbol_map``) cannot be guessed from the
name at all.

The file, once written, is the whole truth -- removing a seeded symbol keeps it removed --
and an absent file means the seeds. Writes go through :func:`mt5copier.shared_data.
shared_lock` + :func:`~mt5copier.shared_data.write_json_atomic` like every shared store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import shared_data
from .logging_setup import get_logger
from .paths import catalog_path

log = get_logger(__name__)

CLASSES = ("forex", "indices", "metals", "energies", "crypto", "stocks", "other")
"""Every asset class a symbol can be in, in the order the UI shows them."""

LISTED = ("indices", "metals", "energies", "crypto", "stocks")
"""The classes held as symbol lists. Forex is derived from the currency list, and *other* is
what remains."""

CLASS_WORDS = {
    "forex": "Forex",
    "indices": "Indices",
    "metals": "Metals",
    "energies": "Energies",
    "crypto": "Crypto",
    "stocks": "Stocks",
    "other": "Everything else",
}

SEED_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD")
"""The majors. SGD and anything else a broker's pairs use are added by hand."""

SEED_LISTS: dict[str, tuple[str, ...]] = {
    "indices": (
        "US30",
        "US100",
        "NAS100",
        "USTEC",
        "US500",
        "SPX500",
        "SP500",
        "US2000",
        "UK100",
        "FTSE100",
        "GER40",
        "DE40",
        "GER30",
        "DE30",
        "DAX40",
        "EU50",
        "EUSTX50",
        "STOXX50",
        "FRA40",
        "F40",
        "ESP35",
        "SPA35",
        "IT40",
        "JP225",
        "JPN225",
        "NIKKEI225",
        "AUS200",
        "HK50",
        "CHINA50",
        "SWI20",
    ),
    "metals": (
        "XAUUSD",
        "GOLD",
        "XAGUSD",
        "SILVER",
        "XPTUSD",
        "PLATINUM",
        "XPDUSD",
        "PALLADIUM",
        "XAUEUR",
        "XAGEUR",
        "COPPER",
        "XCUUSD",
    ),
    "energies": (
        "USOIL",
        "WTI",
        "XTIUSD",
        "UKOIL",
        "BRENT",
        "XBRUSD",
        "NGAS",
        "NATGAS",
        "XNGUSD",
    ),
    "crypto": (
        "BTCUSD",
        "ETHUSD",
        "XRPUSD",
        "LTCUSD",
        "SOLUSD",
        "ADAUSD",
        "DOGEUSD",
        "BNBUSD",
    ),
    "stocks": (),
}


def normal(symbol: str) -> str:
    """A name as it is matched: upper case, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


@dataclass(frozen=True, slots=True)
class Catalog:
    """One reading of the catalog, immutable, safe to hand to guard rules."""

    currencies: frozenset[str]
    lists: dict[str, tuple[str, ...]]
    """Per listed class, its names as saved (for the settings card)."""
    normals: dict[str, frozenset[str]] = field(default_factory=dict)
    """Per listed class, the same names as they are matched. Built by :func:`make_catalog`,
    never by hand, so the two can only agree."""

    def classify(self, symbol: str) -> str:
        """The class this symbol's name falls in; ``"other"`` when nothing claims it.

        An explicit listing outranks every reading, so a symbol filed by hand stays where it
        was put even when its name would read as a pair.
        """
        name = normal(symbol)
        if not name:
            return "other"
        for clas in LISTED:
            if name in self.normals.get(clas, ()):
                return clas
        best: tuple[str, str] | None = None
        for clas in LISTED:
            for entry in self.normals.get(clas, ()):
                if entry and entry in name and (best is None or len(entry) > len(best[1])):
                    best = (clas, entry)
        if best is not None:
            return best[0]
        for start in range(len(name) - 5):
            first, second = name[start : start + 3], name[start + 3 : start + 6]
            if first in self.currencies and second in self.currencies and first != second:
                return "forex"
        return "other"

    def payload(self) -> dict[str, object]:
        return {
            "currencies": sorted(self.currencies),
            "classes": {clas: list(self.lists.get(clas, ())) for clas in LISTED},
        }


def make_catalog(currencies: frozenset[str], lists: dict[str, tuple[str, ...]]) -> Catalog:
    return Catalog(
        currencies=currencies,
        lists=lists,
        normals={clas: frozenset(normal(n) for n in lists.get(clas, ())) for clas in LISTED},
    )


DEFAULT = make_catalog(frozenset(SEED_CURRENCIES), dict(SEED_LISTS))


def _parse(raw: object) -> Catalog:
    if not isinstance(raw, dict):
        return DEFAULT
    currencies = raw.get("currencies")
    classes = raw.get("classes")
    if not isinstance(currencies, list) or not isinstance(classes, dict):
        return DEFAULT
    kept = frozenset(normal(c) for c in currencies if isinstance(c, str) and normal(c))
    lists: dict[str, tuple[str, ...]] = {}
    for clas in LISTED:
        got = classes.get(clas)
        if isinstance(got, list):
            lists[clas] = tuple(
                dict.fromkeys(s.strip().upper() for s in got if isinstance(s, str) and s.strip())
            )
        else:
            lists[clas] = ()
    return make_catalog(kept, lists)


class CatalogStore:
    """The file-backed catalog, re-read when another window saves it (told by mtime, the way
    the shared account names poll)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else catalog_path()
        self._stamp: float | None = None
        self._cached: Catalog = DEFAULT

    def _mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def current(self) -> Catalog:
        stamp = self._mtime()
        if stamp is None:
            self._stamp = None
            self._cached = DEFAULT
            return DEFAULT
        if stamp == self._stamp:
            return self._cached
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("catalog at %s unreadable (%s); using the seeds", self._path, exc)
            return DEFAULT
        self._stamp = stamp
        self._cached = _parse(raw)
        return self._cached

    def save(self, currencies: list[str], lists: dict[str, list[str]]) -> Catalog:
        """Replace the catalog with what the settings card holds, whole -- the saved file is
        the complete truth, which is what lets a removed seed stay removed."""
        fresh = _parse({"currencies": currencies, "classes": lists})
        with shared_data.shared_lock():
            shared_data.write_json_atomic(self._path, fresh.payload())
        self._stamp = self._mtime()
        self._cached = fresh
        return fresh
