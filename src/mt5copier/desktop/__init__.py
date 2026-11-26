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

import os
import socket
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    is_frozen,
    user_data_dir,
)

log = get_logger(__name__)

_WEB = Path(__file__).resolve().parent / "web"


_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".map": "application/json",
}
"""Spelled out rather than ``mimetypes``, which on Windows reads the registry and has
served .js as text/plain on damaged machines."""


def _load_web() -> dict[str, tuple[bytes, str]]:
    """Every file the window may ask for, read once -- ``{"/app.js": (bytes, type)}``.

    A frozen (onefile) run serves from this snapshot, never from disk after boot: the exe
    extracts itself to a ``_MEI...`` temp directory, and that directory has been seen gutted
    *while the app ran* -- a worker child process exiting tripped the bootloader's cleanup,
    the unlocked data files went, and from then on the window's server answered 404 to
    everything it was asked. The page already on screen hid it until something reloaded.
    """
    out: dict[str, tuple[bytes, str]] = {}
    for f in sorted(_WEB.rglob("*")):
        if not f.is_file():
            continue
        ctype = _TYPES.get(f.suffix.lower(), "application/octet-stream")
        out["/" + f.relative_to(_WEB).as_posix()] = (f.read_bytes(), ctype)
    return out


class _WebHandler(BaseHTTPRequestHandler):
    """Serves the window's files so WebView2 can never run them stale.

    pywebview's built-in server reuses one port (42001), and with the profile's
    persistent WebView2 storage that stable origin let the browser's disk cache serve
    old app.js and app.css across restarts -- the window ran superseded code while the
    files on disk were current (seen live: transferSize 0 on both). ``no-cache`` was not
    enough: entries stored before it carried heuristic freshness and were served without
    ever asking the server again, so they never saw the new header (seen live too: a
    restarted window kept a superseded Settings page while this server already served the
    current one). ``no-store`` keeps these files out of the cache altogether -- they are
    a few local kilobytes, and a cache can only ever make them wrong.

    Frozen, the bytes come from the boot-time snapshot (see :func:`_load_web`); a source
    run reads the file from disk on every request instead, so an edit and a reload is
    still the whole dev loop.
    """

    server: _WebServer

    def do_GET(self) -> None:
        self._answer(head=False)

    def do_HEAD(self) -> None:
        self._answer(head=True)

    def _answer(self, *, head: bool) -> None:
        # Only the window itself is served. The socket already binds loopback, but a web
        # page in an ordinary browser can still aim requests at 127.0.0.1 (DNS rebinding
        # gets it past the browser's origin checks by lying in DNS, not in Host) -- and
        # such a request arrives carrying the attacker's hostname here. WebView2 always
        # asks by the literal address it was given.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self.send_error(403, "Forbidden")
            return
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        hit = self.server.files.get(path)
        if hit is None and self.server.files_dir is not None:
            f = (self.server.files_dir / path.lstrip("/")).resolve()
            if f.is_file() and f.is_relative_to(self.server.files_dir):
                hit = (f.read_bytes(), _TYPES.get(f.suffix.lower(), "application/octet-stream"))
        if hit is None:
            self.send_error(404, "File not found")
            return
        data, ctype = hit
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("web: " + format, *args)


class _WebServer(ThreadingHTTPServer):
    """The window's file server, bound *exclusively*.

    ``http.server`` turns SO_REUSEADDR on, and on Windows that lets a second process bind
    a port another is already serving on -- with the connections still going to the first.
    Seen live: a source run computed the same stable port as the installed app's running
    window (same profile), "bound" it, and the dev window was answered by the installed
    app's server instead. With reuse off (and SO_EXCLUSIVEADDRUSE, which stops others
    doing the same to us) a held port fails the bind honestly, and the caller walks on
    to a free one.
    """

    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        files: dict[str, tuple[bytes, str]],
        files_dir: Path | None,
    ) -> None:
        self.files = files
        self.files_dir = files_dir
        super().__init__(address, _WebHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _serve_web(profile: str) -> str:
    """Start the window's own file server; returns the URL the window opens.

    The port is stable per profile: the origin keys localStorage, so it must survive
    restarts -- and differ between profiles, whose windows run side by side. A source run
    seeds the hash differently, so dev and the installed app never share a port (or an
    origin, or its storage) even on the same profile: the two are different apps on
    different data, exactly as :func:`mt5copier.paths.base_data_dir` separates them.
    """
    frozen = is_frozen()
    seed = profile if frozen else profile + "\ndev"
    files = _load_web() if frozen else {}
    files_dir = None if frozen else _WEB
    base = 42101 + zlib.crc32(seed.encode("utf-8")) % 800
    last: OSError | None = None
    for port in range(base, base + 20):
        try:
            server = _WebServer(("127.0.0.1", port), files, files_dir)
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
    shared_data.migrate()  # before anything reads a shared store: it builds the shared folder
    cfg = config_mod.load()
    guards = guards_mod.load()
    try:
        config_mod.merge_discovery(cfg, discover_terminals())
        config_mod.fold_ctrader_accounts(cfg)
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
    api = DesktopApi(cfg, engine, guards)
    title = "Copier" if profile == DEFAULT_PROFILE else f"Copier — {profile}"
    if not is_frozen():
        # A source run works against MT5Copier-Dev data (see paths.base_data_dir); say so in
        # the taskbar so a dev window is never mistaken for the installed app.
        title += " [dev]"
    if "--remote-debugging-port" in os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", ""):
        # The CDP test recipe's flag. The port it opens is an unauthenticated control
        # channel into this window -- CDP speaks WebSocket, which any web page may open
        # to 127.0.0.1 without CORS standing in the way -- so a session with it on is for
        # testing, never for trading. Said in the taskbar, not only the log, because the
        # danger is precisely the session someone forgot was started this way.
        log.warning(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS opens a remote-debugging port: "
            "this window can be driven by any local process or web page; "
            "do not trade in this session"
        )
        title += " [DEBUG PORT OPEN]"
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
