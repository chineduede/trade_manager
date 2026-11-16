"""The virtual master: the book a direct-trading profile trades against.

In trading mode there is no master terminal. The person places trades from the UI and they
land here, in a persistent synthetic book of open positions and resting orders; the engine
reads this book exactly where it reads a real master's snapshot, so sizing, guards, the news
rule, breakeven, the day book and the history all run unchanged downstream.

The book is **idealized on purpose**: a market order fills at the feed's current bid/ask with
no spread games, a pending fills at exactly its price the moment the feed crosses it, a stop
loss closes at exactly its level. The copies on the slaves live on real brokers and fill
however those brokers fill -- the book only has to be an honest *master*, not a simulated
broker, and the one behaviour it must reproduce faithfully is MT5's fill rule the reconciler
is built on: **a pending order that fills becomes a position carrying the order's own
ticket** (see :func:`mt5copier.reconcile.reconcile_slave_orders`).

Prices come from a feed terminal -- any connected MT5 slave -- via the quotes on its
snapshots. No quote means *unknown*, never zero: with no price for a symbol nothing on that
symbol fills, closes or opens, because guessing a fill price breaks the risk sizing every
slave derives from the master's entry-to-stop distance.

Every mutation is written straight to disk, atomically. Losing this file loses the master
side of every live copy -- the copies would surface as orphans on the next start -- which is
why the book lives beside ``mapping.db`` and not in memory.

Thread-safety is the caller's: the engine mutates and reads the book under its own lock, the
same lock its reconcile tick holds, so a tick never sees half an edit.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

from .logging_setup import get_logger
from .protocol import OrderType, PendingOrder, Position, PositionType

log = get_logger(__name__)

MIN_LOT = 0.01
MAX_LOT = 1000.0
"""The book's own bounds, deliberately loose: each slave sizes from its own risk and the
master's volume only matters as the base of partial-close fractions (and of the opt-in
no-stop lot multiple, which direct mode never uses because a stop is required here)."""

_SL_REQUIRED = (
    "a stop loss is required on every direct trade: the slaves size from the entry-to-stop distance"
)


def _round_lots(lots: float) -> float:
    return round(lots, 2)


class VirtualBook:
    """The synthetic master book, persisted at ``path``.

    All prices are in the instrument's own quote terms; all symbols are clean canonical
    names, upper-cased on the way in -- each slave decorates them through its own symbol
    mapping, which is the exact inverse of how a real master's decoration is stripped.

    Mutators return ``None`` for done, else why not -- the project's usual refusal shape.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._positions: dict[int, Position] = {}
        self._orders: dict[int, PendingOrder] = {}
        self._next_ticket = 1
        self._load()

    # --- reading ------------------------------------------------------------------------

    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[t] for t in sorted(self._positions))

    def orders(self) -> tuple[PendingOrder, ...]:
        return tuple(self._orders[t] for t in sorted(self._orders))

    def symbols(self) -> set[str]:
        """Every symbol the book holds something on: what the feed must watch."""
        return {p.symbol for p in self._positions.values()} | {
            o.symbol for o in self._orders.values()
        }

    def get_position(self, ticket: int) -> Position | None:
        return self._positions.get(ticket)

    def get_order(self, ticket: int) -> PendingOrder | None:
        return self._orders.get(ticket)

    # --- trading ------------------------------------------------------------------------

    def open_market(
        self,
        symbol: str,
        direction: PositionType,
        lots: float,
        sl: float,
        tp: float,
        quote: tuple[float, float] | None,
        *,
        now: float,
    ) -> str | None:
        """Open a position at the feed's price: a buy at the ask, a sell at the bid."""
        symbol = symbol.strip().upper()
        problem = _check_symbol(symbol) or _check_lots(lots)
        if problem is not None:
            return problem
        if quote is None:
            return f"no price for {symbol} from the feed yet, so a fill price cannot be known"
        bid, ask = quote
        fill = ask if direction is PositionType.BUY else bid
        if fill <= 0:
            return f"the feed's price for {symbol} is unusable"
        problem = _check_levels(direction, fill, sl, tp)
        if problem is not None:
            return problem
        ticket = self._take_ticket()
        self._positions[ticket] = Position(
            ticket=ticket,
            symbol=symbol,
            type=direction,
            volume=_round_lots(lots),
            price_open=fill,
            sl=sl,
            tp=tp,
            time=int(now),
        )
        self._save()
        log.info(
            "direct: opened #%s %s %s %g lots at %g (sl %g, tp %s)",
            ticket,
            symbol,
            direction.name.lower(),
            _round_lots(lots),
            fill,
            sl,
            f"{tp:g}" if tp else "none",
        )
        return None

    def place_pending(
        self,
        symbol: str,
        order_type: OrderType,
        lots: float,
        entry: float,
        sl: float,
        tp: float,
        quote: tuple[float, float] | None,
        *,
        expiration_utc: int = 0,
        now: float,
    ) -> str | None:
        """Rest a limit or stop order, to fill when the feed crosses its price."""
        symbol = symbol.strip().upper()
        problem = _check_symbol(symbol) or _check_lots(lots)
        if problem is not None:
            return problem
        if not order_type.is_pending or order_type.is_stop_limit:
            return "only a plain limit or stop order can be placed here"
        if entry <= 0:
            return "the entry price must be set"
        if quote is None:
            # The same refusal as a market order's, and for a cousin of the reason: with no
            # price there is no telling which side of the market the order would rest on,
            # and a limit placed through the price fills on the spot, which is not what was
            # asked for.
            return f"no price for {symbol} from the feed yet, so the order cannot be placed"
        problem = _check_resting_side(order_type, entry, quote)
        if problem is not None:
            return problem
        problem = _check_levels(order_type.direction, entry, sl, tp)
        if problem is not None:
            return problem
        ticket = self._take_ticket()
        self._orders[ticket] = PendingOrder(
            ticket=ticket,
            symbol=symbol,
            type=order_type,
            volume=_round_lots(lots),
            price_open=entry,
            sl=sl,
            tp=tp,
            time_setup=int(now),
            expiration_utc=expiration_utc,
        )
        self._save()
        log.info(
            "direct: placed #%s %s %s %g lots at %g (sl %g, tp %s%s)",
            ticket,
            symbol,
            order_type.name.lower().replace("_", " "),
            _round_lots(lots),
            entry,
            sl,
            f"{tp:g}" if tp else "none",
            f", expires {expiration_utc}" if expiration_utc else "",
        )
        return None

    def modify_position(self, ticket: int, sl: float, tp: float) -> str | None:
        """Move a position's stop and target. The stop stays required.

        Unlike an opening's levels, a held position's stop may sit AT or PAST its entry:
        that is breakeven, or profit locked in, not a mistake -- the entry-side rule only
        protects the risk distance that sizing needs, and the copies were sized when the
        trade opened. The target keeps the opening rule: one on the losing side of the
        entry would be an instant fill pretending to be a target.
        """
        p = self._positions.get(ticket)
        if p is None:
            return f"#{ticket} is not an open position in this book"
        if sl <= 0:
            return _SL_REQUIRED
        if p.type is PositionType.BUY:
            if tp and tp <= p.price_open:
                return (
                    f"a buy's target must be above its price ({tp:g} is not above {p.price_open:g})"
                )
        elif tp and tp >= p.price_open:
            return f"a sell's target must be below its price ({tp:g} is not below {p.price_open:g})"
        if sl == p.sl and tp == p.tp:
            return None
        self._positions[ticket] = replace(p, sl=sl, tp=tp)
        self._save()
        log.info(
            "direct: #%s %s stop %g, target %s", ticket, p.symbol, sl, f"{tp:g}" if tp else "none"
        )
        return None

    def modify_order(
        self, ticket: int, entry: float, sl: float, tp: float, expiration_utc: int
    ) -> str | None:
        """Move a resting order: its entry can change, which no position's can."""
        o = self._orders.get(ticket)
        if o is None:
            return f"#{ticket} is not a resting order in this book"
        if entry <= 0:
            return "the entry price must be set"
        problem = _check_levels(o.type.direction, entry, sl, tp)
        if problem is not None:
            return problem
        moved = replace(o, price_open=entry, sl=sl, tp=tp, expiration_utc=expiration_utc)
        if moved == o:
            return None
        self._orders[ticket] = moved
        self._save()
        log.info(
            "direct: #%s %s moved to %g (sl %g, tp %s)",
            ticket,
            o.symbol,
            entry,
            sl,
            f"{tp:g}" if tp else "none",
        )
        return None

    def close_position(self, ticket: int) -> str | None:
        p = self._positions.pop(ticket, None)
        if p is None:
            return f"#{ticket} is not an open position in this book"
        self._save()
        log.info("direct: closed #%s %s", ticket, p.symbol)
        return None

    def partial_close(self, ticket: int, percent: float) -> str | None:
        """Close a percent of a position; every copy mirrors the same fraction."""
        p = self._positions.get(ticket)
        if p is None:
            return f"#{ticket} is not an open position in this book"
        if not 0 < percent < 100:
            return "the percent to close must be between 0 and 100 (use close for all of it)"
        slice_lots = _round_lots(p.volume * percent / 100.0)
        left = _round_lots(p.volume - slice_lots)
        if slice_lots < MIN_LOT:
            return f"{percent:g}% of {p.volume:g} lots is under {MIN_LOT:g}, too small a slice"
        if left < MIN_LOT:
            return (
                f"closing {percent:g}% would leave {left:g} lots, under {MIN_LOT:g}; "
                "close the position instead"
            )
        self._positions[ticket] = replace(p, volume=left)
        self._save()
        log.info("direct: #%s %s reduced by %g%% to %g lots", ticket, p.symbol, percent, left)
        return None

    def cancel_order(self, ticket: int) -> str | None:
        o = self._orders.pop(ticket, None)
        if o is None:
            return f"#{ticket} is not a resting order in this book"
        self._save()
        log.info("direct: cancelled #%s %s", ticket, o.symbol)
        return None

    # --- the market moving --------------------------------------------------------------

    def tick(self, quotes: Mapping[str, tuple[float, float]], now: float) -> list[str]:
        """Let the feed's prices act on the book, as a broker would on a real account.

        Expired orders are deleted, pendings whose price the feed crossed fill into
        positions **keeping their ticket**, and positions whose stop or target the feed
        crossed are closed -- the stop winning when one tick gaps past both, because that is
        the conservative reading of a price that visited both levels unobserved.

        A symbol with no quote is left entirely alone: unknown is not flat.

        Returns one line per event, for the engine to log; the book is already saved.
        """
        events: list[str] = []

        for ticket in sorted(self._orders):
            o = self._orders[ticket]
            if o.expiration_utc and now >= o.expiration_utc:
                del self._orders[ticket]
                events.append(f"order #{ticket} {o.symbol} expired and was deleted")
                continue
            quote = quotes.get(o.symbol)
            if quote is None:
                continue
            if _pending_fills(o.type, o.price_open, quote):
                del self._orders[ticket]
                # The order's own ticket, exactly as MT5 does it: the reconciler reads a
                # departed order ticket reappearing as a position as ORDER_FILLED, and any
                # other ticket here would read as a cancel plus an uncopyable open.
                self._positions[ticket] = Position(
                    ticket=ticket,
                    symbol=o.symbol,
                    type=o.type.direction,
                    volume=o.volume,
                    price_open=o.price_open,
                    sl=o.sl,
                    tp=o.tp,
                    time=int(now),
                )
                events.append(
                    f"order #{ticket} {o.symbol} "
                    f"{o.type.name.lower().replace('_', ' ')} filled at {o.price_open:g}"
                )

        for ticket in sorted(self._positions):
            p = self._positions[ticket]
            quote = quotes.get(p.symbol)
            if quote is None:
                continue
            hit = _level_hit(p, quote)
            if hit is not None:
                del self._positions[ticket]
                events.append(f"#{ticket} {p.symbol} closed: {hit}")

        if events:
            self._save()
            for line in events:
                log.info("direct: %s", line)
        return events

    # --- persistence ----------------------------------------------------------------------

    def _take_ticket(self) -> int:
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            positions = {int(d["ticket"]): _position_from(d) for d in raw.get("positions", ())}
            orders = {int(d["ticket"]): _order_from(d) for d in raw.get("orders", ())}
            next_ticket = int(raw.get("next_ticket", 1))
        except Exception as exc:
            # Moved aside, never deleted: this file is the master side of live copies, and a
            # parse error must leave the evidence for a human rather than silently starting
            # an empty book whose first tick would close every copy.
            backup = self._path.with_suffix(f".bad-{os.getpid()}.json")
            try:
                self._path.replace(backup)
                log.error(
                    "virtual book at %s was invalid (%s); moved to %s", self._path, exc, backup
                )
            except OSError:
                log.error(
                    "virtual book at %s was invalid (%s) and could not be moved aside",
                    self._path,
                    exc,
                )
            return
        self._positions = positions
        self._orders = orders
        highest = max([0, *positions, *orders])
        self._next_ticket = max(next_ticket, highest + 1)

    def _save(self) -> None:
        payload = {
            "next_ticket": self._next_ticket,
            "positions": [asdict(p) for p in self.positions()],
            "orders": [asdict(o) for o in self.orders()],
        }
        text = json.dumps(payload, indent=2) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


