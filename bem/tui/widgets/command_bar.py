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

    # Commands whose argument is a folder/label name — Tab completes against the
    # folder list the screen hands us via set_completions().
    _FOLDER_CMDS = {"move", "mv", "folder", "cd", "go"}

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffer: str = ""
        self._completions: list[str] = []      # candidate folder names
        self._tab_stem: str = ""               # what the user typed before Tab
        self._tab_matches: list[str] | None = None
        self._tab_index: int = 0

    def render(self) -> str:
        return f":{self._buffer}"

    def show(self, prefill: str = "") -> None:
        self._buffer = prefill
        self._reset_tab()
        self.display = True
        self.focus()
        self.refresh()

    def hide(self) -> None:
        self._buffer = ""
        self._reset_tab()
        self.display = False
        self.refresh()

    def suggest(self, prefix: str, text: str) -> None:
        """Complete the buffer with an async suggestion — but only if the user
        hasn't typed beyond the prefix it was requested for."""
        if self.display and self._buffer == prefix:
            self._buffer = prefix + text
            self.refresh()

    def set_completions(self, names: list[str]) -> None:
        """Hand the bar the current folder names for Tab completion."""
        self._completions = names

    def _reset_tab(self) -> None:
        self._tab_matches = None

    def _complete(self) -> None:
        """Tab: complete the folder argument, cycling through matches on repeat."""
        parts = self._buffer.split(None, 1)
        if not parts or parts[0].lower() not in self._FOLDER_CMDS or not self._completions:
            return
        if self._tab_matches is None:
            stem = parts[1] if len(parts) > 1 else ""
            lowered = stem.lower()
            matches = [c for c in self._completions if c.lower().startswith(lowered)]
            if not matches:  # fall back to substring match
                matches = [c for c in self._completions if lowered in c.lower()]
            if not matches:
                return
            self._tab_stem = stem
            self._tab_matches = matches
            self._tab_index = 0
        else:
            self._tab_index = (self._tab_index + 1) % len(self._tab_matches)
        self._buffer = f"{parts[0]} {self._tab_matches[self._tab_index]}"
        self.refresh()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        if event.key == "tab":
            self._complete()
            return
        # Any other key ends the current Tab-cycle; the next Tab recomputes.
        self._reset_tab()
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
