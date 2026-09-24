# Roadmap

Stage 1 (discovery, designation, supervision) is complete and user-accepted. It contains no
trading logic. This document is the plan for everything that does.

## Where the project is — 2026-09-21

**An order can now leave this machine, and so can a change to one.** The engine builds
`OrderCommand`s and consumes `OrderResult`s; `dry_run` is the only thing standing between a
decision and a broker, and it still defaults to true. The executor's demo-only guard is still
in place underneath.

**Nothing here has been validated against a broker yet.** Every claim below is backed by tests
and by the Stage 2a dry run, not by an order that actually filled. **The one-slave demo run in
"Verifying the wiring" is still the next thing to do, before this is pointed at anything
else** — Stage 3 landed before it because it was cheap to build and changes what that run
should check, not because the run stopped mattering.

| Stage | State |
|---|---|
| 1 — discovery, designation, supervision | accepted |
| 2a — the diff engine, dry run | **validated on a demo master 2026-09-17** |
| 2b — execution primitives | complete (`sizing`, `symbols`, `execution`, `executor`) |
| 2c — live copying | **wired, awaiting its demo run** |
| 3 — SL/TP sync, partial closes | **built 2026-09-21, awaiting its demo run** |
| 4 — pending orders | detection validated; PLACE / MODIFY_ORDER / CANCEL dispatch, untested live |
| 5 — reverse and filtered copying | inversion implemented, never live-tested |

**The next step: verify it on one demo slave.** See "Verifying the wiring" under Stage 2c for
the ordered checklist, then "Verifying the sync" under Stage 3. Do not widen past one slave
until the demo-only refusal has been seen working with its own eyes — it is the guard that
stands between a bug here and a prop account.

Everything in "Safety rails" under Stage 2c is now built — the demo-only guard landed with
Stage 2b deliberately, in the same commit as the first code able to send.

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

## Per-terminal settings editor — **half built, 2026-09-24**

**Built — press `e` on a terminal.** A modal covering sizing (`sizing_mode`, `risk_percent`,
`risk_cash`, `max_lot`, commission, slippage, max deviation), the no-stop policy and its
multiple, and the three copy flags. A blank `max_lot` means no ceiling. Ctrl+S or Enter saves,
Esc cancels. The pure half is `settings_edit.py`; the form is `tui/settings_screen.py`.

**Still to build, in this order:**

1. **The symbol section, with the live mapping tester.** `symbol_prefix`, `symbol_suffix` and
   `symbol_map` are all still hand edits. The tester has a real design question in it: the UI
   process holds no MT5 connection (each worker owns its own), and `CommandKind` is only
   `STOP` / `RECONNECT` / `PING`, so testing a mapping against a broker's symbol list needs a
   new worker command and a reply. It is not form work.
2. **The `invert` guard** — see the table below. Until it exists, `invert` is shown read-only.
3. **Filters** (`symbol_whitelist`, `symbol_blacklist`), `label`, `enabled`.

**Decisions taken building it, none of them obvious from the code:**

- **`invert` and `allow_live_trading` are deliberately not editable.** The first needs the
  guard below. The second is the demo-only guard's override, and a guard that a stray keypress
  can lift is not a guard; it stays a hand edit with the app closed. Both are shown read-only
  in the form.
- **Validation is pydantic's, not the form's.** The bounds live on `TerminalConfig`, so the
  form cannot accept what `load()` would reject — which would read as a corrupt config and
  reset the user's settings. The form adds only blank-means-`None` for `max_lot` and a refusal
  of `inf` / `nan`, because `risk_cash` has only a lower bound and `inf` would pass it.
- **An edit is built on the terminal's *live* settings at the moment of saving, then swapped
  in whole** (`AppConfig.replace_terminal`, one dict assignment). The engine's drain thread
  reads a slave's settings every tick and takes no lock against the UI thread, so patching
  fields one at a time could size an order from half an edit. The swap refuses a change of
  role; `set_role` is the only place the one-master rule is kept.
- **A field whose text is unchanged is skipped, not re-parsed.** One third does not survive a
  trip through a text box, and editing an unrelated field must never nudge a risk setting.
- **A refused form changes nothing** — including the fields beside the bad one that were fine.
  Half an edit would leave the user believing all of it went in.
