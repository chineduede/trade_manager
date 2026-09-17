"""Mapping a master's symbol name to the slave broker's name for the same instrument.

Pure: the caller supplies the slave's symbol list (from ``symbols_get()``) and this decides.
That keeps the fallback scan -- the part most likely to be subtly wrong -- testable against
real broker naming without a connection.

Brokers name the same instrument every way imaginable: ``EURUSD``, ``EURUSD.r``, ``EURUSDm``,
``mEURUSD``, ``EURUSD_SB``, ``EURUSD.pro``. Getting this wrong does not fail loudly; it fails
by copying the trade onto a *different instrument*, so an unresolved symbol must be a refusal
rather than a guess.

The cache key is ``(terminal_id, master_symbol)`` and never the symbol alone. Caching on the
symbol is the bug in ``zele-mt5``'s ``SymbolMapper``: two sources mapping ``GOLD`` differently
collide, and the second broker silently gets the first broker's answer. That mapper also has
no prefix support at all.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .config import TerminalConfig
from .logging_setup import get_logger

log = get_logger(__name__)

_COMMON_SUFFIXES = (".r", ".pro", ".raw", ".ecn", ".c", ".m", ".stp", "_SB", "m", "c", "#", "+")
"""Decorations brokers bolt onto an otherwise-standard name. Used only to derive a *base* name
when scanning, never to invent a mapping -- a candidate still has to exist on the slave."""


class Resolution(StrEnum):
    """How a name was arrived at. Worth recording: an EXACT match and a SCAN match deserve
    different levels of trust, and the log should say which one a copy was placed on."""

    EXACT = "exact name match"
    CONFIGURED = "configured prefix/suffix"
    SCAN = "base-name scan of the slave's symbol list"
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


def candidates(master_symbol: str, cfg: TerminalConfig) -> list[str]:
    """Names to try, most trustworthy first. Order is the whole point."""
    out = [master_symbol]
    if cfg.symbol_prefix or cfg.symbol_suffix:
        out.append(f"{cfg.symbol_prefix}{master_symbol}{cfg.symbol_suffix}")
        # The master's own name may already carry a decoration this broker does not use.
        stripped = base_name(master_symbol)
        if stripped != master_symbol.upper():
            out.append(f"{cfg.symbol_prefix}{stripped}{cfg.symbol_suffix}")
    return list(dict.fromkeys(out))


def resolve(
    master_symbol: str,
    available: Collection[str],
    cfg: TerminalConfig,
) -> ResolvedSymbol:
    """Resolve to a name that actually exists on this slave, or refuse.

    Exact match, then the configured prefix/suffix, then a base-name scan of the slave's own
    list, then fail. ``available`` is that slave's ``symbols_get()`` output.

    Returning UNRESOLVED is a real answer, not an error path to paper over: a trade whose
    instrument cannot be identified must not be placed on a guess.
    """
    by_upper = {name.upper(): name for name in available}

    if master_symbol.upper() in by_upper:
        return ResolvedSymbol(by_upper[master_symbol.upper()], Resolution.EXACT)

    for candidate in candidates(master_symbol, cfg)[1:]:
        if candidate.upper() in by_upper:
            return ResolvedSymbol(by_upper[candidate.upper()], Resolution.CONFIGURED)

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
            cfg.label or cfg.terminal_id[:8],
            ", ".join(matches[:5]),
        )
    return ResolvedSymbol(None, Resolution.UNRESOLVED)


@dataclass
class SymbolCache:
    """Per-terminal resolution cache, keyed by ``(terminal_id, master_symbol)``.

    Not keyed on the symbol alone -- see the module docstring. Small enough to keep for the
    life of the process; symbol lists do not change under a running terminal often enough to
    be worth invalidating on a timer, and :meth:`forget` covers the case where they do.
    """

    _entries: dict[tuple[str, str], ResolvedSymbol] = field(default_factory=dict)

    def get(self, terminal_id: str, master_symbol: str) -> ResolvedSymbol | None:
        return self._entries.get((terminal_id, master_symbol))

    def put(self, terminal_id: str, master_symbol: str, resolved: ResolvedSymbol) -> None:
        self._entries[(terminal_id, master_symbol)] = resolved

    def resolve(
        self,
        terminal_id: str,
        master_symbol: str,
        available: Iterable[str],
        cfg: TerminalConfig,
    ) -> ResolvedSymbol:
        """Cached :func:`resolve`. Failures are cached too -- a symbol this broker does not
        list will not start existing, and re-scanning the whole list per tick is wasteful."""
        hit = self.get(terminal_id, master_symbol)
        if hit is not None:
            return hit
        resolved = resolve(master_symbol, list(available), cfg)
        self.put(terminal_id, master_symbol, resolved)
        return resolved

    def forget(self, terminal_id: str | None = None) -> None:
        """Drop cached answers -- for one terminal, or all of them."""
        if terminal_id is None:
            self._entries.clear()
            return
        for key in [k for k in self._entries if k[0] == terminal_id]:
            del self._entries[key]
