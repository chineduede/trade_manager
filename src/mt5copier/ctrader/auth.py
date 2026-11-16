"""Logging in to cTrader: the app's credentials, the person's tokens, and the accounts they open.

cTrader has no terminal on this machine to find. Access is granted to an *app* (registered at
openapi.ctrader.com, which gives a client id and secret) by a *person* (their cTrader ID), who
picks which of their trading accounts it may use. The grant arrives as an authorisation code on
the app's redirect URI -- here a loopback address, ``http://127.0.0.1:<port>/...``, on which
this module listens for the one request the browser makes -- and is exchanged for an access
token (about 30 days) and a refresh token (no expiry, but replaced on every refresh).

All of it lives in :func:`mt5copier.paths.ctrader_path`, never in ``config.json``, encrypted
for the Windows user on this machine (:mod:`mt5copier.dpapi`): a copy of the file taken anywhere
else is useless. A portable copy moved to another machine therefore has to log in again.

Only the supervisor refreshes tokens, never a worker: a refresh invalidates the old refresh
token, so two processes refreshing at once would leave one of them holding a dead one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .. import dpapi, shared_data
from ..guards.model import write_json
from ..logging_setup import get_logger
from ..paths import ctrader_path
from .client import DEMO_HOST, CTraderClient, OpenApiError
from .proto import OpenApiMessages_pb2 as oa

log = get_logger(__name__)

DEFAULT_REDIRECT_URI = "http://localhost:9290/callback"
AUTHORIZE_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"

REFRESH_BEFORE_S = 7 * 24 * 3600
"""Refresh a token with less than this left. It lasts about 30 days, so a copier started at
least weekly never sees one expire."""

LOGIN_TIMEOUT_S = 300.0

_FORMAT = "dpapi"
"""The saved file is ``{"format": "dpapi", "data": <base64 of the encrypted JSON>}``. A file
without it is the plain JSON written before encryption, read as it is until
:func:`encrypt_saved` rewrites it."""


class CTraderAccount(BaseModel):
    """One trading account the person let the app use."""

    account_id: int
    """``ctidTraderAccountId``: the Open API's id for it, not the login the broker shows."""
    is_live: bool
    login: int | None = None
    broker: str = ""


class CTraderCredentials(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    """When the access token stops working, in UTC seconds."""
    accounts: list[CTraderAccount] = Field(default_factory=list)

    @property
    def has_app(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)

    def account(self, account_id: int) -> CTraderAccount | None:
        return next((a for a in self.accounts if a.account_id == account_id), None)


class LoginError(Exception):
    """The login could not finish; the message says why, in words for the person."""


class LoginCancelled(LoginError):  # noqa: N818 -- a subclass of one, not a new error
    """The person gave the login up, by starting another."""


def _read(target: Path) -> tuple[CTraderCredentials, bool]:
    """The saved credentials, and whether they were encrypted."""
    raw = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("format") == _FORMAT:
        try:
            plain = dpapi.unprotect(base64.b64decode(str(raw["data"])))
        except OSError as exc:
            raise ValueError(
                "they were encrypted by another Windows user or on another machine, or are "
                f"damaged ({exc.strerror or exc})"
            ) from None
        return CTraderCredentials.model_validate(json.loads(plain)), True
    return CTraderCredentials.model_validate(raw), False


def _why(exc: Exception) -> str:
    """Why the file could not be read, in words that never quote it: a validation error names
    the values it refused, and those may be secrets."""
    if isinstance(exc, ValidationError):
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "the whole file" for e in exc.errors()}
        )
        return f"invalid {', '.join(fields)}"
    if isinstance(exc, json.JSONDecodeError):
        return f"not JSON (line {exc.lineno}, column {exc.colno})"
    if isinstance(exc, ValueError):
        return str(exc)
    return type(exc).__name__


def load(path: Path | None = None) -> CTraderCredentials:
    target = path or ctrader_path()
    if not target.exists():
        return CTraderCredentials()
    try:
        return _read(target)[0]
    except Exception as exc:
        backup = target.with_suffix(f".bad-{os.getpid()}.json")
        try:
            target.replace(backup)
            log.error(
                "cTrader credentials at %s could not be read (%s); moved to %s; press t to "
                "log in again",
                target,
                _why(exc),
                backup,
            )
        except OSError:
            log.error("cTrader credentials at %s could not be read (%s)", target, _why(exc))
        return CTraderCredentials()


def save(creds: CTraderCredentials, path: Path | None = None) -> Path:
    target = path or ctrader_path()
    blob = dpapi.protect(creds.model_dump_json().encode("utf-8"))
    write_json(target, {"format": _FORMAT, "data": base64.b64encode(blob).decode("ascii")})
    return target


