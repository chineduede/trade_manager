"""The window's file server: boot-time snapshot, exclusive port, honest 404s.

Two live failures shaped this server. A frozen (onefile) run's ``_MEI`` extraction dir was
gutted while the app ran -- a worker child exiting tripped the bootloader cleanup -- and the
on-disk server answered 404 to everything from then on. And ``http.server``'s SO_REUSEADDR
let a dev run "bind" the port the installed app was already serving on, so the dev window
was answered by the other process entirely.
"""

from __future__ import annotations

import http.client
import threading
import urllib.request

import pytest

from mt5copier.desktop import _WEB, _load_web, _WebServer


@pytest.fixture
def served():
    """A bound server plus a tiny client; the factory picks snapshot or disk mode."""
    servers: list[_WebServer] = []

    def start(files, files_dir) -> int:
        server = _WebServer(("127.0.0.1", 0), files, files_dir)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1]

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def get(port: int, path: str):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)


def test_the_snapshot_holds_the_whole_page() -> None:
    files = _load_web()
    assert set(files) >= {"/index.html", "/app.js", "/app.css"}
    body, ctype = files["/index.html"]
    assert b"<!doctype html>" in body.lower()
    assert ctype.startswith("text/html")
    assert files["/app.js"][1].startswith("text/javascript")


def test_a_frozen_run_serves_from_memory_alone(served) -> None:
    port = served(_load_web(), None)  # files_dir None: disk is never touched again
    with get(port, "/index.html") as r:
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("text/html")
        assert "no-store" in r.headers["Cache-Control"]
        assert len(r.read()) == int(r.headers["Content-Length"])
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(port, "/nothing-here.js")
    assert caught.value.code == 404


def test_a_source_run_serves_todays_file_from_disk(served) -> None:
    port = served({}, _WEB)
    with get(port, "/app.css") as r:
        assert r.status == 200
        assert r.read() == (_WEB / "app.css").read_bytes(), "the file as it is now, not a copy"


def test_disk_mode_never_leaves_the_web_folder(served) -> None:
    port = served({}, _WEB)
    # Raw connection: urllib would tidy the .. away before the server ever saw it.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/../api.py")  # a real file, one level up
    assert conn.getresponse().status == 404
    conn.close()


def test_a_held_port_refuses_the_second_bind() -> None:
    first = _WebServer(("127.0.0.1", 0), {}, _WEB)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            _WebServer(("127.0.0.1", port), {}, _WEB)
    finally:
        first.server_close()
