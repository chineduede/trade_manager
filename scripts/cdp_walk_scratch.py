"""Scratch-profile UI walkthrough over CDP: every page, the modals, the dock.

Read-only by intention: flows are opened, probed, screenshotted and cancelled;
nothing is saved. Prints one JSON report. Screenshot dir comes as argv[1].
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import cdp_drive as lib

PORT = 9223
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
WS = lib._connect(PORT)
REPORT: list = []
_n = [1]


def ev(js: str):
    result = lib._rpc(
        WS, "Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}
    )
    if "exceptionDetails" in result:
        detail = result["exceptionDetails"]
        return {"__err__": detail.get("exception", {}).get("description", detail.get("text"))}
    return result.get("result", {}).get("value")


def shot(name: str) -> str:
    data = lib._rpc(WS, "Page.captureScreenshot", {"format": "png"})["data"]
    path = SHOTS / f"{_n[0]:02d}_{name}.png"
    _n[0] += 1
    path.write_bytes(base64.b64decode(data))
    return path.name


def mouse(kind: str, x: float, y: float, button: str = "left") -> None:
    lib._rpc(
        WS, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"}
    )
    if kind == "move":
        return
    for ev_ in ("mousePressed", "mouseReleased"):
        lib._rpc(
            WS,
            "Input.dispatchMouseEvent",
            {"type": ev_, "x": x, "y": y, "button": button, "clickCount": 1},
        )


def click_sel(sel: str, button: str = "left") -> bool:
    rect = ev(
        "(()=>{const e=document.querySelector(" + json.dumps(sel) + ");if(!e)return null;"
        "e.scrollIntoView({block:'center'});"
        "const r=e.getBoundingClientRect();return [r.x+r.width/2, r.y+r.height/2]})()"
    )
    if not rect:
        return False
    time.sleep(0.15)
    rect = ev(
        "(()=>{const e=document.querySelector(" + json.dumps(sel) + ");"
        "const r=e.getBoundingClientRect();return [r.x+r.width/2, r.y+r.height/2]})()"
    )
    mouse("click", rect[0], rect[1], button)
    return True


def press(key: str, code: int) -> None:
    for ev_ in ("rawKeyDown", "keyUp"):
        lib._rpc(
            WS,
            "Input.dispatchKeyEvent",
            {
                "type": ev_,
                "key": key,
                "code": key,
                "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code,
            },
        )


def note(step: str, **facts) -> None:
    REPORT.append({"step": step, **facts})


TOASTS = "[...document.querySelectorAll('.toast')].map(t=>t.textContent.trim().slice(0,120))"
VISIBLE = "[...document.querySelectorAll('[id^=page-]')].filter(p=>!p.hidden).map(p=>p.id)"

# ---- 1. each nav page ----
PROBES = {
    "trading": "({sub: document.getElementById('page-sub')?.textContent.trim().slice(0,160),"
    " navClass: document.querySelector('[data-page=trading]').className})",
    "guards": "({pick: document.getElementById('guard-pick-btn')?.textContent.trim().slice(0,100),"
    " empty: document.getElementById('guard-empty')?.hidden===false,"
    " none: document.getElementById('guard-none')?.textContent.trim().slice(0,160),"
    " buttons: ['g-add','g-change','g-toggle','g-remove','g-templates','g-copy','g-all']"
    ".map(i=>{const b=document.getElementById(i);return b?(i+(b.disabled?':off':':on')):i+':missing'})})",
    "news": "({scanned: document.getElementById('news-scanned')?.textContent.trim().slice(0,120),"
    " nextTitle: document.getElementById('news-next-title')?.textContent.trim().slice(0,120),"
    " days: document.getElementById('news-days')?.children.length,"
    " empty: document.getElementById('news-empty')?.hidden===false,"
    " norule: document.getElementById('news-norule')?.hidden===false,"
    " problem: (d=>d&&!d.hidden?d.textContent.trim().slice(0,160):null)(document.getElementById('news-problem'))})",
    "history": "({count: document.getElementById('hist-count')?.textContent.trim(),"
    " empty: (d=>d&&!d.hidden?d.textContent.trim().slice(0,160):null)(document.getElementById('hist-empty')),"
    " cards: document.getElementById('hist-cards')?.children.length})",
    "setups": "({count: document.getElementById('setups-count')?.textContent.trim(),"
    " empty: (d=>d&&!d.hidden?d.textContent.trim().slice(0,200):null)(document.getElementById('setups-empty')),"
    " now: document.getElementById('setups-now-state')?.textContent.trim().slice(0,200),"
    " rows: document.getElementById('setups-rows')?.children.length,"
    " buttons: ['st-save','st-update','st-load','st-rename','st-delete']"
    ".map(i=>{const b=document.getElementById(i);return b?(i+(b.disabled?':off':':on')):i+':missing'})})",
    "settings": "({modeNow: document.getElementById('cfg-mode-now')?.textContent.trim().slice(0,160),"
    " cards: [...document.querySelectorAll('#cfg-cards h2,#cfg-cards h3')].map(h=>h.textContent.trim()),"
    " said: document.getElementById('cfg-foot-said')?.textContent.trim().slice(0,160),"
    " save: document.getElementById('cfg-save')?.disabled})",
}
for page, probe in PROBES.items():
    ok = click_sel(f'[data-page="{page}"]')
    time.sleep(1.0)
    facts = ev(f"JSON.parse(JSON.stringify({probe}))")
    note(
        f"page:{page}",
        clicked=ok,
        visible=ev(VISIBLE),
        facts=facts,
        toasts=ev(TOASTS),
        shot=shot(f"page_{page}"),
    )

# ---- 2. Help via F1 ----
press("F1", 112)
time.sleep(0.5)
note(
    "help:F1",
    visible=ev(VISIBLE),
    sections=ev("[...document.querySelectorAll('#page-help h2')].map(h=>h.textContent.trim())"),
    shot=shot("help"),
)

# ---- 3. log dock ----
click_sel("#log-btn")
time.sleep(1.5)
note(
    "logdock:open",
    hidden=ev("document.getElementById('logdock').hidden"),
    count=ev("document.getElementById('log-count')?.textContent.trim()"),
    rows=ev("document.getElementById('log-rows')?.children.length"),
    firstRows=ev(
        "[...document.getElementById('log-rows').children].slice(0,4).map(r=>r.textContent.trim().replace(/\\s+/g,' ').slice(0,110))"
    ),
    shot=shot("logdock"),
)
click_sel("#log-problems")
time.sleep(1.0)
note("logdock:problems", count=ev("document.getElementById('log-count')?.textContent.trim()"))
click_sel("#log-problems")
click_sel("#log-btn")
time.sleep(0.3)
note("logdock:closed", hidden=ev("document.getElementById('logdock').hidden"))

# ---- 4. profiles modal ----
click_sel("#profile-btn")
time.sleep(0.8)
note(
    "profiles:modal",
    title=ev("document.getElementById('modal-title')?.textContent.trim()"),
    body=ev(
        "document.getElementById('modal-body')?.textContent.trim().replace(/\\s+/g,' ').slice(0,420)"
    ),
    shot=shot("profiles_modal"),
)
press("Escape", 27)
time.sleep(0.3)
note("profiles:closed", modalGone=ev("document.getElementById('modal-back')?.hidden"))

# ---- 5. terminals: context menu + cTrader modal + rescan ----
click_sel('[data-page="terminals"]')
time.sleep(0.8)
rect = ev(
    "(()=>{const e=[...document.querySelectorAll('.tcard')].find(c=>c.textContent.includes('Blackbull'));"
    "const r=e.getBoundingClientRect();return [r.x+r.width/2, r.y+r.height/2]})()"
)
mouse("click", rect[0], rect[1], "right")
time.sleep(0.4)
note(
    "terminals:ctxmenu",
    items=ev(
        "[...document.querySelectorAll('#ctx-menu button,#ctx-menu .mi')].map(b=>b.textContent.trim().slice(0,60))"
    ),
    shot=shot("ctx_menu"),
)
press("Escape", 27)
time.sleep(0.2)

click_sel("#ctrader-btn")
time.sleep(0.5)
note(
    "terminals:ctrader_modal",
    title=ev("document.getElementById('modal-title')?.textContent.trim()"),
    fields=ev(
        "[...document.querySelectorAll('#modal-body input')].map(i=>i.type+':'+(i.placeholder||i.value.slice(0,30)))"
    ),
    shot=shot("ctrader_modal"),
)
click_sel("#modal-cancel")
time.sleep(0.3)

click_sel("#rescan-btn")
time.sleep(2.0)
note(
    "terminals:rescan",
    toasts=ev(TOASTS),
    btn=ev("document.getElementById('rescan-btn')?.textContent.trim()"),
)

# ---- 6. guards flows (opened and cancelled) ----
click_sel('[data-page="guards"]')
time.sleep(0.8)
click_sel("#guard-pick-btn")
time.sleep(0.4)
note(
    "guards:pick_open",
    menuHidden=ev("document.getElementById('guard-pick-menu')?.hidden"),
    groups=ev(
        "[...document.querySelectorAll('#guard-pick-menu .pick-group,#guard-pick-menu h4')].map(g=>g.textContent.trim().slice(0,40))"
    ),
    rows=ev(
        "[...document.querySelectorAll('#guard-pick-list button,#guard-pick-list .pick-row')].map(r=>r.textContent.trim().replace(/\\s+/g,' ').slice(0,80))"
    ),
    shot=shot("guard_pick"),
)
ev(
    "(()=>{const s=document.getElementById('guard-pick-search');s.value='black';s.dispatchEvent(new Event('input',{bubbles:true}));})()"
)
time.sleep(0.4)
note(
    "guards:pick_search",
    rows=ev(
        "[...document.querySelectorAll('#guard-pick-list button,#guard-pick-list .pick-row')].filter(r=>r.offsetParent).map(r=>r.textContent.trim().replace(/\\s+/g,' ').slice(0,80))"
    ),
)
clicked = ev(
    "(()=>{const r=[...document.querySelectorAll('#guard-pick-list button,#guard-pick-list .pick-row')].find(b=>b.offsetParent&&/black/i.test(b.textContent));if(!r)return false;r.click();return true})()"
)
time.sleep(1.0)
note(
    "guards:picked_blackbull",
    clicked=clicked,
    pick=ev("document.getElementById('guard-pick-btn')?.textContent.trim().slice(0,90)"),
    rows=ev("document.getElementById('guard-rows')?.children.length"),
    none=ev(
        "(d=>d&&!d.hidden?d.textContent.trim().slice(0,160):null)(document.getElementById('guard-none'))"
    ),
    shot=shot("guards_blackbull"),
)
click_sel("#g-add")
time.sleep(0.6)
note(
    "guards:add_modal",
    title=ev("document.getElementById('modal-title')?.textContent.trim()"),
    body=ev(
        "document.getElementById('modal-body')?.textContent.trim().replace(/\\s+/g,' ').slice(0,420)"
    ),
    shot=shot("guard_add"),
)
press("Escape", 27)
time.sleep(0.3)
click_sel("#g-templates")
time.sleep(0.5)
note(
    "guards:templates_modal",
    title=ev("document.getElementById('modal-title')?.textContent.trim()"),
    body=ev(
        "document.getElementById('modal-body')?.textContent.trim().replace(/\\s+/g,' ').slice(0,300)"
    ),
    shot=shot("guard_templates"),
)
press("Escape", 27)
time.sleep(0.3)

# ---- 7. settings live validation (no save) ----
click_sel('[data-page="settings"]')
time.sleep(0.8)
orig = ev("document.getElementById('cfg-poll').value")
ev(
    "(()=>{const f=document.getElementById('cfg-poll');f.value='1';f.dispatchEvent(new Event('input',{bubbles:true}));})()"
)
time.sleep(0.9)
note(
    "settings:bad_poll",
    err=ev("document.getElementById('cfg-err-poll_interval_ms')?.textContent.trim().slice(0,160)"),
    said=ev("document.getElementById('cfg-foot-said')?.textContent.trim().slice(0,160)"),
    save=ev("document.getElementById('cfg-save')?.disabled"),
    shot=shot("settings_badpoll"),
)
ev(
    "(()=>{const f=document.getElementById('cfg-poll');f.value="
    + json.dumps(orig)
    + ";f.dispatchEvent(new Event('input',{bubbles:true}));})()"
)
time.sleep(0.9)
note(
    "settings:restored",
    err=ev("document.getElementById('cfg-err-poll_interval_ms')?.textContent.trim().slice(0,160)"),
    said=ev("document.getElementById('cfg-foot-said')?.textContent.trim().slice(0,160)"),
)

# ---- 8. halt/resume button wording (no click: scratch is halted already) ----
note(
    "titlebar",
    halt=ev("document.getElementById('halt-btn')?.textContent.trim()"),
    livePill=ev("document.getElementById('live-pill')?.textContent.trim()"),
    statusDry=ev("document.getElementById('status-dry')?.textContent.trim()"),
    statusCopying=ev("document.getElementById('status-copying')?.textContent.trim()"),
)

# ---- errors collected the whole way ----
note("js_errors", errs=ev("window.__errs||[]"))

print(json.dumps(REPORT, indent=1)[:12000])
WS.close()
