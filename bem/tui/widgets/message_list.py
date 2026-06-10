from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message as TMessage
from textual.widgets import DataTable

from bem.gmail.models import TRIAGE_STYLES, Thread, TriageLevel


class MessageList(DataTable):
    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
        border-bottom: solid $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Next", show=False),
        Binding("k", "cursor_up", "Prev", show=False),
        Binding("G", "cursor_bottom", "Last", show=False),
        Binding("/", "search", "Search", show=False),
    ]

    class ThreadHighlighted(TMessage):
        def __init__(self, thread: Thread) -> None:
            super().__init__()
            self.thread = thread

    class ThreadSelected(TMessage):
        def __init__(self, thread: Thread) -> None:
            super().__init__()
            self.thread = thread

    _TRIAGE_STYLE = TRIAGE_STYLES

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._threads: list[Thread] = []
        self._thread_map: dict[str, Thread] = {}
        self._triage: dict[str, TriageLevel] = {}
        self._last_key: str = ""
        self.cursor_type = "row"
        self.show_header = False
        self.zebra_stripes = False

    def on_mount(self) -> None:
        self.add_column("u", width=2, key="u")
        self.add_column("from", width=22, key="from")
        self.add_column("subject", key="subject")
        self.add_column("n", width=4, key="n")
        self.add_column("date", width=9, key="date")

    COLUMN_KEYS = ("u", "from", "subject", "n", "date")

    def populate(self, threads: list[Thread], cursor_row: int = 0) -> None:
        self._threads = threads
        self._thread_map = {t.id: t for t in threads}
        self.clear()
        for thread in threads:
            self._add_thread_row(thread)
        if threads:
            self.move_cursor(row=max(0, min(cursor_row, len(threads) - 1)))

    def apply_triage(self, triage: dict[str, TriageLevel]) -> None:
        """Colour rows based on triage level map {thread_id: TriageLevel}."""
        self._triage = triage
        self.clear()
        for thread in self._threads:
            self._add_thread_row(thread)

    def _row_cells(self, thread: Thread) -> tuple[Text, ...]:
        bold = thread.is_unread
        star = "★" if (thread.last_message and thread.last_message.is_starred) else " "
        unread_dot = "●" if thread.is_unread else " "
        indicator = Text(f"{unread_dot}{star}", style="bold cyan" if bold else "dim")

        sender = _truncate(thread.sender, 20)
        subj = thread.subject
        count_str = f"({thread.message_count})" if thread.message_count > 1 else ""

        triage = self._triage.get(thread.id)
        if triage:
            style = self._TRIAGE_STYLE[triage]
        else:
            style = "bold" if bold else ""

        return (
            indicator,
            Text(sender, style=style),
            Text(subj, style=style),
            Text(count_str, style=style if triage else "dim"),
            Text(_format_date(thread.date), style=style),
        )

    def _add_thread_row(self, thread: Thread) -> None:
        self.add_row(*self._row_cells(thread), key=thread.id)

    def append_threads(self, threads: list[Thread]) -> None:
        """Add a further page of threads without rebuilding the table."""
        for thread in threads:
            if thread.id in self._thread_map:
                continue
            self._threads.append(thread)
            self._thread_map[thread.id] = thread
            self._add_thread_row(thread)

    def update_thread(self, thread: Thread) -> None:
        """Refresh a single row in place after a mutation (mark read, star, etc.)."""
        if thread.id not in self._thread_map:
            return
        self._thread_map[thread.id] = thread
        for i, t in enumerate(self._threads):
            if t.id == thread.id:
                self._threads[i] = thread
                break
        try:
            for col_key, value in zip(self.COLUMN_KEYS, self._row_cells(thread)):
                self.update_cell(thread.id, col_key, value)
        except Exception:
            pass  # row no longer present (e.g. list reloaded underneath us)

    def on_key(self, event: events.Key) -> None:
        if event.key == "g":
            if self._last_key == "g":
                self.move_cursor(row=0)
                self._last_key = ""
                event.prevent_default()
                return
            self._last_key = "g"
            event.prevent_default()
            return
        self._last_key = event.key

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            thread = self._thread_map.get(event.row_key.value)
            if thread:
                self.post_message(self.ThreadHighlighted(thread))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            thread = self._thread_map.get(event.row_key.value)
            if thread:
                self.post_message(self.ThreadSelected(thread))

    def action_search(self) -> None:
        self.app.action_search()


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s.ljust(max_len)
    return s[: max_len - 1] + "…"


def _format_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        now = datetime.now().astimezone()
        if local.date() == now.date():
            return local.strftime("%H:%M")
        if 0 <= (now - local).days < 7:
            return local.strftime("%a")
        if local.year == now.year:
            return local.strftime("%d %b")
        return local.strftime("%d/%m/%y")
    except Exception:
        return ""
