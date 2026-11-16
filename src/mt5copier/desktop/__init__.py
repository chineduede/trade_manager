"""The pywebview desktop window -- the app's only UI.

It began 2026-11-14 as one window holding the direct-trading page -- the ticket, a real
candlestick chart with draggable levels, and the book -- grew a page per TUI feature, and
on 2026-11-16 replaced the TUI outright.

Boot is what the TUI's ``run`` used to do -- same config, same guards, same engine --
because the window is a view, not a different app. The WebView2 profile lives under this
profile's own data directory, so side-by-side profiles stay isolated down to their
browser storage, matching the engine-level claim locks.
"""

from __future__ import annotations

import threading
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config as config_mod
from .. import shared_data
from ..claims import ClaimBook
from ..ctrader import auth as ctrader_auth
from ..discovery import discover_terminals
from ..engine import Engine
from ..guards import model as guards_mod
from ..logging_setup import get_logger
from ..paths import (
    DEFAULT_PROFILE,
    active_profile,
    claims_dir,
    config_path,
    guards_path,
    is_frozen,
    user_data_dir,
)
from ..setups import load as load_setups

log = get_logger(__name__)

_WEB = Path(__file__).resolve().parent / "web"


class _WebHandler(SimpleHTTPRequestHandler):
    """Serves the window's files so WebView2 can never run them stale.

    pywebview's built-in server reuses one port (42001), and with the profile's
    persistent WebView2 storage that stable origin let the browser's disk cache serve
    old app.js and app.css across restarts -- the window ran superseded code while the
    files on disk were current (seen live: transferSize 0 on both). ``no-cache`` makes
    every load revalidate against disk, a localhost 304 when nothing changed.
    """

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("web: " + format, *args)


def _serve_web(profile: str) -> str:
    """Start the window's own file server; returns the URL the window opens.

    The port is stable per profile: the origin keys localStorage, so it must survive
    restarts -- and differ between profiles, whose windows run side by side.
    """
    handler = partial(_WebHandler, directory=str(_WEB))
    base = 42101 + zlib.crc32(profile.encode("utf-8")) % 800
    last: OSError | None = None
    for port in range(base, base + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            last = exc
            continue
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        return f"http://127.0.0.1:{port}/index.html"
    raise RuntimeError(f"no free port for the window's files near {base}: {last}")


def run() -> None:
    """Entry point: load config, fold in a discovery sweep, then hand over to the window."""
    import webview  # heavy: imported only once a window is actually about to open

    from .api import DesktopApi

    profile = active_profile()
    shared_data.migrate()  # before anything reads a shared path: it builds the shared folder
    cfg = config_mod.load()
    guards = guards_mod.load(guards_path())
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.save(cfg)
    except Exception:
        log.exception("initial discovery failed; starting with saved config only")
    try:
        ctrader_auth.encrypt_saved()
        ctrader_auth.refresh_if_due()
    except Exception as exc:
        log.warning("could not refresh the cTrader login (%s)", exc)
    log.info("config at %s (profile %s, desktop)", config_path(), profile)

    engine = Engine(cfg, guards=guards, claims=ClaimBook(claims_dir(), profile))
    api = DesktopApi(cfg, engine, guards, load_setups())
    title = "Copier" if profile == DEFAULT_PROFILE else f"Copier — {profile}"
    if not is_frozen():
        # A source run works against MT5Copier-Dev data (see paths.base_data_dir); say so in
        # the taskbar so a dev window is never mistaken for the installed app.
        title += " [dev]"
    window = webview.create_window(
        title,
        url=_serve_web(profile),
        js_api=api,
        width=1500,
        height=980,
        min_size=(1080, 720),
        frameless=True,
        easy_drag=False,
        background_color="#1b1813",
    )
    if window is None:  # pywebview types this as fallible; in practice it raises instead
        raise RuntimeError("pywebview could not create the window")
    api.attach(window)

    def _native_frame(*_args: object) -> None:
        """Once the window exists, give it back the styles Snap needs -- without the frame
        they would normally paint. See :mod:`.winframe`."""
        from System import Action  # pythonnet, loaded with webview

        from . import winframe

        try:
            # ``native`` is the WinForms form; pywebview types it as None until shown.
            form = window.native
            hwnd = int(form.Handle.ToInt64())  # type: ignore[attr-defined]

            # comctl32's subclassing helpers refuse to work across threads, and pywebview
            # fires ``shown`` off the UI thread -- marshal onto the thread that owns the
            # window, or SetWindowSubclass fails and the frame stays borderless.
            def _dress_window() -> None:
                winframe.adopt(hwnd)
                winframe.set_icon(hwnd, _WEB / "icon.ico")

            form.Invoke(Action(_dress_window))  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not adopt native frame behaviours; the window still runs")

    window.events.shown += _native_frame

    engine.start()
    try:
        # Blocks until the window closes. One window per process, one process per profile:
        # the profile's own WebView2 storage keeps instances apart, like everything else.
        webview.start(
            private_mode=False,
            storage_path=str(user_data_dir() / "webview"),
        )
    finally:
        engine.stop()


__all__ = ["run"]