def encrypt_saved(path: Path | None = None) -> bool:
    """Rewrite credentials saved in plain JSON, encrypted. True if it did.

    Only the supervisor calls this, at start-up, before any worker reads the file: a worker
    rewriting it could put back tokens the supervisor had just refreshed."""
    target = path or ctrader_path()
    if not target.exists():
        return False
    try:
        creds, encrypted = _read(target)
    except Exception:
        return False  # load() deals with it, and says why.
    if encrypted:
        return False
    save(creds, target)
    log.info("cTrader credentials at %s are now encrypted", target)
    return True


def authorize_url(creds: CTraderCredentials, state: str = "") -> str:
    """The login page. ``state`` is a one-off value for the redirect to carry back, so that a
    redirect from a login this app did not start can be told apart (see :func:`wait_for_code`)."""
    params = {
        "client_id": creds.client_id,
        "redirect_uri": creds.redirect_uri,
        "scope": "trading",
        "product": "web",
    }
    if state:
        params["state"] = state
    query = urllib.parse.urlencode(params)
    return f"{AUTHORIZE_URL}?{query}"


class _HTTPServer6(HTTPServer):
    address_family = socket.AF_INET6


def _listen(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> list[HTTPServer]:
    """The listening sockets for the redirect. ``localhost`` gets one on IPv4 and one on IPv6,
    since a browser may try either first. A port still held by a login just given up is waited
    for, briefly."""
    if host == "localhost":
        addresses: list[tuple[type[HTTPServer], str]] = [
            (HTTPServer, "127.0.0.1"),
            (_HTTPServer6, "::1"),
        ]
    else:
        addresses = [(_HTTPServer6 if ":" in host else HTTPServer, host)]
    deadline = time.monotonic() + 3.0
    while True:
        servers: list[HTTPServer] = []
        first_error: OSError | None = None
        for i, (cls, address) in enumerate(addresses):
            try:
                servers.append(cls((address, port), handler))
            except OSError as exc:
                if i == 0:
                    first_error = exc
        # The first address must listen; the IPv6 one beside localhost is a bonus, as a
        # machine without IPv6 cannot bind it.
        if first_error is None:
            return servers
        for server in servers:
            server.server_close()
        if time.monotonic() > deadline:
            raise LoginError(f"could not listen on {host}:{port} for the login ({first_error})")
        time.sleep(0.2)


def wait_for_code(
    redirect_uri: str,
    timeout: float = LOGIN_TIMEOUT_S,
    cancel: threading.Event | None = None,
    state: str = "",
) -> str:
    """Listen on the redirect URI's port for the browser's one request and return its code.

    Any path is accepted: cTrader only ever sends the browser to the URI the app registered,
    and what matters is the ``code`` (or ``error``) on it. Setting ``cancel`` gives up within
    half a second, with :class:`LoginCancelled`.

    A redirect carrying a ``state`` other than the one given is turned away and the wait goes
    on: it is not from this login, and taking its code would link someone else's cTrader
    accounts. One carrying no state is taken, since cTrader's documentation does not promise
    to send it back; whether it did is logged."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    found: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- the stdlib's name
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            came_back = query.get("state", [None])[0]
            if state and came_back is not None and came_back != state:
                log.warning("turned away a login redirect that was not from this login")
                body = b"This link is not from the login the copier started."
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "code" in query:
                found["code"] = query["code"][0]
                if state:
                    log.info(
                        "cTrader %s the login's state",
                        "sent back" if came_back is not None else "did not send back",
                    )
                words = "cTrader is linked. You can close this tab and go back to the copier."
            elif "error" in query:
                found["error"] = query.get("error_description", query["error"])[0]
                words = f"cTrader said: {found['error']}. Go back to the copier."
            else:
                # A favicon request, or something else: not the redirect.
                self.send_response(404)
                self.end_headers()
                return
            body = f"<!doctype html><title>mt5copier</title><p>{words}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # Not the request line: its query carries the authorisation code.
            path = urllib.parse.urlparse(getattr(self, "path", "")).path
            log.debug("redirect listener: %s %s", getattr(self, "command", "?"), path)

    servers = _listen(host, port, Handler)
    deadline = time.monotonic() + timeout
    try:
        while not found and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise LoginCancelled("the login was given up")
            ready, _, _ = select.select(servers, [], [], 0.5)
            for server in ready:
                server.handle_request()
    finally:
        for server in servers:
            server.server_close()
    if "error" in found:
        raise LoginError(f"cTrader refused the login: {found['error']}")
    if "code" not in found:
        raise LoginError("no answer from cTrader in time; the login was given up")
    return found["code"]


def _token_call(params: dict[str, str]) -> dict[str, object]:
    url = f"{TOKEN_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LoginError(f"could not reach cTrader for a token ({exc})") from exc
    if not isinstance(answer, dict):
        raise LoginError("cTrader's token answer was not understood")
    if answer.get("errorCode") or not answer.get("accessToken"):
        raise LoginError(
            f"cTrader would not give a token: {answer.get('description') or answer.get('errorCode')}"
        )
    return answer


def _take_tokens(creds: CTraderCredentials, answer: dict[str, object]) -> None:
    creds.access_token = str(answer["accessToken"])
    creds.refresh_token = str(answer.get("refreshToken") or creds.refresh_token)
    expires_in = answer.get("expiresIn")
    seconds = float(expires_in) if isinstance(expires_in, (int, float, str)) else 2_628_000.0
    creds.expires_at = time.time() + seconds


def exchange_code(creds: CTraderCredentials, code: str) -> None:
    _take_tokens(
        creds,
        _token_call(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": creds.redirect_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
            }
        ),
    )


def refresh(creds: CTraderCredentials) -> None:
    if not creds.refresh_token:
        raise LoginError("there is no cTrader login to refresh")
    _take_tokens(
        creds,
        _token_call(
            {
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
            }
        ),
    )


def refresh_if_due(path: Path | None = None) -> bool:
    """Refresh the saved token if it is close to expiring. True if it was refreshed.

    Called by the supervisor at start-up, before any cTrader worker reads the file. The file
    is shared by every profile and a refresh invalidates the old refresh token, so the whole
    check-refresh-save sits under the shared-folder lock, and the check reads the file only
    once inside it: two windows starting together refresh once -- the loser waits, re-reads
    a token that is no longer due, and walks away with it. Generous timeout: the winner is
    holding the lock across the token call itself."""
    quick = load(path)
    if not (quick.has_app and quick.refresh_token):
        return False
    with shared_data.shared_lock(timeout_s=30.0):
        creds = load(path)  # fresh: the lock's previous holder may have refreshed it
        if not (creds.has_app and creds.refresh_token):
            return False
        if creds.expires_at - time.time() > REFRESH_BEFORE_S:
            return False
        refresh(creds)
        save(creds, path)
    log.info("cTrader token refreshed; good until %s", time.ctime(creds.expires_at))
    return True


async def _fetch_accounts(creds: CTraderCredentials) -> list[CTraderAccount]:
    # The account list is the same from either host.
    client = CTraderClient(DEMO_HOST)
    await client.connect()
    try:
        await client.request(
            oa.ProtoOAApplicationAuthReq(clientId=creds.client_id, clientSecret=creds.client_secret)
        )
        answer = await client.request(
            oa.ProtoOAGetAccountListByAccessTokenReq(accessToken=creds.access_token)
        )
    finally:
        await client.close()
    return [
        CTraderAccount(
            account_id=int(a.ctidTraderAccountId),
            is_live=bool(a.isLive),
            login=int(a.traderLogin) if a.HasField("traderLogin") else None,
            broker=a.brokerTitleShort,
        )
        for a in answer.ctidTraderAccount
    ]


def fetch_accounts(creds: CTraderCredentials) -> list[CTraderAccount]:
    try:
        return asyncio.run(_fetch_accounts(creds))
    except OpenApiError as exc:
        raise LoginError(f"cTrader would not list the accounts: {exc}") from exc
    except (OSError, TimeoutError, ConnectionError) as exc:
        raise LoginError(f"could not reach cTrader to list the accounts ({exc!r})") from exc


def log_in(
    creds: CTraderCredentials,
    path: Path | None = None,
    cancel: threading.Event | None = None,
) -> list[CTraderAccount]:
    """The whole login: the browser, the code, the token, the accounts. Blocks for as long as
    the person takes in the browser, so never call it on the UI thread.

    Saves the credentials when it succeeds and returns the accounts the person allowed."""
    if not creds.has_app:
        raise LoginError("the cTrader app's client id and secret are not set")
    state = secrets.token_urlsafe(16)
    url = authorize_url(creds, state)
    log.info("opening the browser for the cTrader login")
    if not webbrowser.open(url):
        log.warning("could not open a browser; the login page is %s", url)
    code = wait_for_code(creds.redirect_uri, cancel=cancel, state=state)
    exchange_code(creds, code)
    creds.accounts = fetch_accounts(creds)
    save(creds, path)
    log.info("cTrader login: %d account(s) allowed", len(creds.accounts))
    return creds.accounts
