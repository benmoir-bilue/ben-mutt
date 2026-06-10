from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from rich.markup import escape
from rich.text import Text
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import RichLog, Label
from textual.containers import Vertical
from textual.app import ComposeResult
from textual import events
from textual.screen import ModalScreen
from textual.worker import Worker


class AIPanel(ModalScreen):
    """Modal overlay that streams AI responses."""

    DEFAULT_CSS = """
    AIPanel {
        align: center middle;
    }
    #ai-container {
        width: 80%;
        height: 80%;
        border: double $accent;
        background: $surface;
        padding: 0;
    }
    #ai-title {
        text-style: bold;
        color: $accent;
        padding: 1 2 0 2;
    }
    #ai-body {
        height: 1fr;
        padding: 0 2;
        background: $surface;
    }
    #ai-footer {
        /* height includes padding: 1 line of text + 1 line bottom padding */
        padding: 0 2 1 2;
        color: $text-muted;
        height: 2;
    }
    """

    BINDINGS = [
        Binding("escape,q", "dismiss", "Close", show=False),
    ]

    def __init__(
        self,
        title: str,
        work_fn: Callable[["AIPanel"], Optional[Worker]],
        confirm_label: Optional[str] = None,
        confirm_fn: Optional[Callable[["AIPanel"], None]] = None,
        line_style: Optional[Callable[[str], Optional[str]]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._work_fn = work_fn
        self._confirm_label = confirm_label
        self._confirm_fn = confirm_fn
        self._line_style = line_style
        self._collected: list[str] = []
        self._awaiting_confirm = False
        self._line_buf: str = ""
        self._worker: Optional[Worker] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="ai-container"):
            yield Label(f"  {self._title}", id="ai-title")
            yield RichLog(id="ai-body", markup=True, wrap=True, highlight=False)
            yield Label("  Esc/q close    j/k/Space/b scroll", id="ai-footer")

    def on_mount(self) -> None:
        self._worker = self._work_fn(self)

    def on_unmount(self) -> None:
        # Stop the streaming worker so it doesn't write into a dismissed screen
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    @property
    def body(self) -> Optional[RichLog]:
        """The output log, or None while the panel is being torn down."""
        if not self.is_mounted:
            return None
        try:
            return self.query_one("#ai-body", RichLog)
        except NoMatches:
            return None

    def _render_line(self, line: str) -> Text:
        style = self._line_style(line) if self._line_style else None
        return Text(line, style=style or "")

    @property
    def full_text(self) -> str:
        """Everything streamed into the panel, for confirm actions."""
        return "".join(self._collected)

    def append_text(self, chunk: str) -> None:
        body = self.body
        if body is None:
            return
        self._collected.append(chunk)
        self._line_buf += chunk
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            body.write(self._render_line(line))

    def set_error(self, message: str) -> None:
        body = self.body
        if body is None:
            return
        body.write(f"[red]Error: {escape(message)}[/red]")

    def mark_done(self) -> None:
        body = self.body
        if body is None:
            return
        if self._line_buf:
            body.write(self._render_line(self._line_buf))
            self._line_buf = ""
        try:
            footer = self.query_one("#ai-footer", Label)
        except NoMatches:
            return
        if self._confirm_label:
            self._awaiting_confirm = True
            footer.update(f"  {self._confirm_label}   y=apply   n/Esc=cancel")
        else:
            footer.update("  Done   Esc/q close")

    def on_key(self, event: events.Key) -> None:
        if self._awaiting_confirm:
            event.stop()
            if event.key == "y":
                self._awaiting_confirm = False
                self.dismiss()
                if self._confirm_fn:
                    self._confirm_fn(self)
            elif event.key in ("n", "escape", "q"):
                self._awaiting_confirm = False
                self.dismiss()
            return
        if event.key in ("space", "j"):
            self.body.scroll_down(animate=False)
            event.prevent_default()
        elif event.key in ("b", "k"):
            self.body.scroll_up(animate=False)
            event.prevent_default()