- **F9 works inside the form.** A modal hides the app's own keys, and copying can be live
  while it is open, so the form binds panic itself. It closes the form and drops unsaved edits.
- **Two saves earn a warning**, because they were a deliberate hand edit and are now one
  keypress: enabling `LOT_MULTIPLE` (the copy opens unprotected) and removing a `max_lot`.
- **Every saved change is logged** (`settings changed for <label>: risk_percent 1 -> 0.5`).

The engine needed no change: it reads a slave's settings from the shared `AppConfig` on every
tick, and workers are handed none at spawn, so an edit reaches the next copied trade and
nothing already open. That is the "Safe" behaviour the table below asks for.

The rest of this section is the original write-up, kept because steps 1–3 still follow it.

`TerminalConfig` carries sixteen per-slave settings and, before this, the UI could edit none of
them. Role assignment was the only thing the keyboard changed; everything else meant quitting
and editing `%APPDATA%\MT5Copier\config.json` by hand. The terminal table even *displays* a
RISK column, which implied it could be changed there, and it could not.

This was a gap rather than a deferral — it was never planned, and it should have been. It is
also the thing most likely to be wanted daily, since `risk_percent` is the setting a user
actually reaches for.

### It has now cost a test cycle — 2026-09-21

The first live attempt failed with `symbol 'EURGBP' not found on this slave`, and the reason
was a single unset character: **BlackBull Markets decorates every one of its 1978 symbols with
a lowercase `p`** (`EURGBPp`, `EURAUDp`, `XAUUSDp`; its indices are undecorated, so `US30` and
`NAS100` match exactly). `symbol_suffix` was `""`, so exact match missed, there was no
configured decoration to try, and the base-name scan missed too — `p` is not in
`symbols._COMMON_SUFFIXES`, so `base_name("EURGBPp")` is `"EURGBPP"`, which does not equal
`"EURGBP"`. Refusing was correct; an unresolved symbol must never become a guess.

Three things follow, in order of how much they would have helped:

- **Setting `symbol_suffix` needed the app closed and the JSON hand-edited**, which is exactly
  the friction above. Two settings were changed by hand that evening (`dry_run` and this one)
  and one of them was the bug.
- **Diagnosing it needed a separate script that connected to the terminal and listed
  symbols.** The editor's symbol-mapping section should be able to *test* a mapping against
  the live symbol list and show what it resolves to and how (`EXACT` / `CONFIGURED` / `SCAN`),
  because that turns a failed trade into a pre-flight check. This is probably the single
  highest-value part of the editor, and it is not obvious from the settings list alone.
- **Adding `p` to `_COMMON_SUFFIXES` was considered and rejected.** The configured suffix is
  the mechanism built for this, it is per-slave so it cannot misfire on another broker, and it
  resolves at `CONFIGURED` rather than the less trustworthy `SCAN`. Widening a heuristic that
  runs for *every* broker to fix one broker is the wrong trade, and the scan is already the
  part of that module most likely to be subtly wrong.

Also worth recording because it was the thing actually verified: the failure came *after* the
demo-only guard, which is how we know BlackBull's `trade_mode` reads 0. The guard order held.

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

- ✅ **The engine wiring.** Each executable `Action` becomes an `OrderCommand` on that slave's
  `cmd_q`; `OrderResult`s come back on the shared queue and are applied to belief. `simulate`
  now follows `dry_run` rather than being forced true, and the refusal is gone.

### What is dispatched, and what is deliberately not

Four conditions, all of which must hold: **not** a dry run, the action is `executable`
(a skip is a record, never an instruction), its kind is in `protocol.DISPATCHABLE_KINDS`, and
the slave still has a worker and a snapshot.

Since Stage 3, `DISPATCHABLE_KINDS` omits only `ORDER_FILLED` — and that is not a gap but a
report about the slave's *own* fill, whose whole effect is to hand tracking from an order to
the position it became. There is nothing to send anywhere.

