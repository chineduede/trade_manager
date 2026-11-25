"""Mapping a master's symbol name to the slave broker's name for the same instrument.

Pure: the caller supplies the slave's symbol list (from ``symbols_get()``) and this decides.
That keeps the fallback scan -- the part most likely to be subtly wrong -- testable against
real broker naming without a connection.

Brokers name the same instrument every way imaginable: ``EURUSD``, ``EURUSD.r``, ``EURUSDm``,
``mEURUSD``, ``EURUSD_SB``, ``EURUSD.pro``. Getting this wrong does not fail loudly; it fails
by copying the trade onto a *different instrument*, so an unresolved symbol must be a refusal
rather than a guess.

**Order of preference, most trusted first:** an explicit ``symbol_map`` entry, then the
configured prefix/suffix, then an exact name match, then a base-name scan.

The configured decoration sitting *above* the exact match is deliberate and was not the
original order. Setting a suffix is an assertion that this broker decorates its names, so a
bare ``EURGBP`` listed alongside ``EURGBPp`` is more likely a different product -- a standard
account's symbol beside a raw one -- than the same instrument. When no decoration is
configured the rung is empty and the order is unchanged, so this affects only terminals that
have opted in. A configured decoration that *misses* still falls through, because a suffix is
a claim about the broker's naming in general and not about any one symbol: BlackBull's indices
are undecorated while everything else takes ``p``.

An explicit mapping is the same kind of assertion, only stronger and about one symbol, which is
why it outranks everything and why a mapping onto a name the broker does not list is refused
outright instead of falling through.

The cache key is ``(terminal_id, master_symbol)`` and never the symbol alone. Caching on the
symbol is the bug in ``zele-mt5``'s ``SymbolMapper``: two sources mapping ``GOLD`` differently
collide, and the second broker silently gets the first broker's answer. That mapper also has
no prefix support at all.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .logging_setup import get_logger

log = get_logger(__name__)

_COMMON_SUFFIXES = (".r", ".pro", ".raw", ".ecn", ".c", ".m", ".stp", "_SB", "m", "c", "#", "+")
"""Decorations brokers bolt onto an otherwise-standard name. Used only to derive a *base* name
when scanning, never to invent a mapping -- a candidate still has to exist on the slave."""


class Resolution(StrEnum):
    """How a name was arrived at. Worth recording: an EXACT match and a SCAN match deserve
    different levels of trust, and the log should say which one a copy was placed on."""

    MAPPED = "explicit per-symbol mapping"
    EXACT = "exact name match"
    CONFIGURED = "configured prefix/suffix"
    SCAN = "base-name scan of the slave's symbol list"
    MAPPED_MISSING = "mapped to a name this broker does not list"
    """Distinct from UNRESOLVED on purpose. Both are refusals, but this one means the user's
    own mapping is wrong -- a typo, or a symbol the broker has retired -- which is a different
    thing to fix and is what the editor's mapping tester exists to catch before a trade does."""
    UNRESOLVED = "no match on the slave"


@dataclass(frozen=True, slots=True)
class ResolvedSymbol:
    name: str | None
    how: Resolution

    @property
    def ok(self) -> bool:
        return self.name is not None


def base_name(symbol: str) -> str:
    """Strip a broker decoration to get at the underlying instrument name.

    Deliberately conservative: only a known trailing decoration is removed, and only when what
    remains is still long enough to be a real symbol. Over-stripping is how ``EURUSDm`` and
    ``EURUSD`` end up looking like different instruments -- or worse, how ``XAUUSDc`` and
    ``XAUUSD`` end up looking like the same one when they are not.
    """
    upper = symbol.upper()
    for suffix in _COMMON_SUFFIXES:
        candidate = suffix.upper()
        if upper.endswith(candidate) and len(upper) - len(candidate) >= 4:
            return upper[: -len(candidate)]
    return upper


def candidates(master_symbol: str, prefix: str = "", suffix: str = "") -> list[str]:
    """Names to try, most trustworthy first. Order is the whole point."""
    out = [master_symbol]
    if prefix or suffix:
        out.append(f"{prefix}{master_symbol}{suffix}")
        # The master's own name may already carry a decoration this broker does not use.
        stripped = base_name(master_symbol)
        if stripped != master_symbol.upper():
            out.append(f"{prefix}{stripped}{suffix}")
    return list(dict.fromkeys(out))