# --- pure rules -----------------------------------------------------------------------------


def _check_symbol(symbol: str) -> str | None:
    if not symbol:
        return "the symbol must be set"
    return None


def _check_lots(lots: float) -> str | None:
    if not MIN_LOT <= _round_lots(lots) <= MAX_LOT:
        return f"lots must be between {MIN_LOT:g} and {MAX_LOT:g}"
    return None


def _check_levels(direction: PositionType, price: float, sl: float, tp: float) -> str | None:
    """A stop is required and must lose; a target is optional and must win.

    Checked against the entry (or fill) price, not the current quote: the broker-side
    minimum-distance rules live on each slave's broker and are its executor's problem.
    """
    if sl <= 0:
        return _SL_REQUIRED
    if direction is PositionType.BUY:
        if sl >= price:
            return f"a buy's stop loss must be below its price ({sl:g} is not below {price:g})"
        if tp and tp <= price:
            return f"a buy's target must be above its price ({tp:g} is not above {price:g})"
    else:
        if sl <= price:
            return f"a sell's stop loss must be above its price ({sl:g} is not above {price:g})"
        if tp and tp >= price:
            return f"a sell's target must be below its price ({tp:g} is not below {price:g})"
    return None


def _check_resting_side(
    order_type: OrderType, entry: float, quote: tuple[float, float]
) -> str | None:
    """A pending must rest on its side of the quote, as every broker requires: a limit waits
    for the price to come to it, a stop waits for the price to run through it."""
    bid, ask = quote
    if order_type is OrderType.BUY_LIMIT and entry >= ask:
        return f"a buy limit must rest below the ask ({ask:g}); use a buy stop above it"
    if order_type is OrderType.BUY_STOP and entry <= ask:
        return f"a buy stop must rest above the ask ({ask:g}); use a buy limit below it"
    if order_type is OrderType.SELL_LIMIT and entry <= bid:
        return f"a sell limit must rest above the bid ({bid:g}); use a sell stop below it"
    if order_type is OrderType.SELL_STOP and entry >= bid:
        return f"a sell stop must rest below the bid ({bid:g}); use a sell limit above it"
    return None


