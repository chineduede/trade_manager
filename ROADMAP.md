# Roadmap

Stage 1 (discovery, designation, supervision) is complete and user-accepted. It contains no
trading logic. This document is the plan for everything that does.

## Where the project is — 2026-10-04

**Everything built so far has been seen working against a real broker.** The live
verification backlog below is closed, and so are the features built after it: the terminal
detail page (demo-verified 2026-10-03), pausing a slave (accepted 2026-10-03) and breakeven
(demo-verified 2026-10-04). The TUI was redesigned and the classic UI removed on 2026-10-04;
the redesign is now the only UI (`master`, 1046 tests as of 2026-10-04).

**What's left is unscoped.** "Future features — not yet scoped" at the end holds the parked
ideas — prop-firm guards (with margin checks), posting trades to Telegram — each of which
needs its policy settled before anything is built.

**An order first left this machine on 2026-09-21:** a demo OPEN (EURGBPp, 13.77 lots) and its
CLOSE both filled on a real demo account (IC Markets Global master → BlackBull Markets slave).
`dry_run` is **false** in the live config; the executor's demo-only guard sits underneath every
send.

| Stage | State |
|---|---|
| 1 — discovery, designation, supervision | accepted |
| 2a — the diff engine, dry run | **validated on a demo master 2026-09-17** |
| 2b — execution primitives | complete (`sizing`, `symbols`, `execution`, `executor`) |
| 2c — live copying | **"Verifying the wiring" checklist closed 2026-10-01** — OPEN, CLOSE, dry-run check, partial close, `copy_sltp` skip, non-default lot-step symbol, and `expire_inflight` all confirmed; the orphan-hold/`o`-ack path specifically rests on unit coverage, not a forced live race |
| 3 — SL/TP sync, partial closes | **"Verifying the sync" closed 2026-10-01**, including the unprotected-stop policy decision |
| 4 — pending orders | **closed 2026-10-01** — PLACE, MODIFY_ORDER, CANCEL, and a live fill-into-position handoff all confirmed |
| 5 — reverse and filtered copying | **inversion closed 2026-10-01** — swapped SL/TP sizing and resting-order kind flip both confirmed live |

