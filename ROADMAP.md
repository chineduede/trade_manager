# Roadmap

Stage 1 (discovery, designation, supervision) is complete and user-accepted. It contains no
trading logic. This document is the plan for everything that does.

## Where the project is — 2026-09-19

**Nothing can send an order yet.** `executor.py` can, and is tested doing so against a fake
session, but the engine constructs no `OrderCommand` and `dry_run = False` is still refused.
Check with `grep -rn "OrderCommand" src/mt5copier/engine.py` rather than assuming.

| Stage | State |
|---|---|
| 1 — discovery, designation, supervision | accepted |
| 2a — the diff engine, dry run | **validated on a demo master 2026-09-17** |
| 2b — execution primitives | complete (`sizing`, `symbols`, `execution`, `executor`) |
| 2c — live copying | **half done** — mapping table and close/reduce/cancel built, not wired |
| 3 — SL/TP sync, partial closes | executor declines MODIFY_SLTP / MODIFY_ORDER |
| 4 — pending orders | detection validated; execution not wired |
| 5 — reverse and filtered copying | inversion implemented, never live-tested |

**The next step.** The first two of the three are done:

1. ~~**Startup reconciliation.**~~ Done. Per slave, before anything is derived: orphans are
   surfaced and **hold that slave until acknowledged**, stale records are dropped so no CLOSE is
   derived against a position that has already gone. `COPIER_MAGIC = 260919` is fixed.
2. ~~**Swap the ledger's backing store**~~ Done — one code path, dry-run pretends every order
   succeeded and live believes the `OrderResult`.
3. **Engine wiring.** `Action` → `OrderCommand` down the slave's existing `cmd_q`, consume
   `OrderResult` in the drain loop, drop the `dry_run` refusal. **This is the first commit where
   a real order can leave the machine.** Give it its own demo run, on one slave rather than
   fifteen.

Everything in "Safety rails" under Stage 2c is now built —
the demo-only guard landed with Stage 2b deliberately, in the same commit as the first code
able to send.

Read `README.md` first for the architecture and the constraints it follows from.

---

## The central design decision: reconcile state, don't consume events

The copier should **poll the master's position list and diff it against the last known state**,
deriving opens, closes, volume changes and SL/TP changes from the difference. It should *not*
try to consume a stream of deals or order events.

Why: a state diff is self-healing. If a tick is missed, the process restarts, or the terminal
briefly disconnects, the next comparison still produces the correct target state. An
event-driven design has to get every event exactly once or it silently diverges — and
divergence in a copier means real positions that nobody is tracking.

Everything below assumes that loop.

## Fixed: the four things the copier does to a position

Settled with the user on 2026-09-15, revised after the 2026-09-16 dry-run test. Every item
happens only while that slave is connected. Pending orders are mirrored too, separately — see
Stage 4.

| Master event | Slave action | Configurable? |
|---|---|---|
| Opens a position *after* the slave connected | Open — see sizing below | no (the sizing mode is) |
| Closes a position | Close | no |
| Modifies SL or TP | Modify to match | yes — `copy_sltp` |
| Reduces volume (takes a partial) | Reduce by the same ratio | yes — `copy_partials` |

### Two sizing paths, and only two

| Master trade | How the copy is sized | Default |
|---|---|---|
| **Has a stop loss** | The slave's own risk budget (percent of its balance, or a fixed cash amount) spread over the master's entry-to-stop distance, **net of that slave's commission and slippage**. The master's lot size is not an input. | always on |
| **No stop loss** | `no_stop_lot_multiple` × the master's own lots. | **off** — `no_stop_policy` defaults to `SKIP` |

The no-stop path is the **single exception** to "a slave never inherits the master's position
size", and it exists only because with no stop there is no risk distance to size from — the
master's own sizing is the only signal left. Two properties of it are inherent, not defects:
the money at risk is not the slave's configured risk and cannot be known in advance, and the
copy opens unprotected exactly as the master's did. `max_lot` is the only remaining bound. That
is why it is opt-in per slave and off by default.

`reconcile.py` decides *which* path applies (`SizingBasis`) because that follows from pure
data; Stage 2b does the arithmetic, because that needs the slave's terminal.

Three things follow that are **not** configurable and must not be made so:

- **A position already open on the master when the slave connects is never copied.** Not by
  default, not behind a flag. The reason is sizing honesty: lots are derived from the master's
  entry-to-stop distance, but a late-joining slave fills at *current* price, so the real money
  at risk is not the configured amount — and when the master is already in profit, it is
  *larger*, silently. There is no setting for which that is acceptable.
- **A decline is final.** Once we decide not to open a position — no stop loss on a slave set
  to `SKIP`, a symbol filter, or it predated our watch — that ticket is never reconsidered for
  that slave. If the master attaches a stop five minutes later, the trade does not become
  copyable; it becomes a late join, which is the same problem under a different name. (This
  retires the "defer the copy until a stop appears" option Stage 2a was going to evaluate.)
- **Volume only ever moves down.** An open position cannot be added to: on a hedging account a
  further entry is a new ticket, which arrives as an ordinary new OPEN. There is no INCREASE
  action, and there must not be one. If a tracked position's volume is ever seen to grow, the
  ledger warns once — it means the master is probably a netting account, which would break the
  partial-close ratios — and mirrors nothing, leaving the copy anchored to the volume it was
  actually sized against.

### The rule that enforces it: continuity of observation

**A position may only be copied if we watched it appear.** Concretely, the slave and the master
must both have been observed connected on the previous reconciliation tick, no more than
`join_grace_seconds` ago (default and hard maximum **5 seconds**, settled 2026-09-15). Any
longer lapse re-baselines that slave: everything the master currently holds becomes permanently
uncopyable for it, reported once as a `PREEXISTING` skip so the log explains the silence.

Positions we already believe we copied are never excluded by a re-baseline — they are ours, and
their closes, modifies and partials go on being mirrored. A re-baseline only decides what we
may *newly* open.

One clock covers every way observation can lapse, because from here they are the same event:
the slave's link drops, the master's link drops (no tick runs at all), a terminal restarts, or
the app was closed for a week. `DryRunLedger._last_ok` holds the monotonic time of the last good
tick per slave, and the absence of ticks *is* the gap.

**Do not measure this with `Position.time`.** That field is the broker's **server** time, and
servers commonly run GMT+2/+3, so comparing it to a local epoch is hours out and a 5-second
window becomes meaningless. Our own `time.monotonic()` has no skew and no timezone.

Why a grace window exists at all: a link blip of a few seconds is not a late join. The reason
adopting an old position is dishonest is that the slave fills far from the master's entry, which
breaks the risk sizing — a trade opened four seconds ago fills within a whisker of it. The cap
is 5 seconds precisely so the window cannot be widened back into the problem it avoids. Zero
disables it, and then any disconnection at all excludes whatever was open.

Also non-negotiable: a slave is only copyable when its worker reports `CONNECTED` **and** those
reports are fresh. Stale-but-`CONNECTED` is the trap — the last snapshot says the link was fine,
and trusting it would make a wedged worker look copyable forever.

## Where the reconciler lives

The **engine** reconciles, not the workers. Only the engine can see the master and every slave
at once, which is what sizing and reconciliation need. Concretely:

- The master's worker publishes its position list in its snapshot. ✅ done in Stage 2a — and
  every worker publishes it, not only the master's, since Stage 2c needs each slave's real
  positions to reconcile against the mapping table at startup.
- The engine diffs, computes each slave's desired state, and sends **order commands** down that
  slave's existing `cmd_q`. The diff half is done; no command is defined or sent yet.
- Slave workers only execute what they are told and report the result.

`protocol.py` has the position payload. The **order-command/result pair is still to write**,
for Stage 2b. The worker's command loop already drains `cmd_q` every tick, so the plumbing
exists.

---

## Stage 2a — detection only, no orders — **done, validated 2026-09-17**

**Goal: prove the diff engine is correct before it can touch a broker.**

- ✅ `Snapshot` carries the master's positions (`Position`: ticket, symbol, type, volume,
  price_open, sl, tp, time, magic, comment). Read via `positions_get()`, which replaced the
  `positions_total()` count — the list already carries the count.
- ✅ `reconcile.py`: pure functions returning intended `Action`s. No I/O, no MT5, no clock.
- ✅ Dry run: `dry_run: bool = True` on `AppConfig`; every intent is logged and none is sent.
  Setting it false is refused with an error, since Stage 2a has nothing to execute with.