`step()` still takes the set rather than assuming it, because that is what keeps an unsendable
kind out of **flight**: an action left waiting for an answer nothing will give freezes that
ticket until the 30s in-flight timeout. The alternative — send it, let the executor decline —
produces one ERROR per distinct stop value, forever, for a gap we already know about, and a log
that trains its reader to ignore ERROR is worse than no log. Narrowing the set again is
therefore safe, but it must not be silent, so the engine warns once per session per kind.

### Correlating results: why `command_id` is checked in the engine

The ledger keys belief on `(slave, master ticket, op)` and ignores `command_id`. That is fine
while exactly one command per key is outstanding, which `_inflight` guarantees — but not after
`expire_inflight` gives up: the decision is re-derived, re-sent under a **new** id, and the
stranded answer to the old one would then be applied to the new command's key and describe the
wrong order.

So the engine keeps `_pending_cmd: key -> (command_id, sent_at)` and refuses any result whose
id does not match, loudly, without touching the ledger. The entry is left in place so a correct
later answer can still land. Pruning is age-based rather than driven by the ledger's expiry,
because `expire_inflight` does not report *which op* it gave up on, and dropping every entry
for a ticket would refuse a legitimate answer to a sibling command on the same position.

**A command that cannot even be sent fails immediately** rather than sitting in flight for the
full timeout while nothing is coming. For an OPEN or PLACE that is a permanent exclusion, which
is the honest reading of "we could not get it out in time".

### Verifying the wiring — the demo run, in this order

Nothing below has been done yet. One slave, on a demo account, `max_lot` small.

1. `dry_run = True` first. Confirm the log still says `[dry-run]` and the command queue
   receives nothing.
2. **Point the slave at a non-demo terminal with `allow_live_trading = False` and confirm the
   refusal** — `ok=False, "account is not a demo"`, and the OPEN permanently excluded. Do this
   *before* anything else works: it is the guard protecting the other fourteen terminals.
3. Back to demo. `dry_run = False`, `copy_sltp = False` on that slave (silences the
   undispatchable warning for run one), `max_lot = 0.02`. Master opens 0.01 lots **with a
   stop**. Confirm, in order: the intent line, the `-> OPEN ... (cmd 1)` dispatch line, the
   worker's result line, a `mapping.db` row carrying the real slave ticket, and magic `260919`
   visible in the slave terminal's Trade tab.
4. Master closes. Confirm CLOSE dispatched, result ok, row gone.
5. Master takes a partial. Confirm REDUCE with the right fraction.
6. Move the master's stop. With `copy_sltp = False` still set from step 3, confirm the action
   is derived and reported as `SKIPPED: copy_sltp is off for this slave`, and that **nothing
   is sent**. Turning it back on is "Verifying the sync" under Stage 3.
7. Kill the slave's worker mid-OPEN. Confirm `expire_inflight` fires at 30s with its
   "will appear as an orphan" error, then restart and confirm the orphan hold engages and `o`
   clears it. This is the loop that startup reconciliation exists to close.

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

## Stage 3 — SL/TP sync and partial closes — **built 2026-09-21, awaiting its demo run**

Mirror stop-loss and take-profit changes after entry, and mirror partial closes
proportionally. Partial closes landed with Stage 2c (`REDUCE`, measured against the master
volume the copy was *sized against*, which is why the mapping table stores it rather than the
tickets alone). This stage is the other half: the two modifications.

| Built | Where |
|---|---|
| `MODIFY_SLTP` — a position's stop and target, `TRADE_ACTION_SLTP` | `executor._modify_sltp` |
| `MODIFY_ORDER` — a resting order's entry price as well, `TRADE_ACTION_MODIFY` | `executor._modify_order` |
| Both kinds added to `protocol.DISPATCHABLE_KINDS` | `protocol.py` |

### What the wire actually requires, and the traps in it

- **`TRADE_ACTION_SLTP` takes five fields** — `action`, `symbol`, `position`, `sl`, `tp` — and
  ignores the rest. Sending `volume` or `type_filling` is not an error, but it is noise, and a
  *recognised* key with the wrong Python type is rejected client-side even when the action
  ignores it.
- **It overwrites both levels every time.** The request is a zero-filled struct, so omitting
  `sl` and sending `sl = 0.0` are the same bytes, and both mean *no stop*. There is no
  "leave unchanged". That is what makes mirroring a master **clearing** its stop fall out of
  the normal path — but it also means the OPEN path's `if cmd.sl:` idiom must never be reused
  here, because it would turn a cleared stop into a no-op.