def undecorate(symbol: str, prefix: str = "", suffix: str = "") -> str:
    """The master's name with its broker's decoration taken off, when it carries it.

    The master-side twin of :func:`candidates`: a slave's prefix/suffix is put *on* a name,
    the master's is taken *off*, so every slave starts from the plain instrument name. Unlike
    :func:`base_name` this knows the decoration rather than guessing at it, which is what makes
    a broker's own ``p`` or ``.a`` -- too short or too odd for the built-in list -- safe to
    strip.

    A name that does not carry the decoration is left alone, for the reason the module
    docstring gives for slaves: brokers decorate by asset class, so ``US30`` stays ``US30``
    beside ``EURUSDp``. Case is folded only for the comparison; what is kept keeps its casing.
    Nothing is stripped to an empty name.
    """
    name = symbol
    if prefix and name.upper().startswith(prefix.upper()) and len(name) > len(prefix):
        name = name[len(prefix) :]
    if suffix and name.upper().endswith(suffix.upper()) and len(name) > len(suffix):
        name = name[: -len(suffix)]
    return name


_INFER_LEAST = 4
"""How many recognizable names a catalog must hold before its decoration is believed at all:
below this, one oddly named instrument decides the vote."""

_INFER_SHARE = 2 / 3
"""The share of recognizable names the winning decoration must cover. A broker that lists both
``EURUSD`` and ``EURUSDp`` splits its own vote and nothing is inferred, which is right: its
bare names already match exactly, and guessing between its products is not this function's
call."""

_INFER_DECOR_MOST = 5
"""The longest leftover believed to be decoration, mirroring the catalog's own bound."""


def infer_decoration(names: Iterable[str], known: Callable[[str], str | None]) -> tuple[str, str]:
    """The decoration a broker's own catalog says it uses, or ``("", "")`` when it is not
    telling: the prefix and suffix that dominate its recognizably named instruments.

    ``known`` maps a (possibly decorated) name to the known instrument inside it --
    :meth:`mt5copier.catalog.Catalog.base_of` -- and compares in letters and digits only, so
    the leftover is read off the broker's own spelling (``EURUSD.r`` votes ``("", ".R")``).
    Every recognizable name votes with whatever surrounds its known base, undecorated names
    voting for nothing; only a clear winner -- most of the vote, enough voters -- is believed,
    and "no decoration" winning is not an inference.

    This exists for direct trading's feed, whose decoration the person has usually never had a
    reason to configure: the configured value, being an assertion, still outranks whatever this
    reads. And it is used only to take decoration *off* a name, or to find the feed's own name
    for a symbol again -- never to pick what another broker trades, which stays with
    :func:`resolve`'s refusal-based rungs.
    """
    votes: Counter[tuple[str, str]] = Counter()
    for name in names:
        upper = name.strip().upper()
        base = known(upper)
        if base is None:
            continue
        at = upper.find(base)
        if at < 0:
            # The known base is not contiguous in the broker's spelling ("EUR/USD"), so what
            # surrounds it cannot be read off.
            continue
        pre, post = upper[:at], upper[at + len(base) :]
        if len(pre) > _INFER_DECOR_MOST or len(post) > _INFER_DECOR_MOST:
            continue
        votes[(pre, post)] += 1
    total = sum(votes.values())
    if total < _INFER_LEAST:
        return "", ""
    (pre, post), count = votes.most_common(1)[0]
    if (pre, post) == ("", "") or count / total < _INFER_SHARE:
        return "", ""
    return pre, post


def mapped_target(master_symbol: str, symbol_map: Mapping[str, str] | None) -> str | None:
    """The explicit override for this symbol, or None.

    Keys are compared upper-cased. :class:`~mt5copier.config.TerminalConfig` normalises them on
    load, so this fold is for callers holding a map that has not been through pydantic -- an
    unsaved editor form, most obviously.
    """
    if not symbol_map:
        return None
    return symbol_map.get(master_symbol.upper())


def configured_name(
    master_symbol: str,
    prefix: str = "",
    suffix: str = "",
    symbol_map: Mapping[str, str] | None = None,
) -> str:
    """The single best guess at the slave's name, without a symbol list to check against.

    For callers that have no connection and must still print *something*: the log's
    approximation in :func:`mt5copier.reconcile.map_symbol`, and the editor before a terminal
    has connected. It cannot do an exact match or a scan, both of which need the broker's list,
    so it is only ever exactly right for a symbol the user mapped by hand -- which is also the
    case where being wrong is most visible.
    """
    return mapped_target(master_symbol, symbol_map) or f"{prefix}{master_symbol}{suffix}"