- ✅ UI: a detection panel, `d` to start/stop, `ctrl+l` to clear the ledger.
- ✅ Continuity: `SlaveLink` per slave, `join_grace_seconds`, and re-baselining — see the rule
  above. A disconnected or stale slave produces no actions at all, not even a close, and the
  master's own outages break continuity through the same mechanism.

Exit criterion — **met 2026-09-17**: opened, closed, partially closed and modified trades by
hand on a demo master; placed, moved, cancelled and filled pending orders; ran a reversed
slave alongside a normal one; and confirmed no spurious actions across app restarts, worker
restarts and link drops.

This stage is where the design is either right or wrong, and it costs nothing to get wrong.
It earned its keep: the diff logic itself needed three corrections (no INCREASE action,
declines final, a sizing path for unstopped trades) and one outright bug surfaced that would
have been far more expensive with execution wired up.

### The stall — what the dry run actually caught

Deleting a pending order produced no `CANCEL`. The diff was correct; it never ran.

`positions_get()` and `orders_get()` answer `None` both for "the call failed" and for "there
is nothing to return", and the engine skips a tick entirely on an unknown read — so emptying
the order list *looked* like a failed read and froze detection. The worker now disambiguates
by asking `orders_total()` / `positions_total()`: zero means genuinely empty, anything else
stays unknown.

Two lessons worth keeping:

- **Never let a guard be silent.** This one logged at DEBUG, below the configured level, so a
  paused copier was indistinguishable from an idle master. It now warns on the transition and
  the UI shows `DETECTION PAUSED`. Any future guard that can stop the copier must announce it.
- **MT5's `None` is ambiguous everywhere**, not just here. Assume it of any `*_get()` call.

### Decisions taken while building it

**`None` is not an empty list.** `Snapshot.open_positions` is `None` when the read failed and
`()` when the account is flat, and reconciliation is skipped entirely on `None`. The reconciler
derives a CLOSE for every copy it cannot find on the master, so reading a failed poll as "flat"
would propose closing every copied position at once. The worker also fails the *whole* read if
any single position will not parse, because dropping one from the list is indistinguishable
from the master having closed it.

**A believed-copies ledger, persisted.** In dry run nothing ever appears on a slave, so a
state reconciler left alone would propose the same OPEN forever. `DryRunLedger` records an
executable OPEN as a believed position, which both makes the diff converge and exercises the
bookkeeping Stage 2c will drive from SQLite. It persists to `dryrun_state.json` purely so that
a restart re-proposes nothing — that is the "no spurious actions across restarts" half of the
exit criterion, and it cannot be demonstrated with in-memory state. Stage 2c replaces the file
with the mapping table; the pure reconciler does not change.

### What the dry run settled (2026-09-16)

The user ran the manual test and the detection logic matched, with three corrections applied:
volume increases removed as an action, declines made final, and no-stop trades given their own
sizing path (`LOT_MULTIPLE`, off by default) rather than being unsizeable.

**Polling rate** is still untuned: detection runs on the master's own snapshot tick, so it
inherits `poll_interval_ms` (250ms). See the performance note at the bottom before changing it.

## Per-terminal settings editor — not yet built

`TerminalConfig` carries sixteen per-slave settings and the UI can edit none of them. Role
assignment is the only thing the keyboard changes; everything else means quitting and editing
`%APPDATA%\MT5Copier\config.json` by hand. The terminal table even *displays* a RISK column,
which implies it can be changed there, and it cannot.

This is a gap rather than a deferral — it was never planned, and it should have been. It is
also the thing most likely to be wanted daily, since `risk_percent` is the setting a user
actually reaches for.

Shape: a modal editor on the selected terminal (`e`), grouped as role/enabled, sizing,
no-stop policy, copy behaviour, symbol mapping and filters. Build the edited values into a
`TerminalConfig` and let **pydantic do the validation** — surface the `ValidationError` in the
form rather than writing something the schema would reject on next load.

### Changing a setting mid-run is not uniformly safe

The editor must not treat every field as equally harmless, because the ledger already believes
things about that slave:

