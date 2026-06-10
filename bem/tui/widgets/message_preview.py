from __future__ import annotations

from datetime import datetime, timezone

from rich.markup import escape as _e
from textual.binding import Binding
from textual.message import Message as TMessage
from textual.widgets import RichLog
from textual import events

from bem.gmail.models import Thread, Message


class MessagePreview(RichLog):
    DEFAULT_CSS = """
    MessagePreview {
        height: 1fr;
        background: $background;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("space", "scroll_down", "Page down", show=False),
        Binding("b", "scroll_up", "Page up", show=False),
    ]

    class NextThread(TMessage):
        pass

    class PrevThread(TMessage):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, wrap=True, highlight=False, **kwargs)
        self._thread: Thread | None = None

    def show_thread(self, thread: Thread) -> None:
        self._thread = thread
        self.clear()
        for i, msg in enumerate(thread.messages):
            if i > 0:
                self.write("─" * 72)
            _write_message(self, msg)
        self.call_after_refresh(self.scroll_home, animate=False)

    def show_message(self, message: Message) -> None:
        self.clear()
        _write_message(self, message)
        self.call_after_refresh(self.scroll_home, animate=False)

    def clear_preview(self) -> None:
        self._thread = None
        self.clear()

    def on_key(self, event: events.Key) -> None:
        if event.key == "j":
            self.scroll_down()
            event.prevent_default()
        elif event.key == "k":
            self.scroll_up()
            event.prevent_default()
        elif event.key == "J":
            self.post_message(self.NextThread())
            event.prevent_default()
        elif event.key == "K":
            self.post_message(self.PrevThread())
            event.prevent_default()


def _write_message(log: RichLog, msg: Message) -> None:
    dt_str = ""
    if msg.date:
        try:
            dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            dt_str = dt.astimezone().strftime("%a %d %b %Y %H:%M")
        except Exception:
            pass

    log.write(f"[bold]From:[/bold]    {_e(msg.from_name)} <{_e(msg.from_address)}>")
    if msg.to:
        log.write(f"[bold]To:[/bold]      {_e(', '.join(msg.to))}")
    if msg.cc:
        log.write(f"[bold]Cc:[/bold]      {_e(', '.join(msg.cc))}")
    log.write(f"[bold]Date:[/bold]    {dt_str}")
    log.write(f"[bold]Subject:[/bold] {_e(msg.subject)}")
    if msg.attachments:
        names = ", ".join(a.filename for a in msg.attachments)
        log.write(f"[bold]Attach:[/bold]  [yellow]{_e(names)}[/yellow]")
    log.write("")

    for line in msg.body.splitlines():
        if line.startswith(">"):
            log.write(f"[dim]{_e(line)}[/dim]")
        else:
            log.write(_e(line) if line else "")