- **`order_send` rejects an int where it wants a double** and returns `None` *before* it
  contacts the terminal, which `classify()` would then report as a lost connection for a
  request that never left the process. Every level is coerced with `float()`.
- **`TRADE_ACTION_MODIFY` takes seven** — `action`, `order`, `price`, `sl`, `tp`, `type_time`,
  `expiration`. A pending order's **volume cannot be modified at all**, and its type cannot
  either. Omitting `type_time` does *not* preserve the order's expiry: the zero-filled struct
  resets it to `ORDER_TIME_GTC`, which is what `PLACE` used, so the two agree only by
  coincidence. It is sent explicitly so the agreement is deliberate.
- **A modification is idempotent**: a repeat that changes nothing answers `NO_CHANGES`, which
  classifies as success. That is what makes retrying safe.
- The symbol for an SLTP is read from the **terminal**, not from our record, for the same
  reason the close volume is: our record is the master's spelling put through a
  prefix/suffix, while the position knows what it actually is.

### Why the modifications do not reuse `_send`

Every recovery in `Executor._send` is a fabrication for a modification. There is no market
price to re-read on a requote — these prices are the master's, and repricing a resting order
would drag it onto the market — and no filling mode to re-negotiate, because nothing is being
filled. Its *result* handling is wrong too: a modification creates no order and no deal, so
MT5 answers with zeroes, which `_send` would report as a fresh slave ticket and a zero-lot
fill. `_send_modification` keeps the demo guard, the attempt budget and the busy-server
backoff, and drops the rest.

### Three bugs this stage had to fix before it could be correct

These were all live before Stage 3 and all made worse by it, because a modification is the
first operation whose belief is a *write* rather than a delete.

- **`ORDER_CHANGED` (10023) was classified as success.** It is a rejection — the order moved
  on between our read and our send. Belief is written from the command rather than read back
  from the terminal, and the diff compares the master against belief and never against the
  slave, so recording a refused level as set means nothing ever looks again. Silent divergence
  is the one failure this design has no recovery for. It is `FATAL` now.
- **`_Session.position()` conflated "gone" with "could not read".** Gone is reported as
  *success* by close, cancel and modify, so a momentary read failure recorded a close that
  never happened. There are three answers now, and `ReadFailure.UNREADABLE` is the third.
- **A failed close or modify was never derived again.** `apply_result` excludes only
  OPEN/PLACE on failure, but `_last` had already recorded the action, so the identical
  decision was suppressed for the rest of the session — a copy stranded with the wrong stop,
  or worse, a close that never happened. `OrderResult.retryable` now distinguishes a
  transient failure (re-derive next tick) from a refusal (stands, loudly, once).

The asymmetry behind that last one is worth keeping: **an OPEN is an entry and goes stale, but
a close or a modify is a target state and does not.** A close is more urgent for being late,
not less. So OPEN and PLACE are still excluded permanently on any failure, and nothing else is.

### Two policy questions this stage deliberately did not answer

Both are trading decisions rather than implementation ones, and both should be settled the way
`no_stop_policy` was — explicitly, per slave, before they can bite.

1. **A stop the slave's broker will not accept.** Brokers declare a minimum distance from the
   market (`SYMBOL_TRADE_STOPS_LEVEL`) and a band inside which levels cannot be touched at all
   (`SYMBOL_TRADE_FREEZE_LEVEL`). A master moving its stop to breakeven while price is a pip
   away is refused with `INVALID_STOPS`, and the copy keeps its old stop. The options are:
   leave it (what happens today — the refusal is surfaced naming the symbol and both levels,
   and any *different* later stop is sent normally); *widen* the stop to the broker's minimum, which
   silently puts more at risk than the trade was sized for; or refuse the whole copy. Widening
   is what MQL5's own sample does, and it is exactly the kind of silent adjustment the
   volume rule ("below the minimum is a refusal, never a round-up") already rejects — so it
   should not be added without being asked for. **Nothing pre-checks the level**, deliberately:
   a second source of truth that can disagree with the server is its own failure mode.