| Change | While copies are believed |
|---|---|
| `risk_percent`, `risk_cash`, commission, slippage, `max_lot` | Safe. Sizing applies to future opens only; existing copies keep the volume they were sized at. |
| `copy_sltp`, `copy_partials`, `copy_pending` | Safe. They gate future actions, and a decline already taken stays taken. |
| Symbol filters | Safe but asymmetric — widening a whitelist does **not** adopt trades already declined, by the "a decline is final" rule. The form should say so. |
| `invert` | **Refuse, or force a ledger clear for that slave.** A believed copy records the direction it was opened in; flipping the flag does not flip a position that already exists, so the ledger would describe the opposite of reality and every later close would be wrong. |
| `enabled` off | Safe: the ledger prunes that slave's state, and re-enabling re-baselines — so anything open at that moment becomes permanently uncopyable for it, which is correct but worth saying in the UI. |

Sequencing is open: it is not on the path to execution, but Stage 2b testing means flipping
sizing settings repeatedly, so doing it first may well pay for itself.

## Stage 2b — execution primitives — **done**

Each of these was independently testable without a terminal, and each is a known failure mode
rather than a hypothetical. All five landed, in two commits:

| Built | Where |
|---|---|
| Risk sizing, cost-aware, with step-correct quantization | `sizing.py` |
| Symbol resolution with the scan fallback and a per-terminal cache | `symbols.py` |
| Filling-mode negotiation, retcode classification | `execution.py` |
| The one module that can create an order, and the demo-only guard | `executor.py` |
| `OrderCommand` / `OrderResult`, and the session methods the executor needs | `protocol.py`, `worker.py` |

The sections below are the reasoning, kept because it is the part worth re-reading; the code
implements all of it.

### Filling-mode negotiation

`symbol_info.filling_mode` is a **bitmask** of what the symbol permits (FOK / IOC), which is a
different enumeration from the `ORDER_FILLING_*` value that goes into the request — an easy
thing to conflate, so verify both against the docs when implementing.

Pick a supported mode from the mask, cache it per `(terminal_id, symbol)`, and **retry on
`TRADE_RETCODE_INVALID_FILL` with the alternative** rather than giving up.

Non-negotiable: `zele-mt5` hardcodes `ORDER_FILLING_IOC` in all four of its order paths and
never looks at the mask. Across FTMO, Eightcap, IC Markets and the rest, FOK-only symbols would
reject *every* copied order. This must land before any live copying.

### Risk-based sizing

**Two modes only, both risk-based.** Settled:

| Mode | Money at risk per trade |
|---|---|
| `PERCENT_OF_BALANCE` (default) | `slave_balance * risk_percent / 100` |
| `FIXED_CASH_RISK` | `risk_cash`, in that slave's own account currency |

There is deliberately **no lot-multiplier and no fixed-lot mode**. A slave never inherits the
master's position size; it sizes from its own balance and its own risk setting every time. That
also means the master's lot size is not an input to sizing at all — only its entry and stop
are.

#### Derive lots with `order_calc_profit`, not manual tick arithmetic

The risk budget is **net of costs**: commission and expected slippage are money lost on a trade
that hits its stop, so they come out of the budget before lots are derived. Sizing from the
bare entry-to-stop distance means every trade risks the configured amount *plus* costs, and the
tighter the stop the worse the overshoot — on a 10-point stop, commission can dominate.

```python
point = symbol_info.point
worse_entry = entry + slippage_buffer_points * point * (1 if is_buy else -1)
worse_stop = sl - slippage_buffer_points * point * (1 if is_buy else -1)

loss_per_lot = abs(mt5.order_calc_profit(order_type, symbol, 1.0, worse_entry, worse_stop))
cost_per_lot = commission_per_lot_round_turn  # already there-and-back
lots = risk_amount / (loss_per_lot + cost_per_lot)
```

Every term is per lot, so the equation stays linear and needs no iteration. Both inputs are
per-slave config (`commission_per_lot_round_turn`, `slippage_buffer_points`, both defaulting to
zero, which reproduces the naive calculation). `max_deviation_points` is separate and affects
execution only — it is MT5's `deviation` request field, never an input to sizing.

`order_calc_profit` asks the broker's own terminal what one lot would lose between two prices,
in the account currency. It is exact and it handles cross-currency instruments for free.

