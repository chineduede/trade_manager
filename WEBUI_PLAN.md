# Web UI plan

Not started. This is the plan for the web front end agreed on 2026-10-01, written before any
code so the decisions are explicit instead of accidental. `ROADMAP.md` stays the plan for
trading logic; this stays the plan for the web presentation layer. Cross-referenced, not merged
— the web UI has no trading-logic decisions of its own, only UI-shaped ones.

## Sequencing (decided 2026-10-01 — do not reorder without asking)

1. **Finish `ROADMAP.md`'s "Live verification backlog" on the current TUI first.** Nothing here
   is blocked on this document, and nothing in this document should be started before that
   backlog is checked off. Shipping a nicer way to drive code that has never been proven to
   refuse a live account correctly just makes the eventual bug harder to isolate.
2. **Build the web UI** (this plan).
3. **New trading features (breakeven, prop-firm guards, "and the rest") are designed and built
   after the web UI exists, and their UI lands only in the webpage — the TUI does not gain
   them.** Decided explicitly, not a default: this means the TUI becomes a frozen, proven
   baseline rather than the ongoing primary interface. The trading *logic* for those features
   still goes through the same policy-then-build-then-demo-verify process as everything already
   in `ROADMAP.md` — the web UI changes where the controls live, not how new trading rules get
   trusted.

## Non-negotiables carried over from the TUI/engine — the UI must not weaken these

- **The demo-only guard is unaffected** — it lives in `executor.py`, below any UI, and stays
  there.
- **The panic switch (`F9` in the TUI) needs an equally prominent, always-reachable control on
  every page** of the web UI. Its meaning (halts copying, never touches positions) is fixed per
  `ROADMAP.md` and carries over unchanged.
- **Live-override arming keeps its friction.** The TUI requires typing the terminal's full name
  to turn it on; a web "Arm" button with a confirm dialog is not an equivalent and must not
  replace it. Port the same typed-name requirement.
- **The orphan-confirmation flow still blocks** before a settings save that would orphan open
  copies — ported as a blocking step in the web flow, not a dismissible toast.
- **`dry_run` defaults on** for anything that can newly touch an account; the web UI inherits
  this default, it does not get to relax it for convenience.

## Architecture

### One `Engine`, one process

The web server runs **inside the same process as the `Engine`**, not a second instance and not
over IPC to a second process. `Engine` already guards its state with `self._lock`, so this is
safe without new synchronization work, and it avoids a split-brain where the TUI and a web
server each believe something different about what is running.

**v1 is exclusive mode:** `mt5copier --web` starts the `Engine` and the web server instead of
the TUI, the same pattern as today's `--list` / `--check` flags. Running the TUI and the web
server concurrently against one `Engine` is feasible later — the locking already supports it —
but it doubles the surface for the first security-sensitive release (two renderers, two things
to reason about touching live trading at once) and is deliberately deferred, not ruled out.

### Backend: FastAPI + Jinja2 + htmx, not a SPA

- **No Node/build pipeline.** The project ships as one PyInstaller exe (`copier.spec`); templates
  and static assets become `datas` in the spec, with no `npm` step added to the release process.
- **Most of the surface is CRUD-shaped forms** — settings editor, live-override, orphan
  confirm, role assignment. htmx (partial HTML swaps over POST) fits that directly; it does not
  need a client-side state store the way a SPA would for the same handful of forms.
- **FastAPI's websocket support gives the live snapshot stream for free**, and its async model
  fits the engine's existing polling/queue design better than a second REST-polling loop would.
- `pydantic` is already a dependency (FastAPI's native validation layer), so this is additive,
  not a second validation story.
- If a later feature genuinely needs rich client interactivity (a chart, drag-resize), that one
  page can load vanilla JS / a bundled Chart.js file — it does not have to pull the whole app
  into a SPA.

### Security — required for v1, not a follow-up

Full control is reachable over HTTP in this design, so these are not optional hardening passed
for later; they ship with Web Stage A.

- **Bind to `127.0.0.1` only.** No config path to `0.0.0.0` without a separate, deliberately
  awkward flag — mirrors the live-override naming-friction pattern on purpose.
- **Token auth, Jupyter-notebook-style.** A random token generated at startup (or persisted to
  `%APPDATA%\MT5Copier\web_token` with restrictive ACLs), required on every API/websocket call;
  printed once at startup as a clickable `http://127.0.0.1:<port>/?token=...` URL. A page open
  in another tab without that token gets nothing back.
- **CSRF defense that doesn't rely on the cookie alone.** Every state-changing call must also
  carry a custom header (e.g. `X-MT5Copier-Token`). A cross-site `<form>` POST cannot set a
  custom header, and `fetch`/XHR cannot either without a CORS preflight — which this server
  never approves (no `Access-Control-Allow-Origin` issued, ever, for any origin).
- **Default-deny CORS.** Page and API are same-origin by construction (one FastAPI app serves
  both); nothing opts back into cross-origin access.
- **State-changing actions are POST-only, never GET.** Closes the classic
  `<img src="http://127.0.0.1:PORT/api/panic">` class of attack outright.

## Staged build

Same culture as `ROADMAP.md`'s staging — each stage is small enough to demo and get sign-off on
before the next starts.

**Web Stage A — read-only dashboard.** Terminal/worker status, live positions & equity over the
websocket, log tail. No write path at all. Lowest risk; proves the FastAPI + htmx + websocket
plumbing and the full auth/CSRF scaffolding *before* anything that can move money is reachable
over HTTP.

**Web Stage B — settings & roles.** Per-terminal settings form and role assignment, calling the
*same* `settings_edit.py` / `Engine.apply_settings` the TUI already uses — no parallel
validation logic written for the web layer. Orphan-confirmation ported as a blocking step.

**Web Stage C — the two highest-friction controls.** Live-override and panic, ported last and
deliberately, with their existing friction preserved exactly (typed terminal name to arm; an
unmissable panic control on every page, not buried in a menu).

**Web Stage D — new trading features.** Breakeven and prop-firm guards get their policy
questions settled first, in writing, the same way Stage 3's policy questions were settled in
`ROADMAP.md` — then their engine-side logic is built and demo-verified like every other trading
rule. Stages A–C only move *where* the controls live; they do not let D skip verification.

## Open decisions — your call, not defaulted

- **Forms (htmx) vs. a full SPA (React/Vue).** Recommending htmx for the no-build-step
  packaging win above. Say so if the richer client-side feel of a SPA is worth taking on a JS
  build step and a second packaging path for.
- **Token delivery: a URL-token printed fresh each run (Jupyter-style) vs. a password set once
  on first run.** The URL-token is less code and fits "full control, same machine, just you (or
  whoever you hand this to) for now." A one-time password feels more like a normal app login
  and doesn't change on every restart, at the cost of a setup step and somewhere to store/reset
  it.

## What this plan deliberately does not cover

Hosting this for anyone not on the same machine, multi-user accounts/roles within the web UI,
and anything about the breakeven/prop-firm-guard trading logic itself (that belongs in
`ROADMAP.md`, once Web Stage D is reached). Out of scope until asked for.
