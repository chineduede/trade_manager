"""The cTrader login: ``t`` on the Map.

The first time, it asks for the Open API app's client id and secret, and the redirect URI the
app was registered with (it must match exactly, or cTrader refuses the login). Then the browser
opens on cTrader's own page, where the person logs in and ticks the accounts the copier may use.
Those accounts appear under NOT YET OPENED, to be made slaves with ``s``.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..ctrader.auth import CTraderCredentials
from .style import MUTED, RED
from .widgets import ButtonRow


class CTraderDialog(ModalScreen[CTraderCredentials | None]):
    """Dismisses with the credentials to log in with, or None when the person backs out."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, creds: CTraderCredentials) -> None:
        super().__init__()
        self._creds = creds

    def compose(self) -> ComposeResult:
        creds = self._creds
        with VerticalScroll(id="dialog", classes="dialog", can_focus=False):
            yield Static(Text("LOG IN TO CTRADER", style="bold"), classes="dialog-title")
            yield Static(
                "Your browser opens on cTrader's login. Tick the accounts the copier may use; "
                "they appear under NOT YET OPENED, and only ever as slaves."
            )
            if creds.accounts:
                yield Static(
                    Text(
                        f"Logged in now, with {len(creds.accounts)} account"
                        f"{'s' if len(creds.accounts) != 1 else ''}. Logging in again picks "
                        "them afresh.",
                        style=MUTED,
                    )
                )
            yield Static(Text("The Open API app (openapi.ctrader.com):", style="bold"))
            with Horizontal(classes="field"):
                yield Static("Client id", classes="field-label")
                yield Input(creds.client_id, id="client-id", compact=True)
            with Horizontal(classes="field"):
                yield Static("Secret", classes="field-label")
                yield Input(creds.client_secret, id="secret", password=True, compact=True)
            with Horizontal(classes="field"):
                yield Static("Redirect URI", classes="field-label")
                yield Input(creds.redirect_uri, id="redirect", compact=True)
            yield Static(
                Text(
                    "The redirect URI must be exactly as registered with the app, on 127.0.0.1 "
                    "or localhost. The id and secret are kept in ctrader.json, not in the config.",
                    style=MUTED,
                )
            )
            yield Static("", id="error")
            with ButtonRow(id="buttons"):
                yield Button("Cancel  esc", id="cancel", classes="keep")
                yield Button("Log in  enter", id="go")

    def on_mount(self) -> None:
        first = "#client-id" if not self._creds.client_id else "#redirect"
        self.query_one(first, Input).focus()

    def _value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _go(self) -> None:
        client_id = self._value("#client-id")
        secret = self._value("#secret")
        redirect = self._value("#redirect")
        problem = None
        if not client_id or not secret:
            problem = "The client id and the secret are both needed."
        elif not redirect.startswith(("http://127.0.0.1:", "http://localhost:")):
            problem = "The redirect URI must be http://127.0.0.1:<port>/... or localhost."
        if problem:
            self.query_one("#error", Static).update(Text(problem, style=RED))
            return
        self.dismiss(
            self._creds.model_copy(
                update={"client_id": client_id, "client_secret": secret, "redirect_uri": redirect}
            )
        )

    @on(Input.Submitted)
    def _on_submit(self) -> None:
        self._go()

    @on(Button.Pressed, "#go")
    def _on_go(self) -> None:
        self._go()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