The manual alternative — `sl_distance * (trade_tick_value / trade_tick_size)` — has two traps.
`SymbolInfo` exposes `trade_tick_value_profit` *and* `trade_tick_value_loss`, which differ on
some instruments, and risk sizing needs the **loss** side; plain `trade_tick_value` (what
`zele-mt5` uses) is the wrong field to reach for. It also gets cross-currency conversion wrong
unless you do the conversion yourself. Note `order_calc_profit` appears in `zele-mt5` only
inside a commented-out block, so there is no prior art to copy — write it fresh.

#### The stop-loss dependency

Risk sizing needs a stop distance, so **a master trade with no stop loss cannot be sized that
way.** This is not an edge case; it is routine for discretionary traders who set stops after
entry.

`NoStopPolicy` on each slave decides:

- `SKIP` (**default**) — don't copy it. Off means off; enabling no-stop copying is a per-slave
  decision the user has to make deliberately.
- `LOT_MULTIPLE` — copy at `no_stop_lot_multiple` × the master's lots. Settled 2026-09-16.
  Implemented in `reconcile.py` as `SizingBasis.MASTER_LOT_MULTIPLE`; Stage 2b does the
  arithmetic, then quantizes and applies `max_lot` exactly as it would for a risk-sized trade.

What `LOT_MULTIPLE` does *not* do: protect the position. The copy opens with no stop, just as
the master's has none. If the master later adds one, Stage 3's SL/TP sync propagates it; until
then that slave position is unprotected, and the UI says so rather than leaving it to be
inferred (`Action.unprotected`).

Deferring the copy until the master attaches a stop was considered and **rejected** on
2026-09-16 — see "a decline is final" above.

### Volume quantization

```python
lots = (raw / step).to_integral_value(ROUND_DOWN) * step  # then clamp to [min, max]
```

Not `Decimal.quantize(step)`, which honours only the step's *exponent* — the bug in `zele-mt5`,
where `volume_step=0.05` silently rounds to 0.01 granularity and emits broker-rejected lots
like `0.07`.

**When the computed size falls below `volume_min`, skip the trade and log loudly. Do not round
up.** Rounding up is the dangerous default: on a small account with a wide stop, `volume_min`
can represent several times the intended risk. Make the policy configurable per slave, but
default to skip.

Apply `max_lot` as a hard ceiling after quantization, not before.

### Symbol resolution

Resolve master symbol to slave symbol by: exact match, then configured prefix/suffix
combinations, then a `symbols_get()` scan for a base-name match, then fail and log.

Cache on `(terminal_id, master_symbol)` — **not** on the symbol alone, which is the cache-key
bug in `zele-mt5`'s `SymbolMapper`, where two sources mapping `GOLD` differently collide. Add
prefix support, which that mapper lacks entirely.

### Retcode-aware retry

Classify rather than treating every non-`DONE` as fatal:

| Class | Codes | Action |
|---|---|---|
| Re-price and retry | `REQUOTE`, `PRICE_CHANGED`, `PRICE_OFF` | fresh tick, bounded retries |
| Change filling and retry | `INVALID_FILL` | alternative from the mask |
| Partial fill | `DONE_PARTIAL` | record *actual* filled volume, do not assume the request |
| Fatal, log and skip | `NO_MONEY`, `INVALID_VOLUME`, `TRADE_DISABLED`, `MARKET_CLOSED` | no retry |
| Connection | `CONNECTION`, `None` result | let the reconnect loop handle it, retry next tick |

Retries must be bounded and fast. A copy that has been retrying for seconds is no longer the
trade the master took — better to abandon it and surface the failure prominently.

## Stage 2c — live open/close on demo — **half done**

Built (`store.py`, and the close/reduce/cancel paths in `executor.py`):

- ✅ SQLite mapping table, `Copy` rows keyed `(terminal_id, master_ticket)`, WAL, committed per
  write. It also hands back `CopiedPosition` / `CopiedOrder` so the diff engine is unchanged.
- ✅ `find_orphans` / `find_missing` — the two queries a restart needs, pure functions over the
  store.
- ✅ `CLOSE`, `REDUCE`, `CANCEL` in the executor, reading volume from the terminal rather than
  from our record.
- ✅ The demo-only guard, which landed early with Stage 2b.

- ✅ `COPIER_MAGIC = 260919` (`protocol.py`), stamped on every order so `find_orphans` can tell
  our positions from the user's own trading. **Fixed once live copies carry it** — changing it
  makes every existing copy unrecognisable, and it would be ignored rather than reported.
