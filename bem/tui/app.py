from __future__ import annotations

from textual.app import App, ComposeResult

from bem.config import Config
from bem.gmail import GmailClient
from bem.tui.screens.inbox import InboxScreen


class BemApp(App):
    """bem — a modern Mutt-inspired terminal email client."""

    CSS = """
    Screen {
        background: $background;
    }
    Notification {
        background: $primary;
        color: $background;
    }
    """

    TITLE = "bem"
    SUB_TITLE = ""

    def __init__(self, gmail: GmailClient, config: Config, **kwargs) -> None:
        super().__init__(**kwargs)
        self._gmail = gmail
        self._config = config

    def on_mount(self) -> None:
        self.push_screen(InboxScreen(gmail=self._gmail, config=self._config))

    def action_search(self) -> None:
        # Delegate to current screen if it supports it
        screen = self.screen
        if hasattr(screen, "action_search_mode"):
            screen.action_search_mode()
