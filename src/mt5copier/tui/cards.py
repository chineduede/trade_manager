"""What each card on the Map says. Pure: a card's text is decided here and only drawn by the
widgets, so the tests can pin it down without starting the app.

A slave card answers, in this order: which one is it (its number, for the 1-9 keys), is it
copying, how much is in it, how is it doing right now, and what is the one thing to press if
something is wrong with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.text import Text

from ..config import SizingMode, TerminalConfig
from ..engine import TerminalView
from ..guards import GuardReport, Level
from ..presentation import RowInfo, slave_badge
from ..protocol import Snapshot
from ..terminal_detail import bar, format_ping, margin_gauge
from .style import (
    BG,
    BLUE,
    FG,
    GREEN,
    MUTED,
    RED,
    TIER_COLOR,
    YELLOW,
    amount,
    badge,
    floating,
    left_right,
)


@dataclass(frozen=True, slots=True)
class SlaveState:
    kind: str
    """One of "copying", "idle", "paused", "held", "blocked", "down": also the card's CSS
    class, which picks its border."""
    status: Text
    """The top-right word or badge."""


def margin_bar(fraction: float | None, width: int = 10) -> str:
    """The details page's solid margin bar (see :func:`~mt5copier.terminal_detail.bar`),
    short enough for a card."""
    return bar(fraction, width)


def margin_line(snap: Snapshot | None) -> Text:
    """``margin ███░░░░░░░ 30%``, the bar coloured by margin level as on the details page:
    blue while healthy, then yellow and red as it nears a margin call."""
    if snap is None:
        return Text("margin —", style=MUTED)
    gauge = margin_gauge(snap)
    margin = Text("margin ", style=MUTED)
    margin.append(margin_bar(gauge.used_fraction), style=TIER_COLOR[gauge.level_tier])
    if gauge.used_fraction is not None:
        margin.append(f" {gauge.used_fraction:.0%}", style=MUTED)
    return margin


# Solid 3x5 glyphs for the big balance, one string per pixel row ("#" is lit). Three rows of
# half blocks draw six pixel rows, so the sixth (only the comma's tail) sits under the line.
_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ".": (" ", " ", " ", " ", "#"),
    ",": (" ", " ", " ", " ", "#", "#"),
    "(": (" #", "# ", "# ", "# ", " #"),
    ")": ("# ", " #", " #", " #", "# "),
}
_HALF = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


def block_width(text: str) -> int:
    """How wide :func:`block_digits` draws ``text``, or 0 if it has a character it cannot."""
    if not text or any(c not in _GLYPHS for c in text):
        return 0
    return sum(len(_GLYPHS[c][0]) for c in text) + len(text) - 1


def block_digits(text: str) -> list[str]:
    """``text`` in solid half-block digits: three lines, a column apart between characters."""
    rows = ["", "", ""]
    for n, c in enumerate(text):
        glyph = _GLYPHS[c]
        wide = len(glyph[0])
        pixels = [*glyph, *([" " * wide] * (6 - len(glyph)))]
        for r in range(3):
            top, bottom = pixels[2 * r], pixels[2 * r + 1]
            if n:
                rows[r] += " "
            rows[r] += "".join(_HALF[top[x] == "#", bottom[x] == "#"] for x in range(wide))
    return rows


def big_amount(value: float | None, width: int) -> Text:
    """A balance in solid 3-row digits, which is as near as a terminal gets to the design's
    large bold type -- or plain bold when the card is too narrow for them."""
    text = amount(value)
    if value is not None and 0 < block_width(text) <= width:
        return Text("\n".join(block_digits(text)), style="bold", no_wrap=True)
    return Text(text, style="bold")


def risk_text(cfg: TerminalConfig, currency: str | None) -> str:
    if cfg.sizing_mode is SizingMode.FIXED_CASH_RISK:
        mark = "$" if (currency or "").upper() in ("USD", "AUD", "CAD", "NZD", "") else ""
        return f"risk {mark}{cfg.risk_cash:,g}" + ("" if mark else f" {currency}")
    return f"risk {cfg.risk_percent:g}%"


def slave_state(info: RowInfo, *, dry_run: bool, copying: bool) -> SlaveState:
    """A link that is down outranks the demo guard, which outranks a hold, which outranks a
    pause."""
    # ``info.tier`` is the link's own state, raised from "good" to "warning" only by a hold: so
    # a "warning" with no hold is a link still coming up.
    if info.tier in ("serious", "critical", "muted") or (
        info.tier == "warning" and info.hold_reason is None
    ):
        text, _ = slave_badge(info, dry_run)
        color = {"muted": MUTED, "warning": YELLOW}.get(info.tier, RED)
        return SlaveState("down", Text(text.lower(), style=f"bold {color}"))
    if info.demo_guard_blocked:
        return SlaveState("blocked", badge("HELD BACK", RED))
    if info.hold_reason is not None:
        return SlaveState("held", badge("HELD", RED))
    if info.paused:
        return SlaveState("paused", badge("PAUSED", BLUE))
    if not copying:
        return SlaveState("idle", Text("not copying", style=f"bold {MUTED}"))
    if dry_run:
        return SlaveState("copying", Text("dry run", style=f"bold {YELLOW}"))
    return SlaveState("copying", Text("copying", style=f"bold {GREEN}"))


def guard_badge(report: GuardReport | None) -> Text:
    """Nothing while every guard is comfortable; ``GUARD 82%`` in yellow once one is past the
    point it acts at, ``GUARD BREACHED`` in red once one is at its limit, and ``GUARD: NO NEW
    TRADES`` in red while one is keeping the slave from new trades."""
    if report is not None and report.holding_back:
        return Text("GUARD: NO NEW TRADES", style=f"bold {RED}")
    worst = report.worst if report is not None else None
    if worst is None or worst.level is Level.OK:
        return Text("")
    if worst.level is Level.BREACHED:
        return Text("GUARD BREACHED", style=f"bold {RED}")
    fraction = worst.fraction
    said = f"GUARD {fraction:.0%}" if fraction is not None else "GUARD"
    return Text(said, style=f"bold {YELLOW}")


def slave_card(
    number: int,
    view: TerminalView,
    info: RowInfo,
    state: SlaveState,
    *,
    refused: int,
    width: int = 0,
    guards: GuardReport | None = None,
) -> RenderableType:
    """``width`` is the card's inside width, which decides whether the balance fits in big
    digits."""
    cfg = view.cfg
    title = Text(f"{number} · SLAVE", style="bold")
    lines: list[RenderableType] = [
        left_right(title, state.status),
        Text(info.label, no_wrap=True, overflow="ellipsis"),
        big_amount(info.balance, width),
    ]
    risk = risk_text(cfg, info.currency)
    opened = "—" if info.open_trades is None else f"{info.open_trades} open"
    if state.kind == "blocked":
        said = "not a demo" + (f" · {refused} refused" if refused else "")
        lines.append(Text(said, style=RED))
        lines.append(_keyed("l", "allow live trading"))
    elif state.kind == "held":
        lines.append(
            Text(info.hold_reason or "held", style=YELLOW, no_wrap=True, overflow="ellipsis")
        )
        lines.append(_keyed("o", "acknowledge"))
    elif state.kind == "paused":
        lines.append(floating(info.floating_pl))
        line = Text(f"{risk} · ", style=MUTED)
        line.append_text(_keyed("p", "resume"))
        lines.append(line)
    else:
        lines.append(floating(info.floating_pl))
        lines.append(Text(f"{risk} · {opened}", style=MUTED))
    # On the margin line, so a card is no taller for having guards.
    lines.append(left_right(margin_line(view.snapshot), guard_badge(guards)))
    return Group(*lines)


def master_title() -> Text:
    """The master's name tag, filled in cream, where a slave's card has plain ``1 · SLAVE``: with
    its double border, what tells the master's card apart at a glance."""
    # A span, not the Text's own style, which would fill the whole cell it sits in.
    return Text.assemble((" A · MASTER ", f"bold {BG} on {FG}"))