- ✅ `find_missing_orders`, the fourth query. It takes *two* ticket lists because an MT5 pending
  order that fills becomes a position with the **same ticket**: an order row whose ticket now
  names a live position has not gone anywhere, it filled, and `ORDER_FILLED` is waiting to
  convert it in place. Forgetting it would copy the trade a second time as a fresh open.
- ✅ **Startup reconciliation, per slave, before anything is derived.** It runs inside
  `_slave_links()`, which is the single gate answering "may we copy to this slave now?" — four
  conditions: CONNECTED, reports are fresh, reconciled with no unacknowledged orphans, and
  sizeable. That placement makes the ordering structural rather than conventional: an
  unreconciled slave is held, so stale rows are always dropped before a diff runs against them.

### How a hold works, and why it is expressed as a broken link

A held slave is passed to the ledger as `SlaveLink(connected=False)` rather than omitted. That
one choice buys three things: the ledger derives nothing for it, its continuity clock stops, and
so **lifting the hold forces a re-baseline** — while we were held we were not copying, so
whatever the master opened meanwhile is not ours to adopt. Omitting the link instead would let
`_prune` discard that slave's exclusions, losing "a decline is final" decisions.

The cost, stated plainly: **a master close during a hold is deferred, not lost.** The belief row
survives, so the CLOSE is derived the moment the slave is copyable again — but until then the
copy rides the move. This is the one place the orphan policy can cost money, and it is the same
behaviour an ordinary link drop already has.

**Orphans are surfaced and never resolved.** Adopting one silently would break sizing honesty,
since we cannot know what it was sized against; closing one silently would be the copier taking
a trading action nobody asked for. Acknowledging (`o` in the UI) records that a human looked —
it closes nothing, and the orphans stay listed afterwards, because they are still real, still
open and still untracked. Consent is keyed to specific slave tickets, so a *new* orphan re-arms
the hold rather than inheriting the old sign-off.

**One mode-dependent branch, and it is load-bearing:** in dry run the stale-row sweep is
**skipped**. Simulated beliefs carry a negated master ticket as their slave ticket
(`_simulated_result`), so every one of them looks missing against a real position list — running
the sweep would delete the whole dry run's state on its first tick.

**An unreadable risk budget is a hold, not a zero.** A zero `risk_amount` is not a soft failure:
`lots_for_risk` refuses it, the executor reports a failed order, and a failed open is excluded
permanently. So a percent-of-balance slave whose balance cannot be read derives nothing until it
can. Fixed-cash slaves never read the balance and are unaffected.

**Still to do — the whole remaining gap before live copying:**

1. **Wire the engine.** Translate each executable `Action` into an `OrderCommand` (the engine
   supplies `risk_amount` from `risk_budget(cfg, snap.balance)`, since it has both), push it down
   that slave's `cmd_q`, and handle `OrderResult` in the drain loop alongside `Snapshot`. Then
   drop the `dry_run = False` refusal in `Engine._reconcile`.

**Position mapping must survive restart.** Options considered:

- *Encode the master ticket in the order comment* — rejected. Brokers truncate and overwrite
  comments; it cannot be relied on.
- *Encode it in `magic`* — rejected. A single 64-bit field cannot hold both a copier identity
  and a master ticket reliably.
- **SQLite mapping table, plus a fixed copier magic so we can recognise our own positions, with
  the comment as a best-effort hint.** This is what `zele-mt5` does (ownership in SQLite, not by
  magic) and it is the right call.

Reuse `zele-mt5`'s `core/database/service.py` largely as-is: singleton per `(db_path, pid)`,
`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000`. That combination is exactly
right for engine-writes / UI-reads across processes.

**Startup reconciliation** is mandatory: on launch, match existing slave positions against the
mapping table before doing anything, so a restart never re-opens positions that already exist.
Orphans (ours by magic, absent from the table) must be surfaced, not silently adopted or closed.

### Safety rails, all before the first live account

- `dry_run` defaults to true; turning it off is deliberate.
- **Demo-only guard**: refuse to copy into an account whose `account_info().trade_mode` is not
  demo, unless explicitly overridden per slave. Most of these 15 terminals are prop/live.
- `max_lot` per slave (already in the schema).
- A visible kill switch — `F9` already halts copying without touching positions, and that
  meaning is fixed.