def resolve(
    master_symbol: str,
    available: Collection[str],
    prefix: str = "",
    suffix: str = "",
    label: str = "",
    symbol_map: Mapping[str, str] | None = None,
) -> ResolvedSymbol:
    """Resolve to a name that actually exists on this slave, or refuse.

    An explicit mapping, then the configured prefix/suffix, then an exact match, then a
    base-name scan, then fail. ``available`` is that slave's ``symbols_get()`` output. See the
    module docstring for why the decoration outranks the exact match.

    Returning a refusal is a real answer, not an error path to paper over: a trade whose
    instrument cannot be identified must not be placed on a guess.
    """
    by_upper = {name.upper(): name for name in available}

    target = mapped_target(master_symbol, symbol_map)
    if target is not None:
        hit = by_upper.get(target.upper())
        if hit is not None:
            return ResolvedSymbol(hit, Resolution.MAPPED)
        # No fall-through. The mapping says these two names are the same instrument; if the
        # named one is absent the assertion is false, and resolving to something else would
        # place the trade on a name the user never wrote while the log claimed a match.
        log.error(
            "%s maps %r to %r, which this broker does not list; refusing to resolve it another way",
            label or "this slave",
            master_symbol,
            target,
        )
        return ResolvedSymbol(None, Resolution.MAPPED_MISSING)

    for candidate in candidates(master_symbol, prefix, suffix)[1:]:
        if candidate.upper() in by_upper:
            return ResolvedSymbol(by_upper[candidate.upper()], Resolution.CONFIGURED)

    if master_symbol.upper() in by_upper:
        return ResolvedSymbol(by_upper[master_symbol.upper()], Resolution.EXACT)

    wanted = base_name(master_symbol)
    matches = sorted({name for name in available if base_name(name) == wanted})
    if len(matches) == 1:
        return ResolvedSymbol(matches[0], Resolution.SCAN)
    if len(matches) > 1:
        # Ambiguous is not "pick the first". Two names sharing a base can be different
        # instruments (a CFD and its cash equivalent, say), and choosing wrongly copies the
        # trade onto the wrong market.
        log.warning(
            "symbol %r matches %d names on %s (%s); refusing to guess",
            master_symbol,
            len(matches),
            label or "this slave",
            ", ".join(matches[:5]),
        )
    return ResolvedSymbol(None, Resolution.UNRESOLVED)


_Inputs = tuple[str, str, str | None]
"""What an answer was computed from: ``(prefix, suffix, mapped target for this symbol)``."""


@dataclass
class SymbolCache:
    """Per-terminal resolution cache, keyed by ``(terminal_id, master_symbol)``.

    Not keyed on the symbol alone -- see the module docstring. Small enough to keep for the
    life of the process; a broker's symbol list does not change under a running terminal often
    enough to be worth invalidating on a timer, and :meth:`forget` covers the case where it
    does (the worker calls it on a reconnect).

    **Each entry remembers the settings that produced it, and a change is a miss.** Without
    that, editing a slave's suffix changed nothing until its worker was restarted: the answer
    -- including a *failure*, which is cached just as readily -- was served from the key alone
    and the new decoration never reached :func:`resolve`. That cost a live test cycle on
    2026-09-21. The remembered inputs are per symbol, not a fingerprint of the whole map, so
    correcting one override does not discard every other answer.
    """

    _entries: dict[tuple[str, str], tuple[ResolvedSymbol, _Inputs]] = field(default_factory=dict)

    def get(self, terminal_id: str, master_symbol: str) -> ResolvedSymbol | None:
        """What we last concluded about this symbol, whatever it was concluded from.

        Deliberately *not* input-aware: this answers "have we looked at this before", which is
        what the tests and any future introspection want. Whether the answer is still valid is
        :meth:`resolve`'s separate question.
        """
        entry = self._entries.get((terminal_id, master_symbol))
        return None if entry is None else entry[0]

    def put(
        self,
        terminal_id: str,
        master_symbol: str,
        resolved: ResolvedSymbol,
        inputs: _Inputs = ("", "", None),
    ) -> None:
        self._entries[(terminal_id, master_symbol)] = (resolved, inputs)

    def resolve(
        self,
        terminal_id: str,
        master_symbol: str,
        available: Iterable[str],
        prefix: str = "",
        suffix: str = "",
        label: str = "",
        symbol_map: Mapping[str, str] | None = None,
    ) -> ResolvedSymbol:
        """Cached :func:`resolve`, reused only while the settings behind it are unchanged.

        Failures are cached alongside successes -- a symbol this broker does not list will not
        start existing, and re-scanning a 2000-name list every tick is wasteful. What does
        change is the user's configuration, so that is what the entry is checked against.
        """
        inputs: _Inputs = (prefix, suffix, mapped_target(master_symbol, symbol_map))
        entry = self._entries.get((terminal_id, master_symbol))
        if entry is not None and entry[1] == inputs:
            return entry[0]
        if entry is not None:
            log.info(
                "symbol settings for %r changed on %s; resolving it again",
                master_symbol,
                label or terminal_id[:8],
            )
        resolved = resolve(master_symbol, list(available), prefix, suffix, label, symbol_map)
        self.put(terminal_id, master_symbol, resolved, inputs)
        return resolved

    def forget(self, terminal_id: str | None = None) -> None:
        """Drop cached answers -- for one terminal, or all of them."""
        if terminal_id is None:
            self._entries.clear()
            return
        for key in [k for k in self._entries if k[0] == terminal_id]:
            del self._entries[key]