def master_card(
    view: TerminalView, info: RowInfo, *, slaves: int, width: int = 0
) -> RenderableType:
    """The same lines as a slave's card, in the same places, so the two read alike."""
    snap = view.snapshot
    if snap is not None and info.tier == "good":
        link = Text("connected", style=f"bold {BLUE}")
    else:
        link = Text(info.status_text, style=f"bold {RED if info.tier != 'muted' else MUTED}")
    ping = format_ping(snap.ping_last_us) if snap is not None else "—"

    trades = info.open_trades
    trade_word = "—" if trades is None else f"{trades} open"
    feeds = f"feeds {slaves} slave{'s' if slaves != 1 else ''}"
    return Group(
        left_right(master_title(), link),
        Text(info.label, no_wrap=True, overflow="ellipsis"),
        big_amount(info.balance, width),
        floating(info.floating_pl),
        Text(f"{trade_word} · {feeds}", style=MUTED),
        left_right(margin_line(snap), Text("" if ping == "—" else ping, style=MUTED)),
    )


def virtual_master_card(
    *, feed: str | None, positions: int, orders: int, slaves: int
) -> RenderableType:
    """The master's card in a profile that trades directly: the virtual book where a real
    master's account would be. Same shape as the master's card, so the Map reads the same."""
    if feed is not None:
        link = Text("feed " + feed, style=f"bold {BLUE}", no_wrap=True, overflow="ellipsis")
    else:
        link = Text("no feed", style=f"bold {RED}")
    opened = f"{positions} open" if positions else "nothing open"
    resting = f" · {orders} resting" if orders else ""
    feeds = f"feeds {slaves} slave{'s' if slaves != 1 else ''}"
    hint = _keyed("n", "order ticket")
    hint.stylize(MUTED)
    return Group(
        left_right(master_title(), link),
        Text("Direct trading", no_wrap=True, overflow="ellipsis"),
        Text("you are the master", style=MUTED),
        Text(f"{opened}{resting}", style="bold"),
        Text(feeds, style=MUTED),
        left_right(
            hint,
            Text("" if feed is not None else "connect an MT5 slave", style=MUTED),
        ),
    )


def no_master_card() -> RenderableType:
    return Group(
        master_title(),
        Text(""),
        Text("none yet: select a terminal under NOT YET OPENED and press m", style=MUTED),
    )


def _keyed(key: str, label: str) -> Text:
    out = Text()
    # No colour of its own: it takes the card's, red on a held-back card and cream elsewhere.
    out.append(key, style="bold underline")
    out.append(f" {label}")
    return out