## Stage 3 — SL/TP sync and partial closes

Mirror stop-loss and take-profit changes after entry, and mirror partial closes
proportionally. This is where most copier edge cases live: a partial close changes the volume
ratio that every later action depends on, so the mapping table must store the master volume the
slave position was sized against, not just the tickets.

## Stage 4 — pending orders — **detection done and validated, execution pending**

In scope (confirmed 2026-09-16), gated per slave by `copy_pending`, default off.

Detection landed with the rest of Stage 2a rather than waiting, so it gets validated the same
way positions were — the whole point of dry run is to be wrong for free, and orders have more
ways to be wrong than positions do. Built:

- ✅ `PendingOrder` on `Snapshot`, from `orders_get()`, same `None`-means-unknown rule. A
  failed *order* read blocks reconciliation entirely, not just the order half — without the
  order list a fill is indistinguishable from a cancellation.
- ✅ `reconcile_slave_orders()` → `PLACE` / `MODIFY_ORDER` / `CANCEL` / `ORDER_FILLED`.
- ✅ The continuity rule, via a second exclusion set.
- ✅ Sizing: unchanged, since an order has an entry and usually a stop.

Still to write in 2b/2c: the `order_send` calls themselves. Note `zele-mt5` has no
`TRADE_ACTION_MODIFY` path at all — it can modify a position's SL/TP but not a pending order's
entry price, so that is fresh work.

### Fill versus cancellation — **confirmed on a demo, 2026-09-17**

An order leaving the master's list has either been **cancelled** or **filled**, and the right
responses are opposites: cancel the slave's copy, or leave it alone because it is about to
fill at the same price. Cancelling a copy that was going to fill silently drops a trade.

The signal used is the ticket: **MT5 gives the position opened by a pending order that
order's own ticket**, so a departed ticket that now names a position was filled. This was the
one assumption in the diff engine that could not be proven without a terminal; a demo run
confirmed it — a filled limit logs `ORDER_FILLED` and a deleted one logs `CANCEL`.

On a fill the ledger hands tracking from the order to the position, which is also what stops
the position diff proposing a second copy of the same trade.

### Declining an order says nothing about its fill

`_excluded` and `_excluded_orders` are separate on purpose. Refusing to mirror a *resting
order* does not exclude the position it becomes: that position is a new event at a current
price, judged on its own rules. This is what makes `copy_pending = False` behave sensibly —
the resting order is not mirrored, but when it fills the position is copied like any other.

### Stop-limit orders are not mirrored

`BUY_STOP_LIMIT` / `SELL_STOP_LIMIT` carry a trigger *and* a separate entry price. Mirroring
one of the two is worse than mirroring neither, so they are reported as skipped. Revisit only
if they turn up in practice.

## Stage 5 — reverse and filtered copying

Per-slave direction inversion (`invert`, in scope, confirmed 2026-09-16), symbol
whitelist/blacklist, plus optional size and session windows. The filters are already honoured;
what remains is size/session windows and live validation.

### Inversion is a swap, not a flip — settled 2026-09-16, validated 2026-09-17

`effective_levels()` **swaps stop and target** for a reversed copy. The master's stop is the
price at which its trade fails, which is precisely where the reversed trade succeeds. Passing
the levels through unswapped would put the reversed position's stop on the profitable side of
entry, where it is hit instantly or rejected.

Two consequences:

- A reversed copy's **risk runs to the master's take-profit**, so that is what sizes it. A
  master trade with a stop but *no target* inverts into a copy with no stop, and falls to
  `no_stop_policy` — meaning a slave set to both `invert` and `SKIP` copies nothing at all
  from a master that does not set targets. Correct, but surprising enough to be worth saying.
- Reversing a **resting order flips the kind as well as the direction**: BUY_LIMIT ↔ SELL_STOP,
  SELL_LIMIT ↔ BUY_STOP. The price level is what must be preserved, and a BUY_LIMIT rests
  *below* the market — so the opposite trade at that level triggers on the way down, which is
  a stop order, not a limit. Getting this wrong produces an order the broker refuses outright.

---

## Performance note

Master position polling wants to be faster than the current 250ms account-snapshot interval —
around 100ms. Each terminal has its own process, so slave polling costs nothing extra; the
master's loop is the only latency-critical one. Measure before tuning.
