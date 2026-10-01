"""Token auth: a constant-time check, and the FastAPI dependency page routes use."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mt5copier.web.auth import TokenAuth


def test_check_accepts_only_the_exact_token() -> None:
    auth = TokenAuth("secret-token")
    assert auth.check("secret-token") is True
    assert auth.check("wrong") is False
    assert auth.check("") is False
    assert auth.check(None) is False


def _protected_app(token: str) -> FastAPI:
    app = FastAPI()
    auth = TokenAuth(token)

    @app.get("/protected")
    def protected(_: None = Depends(auth.require)) -> dict[str, bool]:
        return {"ok": True}

    return app


def test_require_rejects_a_missing_token() -> None:
    client = TestClient(_protected_app("secret"))
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_require_rejects_the_wrong_token() -> None:
    client = TestClient(_protected_app("secret"))
    resp = client.get("/protected?token=wrong")
    assert resp.status_code == 401


def test_require_admits_the_correct_token() -> None:
    client = TestClient(_protected_app("secret"))
    resp = client.get("/protected?token=secret")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
