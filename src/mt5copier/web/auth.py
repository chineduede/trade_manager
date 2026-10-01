"""Token auth, Jupyter-notebook style: one random token per run, required on every call.

Stage A has no write path, so there is nothing here for the custom-header CSRF defense
described in WEBUI_PLAN.md to protect yet -- that lands in Stage B, with the first POST route.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Query


class TokenAuth:
    """Holds the run's token and checks it against incoming requests.

    A small object rather than a module-level global so ``create_app`` is the one place a
    token is ever threaded through, and tests can construct one with a known token instead of
    monkeypatching module state.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def check(self, candidate: str | None) -> bool:
        """Constant-time compare -- a token check is exactly the kind of thing timing can leak."""
        return candidate is not None and secrets.compare_digest(candidate, self._token)

    def require(self, token: str | None = Query(default=None)) -> None:
        """FastAPI dependency for page routes: ``Depends(auth.require)``.

        The websocket route does not use this -- it calls :meth:`check` directly and closes the
        connection itself (see ``routes.py``), because a dependency raising an exception cannot
        reject a websocket handshake before ``accept()``.
        """
        if not self.check(token):
            raise HTTPException(status_code=401, detail="missing or invalid token")