**"Verifying the wiring" (Stage 2c's demo-run checklist) is closed as of 2026-10-01.** The
demo-only refusal is confirmed against a real live account (see the backlog below), and so are
the `dry_run` sanity check, a partial close (`REDUCE`), the `copy_sltp = False` skip, a
non-default lot-step symbol (US30), and the mid-OPEN worker-kill via `expire_inflight` (30s
timeout, correct exclusion) — the orphan-hold/`o`-ack half of that last one rests on unit
coverage rather than a forced live race, which proved impractical to hit by hand.

**"Verifying the sync" (Stage 3) is closed as of 2026-10-01**, also on BlackBull:
`MODIFY_SLTP` dispatch with `synced_sl`/`synced_tp` actually updated, a take-profit move
leaving the stop untouched, a slave copy closed by hand mid-session (one WARNING, row tidied
away on the master's eventual close), the settings-edit-gives-up-held-copies rule (a correction
to how this item was originally phrased — see the backlog), two rapid stop moves and
`INVALID_STOPS` (both accepted on unit coverage / code reasoning rather than a forced live
hit), and clearing the master's stop entirely — confirmed mirroring onto the slave unprotected,
**and decided that is the wanted behaviour**, no change needed.

**Stage 4 (pending orders) is also closed as of 2026-10-01**, all on BlackBull/EURUSDp: `PLACE`,
`MODIFY_ORDER`, `CANCEL` all dispatch correctly, and — the one item that needed a genuinely
live re-check, since it was previously only proven in dry-run — a pending order filling on the
master logs `ORDER_FILLED` and hands off to position tracking with no duplicate copy.

**Stage 5 (inversion) is also closed as of 2026-10-01** — `invert` turned on for BlackBull
mid-session, with no copies held at the time so the settings-edit give-up rule didn't trigger.
Both a reversed copy's swapped SL/TP sizing and a reversed resting order's kind flip
(`BUY_LIMIT → SELL_STOP`) confirmed live.

**The TUI backlog is also closed as of 2026-10-01**: the settings editor's orphan-confirmation
flow (confirmed as a side effect of the "copy_sltp mid-stream" test) and the live-override
dialog (refusal on a wrong name, correct arm, status bar, one-press disarm, all logged at
WARNING) both run correctly against a real terminal.

**Closing a terminal the copier launched is confirmed live too**, previously only unit-tested:
a terminal already running at connect time is correctly never claimed or closed (ownership is
re-derived fresh every `connect()`, not carried over from an earlier worker instance), and a
freshly-launched one closes correctly on unassign, twice, across several back-to-back restart
cycles with no pid confusion between them.

**The last gap in the live-verification backlog — what removing a role does to open copies —
was decided and built on 2026-10-02 (option "A": ask, then give them up).** Tracing it turned up
a real hazard as well as a missing question: copy records are filed by master *ticket*, not by
master, and unassigning kept them. So after switching masters, the first tick under the new one
found none of the old tickets and proposed a CLOSE for every copy on every slave — real closes,
in live mode, that nobody asked for. Now every change that strands copies goes through the same
rule settings edits already used (`Engine.copies_at_stake` / `Engine.unassign` /
`apply_settings`): unassigning a slave stakes its copies; unassigning the master (which sweeps
its slaves) or disabling it stakes every slave's. The person sees them, per slave, and has to
agree; agreeing gives them up as acknowledged orphans, so no record outlives its master. Unit
covered, and **confirmed on a demo run 2026-10-02**: unassign a master over a live copy, assign
a new master, nothing closes. Recording the master on each copy record (so a foreign ticket can
never match) is not done: multi-master was dropped from the roadmap on 2026-10-02, and with one
master at a time the give-up rule above is what keeps tickets unambiguous.

Everything in "Safety rails" under Stage 2c is now built — the demo-only guard landed with
Stage 2b deliberately, in the same commit as the first code able to send.

Read `README.md` first for the architecture and the constraints it follows from.

## Live verification backlog — 2026-10-01

Nothing here has a broker result yet except the items marked done. Work top to bottom — later
items assume earlier ones hold. Where a procedure is already written out step-by-step elsewhere
in this file, it's linked by name instead of repeated here.

**Safety guard, before anything else**
- [x] **Demo-only refusal — confirmed 2026-10-01, against a real live account.** Eightcap
  Global (a genuine live account, `trade_mode=2`) was temporarily assigned as a second slave
  alongside BlackBull, `allow_live_trading` left at its default `False`. The master's next OPEN
  dispatched to both: BlackBull filled normally, Eightcap was refused at the guard —
  `OPEN #1980488017 FAILED: account is not a demo (trade_mode=2); refused`, followed by
  `open for #1980488017 on 492CD019 failed, and will not be retried` (the decline-is-final
  behaviour, confirmed too). `mapping.db` afterward held exactly one row (BlackBull's copy) and
  zero for Eightcap — nothing was recorded, nothing was sent. Eightcap's own Trade tab was
  checked by hand and showed nothing. Eightcap was unassigned again afterward, back to the one
  proven demo slave.

**Stage 2c — "Verifying the wiring"**
- [x] OPEN with a stop dispatches, fills, and is recorded — demo OPEN filled 2026-09-21.
- [x] CLOSE dispatches and the row is removed — demo CLOSE filled 2026-09-21.
- [x] `dry_run = True` sanity check — confirmed 2026-10-01: log tagged `[dry-run]`, command
  queue stayed empty, nothing dispatched.
- [x] Partial close (`REDUCE`) with the right fraction — confirmed 2026-10-01, BlackBull: the
  reduce dispatched and filled accurately against the master's partial.
- [x] Moving the master's stop with `copy_sltp = False` is derived, reported `SKIPPED`, and
  sends nothing — confirmed 2026-10-01, BlackBull: log showed
  `-- SKIPPED: copy_sltp is off for this slave`, slave stop untouched.
- [x] Kill the slave worker mid-OPEN — partially confirmed 2026-10-01, BlackBull.
  `expire_inflight` fires exactly at 30s with `OPEN for #<ticket> on 16D9C170 was never
  answered after 30s; giving up -- if it reached the broker it will appear as an orphan on the
  next startup reconciliation`, and the ticket is excluded as designed. The orphan-hold/`o`-ack
  half needs the kill to land in the gap between the broker accepting the order and the result
  reaching the engine — too narrow to hit by hand with Task Manager; tried a few times, always
  landed before the send instead (`0 tracked, 0 orphans, 0 stale`). Left to unit coverage
  (`test_reconcile.py`, `test_store.py`, `test_tui.py`) rather than forced live.
- [x] Open on a symbol with a non-default lot step — confirmed 2026-10-01, BlackBull, US30
  (0.1 step): `#1980728322 on US30: 37.4 lots (raw 37.4813)`, filled at `37.4`. `quantize_volume()`
  correctly floored to one decimal against BlackBull's real `symbol_info`, not a 0.01 grid.
  Getting here first surfaced and fixed two real bugs on the way (hunting for a symbol to test
  with, on EURUSDp/EURAUDp): `symbol_facts()` trusted a non-`None` `symbol_info` even when
  `.visible` was false (some brokers zero out fields like the filling mask on a hidden symbol
  rather than answering `None` — now checked and reselected per the same pattern another app
  uses), and a freshly-selected symbol's first `symbol_info_tick()` could come back empty for
  anywhere from seconds to 30+ minutes even while the terminal's own Market Watch showed it
  ticking live — fatal before, since a failed OPEN is permanently excluded. `_first_tick()` in
  `executor.py` now retries once after a 2s wait before giving up.

**Stage 3 — "Verifying the sync"**
- [x] `MODIFY_SLTP` on a stop move — confirmed 2026-10-01, BlackBull, US30 (`#1980780681`):
  dispatched, logged `MODIFY_SLTP #1980780681 US30 succeeded` (not a misleading "filled 0"),
  and `mapping.db` shows `synced_sl`/`synced_tp` updated (`updated_at` ~11.7s past
  `opened_at`, matching the OPEN-to-modify gap in the log).
- [x] Two rapid stop moves: only one in flight, the second value wins — attempted live
  2026-10-01 on `#1980780681` (US30), but each move fully resolved (sub-second round trip)
  before the next was made, so no genuine overlap was ever created; forcing two modifications
  to actually race through MT5's own Modify dialog is impractical by hand, the same problem as
  #6's orphan timing. Accepted on unit coverage instead:
  `test_a_changed_decision_is_still_withheld_while_in_flight` (`test_reconcile.py:973`) proves
  the dedup-while-in-flight logic directly.
- [x] Take-profit move leaves the stop untouched — confirmed 2026-10-01, BlackBull, US30
  (`#1980780681`): `sl 50655.9 tp 51375.2` → `sl 50655.9 tp 51249.4`, stop identical across
  both moves, only the target changed, both succeeded.
- [x] A stop inside the broker's minimum distance: one `INVALID_STOPS`, not retried at that
  value, a different value afterwards sent normally. Attempted live 2026-10-01 but never
  actually triggered — no `INVALID_STOPS` anywhere in `copier.log`, because the "p"-suffixed
  symbols used so far run `SYMBOL_TRADE_STOPS_LEVEL = 0` (no distance is ever rejected) and a
  symbol with a genuine non-zero stop level wasn't hunted down. Accepted without forcing it:
  `Retcode.INVALID_STOPS` (10016) is classified `Outcome.FATAL` (`execution.py:184`), which
  `_send_modification` breaks on immediately rather than retrying (`executor.py:488-491`, only
  `BACKOFF` loops), and the belief is left untouched since `apply_result` only writes
  `synced_sl`/`synced_tp` on success — so "not retried at that value" and "a different value
  sent normally afterwards" both follow directly from logic already exercised by the other
  MODIFY_SLTP tests above, not from anything specific to this one retcode.
- [x] Clearing the master's stop entirely — confirmed 2026-10-01, BlackBull, US30
  (`#1980787161`): `MODIFY_SLTP #1980787161 US30p buy sl 0 tp 51215.2
  [UNPROTECTED: removes the slave's stop]`, dispatched and succeeded. **Policy question
  decided 2026-10-01**: mirroring the master's cleared stop onto the slave, unprotected, is the
  wanted behaviour — no change needed. The `[UNPROTECTED: ...]` tag stays purely informational.
- [x] Slave copy closed by hand mid-session — confirmed 2026-10-01, BlackBull (`#1980813755`):
  moving the master's stop against a hand-closed slave position logged a WARNING (not an
  error) — `slave position 217399440 is gone, so its stops cannot follow the master` — and the
  row survived until the master's own eventual CLOSE, at which point `store.forget()` removed
  it (`already gone` / `filled 0`).
- [x] Settings edit on a terminal holding an open copy — confirmed 2026-10-01, BlackBull
  (`#1980814744`), rewritten from how this item was originally phrased. Toggling `copy_sltp`
  while a copy is open does not narrowly skip future SL/TP sync on that one ticket — per
  `engine.py`'s `apply_settings`, *any* settings edit on a terminal holding open copies gives
  up **all** of them: `orphaned on BlackBull Markets: position #217399610 US30 28.3 lots
  (master #1980814744); settings changed, so it is no longer copied, closed or moved, and
  stays open`. Deliberate and blanket, not `copy_sltp`-specific — a single rule instead of
  reasoning per-field about what's safe to change live. The narrower "just this one action is
  skipped, tracking otherwise continues" behavior this item originally described is not
  reachable through the settings editor at all; this is what actually happens instead.

**Stage 4 — pending orders — closed 2026-10-01**
- [x] `PLACE`: master places a limit with `copy_pending = True`, resting order appears on the
  slave — confirmed, BlackBull, EURUSDp (`#1980838270`): dispatched, resting.
- [x] `MODIFY_ORDER`: master moves the resting order's entry price, slave order moves —
  confirmed, BlackBull, EURUSDp (`#1980827726`): entry held, `sl` moved, dispatched,
  `MODIFY_ORDER #1980827726 EURUSDp succeeded`. `type_time` staying GTC rests on the code
  (`executor.py`'s `_modify_order` sends `type_time=0` explicitly) rather than a manual check
  of the order's expiry in the terminal.
- [x] `CANCEL`: master cancels the resting order, slave order is removed — confirmed, several
  times (`#1980827726`, `#1980829575`, `#1980830508`): dispatched, removed each time.
- [x] A pending order fills on the master: logs `ORDER_FILLED` (not `CANCEL`), hands off to
  position tracking without a duplicate copy — confirmed live 2026-10-01 (previously only
  dry-run, 2026-09-17), BlackBull, EURUSDp (`#1980838270`):
  ```
  PLACE        #1980838270 ... filled 35.27 @1.12416   (resting)
  ORDER_FILLED #1980838270 EURUSDp buy_limit  -- SKIPPED: the slave's own order fills by itself
  CLOSE        #1980838270 EURUSDp buy
  ```
  The next action derived for that ticket was a CLOSE, not a second OPEN — the row correctly
  flipped from order-tracking to position-tracking internally, no duplicate copy dispatched.

**Stage 5 — inversion — closed 2026-10-01**, BlackBull/EURUSDp, `invert` turned on mid-session
(no copies were held at the time, so no give-up was triggered).
- [x] A reversed copy sizes off the master's take-profit and opens with the swapped levels —
  confirmed. `effective_levels()` (`reconcile.py:271`) is unconditional (`return (tp, sl) if
  cfg.invert else (sl, tp)`), so this follows mechanically once `invert=True`; cross-checked
  directly against the master's own Trade tab — its SL became the slave's TP and vice versa, as
  watched live, e.g. `OPEN #1980847893 EURUSDp buy @1.12418 sl 1.11684 tp 1.13128`.
- [x] A reversed resting order flips kind as well as direction — confirmed. The master placed
  its usual limit orders throughout (`buy_limit`/`sell_limit`, same as every earlier
  non-inverted test), but BlackBull received **stop** orders instead once inverted:
  `PLACE #1980852545 EURUSDp sell_stop`, `#1980854633 sell_stop`, `#1980854854 sell_stop`
  (dispatched, filled/resting, cancelled cleanly) — matching `_INVERTED_ORDER_TYPE`
  (`reconcile.py:250`): `BUY_LIMIT → SELL_STOP`, preserving which side of the market the order
  rests on rather than producing a price the broker would reject.

**TUI, run against a real terminal, not headless**
- [x] The settings editor (`e`): every field saves correctly, and the orphan-confirmation flow
  actually blocks and resumes as designed — confirmed 2026-10-01, BlackBull, as a side effect
  of the "copy_sltp mid-stream" test above. `apply_settings()` refuses outright
  (`"<N> copies would be orphaned; that has to be confirmed"`, `engine.py:981`) unless
  resubmitted with the exact held tickets as `orphan`; the log shows both the give-up
  (`orphaned on BlackBull Markets: ...`) and the save completing
  (`settings changed for BlackBull Markets (16D9C170): copy_sltp on -> off`), which only
  happens if the editor caught the refusal, showed the held copy, and resubmitted correctly —
  not a silent bypass.
- [x] The live-override dialog (`l`) — confirmed 2026-10-01, BlackBull. Typing the name wrong
  on purpose was refused correctly (`that is not the name: type BlackBull Markets to confirm`,
  nothing changed); typing it correctly armed it, the status bar showed
  `NON-DEMO ALLOWED: BlackBull Markets`; OFF disarmed in one press, no typing needed, segment
  disappeared. Logged at WARNING both ways: `settings changed for BlackBull Markets
  (16D9C170): allow_live_trading off -> on` / `... on -> off`.

**Landed 2026-10-01, confirmed live the same day**
- [x] Closing a terminal the copier launched, on `stop()` and on unassign — confirmed,
  BlackBull. Unassigning while its terminal was already running (left over from an earlier
  restart) correctly closed nothing: `_launched_pid` is re-derived fresh on every `connect()`
  (`worker.py:69-74`), so a terminal already alive at connect time is never claimed, matching
  "a terminal the user already had open is left alone" even though it got there via a prior
  restart rather than the user opening it by hand. With the terminal fully closed first, then
  reassigned (fresh launch) and unassigned again, the close fired correctly, twice:
  `closed terminal pid=78224 (we launched it)` and `closed terminal pid=45028 (we launched
  it)` — each a fresh, correctly-tracked pid, no confusion across the several back-to-back
  spawn/stop/respawn cycles run in the process, which also covers "a restart mid-run still
  leaves things sane." Previously unit-tested only, against a faked `psutil.Process`
  (commit `dcd4e60`).

**Closed 2026-10-02**
- [x] What disabling or unassigning the **master** does to its slaves' open copies — decided
  and built (`cef19d2`): every slave's copies are staked, shown, and given up as acknowledged
  orphans only once the person agrees. Confirmed on a demo run; see "Where the project is".

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

## Per-terminal settings editor — **built, 2026-09-24**

**Press `e` on a terminal.** A modal covering every per-slave setting except the three below:
sizing (`sizing_mode`, `risk_percent`, `risk_cash`, `max_lot`, commission, slippage, max
deviation), the no-stop policy and its multiple, `enabled`, `invert`, the three copy
flags, and the symbol section (`symbol_prefix`, `symbol_suffix`, `symbol_map`, the whitelist and
the blacklist). A blank `max_lot` means no ceiling. Ctrl+S or Enter saves, Esc cancels. The pure
half is `settings_edit.py`; the form is `tui/settings_screen.py`; the guarded swap is
`Engine.apply_settings`.

**Not editable in this form, on purpose:** `allow_live_trading` (it has its own key and dialog,
below), `label`, `role`, and the discovery fields. A test
(`test_every_setting_is_either_editable_or_deliberately_not`) makes a setting added to the model
without a decision about the form fail, so the list cannot drift.

**Dropped by the user, 2026-09-24: the live mapping tester.** It was planned as the most valuable
part of the symbol section, and it would have needed the worker to send its symbol list to the
UI. The symbol fields are plain text and a wrong mapping shows itself as a refusal at trade time.
If that ever costs a trade again, the cheap design is for the worker to push its list once per
connection and for the UI to run the pure `symbols.resolve()` against it, not a command and a
reply. It works only for a connected slave.

**Decisions taken building it, none of them obvious from the code:**

- **`allow_live_trading` is not a row in the form** — it is the demo-only guard's override, and a
  guard that a stray keypress can lift is not a guard. Until 2026-09-24 it was a hand edit with
  the app closed; the user then asked for it in the TUI, which reversed that. What was kept is
  the property that mattered: the form is saved with `ctrl+s` or Enter, so the override is **its
  own key (`l`) and its own dialog** (`tui/live_override_screen.py`), where nothing can lift it
  by accident. The decisions in it are below.
- **The override's two directions are deliberately unequal** (`settings_edit.set_live_override`).
  **On** needs the terminal's *name* typed out in full — the name and not a fixed word, because
  the mistake being guarded against is the wrong terminal, and a fixed word can be typed without
  looking at which one is selected. Case and surrounding space are forgiven; nothing else is,
  and a caller that passes nothing is refused rather than waved through. **Off** takes one
  press, because it is the safe direction and the one someone reaches for in a hurry.
- **Only a slave can have the guard lifted.** The guard only ever stands between a slave and a
  broker, and `AppConfig.set_role` leaves `allow_live_trading` alone, so one lifted on a
  terminal that is not a slave would ride along, unseen, on the day it is made one. Turning it
  *off* works on any role, for the same reason: a terminal whose role changed with the guard
  still lifted must be able to have it put back.
- **The guard also blocks closes and stop moves,** not only opens (`Executor._execute` checks it
  before every op). So putting it back while copies are open on a non-demo account would leave
  them unmanaged — which is what any settings change now does to them anyway, so it goes through
  the same question as every other edit (below). Turning it on later does not retroactively copy
  a trade declined while it was off: a decline is final.
- **A lifted guard is visible.** The change is logged at WARNING rather than INFO, and the status
  bar says `NON-DEMO ALLOWED: <names>` while any terminal has it on — early in the line, where
  an 80-column terminal does not cut it off, and for a terminal that is no longer a slave too.
- **Not done: clearing the flag when a terminal stops being a slave.** It would close the
  leftover-flag hole at the source, but it changes `set_role`, which was not asked for. The
  status bar marker is what makes the leftover visible instead.
- **`label` is not editable because discovery owns it.** `merge_discovery` sets it on every
  rescan and at every start, so an edit would silently revert the next time the app ran.
- **An edit to a terminal that holds copies gives them up, after being asked — for every setting
  alike (2026-09-24, the user's design).** It replaced a rule per setting: `invert` used to be
  refused under an open copy, `enabled` off used to be warned about, and every other setting
  was assumed safe. Now `OrphanConfirmScreen` comes first whenever the terminal has a believed
  position or resting order. It lists the copies, says they stay open and will no longer be
  closed, moved or partially mirrored, and **rests on Cancel**. Declining changes nothing and
  leaves the form open with the edit still typed. Agreeing calls
  `Engine.apply_settings(new, orphan=<the master tickets that were shown>)`, which — under the
  lock the drain thread dispatches under, so nothing can slip in between — refuses if the store
  is unreadable ("we could not tell" is not empty), refuses if what is held *now* is not exactly
  what was agreed to (a copy that opened while the person was deciding is one they were not
  told about), refuses while a command to that slave is in flight (a result would find no row to
  update), and only then gives the copies up and swaps. The override dialog goes through the
  same path. The list is read from the **persisted store**, not the ledger's memory: the store
  opens lazily, so until the first reconcile the ledger would say "nothing" about a slave whose
  `mapping.db` rows are alive.
- **What "orphaned" means, mechanically** (`CopyLedger.abandon`, `Engine._give_up_copies`). The
  rows are forgotten and **each master ticket is excluded, permanently**. Forgetting alone is a
  trap: the master still holds the trade, so a row-less, unexcluded ticket reads as one that has
  just appeared and would be copied a second time. (The ledger's de-duplication hides this only
  while the re-derived action is identical — the moment the master moves a stop, the duplicate
  goes out; the tests are written to expose that.) A resting order is excluded as an order **and
  as a position**, because its fill takes the order's own ticket. Nothing is sent to the broker.
  Each copy is registered as an orphan that is **already acknowledged** — the person agreed
  in advance — so it shows in the panel like a startup orphan (open, unmanaged, not closed by the
  master) but the slave is *not* held and carries on under the new settings. After a restart
  startup reconciliation finds them by the usual route and holds until they are acknowledged
  again with `o`. A dry run does the same to its simulated copies, worded as such, and reports no
  orphans, since their tickets are not real.
- **Accepted cost: even a trivial edit orphans every open copy.** That is the price of one rule
  instead of a list, and it was chosen with the cost in view. If it bites, the narrowing is to
  skip the question for settings that only govern *new* trades (sizing, symbols, filters) — not
  done, because that is the list again. `enabled` off is no longer resumable either: its copies
  are orphaned, so turning it back on does not pick them up.
- **Symbol settings only steer copies opened from now on.** A modification resolves the symbol
  "only so the result line names an instrument" (`executor.py`), so changing a suffix cannot
  redirect a stop move on a copy that already exists.
- **The symbol map is one `MASTER=BROKER` pair per line**, separated by lines, commas or spaces,
  with spaces around the `=` forgiven. Two entries for one symbol that disagree are refused at
  entry -- the model refuses them on load, and the form is where the user can fix it. The
  filters are lists of master names, matched case-insensitively and exactly, with no wildcards
  (`reconcile._filter_reason`); the help text says adding one does not copy a trade that was
  already declined.
- **Errors sit under their field, not beside it,** so a wide field leaves no room to squeeze a
  message, and they take no space until there is one to show.
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
- **Three saves earn a warning**, because they were a deliberate hand edit and are now one
  keypress: enabling `LOT_MULTIPLE` (the copy opens unprotected), removing a `max_lot`, and
  disabling a slave that has copies open.
- **Every saved change is logged** (`settings changed for <label>: risk_percent 1 -> 0.5`).

The engine needed no change: it reads a slave's settings from the shared `AppConfig` on every
tick, and workers are handed none at spawn, so an edit reaches the next copied trade and
nothing already open. That is the "Safe" behaviour the table below asks for.

The rest of this section is the original write-up, kept for the reasoning behind it.

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
  symbols.** The plan was for the symbol section to *test* a mapping against the live symbol
  list and show what it resolves to and how (`EXACT` / `CONFIGURED` / `SCAN`), turning a failed
  trade into a pre-flight check. **The user dropped that on 2026-09-24**; see the editor section
  above for what was decided instead.
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
| `invert` | Was refused under a copy (a believed copy records the direction it opened in, so flipping the flag would leave the ledger describing the opposite of reality). **Superseded 2026-09-24:** every edit under a copy now asks, then gives the copies up — see the settings-editor decisions above. |
| `enabled` off | **Superseded the same way.** It used to keep the rows and match them back up on re-enable; now the copies are orphaned and are not picked up again. |
| Symbol prefix, suffix, map | Safe. They steer copies opened from now on; a modification does not resolve the symbol to act. |

*The "Safe" rows above describe what each setting does to a copy that stays tracked. Since
2026-09-24 nothing stays tracked across an edit, so they are now about what applies to new trades.*

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

## Stage 2c — live open/close on demo — **built, demo-run checklist closed 2026-10-01**

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

### Two policy questions this stage deliberately did not answer (both now settled)

Both are trading decisions rather than implementation ones, and both were settled the way
`no_stop_policy` was — explicitly, before they could bite.

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

   **Settled 2026-09-24, by the user: reject and log.** A stop the broker refuses is a refusal
   of that order, reported at ERROR naming the symbol and the levels — never widened, never
   sent without its stop, never retried at the same value. For an OPEN or a PLACE that means
   no copy at all; for a modification of an open copy it means the copy keeps the stop it has.
   Closing an open copy because a later stop was refused was not asked for and is not done.
2. **A master that clears its stop.** Mirrored, and the action carries
   `[UNPROTECTED: removes the slave's stop]`. Mirroring is the contract, and the alternative
   leaves the copy exiting where the master does not — but it does mean a slave set to
   `no_stop_policy: SKIP`, which refuses to *open* an unstopped copy, will still have one
   *disarmed*. On a reversed slave it is stranger still: `effective_levels` swaps the pair, so
   it is the master removing its **take-profit** that disarms the copy.

   **Settled 2026-09-27, by the user: keep mirroring it, unprotected flag and all.** The
   disarm-instead-of-mirror alternative was considered and explicitly declined — an already-open
   copy is never left holding a stop the master no longer has, at the cost of the `SKIP` and
   reversed-slave oddities above, which are accepted rather than solved.

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

## Future features — not yet scoped

Ideas parked for after the live verification backlog is done. None of these has a design yet —
each needs its own policy-then-build-then-demo-verify pass, the same as every stage above,
before it is built. Their UI, if any, lands in the TUI — the web UI was dropped on 2026-10-02 in
favour of making the TUI simpler and better-looking.

**Multi-master support was dropped on 2026-10-02**, by the user. One master at a time stays the
model; `AppConfig`'s one-master rule and `Engine._reconcile()`'s single `self._cfg.master` read
are the design, not a limitation waiting to be lifted.

- **Terminal detail page — planned and built 2026-10-02, demo-verified 2026-10-03 by the user.** More detail per terminal, so it can be
  shown visually. The user's choices: live per-position P/L, risk/margin gauges, and copy-health
  stats (equity history was not picked). They live on **a new per-terminal page** (Enter on a
  terminal row opens it; Esc goes back), and the history is **kept for the session only**, in
  bounded in-memory buffers that start empty on every launch.

  **The rule that shapes the data: live values never touch `Position`.** `Position` deliberately
  holds only the fields that stay the same from tick to tick, so the reconciler sees a position
  as unchanged until it really changes. Floating P/L, current price and swap change on every
  tick. They go in a **separate** payload on the `Snapshot`, keyed by ticket, which nothing in
  `reconcile.py` or the ledger reads. Mixing them in would make every tick look like a
  modification.

  *Phase 1, the data (worker → `Snapshot`, no UI) — **built 2026-10-02**, unit-tested; nothing
  displays it yet:*
  - `PositionLive(ticket, price_current, profit, swap)`, as `Snapshot.live_positions`, with the
    usual rule: `None` means the read failed, not "no positions". This adds no MT5 calls:
    `positions_get()` already returns these fields, and today they are thrown away.
  - From `account_info()`: `margin`, `margin_level`, `profit` (`margin_free` is already carried,
    but nothing uses it yet).
  - From `terminal_info()`: `ping_last` (microseconds, the terminal's own trade-server ping).
  - Everything is optional and defaults to `None`, so a missing field on an odd broker shows
    as "—" and never fails the snapshot. That differs from a position that won't parse, which
    has to fail the whole read.

  *Phase 2, copy-health stats (engine-side, session only) — **built 2026-10-02** in
  `copy_stats.py`, unit-tested; nothing displays it yet. As built: `OrderResult` carries `point`
  on a filled OPEN/PLACE; uptime is the current connection's length, not a ratio; a command the
  queue refused counts as a transient failure with no latency; late answers are counted too:*
  - **Latency** = when the result arrived − when the command was sent. `_pending_cmd` already
    holds `sent_at`, so this is one subtraction at the place the result is matched. Kept per
    slave in a ring buffer (around 200 samples) for a sparkline, plus last/median/max.
  - **Slippage** on OPEN = the slave's fill `price` against the master's `price_open`, in points.
    That needs the symbol's `point`, which only the worker knows, so `OrderResult` gains `point`
    (or the executor reports the slippage itself). The sign is "against us", not
    buy/sell-relative.
  - **Counters** per slave: filled, refused (FATAL), retried (`attempts > 1`), expired
    (`expire_inflight`), skipped (by reason). These are counts of what the ledger already logs.
  - **Connection uptime**: time spent `CONNECTED` versus anything else since the app launched,
    plus the number of disconnects, taken from the snapshot state changes the engine already
    watches.
  - The UI reads all of this through one read-only `Engine.terminal_stats(terminal_id)` that
    returns a frozen copy. It never gets a reference into the engine's live state, which the
    drain thread changes without a lock against the UI.

  *Phase 3, the page (`tui/terminal_screen.py`, view-model in `terminal_detail.py`) — **built 2026-10-02**, unit- and Pilot-tested, not yet seen against real terminals. As built: Enter opens it (a mouse click never does, so the button cells stay safe), Esc goes back; prices are shown with decimals read off the prices themselves, since the snapshot carries no `digits`:*
  - Header: name, role, demo/live, connection state, ping, balance → equity, and floating P/L
    in colour.
  - **Positions table**: symbol, side, lots, open price, current price, SL/TP and the distance
    to each, swap, and P/L. On a **slave**, each copy sits next to its master position (from
    the mapping store: master ticket, master P/L, and the copy's P/L as a % of the master's).
    On the **master**, the reverse: how many slaves hold each position. Orphans are marked.
  - **Gauges** (Textual `ProgressBar` / bar text): margin used against equity, a margin-level %
    with warn/danger colours, free margin, and **exposure per symbol** (net lots per symbol,
    long against short).
  - **Health**: a latency `Sparkline`, slippage stats, the counters, uptime and disconnects.
    The panel is empty on the master, which receives no commands.
  - It refreshes on the app's existing 0.5s timer, and only while the page is visible.

  *Explicitly not in scope:* money at risk to the stop per position. It needs an
  `order_calc_profit` call per position per tick, inside the worker, which is a
  cost on the latency-critical master loop, so it is left for the margin/prop-firm pass below.
  Also out: any persistence (the user chose session only) and any charting library (Textual's
  own widgets are enough).

  *Verification:* unit tests for each phase (that the live payload leaves `Position` equality
  alone and that `None` is handled; latency/slippage/counters from scripted results; the page
  rendered headless with Textual's `Pilot`). Then a demo run: open, partial-close, move a stop
  and close on the master, and check that the page tracks it against the Trade tab on both
  terminals.
- **Pausing a slave — built 2026-10-03, replacing the `Enabled` setting.** The user found
  `Enabled` in the settings form made little sense: turning it off disconnected the terminal
  *and closed it* (it went down the same `sync()` path as unassigning), and it sat on the master
  too, where it was the only setting left once the slave-only ones were hidden. Now a slave row
  has a ⏸/▶ button (and `p`); the master has no settings form at all. **The user's rule: a pause
  only stops new trades.** A paused slave declines the master's new positions and new resting
  orders (`SkipReason.PAUSED`, final like every decline, so resuming does not adopt what opened
  meanwhile), while closes, partials, SL/TP moves and order modifies/cancels on what it already
  holds carry on; it stays connected and nothing is given up. Saved in `config.json`
  (`paused`), cleared by any role change. `enabled` stays in the schema only so older config
  files still load. Unit and Pilot tested; the user accepted it in the TUI on 2026-10-03.

  Found on the way: turning a terminal off and on left it **"stale" for good**. A stopped
  worker's last snapshots were drained after it was reaped, and its high `seq` made the
  out-of-order check refuse every snapshot from the replacement, which counts from 0. Snapshots
  now carry `worker_pid`, and the drain drops any not from the terminal's current worker.
- **Breakeven — planned and built 2026-10-03; unit- and Pilot-tested, demo-verified 2026-10-04 by the user.** Move a copy's stop to entry (± buffer) once price has
  moved some distance. The user's choices: the trigger is **per slave, either a distance in
  pips or a multiple of R**; the stop goes to **entry plus a buffer in pips**; both are
  measured on **the slave's own position** (its own fill and current price, so slippage and
  reversed copies come out right); **slaves only**, never the master's own trades.

  **The rule that shapes it: a breakeven is a floor, not a one-off move.** The diff compares
  the master's stop against `synced_sl` (the stop last sent), so a stop moved by anything
  else is put back on the next tick. Instead, a triggered copy records `breakeven_sl`, and
  `reconcile_slave` sends the *more protective* of that floor and the master's stop. A master
  stop that moves in a more protective direction (trailing) is still copied. One that moves
  back toward a loss, or is cleared, is ignored for that copy. The floor lives on the copy
  record (SQLite), so it survives a restart, and the reconciler stays pure: it reads belief,
  never live prices.

  **Revised 2026-10-03, after the first demo run (the user's call): no pips anywhere.** On a
  2-digit symbol the old rule made a pip 1 point, so on BTCUSD the default 1-pip buffer was one
  cent and a 10-pip trigger was ten cents. Now:
  - **The buffer is the slave's costs:** entry + the round-turn commission as a price distance
    (`commission_per_lot_round_turn` ÷ the value of a 1.0 price move per lot) + the
    `slippage_buffer_points`. The value per price move is asked of the slave's terminal on each
    fill (`order_calc_profit` over 1000 points, the call sizing already uses), carried as
    `OrderResult.price_value`, and kept on the copy record. The conversion runs at trigger
    time, so a commission edit applies to open copies. The stop is rounded *away* from entry.
    The user knows the commission setting doesn't apply to every symbol; there the stop just
    sits further out. A copy opened before `price_value` was kept can't be priced when a
    commission is set, so it never triggers, and the engine says so once with a WARNING.
  - **The trigger is a fraction of R** (`breakeven_trigger_r`, e.g. 0.3 or 1), **or a raw price
    per position** (`B`, unchanged). `breakeven_trigger_pips` and `breakeven_buffer_pips` are
    gone. They're stripped on load, and a saved `breakeven_mode: "pips"` loads as `off` with a
    WARNING, never guessed into some R.

  The original plan follows; where it says pips, read the revision above.

  *Settings (per slave, "Breakeven" section of the editor):* `breakeven_mode` (off / pips /
  R, default off), `breakeven_trigger` (pips or R, by mode), `breakeven_buffer_pips` (default
  1, 0 = exact entry). A pip is 10 points on a 3- or 5-digit symbol and 1 point otherwise.
  The pips trigger has to exceed the buffer, or the stop would land beyond the current price.

  *Data the copy record gains:* `point` and `digits` (from the OPEN/PLACE result, which
  already carries `point`; `digits` is new), and `risk_distance` (the initial distance from
  fill to stop, the "1R" an R trigger measures in; 0 when the copy opened with no stop, so
  an R trigger never fires on a lot-multiple copy). A filled pending order carries all
  three over in `_on_fill`. Store columns are added by `ALTER TABLE` when missing.

  *The trigger (engine side):* before each `ledger.step`, for each connected slave with
  breakeven on, compare each untriggered copy's slave position (`price_open` from
  `open_positions`, `price_current` from `live_positions`, matched by `slave_ticket`) to its
  threshold. On crossing it, write the floor and log it at INFO; the next diff sends the
  `MODIFY_SLTP` through the existing path. That makes this the first reader of
  `PositionLive` outside the UI, a deliberate exception. It only feeds the trigger, never
  the diff. A dry run never triggers, since its copies don't exist on the slave.

  *Interactions, decided:*
  - **`copy_sltp` off:** breakeven still applies. The floor is raised against the copy's own
    synced stop rather than the master's.
  - **Take-profit:** untouched.
  - **Editing breakeven settings is exempt from the give-up rule** (the user's call): an edit
    that changes *only* breakeven fields keeps the slave's copies, and the new settings apply
    to trades already open. They change how a stop is managed from now on, never a copy's
    direction or size, which is what the rule protects.
  - **Per-trade exemption** (the user's call): on the detail page, `b` on a slave's copy row
    toggles `breakeven_exempt` on that copy record. An exempt copy never triggers. If its
    floor was already set, exempting clears it and the copy follows the master's stop again.
  - **Per-trade breakeven price** (the user's call, added while building): `B` on a copy row
    sets `breakeven_at`, the price at which that copy goes to breakeven. It replaces the
    slave's pips/R trigger for that copy, and works even with the slave's breakeven off. It
    must be past entry + buffer on the profitable side (checked against the copy's own fill);
    blank clears it. Setting a price lifts an exemption, and exempting clears the price.
  - **Making the user aware of the floor** (the user's call: keep the rule, but say so).
    The settings help text and the `B` dialog state it, and the log line when breakeven fires
    says the stop now only tightens. Whenever the master's stop is looser than a copy's
    floor, the diff reports a skipped `MODIFY_SLTP` (`SkipReason.BREAKEVEN_HOLDS`) naming the
    master's level. It appears once per distinct level, because the ledger de-duplicates
    on value, and right after breakeven fires it doubles as confirmation that the floor
    holds.
  - **Floats:** the trigger allows half a point of slack, so a copy sitting exactly on its
    trigger isn't missed by a rounding error (a unit test caught 0.0009999 < 0.001 on a sell).
  - **A refused move is retried, but only with more room — found on the first demo run,
    2026-10-03.** BlackBull, BTCUSD: breakeven fired (a hand-set price) with the price about
    $19 above the new stop, and the broker refused it, `INVALID_STOPS`. The symbol reports
    `trade_stops_level` 0, but its spread floats around 1300 points ($13), and the broker
    enforces a minimum distance it doesn't publish. Two gaps then made the failure silent.
    The refusal was final (the Stage 3 rule for any refused modify). And when breakeven
    fired again later at the same level, the identical `MODIFY_SLTP` matched the refused one
    in the ledger's de-duplication and was dropped without a word. Now a refused breakeven
    clears the floor (the copy stays on the master's stop, which the broker accepted) and
    logs a WARNING. The engine re-arms it only once the price is 1.5× further from the stop
    than at the refused attempt, so retries happen at strictly better distances and never
    once per tick. And `set_breakeven` forgets the last reported stop move, so a re-armed
    floor is always sent.

  *Verification:* unit tests for the trigger maths (pips/R, buy/sell, 3/5-digit vs. index,
  no-stop copies), the floor in the diff (trailing master followed, loosening master and
  cleared stop ignored, `copy_sltp` off), the store migration, and the settings validation.
  Then a demo run: open on the master, let a slave copy cross the trigger, see its stop move
  to entry + buffer while the master's stays put, and move the master's stop back to see it
  ignored.
- **Prop-firm guards — designed 2026-10-04 and accepted by the user; not built.**
  Prop firms' rules are many and differ from firm to firm (daily loss, static or trailing
  drawdown, max floating loss, margin or risk per trade, max lots or positions, weekend and
  news bans, consistency), and more will be wanted later. **The user's call: a plug-and-play
  rule system, chosen per terminal**, not a fixed set of settings fields. Rules apply to
  slaves only; the master is just the source of trades.

  **The rule that shapes it: rules decide, the engine acts.** A rule never sends anything. It
  returns a verdict from a fixed vocabulary the engine already knows how to carry out, and the
  most severe verdict among a slave's rules wins:

  | Verdict | Effect | Built on |
  |---|---|---|
  | `allow` | nothing | — |
  | `warn` | logged, shown on the card; copying carries on | log + toast |
  | `decline` | this one new trade is refused, final like every decline | `SkipReason` |
  | `shrink` | the new trade is sent smaller, down to a cap | `max_lot` capping |
  | `block_new` | the slave takes no new trades; upkeep of held ones carries on | pause |
  | `close_worst` | close the copy with the largest floating loss; re-checked next snapshot | **new** |
  | `flatten` | close every copy on the slave, then `block_new` | **new** |

  Adding a rule therefore never adds a new way to trade: a wrong rule can at worst stop
  trading or close what it holds, both of which are reviewed once, in the engine. The two
  closing verdicts are the only new sending paths and go through the existing CLOSE dispatch.

  *The four checks — a rule implements only the ones it needs:*
  - **`gate(trade)`** — before a new trade, on what is known without sizing: symbol, side,
    time, open position count, whether it carries a stop. Max positions, weekend and news
    windows, stop required. Runs in `reconcile.py`, beside `_paused_reason` and
    `_filter_reason`, so it stays pure and dry-run shows it.
  - **`caps(account)`** — after sizing, but **without rule code in the worker**: the engine
    asks each rule for numeric caps and sends the tightest on the command, as `max_lot`
    already travels (`protocol.py`, `engine.py`). Caps: `max_lot`, `max_risk_money` (what the
    stop would lose), `max_margin` (from `order_calc_margin`), each with `decline` or `shrink`.
    The executor enforces numbers and knows nothing of rules. Max risk per trade, max margin
    per trade, and "a stop-out here must not cross the daily floor" (cap = room left) all
    reduce to these.
  - **`watch(account)`** — on every snapshot: daily loss, drawdown, max floating loss. This is
    where `block_new`, `close_worst` and `flatten` come from.
  - **`status(account)`** — what to show: "daily room $412 of $500", a bar, the state.

  *The account view*, built once per snapshot and read-only to every rule: balance, equity,
  margin, free margin, open positions with floating P/L, broker server time, today's start
  balance and equity, and that rule's own saved state. **New worker reads needed:** server
  time (the last tick's time) and today's closed deals (`history_deals_get`). Today's start
  balance is rebuilt from history — current balance minus today's closed profit, commission
  and swap — so it is right however late the copier was started; start-of-day equity can only
  be recorded if the copier is running at the reset, so rules based on it say so when it is
  missing. Each rule's reset time and timezone is its own parameter (firms differ).

  *Saved rule state* (peak equity for trailing drawdown, a breach latch, today's start
  equity) goes in a `guard_state` table in `mapping.db`. **A breach latches:** once tripped it
  holds until that rule's next reset, or until cleared by hand behind a red confirm dialog —
  otherwise a block would lift the moment equity bounced.

  *Rules are built in, not loaded from user files*: each is one module in
  `mt5copier/rules/`, registered by type name, declaring its parameters (name, kind, unit,
  default, bounds). Adding a rule = one file plus its tests; nothing else changes. Every rule
  that can produce `block_new` or stronger also takes a **buffer** (act at e.g. 80% of the
  limit), because the firm checks every tick on its own server, the copier only sees a
  snapshot per poll and nothing while it is off, and gaps jump thresholds. The guards are a
  safety net, never a guarantee, and the page says so.

  *Where they live — not in settings.* Settings say how to copy and change only when edited;
  guards say how much harm an account may take, change state by themselves, and can close
  trades, so they need watching as much as editing.
  - **A Guards page per slave** (from the card's action menu, its own key, and the details
    page): one row per rule with its bar, room left, state and verdict on breach. On it: add,
    edit, remove a rule (the form generated from the rule's declared parameters, in the
    settings form's sentence style), save the set as a named template or apply one, copy the
    set from another slave, switch the whole set on/off, and
    clear a latched breach.
  - **A badge on the slave's Map card**: nothing when healthy; amber `GUARD 82%` inside the
    buffer; red `BREACHED · blocked` / `FLATTENED` after.
  - **Stored in `guards.json`**, separate from `config.json`, so a slip editing rules cannot
    disturb copy settings. A template is a named list of rules in the same format, saved by
    the user, and copying guards from another terminal falls out of it.

  *Build order:*
  1. Framework, account view, server time and deal history in the worker, `guard_state`, the
     Guards page and badge with **`status` only, enforcing nothing** — so the numbers can be
     checked against a firm's dashboard on a demo first.
  2. Daily loss, static drawdown, max floating loss, margin per trade, risk per trade — with
     `warn`, `decline`, `shrink`, `block_new`.
  3. `close_worst` and `flatten`, after step 2's numbers are demo-verified.
  4. More rules (trailing drawdown, max positions, weekend, news, consistency as `warn` only)
     and user templates.

  **Firm-neutral — the user's call, 2026-10-04.** The user trades many firms and wants no
  firm baked in: nothing is named after a firm and nothing ships preset. Wherever firms
  differ in a definition, it is a parameter of the rule, never a choice made in code — e.g.
  daily loss's basis (start balance, start equity, or the higher), its reset time and
  timezone, limits in money or %, drawdown measured from initial balance, peak balance or
  peak equity, and whether a trailing floor stops at the initial balance. Reusing a setup
  across accounts is the user's own templates, and copying from another slave.

  *Settled with the user 2026-10-04:* the default buffer is **80%** of a limit. **A required
  stop loss is its own rule** (`gate`, declining a new trade with no stop), since some firms
  mandate one and others do not; without that rule a copy with no stop is not refused, and a
  risk cap cannot measure it, so it passes the cap. The Guards page opens with **`r`**.

  **Margin checks belong here (parked 2026-10-02).** MT5 can price a trade's margin before it
  is sent (`order_calc_margin(action, symbol, volume, price)`, in the account currency), and
  every snapshot already carries `margin_free` (unused so far; `account_info()` also gives
  `margin` and `margin_level`). So the executor could, after sizing and before sending, compute
  "this copy needs $X, which is Y% of free margin", show it, and enforce a per-slave limit.
  Undecided, to settle alongside the other prop-firm rules: what an over-limit trade does
  (refuse it, shrink it to fit — which copies at less risk than asked — or only report it), and
  whether there is a default limit. Motivating incident: on 2026-10-02 a first trade on a
  freshly selected symbol (BlackBull, EURGBPp) was sized at 279 lots, capped to 100 by
  `volume_max`, and only stopped by the broker's `NO_MONEY`. The likely cause is
  `order_calc_profit` answering wrongly before the symbol's prices were ready (unconfirmed);
  either a margin limit or a sanity check on that number would have caught it before sending.
  Until then, a per-slave **Max lot** is the backstop.
- **Post trades to Telegram.** Notify a channel/bot when a copy opens, closes, or modifies.
  Read-only with respect to trading — it must never become a control surface, nothing it does
  can feed back into a trading decision — so it's lower-risk than the two above, but still needs
  its own pass on: which events to post, what the message contains (symbol, lots, price — is
  any of that meant to stay private), what happens to the copy loop if the Telegram API call is
  slow or fails (never blocks or delays a copy), and where the bot token lives (not plaintext in
  `config.json` if avoidable).
- **Copy settings from another terminal — asked for 2026-10-04.** In a slave's settings form,
  fill every field from another slave's settings, to save setting up similar accounts by hand.
  Still to settle: whether symbol renames come along (they are broker-specific, so probably
  not, or only on request), and whether it fills the form for review (nothing saved until the
  usual save, so the orphan-on-settings-edit rule still applies) — the expected answer.
- **Revert unsaved changes in the settings form — asked for 2026-10-04.** One key that puts
  every field back to its saved value without leaving the form.

---

## Performance note

Master position polling wants to be faster than the current 250ms account-snapshot interval —
around 100ms. Each terminal has its own process, so slave polling costs nothing extra; the
master's loop is the only latency-critical one. Measure before tuning.
