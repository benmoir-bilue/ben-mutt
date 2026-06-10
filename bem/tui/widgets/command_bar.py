from __future__ import annotations

from textual.message import Message as TMessage
from textual.widget import Widget
from textual import events


class CommandBar(Widget):
    """Vim-style command line. Captures raw keypresses — no Input widget."""

    can_focus = True

    DEFAULT_CSS = """
    CommandBar {
        dock: bottom;
        height: 1;
        display: none;
        background: $surface;
        color: $foreground;
        padding: 0 0;
    }
    """

    class CommandSubmitted(TMessage):
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    class Dismissed(TMessage):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffer: str = ""

    def render(self) -> str:
        return f":{self._buffer}"

    def show(self, prefill: str = "") -> None:
        self._buffer = prefill
        self.display = True
        self.focus()
        self.refresh()

    def hide(self) -> None:
        self._buffer = ""
        self.display = False
        self.refresh()

    def suggest(self, prefix: str, text: str) -> None:
        """Complete the buffer with an async suggestion — but only if the user
        hasn't typed beyond the prefix it was requested for."""
        if self.display and self._buffer == prefix:
            self._buffer = prefix + text
            self.refresh()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        if event.key == "escape":
            self.hide()
            self.post_message(self.Dismissed())
        elif event.key == "enter":
            cmd = self._buffer.strip()
            self.hide()
            if cmd:
                self.post_message(self.CommandSubmitted(cmd))
            else:
                self.post_message(self.Dismissed())
        elif event.key == "backspace":
            self._buffer = self._buffer[:-1]
            self.refresh()
        elif event.character and event.character.isprintable():
            self._buffer += event.character
            self.refresh()
