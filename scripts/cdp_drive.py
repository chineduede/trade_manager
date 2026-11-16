"""Drive the desktop app over CDP for interaction testing.

WebView2 is Chromium: launching the app with
``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>`` exposes the
DevTools protocol. This script launches a profile that way, finds the app page, and
runs JavaScript / takes screenshots against the real running app.

Usage (run under the project venv, websocket-client needed for everything but launch):

    uv run --with websocket-client python scripts/cdp_drive.py launch <profile> [port]
    uv run --with websocket-client python scripts/cdp_drive.py wait [port]
    uv run --with websocket-client python scripts/cdp_drive.py eval "<js>" [port]
    uv run --with websocket-client python scripts/cdp_drive.py shot <out.png> [port]
    uv run --with websocket-client python scripts/cdp_drive.py close [port]

``eval`` awaits promises and prints the JSON value. Each call reconnects; state lives
in the app, not here.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

DEFAULT_PORT = 9223


def _targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _app_target(port: int) -> dict | None:
    try:
        targets = _targets(port)
    except OSError:
        return None
    for t in targets:
        if t.get("type") == "page" and "127.0.0.1:42" in t.get("url", ""):
            return t
    return None


def _connect(port: int):
    import websocket

    target = _app_target(port)
    if target is None:
        raise SystemExit(f"no app page on CDP port {port} (is the app up?)")
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
    return ws


def _rpc(ws, method: str, params: dict | None = None, _id: list[int] = [0]) -> dict:  # noqa: B006 -- shared call-id counter, on purpose
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id[0]:
            if "error" in msg:
                raise SystemExit(f"CDP error: {msg['error']}")
            return msg.get("result", {})


def cmd_launch(profile: str, port: int) -> int:
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "mt5copier", "--desktop", "--profile", profile],
        env=env,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print(f"launched pid {proc.pid} profile {profile!r} cdp port {port}")
    return 0


def cmd_wait(port: int, timeout_s: float = 60.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        target = _app_target(port)
        if target is not None:
            print(json.dumps({"url": target["url"], "title": target.get("title", "")}))
            return 0
        time.sleep(0.5)
    print("timed out waiting for the app page")
    return 1


def cmd_eval(expr: str, port: int) -> int:
    ws = _connect(port)
    try:
        result = _rpc(
            ws,
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
        )
    finally:
        ws.close()
    if "exceptionDetails" in result:
        detail = result["exceptionDetails"]
        text = detail.get("exception", {}).get("description") or detail.get("text", "")
        print(f"JS exception: {text}")
        return 1
    print(json.dumps(result.get("result", {}).get("value"), default=str))
    return 0


def cmd_shot(out_path: str, port: int) -> int:
    ws = _connect(port)
    try:
        result = _rpc(ws, "Page.captureScreenshot", {"format": "png"})
    finally:
        ws.close()
    with open(out_path, "wb") as fh:
        fh.write(base64.b64decode(result["data"]))
    print(out_path)
    return 0


_KEYS = {
    "Escape": ("Escape", 27),
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "F1": ("F1", 112),
    "ArrowDown": ("ArrowDown", 40),
    "ArrowUp": ("ArrowUp", 38),
}


def cmd_input(spec: str, port: int) -> int:
    """One or more input steps, ';'-separated.

    move:X:Y | click:X:Y | rclick:X:Y | dblclick:X:Y | press:Escape | type:some text |
    pause:MS — mouse events go through real CDP hit-testing.
    """
    ws = _connect(port)
    try:
        for step in spec.split(";"):
            step = step.strip()
            if not step:
                continue
            kind, _, rest = step.partition(":")
            if kind == "pause":
                time.sleep(int(rest) / 1000)
                continue
            if kind == "type":
                _rpc(ws, "Input.insertText", {"text": rest})
                continue
            if kind == "press":
                key, code = _KEYS[rest]
                for ev in ("rawKeyDown", "keyUp"):
                    _rpc(
                        ws,
                        "Input.dispatchKeyEvent",
                        {
                            "type": ev,
                            "key": key,
                            "code": key,
                            "windowsVirtualKeyCode": code,
                            "nativeVirtualKeyCode": code,
                        },
                    )
                continue
            x, y = (float(v) for v in rest.split(":"))
            if kind == "move":
                _rpc(
                    ws,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                )
                continue
            button = "right" if kind == "rclick" else "left"
            clicks = 2 if kind == "dblclick" else 1
            _rpc(
                ws,
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
            )
            for _ in range(clicks):
                for ev in ("mousePressed", "mouseReleased"):
                    _rpc(
                        ws,
                        "Input.dispatchMouseEvent",
                        {
                            "type": ev,
                            "x": x,
                            "y": y,
                            "button": button,
                            "clickCount": clicks,
                        },
                    )
    finally:
        ws.close()
    print("ok")
    return 0


def cmd_close(port: int) -> int:
    ws = _connect(port)
    try:
        _rpc(ws, "Runtime.evaluate", {"expression": "pywebview.api.win_close()"})
    finally:
        ws.close()
    print("close asked")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    if cmd == "launch":
        profile = rest[0]
        port = int(rest[1]) if len(rest) > 1 else DEFAULT_PORT
        return cmd_launch(profile, port)
    if cmd == "wait":
        return cmd_wait(int(rest[0]) if rest else DEFAULT_PORT)
    if cmd == "eval":
        expr = rest[0]
        port = int(rest[1]) if len(rest) > 1 else DEFAULT_PORT
        return cmd_eval(expr, port)
    if cmd == "shot":
        out = rest[0]
        port = int(rest[1]) if len(rest) > 1 else DEFAULT_PORT
        return cmd_shot(out, port)
    if cmd == "input":
        spec = rest[0]
        port = int(rest[1]) if len(rest) > 1 else DEFAULT_PORT
        return cmd_input(spec, port)
    if cmd == "close":
        return cmd_close(int(rest[0]) if rest else DEFAULT_PORT)
    print(f"unknown command {cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