2. **A master that clears its stop.** Today it is mirrored, and the action carries
   `[UNPROTECTED: removes the slave's stop]`. Mirroring is the contract, and the alternative
   leaves the copy exiting where the master does not — but it does mean a slave set to
   `no_stop_policy: SKIP`, which refuses to *open* an unstopped copy, will still have one
   *disarmed*. On a reversed slave it is stranger still: `effective_levels` swaps the pair, so
   it is the master removing its **take-profit** that disarms the copy.

### Known and accepted, for now

- **A refused level pair is refused for the life of that position, not just for now.** `_emit`
  de-duplicates actions by value, so an action the broker rejected stays in `_last` and an
  identical one later is dropped silently. Concretely: the master moves its stop to breakeven,
  the broker refuses it as too close, the master moves the stop away and then back to that
  same breakeven price — the second attempt never leaves the machine, even though the market
  may since have moved far enough for it to be accepted. Any *other* level is sent normally,
  and the one ERROR at the time of the refusal is the only notice. Fixing it properly means a
  per-key retry budget with backoff rather than the current boolean, which is a bigger change
  than Stage 3 should carry.
- **A copy that is gone while the master still holds it.** The executor reports the
  modification as a success with `the target is gone`, at WARNING, and belief converges — the
  alternative is one ERROR per distinct stop value for as long as the master keeps trailing.
  The row is cleared by the master's eventual CLOSE, which finds the position already gone and
  reports that as a success too. The cost is that belief briefly describes a position that does
  not exist.
- **Commands execute inline on the worker's poll thread** (`worker.py`'s `while running`
  loop), so a fast-trailing master delays that slave's snapshots by up to `DEADLINE_S` per
  command. Bounded in practice, because only one command per `(slave, ticket, op)` is in
  flight at a time — but a slave whose snapshots go quiet for `heartbeat_timeout_s` (15s) is
  held and then re-baselined, so this is the thing to watch first if a trailing master ever
  causes trouble.

### Verifying the sync — the demo run, in this order

Continues from "Verifying the wiring", same one demo slave, `max_lot` small. Set
`copy_sltp = True` (the default) for this run.

1. Master opens with a stop. Confirm the copy opens and `mapping.db` records `synced_sl`.
2. **Move the master's stop.** Confirm, in order: the intent line carrying the new level, the
   `-> MODIFY_SLTP ... (cmd N)` dispatch, a result line reading `MODIFY_SLTP #… <symbol>
   succeeded` — *not* `filled 0` — the new stop visible on the slave's position in the
   terminal, and `synced_sl` updated in `mapping.db`.
3. Move it twice in quick succession. Confirm only one command goes out until the first is
   answered, and that the **second** value is the one that ends up on the slave.
4. Move the master's take-profit. Same, and confirm the stop is not disturbed.
5. **Set the master's stop to something inside the broker's minimum distance** (a few points
   from the market). Confirm one `INVALID_STOPS` failure naming the symbol and both levels,
   that it is not retried at that value, and that a *different* stop afterwards is sent
   normally.
6. **Clear the master's stop entirely.** Confirm the action carries `[UNPROTECTED: removes the
   slave's stop]` and that the slave's stop is actually removed. Decide, seeing it, whether
   that is the behaviour you want — see the policy question above.
7. Close the slave's copy by hand while the master still holds it, then move the master's
   stop. Confirm one WARNING that the slave position is gone, a result that is *not* an error,
   and that the master's eventual close tidies the row away.
8. With `copy_pending = True`: place a limit on the master, then **move its entry price**.
   Confirm `MODIFY_ORDER` goes out with the new price, that the resting order moves on the
   slave, and that `type_time` left the order GTC rather than giving it an expiry.
9. Set `copy_sltp = False` and move the master's stop. Confirm the action is derived and
   reported as skipped, and that nothing is sent.

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

The `order_send` calls are written now: `PLACE` and `CANCEL` with Stage 2c, `MODIFY_ORDER`
with Stage 3. None of the three has been run against a broker. Note `zele-mt5` had no
`TRADE_ACTION_MODIFY` path at all — it can modify a position's SL/TP but not a pending order's
entry price — so that half had no prior art to copy and was written fresh against the MQL5
field list.

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