def _pending_fills(order_type: OrderType, entry: float, quote: tuple[float, float]) -> bool:
    """Whether the feed has crossed a resting order's price. Buys fill off the ask, sells off
    the bid, matching which price a market order of that direction would take."""
    bid, ask = quote
    if order_type is OrderType.BUY_LIMIT:
        return ask <= entry
    if order_type is OrderType.BUY_STOP:
        return ask >= entry
    if order_type is OrderType.SELL_LIMIT:
        return bid >= entry
    if order_type is OrderType.SELL_STOP:
        return bid <= entry
    return False


def _level_hit(p: Position, quote: tuple[float, float]) -> str | None:
    """Which protective level the quote has crossed, if either; the stop wins a gap that
    visited both. A long closes on the bid, a short on the ask -- the side its close would
    execute at."""
    bid, ask = quote
    if p.type is PositionType.BUY:
        if p.sl and bid <= p.sl:
            return f"the stop loss at {p.sl:g} was hit (bid {bid:g})"
        if p.tp and bid >= p.tp:
            return f"the target at {p.tp:g} was reached (bid {bid:g})"
    else:
        if p.sl and ask >= p.sl:
            return f"the stop loss at {p.sl:g} was hit (ask {ask:g})"
        if p.tp and ask <= p.tp:
            return f"the target at {p.tp:g} was reached (ask {ask:g})"
    return None


def _position_from(d: dict) -> Position:
    return Position(
        ticket=int(d["ticket"]),
        symbol=str(d["symbol"]),
        type=PositionType(int(d["type"])),
        volume=float(d["volume"]),
        price_open=float(d["price_open"]),
        sl=float(d.get("sl", 0.0)),
        tp=float(d.get("tp", 0.0)),
        time=int(d.get("time", 0)),
        magic=int(d.get("magic", 0)),
        comment=str(d.get("comment", "")),
    )


def _order_from(d: dict) -> PendingOrder:
    return PendingOrder(
        ticket=int(d["ticket"]),
        symbol=str(d["symbol"]),
        type=OrderType(int(d["type"])),
        volume=float(d["volume"]),
        price_open=float(d["price_open"]),
        sl=float(d.get("sl", 0.0)),
        tp=float(d.get("tp", 0.0)),
        price_stoplimit=float(d.get("price_stoplimit", 0.0)),
        time_setup=int(d.get("time_setup", 0)),
        magic=int(d.get("magic", 0)),
        comment=str(d.get("comment", "")),
        expiration_utc=int(d.get("expiration_utc", 0)),
    )
