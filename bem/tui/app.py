from __future__ import annotations

from textual.app import App, ComposeResult
from textual.theme import Theme

from bem.config import Config
from bem.gmail import GmailClient
from bem.tui.screens.inbox import InboxScreen


# A 1980s phosphor-green CRT look: near-black background, glowing green text and
# chrome. Errors stay a touch red so failures don't vanish into the green.
GREEN_SCREEN_THEME = Theme(
    name="green-screen",
    primary="#33ff33",
    secondary="#1f9f3f",
    accent="#7bff8a",
    foreground="#2be02b",
    background="#001000",
    surface="#001a00",
    panel="#002600",
    success="#33ff66",
    warning="#d7d000",
    error="#ff5f5f",
    dark=True,
    variables={
        "block-cursor-foreground": "#001000",
        "block-cursor-background": "#33ff33",
        "border": "#1f9f3f",
    },
)

# Friendly config/command names → the registered Textual theme id.
_THEME_IDS = {
    "dark": "textual-dark",
    "light": "textual-light",
    "green": "green-screen",
    "green-screen": "green-screen",
}
# The names accepted by `theme = …` in config and the `:theme` command.
THEME_CHOICES = ("dark", "light", "green")


def resolve_theme(name: str | None) -> str:
    """Map a config/command theme name to a registered Textual theme id."""
    return _THEME_IDS.get((name or "dark").strip().lower(), "textual-dark")


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
        self.register_theme(GREEN_SCREEN_THEME)
        self.theme = resolve_theme(self._config.theme)
        self.push_screen(InboxScreen(gmail=self._gmail, config=self._config))

    def action_search(self) -> None:
        # Delegate to current screen if it supports it
        screen = self.screen
        if hasattr(screen, "action_search_mode"):
            screen.action_search_mode()
