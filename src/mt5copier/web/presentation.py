"""Pure view-model logic for the web dashboard: status tiers, row data, log tails.

Kept separate from ``routes.py`` so the mapping from engine state to a display tier, and the
log-tail arithmetic, can be unit tested without an ASGI app -- the same reason ``sizing.py`` and
``symbols.py`` stay pure and separate from the I/O-heavy modules.

Status tiers follow the mapping decided in WEBUI_PLAN.md's "UI feel" section, reusing this
project's own existing vocabulary (``protocol.WorkerState``, ``TerminalView.status()``,
``Engine.holds()``, ``Engine.blocked_reason``) rather than inventing a parallel one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from markupsafe import Markup, escape

from ..engine import Engine, TerminalView

Tier = str
"""One of "good" / "warning" / "serious" / "critical" / "muted" -- see WEBUI_PLAN.md."""

_TIER_BY_STATUS: dict[str, Tier] = {
    "connected": "good",
    "connecting": "warning",
    "starting": "warning",
    "stopping": "warning",
    "disconnected": "serious",
    "stale": "serious",
    "worker dead": "critical",
    "failed": "critical",
    "not connected": "muted",
    "-": "muted",
    "stopped": "muted",
}

STATUS_BADGE: dict[Tier, str] = {
    "good": "[OK]",
    "warning": "[WARN]",
    "serious": "[ERR]",
    "critical": "[DEAD]",
    "muted": "[OFF]",
}
"""Bracketed text badges instead of an icon. Text reads as its own tier without relying on an
icon *shape* to carry the distinction -- it is the same accessibility rule the previous SVG
icons were chasing (a status color never appears without a non-color cue), just satisfied in a
way that also happens to fit a retro-terminal status line better than an SVG glyph would."""


def tier_for_status(status_text: str) -> Tier:
    """A held slave overrides this to "warning" at the call site (see :func:`row_info`) --
    holding is orthogonal to connection status, so it is not folded into this table."""
    return _TIER_BY_STATUS.get(status_text, "muted")


def badge_for_tier(tier: Tier) -> str:
    return STATUS_BADGE.get(tier, "[??]")


@dataclass(frozen=True, slots=True)
class RowInfo:
    """Everything one dashboard row needs to render, already derived from a ``TerminalView``."""

    terminal_id: str
    label: str
    role: str
    status_text: str
    tier: Tier
    hold_reason: str | None
    login: int | None
    server: str | None
    balance: float | None
    equity: float | None
    floating_pl: float | None
    floating_pl_tier: Tier | None


def row_info(view: TerminalView, *, timeout_s: float, hold_reason: str | None) -> RowInfo:
    status_text = view.status(timeout_s)
    tier = tier_for_status(status_text)
    if hold_reason is not None and tier == "good":
        # A held slave is still connected -- the connection is fine, copying to it isn't.
        tier = "warning"
    snap = view.snapshot
    balance = snap.balance if snap else None
    equity = snap.equity if snap else None
    floating_pl = None if balance is None or equity is None else equity - balance
    # Reuses the status good/critical tokens directly for P&L, per WEBUI_PLAN.md's collision
    # rule, instead of a second green/red pair kept in sync by hand.
    pl_tier: Tier | None = (
        None if floating_pl is None else ("good" if floating_pl >= 0 else "critical")
    )
    return RowInfo(
        terminal_id=view.cfg.terminal_id,
        label=view.cfg.label,
        role=str(view.cfg.role),
        status_text=status_text,
        tier=tier,
        hold_reason=hold_reason,
        login=snap.login if snap else None,
        server=snap.server if snap else None,
        balance=balance,
        equity=equity,
        floating_pl=floating_pl,
        floating_pl_tier=pl_tier,
    )


def all_rows(engine: Engine) -> list[RowInfo]:
    """One :class:`RowInfo` per terminal, in the same order ``engine.views()`` already sorts."""
    timeout = engine.config.heartbeat_timeout_s
    holds = engine.holds()
    return [
        row_info(v, timeout_s=timeout, hold_reason=holds.get(v.cfg.terminal_id))
        for v in engine.views()
    ]


def read_log_tail(path: Path, *, max_lines: int = 200) -> str:
    """The last ``max_lines`` of the log file, as plain text.

    A missing file (nothing logged yet, or a fresh install) reads as empty, not an error --
    nothing has gone wrong, there is simply nothing to show yet. Each poll of the log page or
    its partial route calls this fresh and the response fully replaces the panel -- there is no
    byte-offset bookkeeping to get wrong, unlike the push-based design this replaced (see
    WEBUI_PLAN.md's "Stage A rebuilt" section for why that tracking was dropped along with the
    websocket).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


_LOG_LEVEL_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\S+)")
_LOG_LEVEL_CLASS = {
    "DEBUG": "log-debug",
    "INFO": "log-info",
    "WARNING": "log-warning",
    "ERROR": "log-error",
    "CRITICAL": "log-critical",
}


def render_log_lines(text: str) -> Markup:
    """Wrap each line of a log tail in a ``<span>`` colored by its level, for the dashboard's
    log panel -- a line is otherwise indistinguishable gray text regardless of whether it's an
    INFO heartbeat or a WARNING worth noticing. A line that doesn't start with a recognised
    level (a traceback continuation, for instance) falls back to a neutral class rather than
    being guessed at. Returns a ``Markup`` so Jinja's autoescape does not re-escape it -- the
    escaping already happened per-line, here.
    """
    if not text:
        return Markup("")
    rendered = []
    for line in text.splitlines():
        match = _LOG_LEVEL_RE.match(line)
        level = match.group(1) if match else None
        css_class = _LOG_LEVEL_CLASS.get(level or "", "log-line-plain")
        rendered.append(f'<span class="log-line {css_class}">{escape(line)}</span>')
    return Markup("\n".join(rendered))
