from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message as TMessage
from textual.widgets import DataTable

from bem.gmail.models import TRIAGE_STYLES, Message, Thread, TriageLevel
from bem.calendar.client import (
    MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED, MARK_OUTOFSYNC,
)
from bem.tui.tree import TreeRow, thread_tree

# Invite mark -> (subject-prefix tag, style). MARK_PENDING is intentionally
# absent: pending invites carry no list tag, only a rich preview banner.
_INVITE_TAGS = {
    MARK_ACCEPTED:  ("✓ accepted · ", "bold green"),
    MARK_TENTATIVE: ("~ maybe · ", "bold yellow"),
    MARK_DECLINED:  ("⊘ declined · ", "bold grey50"),
    MARK_CANCELLED: ("✗ cancelled · ", "bold red"),
    MARK_OUTOFSYNC: ("⚠ not on calendar · ", "bold dark_orange"),
}

# Row-key separator between thread id and message id for expanded message
# rows. Gmail ids are hex, so "::" can never collide.
_SEP = "::"


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
        Binding("v", "toggle_thread", "Expand/collapse thread", show=False),
        Binding("right", "expand_thread", "Open thread", show=False),
        Binding("left", "collapse_thread", "Close thread", show=False),
        Binding("V", "toggle_all_threads", "Expand/collapse all", show=False),
        Binding("P", "parent_message", "Parent message", show=False),
    ]

    class ThreadHighlighted(TMessage):
        def __init__(self, thread: Thread) -> None:
            super().__init__()
            self.thread = thread

    class ThreadSelected(TMessage):
        def __init__(self, thread: Thread) -> None:
            super().__init__()
            self.thread = thread

    class MessageHighlighted(TMessage):
        """Cursor is on a single message row inside an expanded thread."""
        def __init__(self, thread: Thread, message: Message) -> None:
            super().__init__()
            self.thread = thread
            self.message = message

    class MessageSelected(TMessage):
        def __init__(self, thread: Thread, message: Message) -> None:
            super().__init__()
            self.thread = thread
            self.message = message

    _TRIAGE_STYLE = TRIAGE_STYLES

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._threads: list[Thread] = []
        self._thread_map: dict[str, Thread] = {}
        self._triage: dict[str, TriageLevel] = {}
        # thread_id -> invite mark ("accepted" | "cancelled"); both decorate the
        # subject and read as "safe to delete".
        self._invite_marks: dict[str, str] = {}
        self._expanded: set[str] = set()
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

    def populate(
        self, threads: list[Thread], cursor_row: int = 0,
        cursor_key: Optional[str] = None,
    ) -> None:
        self._threads = threads
        self._thread_map = {t.id: t for t in threads}
        self._expanded &= set(self._thread_map)
        # cursor_key (a thread id) keeps the selection on the same email across a
        # background refresh even when rows shift; falls back to cursor_row.
        self._rebuild(cursor_row=cursor_row, cursor_key=cursor_key)

    def selected_key(self) -> Optional[str]:
        """The id of the currently selected thread row, or None."""
        return self._cursor_key() or None

    def apply_triage(self, triage: dict[str, TriageLevel]) -> None:
        """Colour rows based on triage level map {thread_id: TriageLevel}."""
        self._triage = triage
        self._rebuild(cursor_key=self._cursor_key() or None)

    def apply_invite_marks(self, marks: dict[str, str]) -> None:
        """Tag invite threads the calendar has handled — 'accepted' (green) or
        'cancelled' (red) — so they read as 'safe to delete'."""
        if marks == self._invite_marks:
            return
        self._invite_marks = dict(marks)
        self._rebuild(cursor_key=self._cursor_key() or None)

    def _rebuild(self, cursor_row: int = 0, cursor_key: Optional[str] = None) -> None:
        """Re-add every row, including message rows for expanded threads.
        cursor_key (a row key) wins over cursor_row when it still exists."""
        self.clear()
        for thread in self._threads:
            self._add_thread_row(thread)
            if thread.id in self._expanded:
                for trow in thread_tree(thread):
                    self._add_message_row(thread, trow)
        if not self.row_count:
            return
        row = cursor_row
        if cursor_key is not None:
            try:
                row = self.get_row_index(cursor_key)
            except Exception:
                pass
        self.move_cursor(row=max(0, min(row, self.row_count - 1)))

    def _row_cells(self, thread: Thread) -> tuple[Text, ...]:
        bold = thread.is_unread
        star = "★" if (thread.last_message and thread.last_message.is_starred) else " "
        unread_dot = "●" if thread.is_unread else " "
        indicator = Text(f"{unread_dot}{star}", style="bold cyan" if bold else "dim")

        sender = _truncate(thread.sender, 20)
        # Fold marker doubles as the "this row expands" affordance, like
        # Mutt's %?M?(+%M)? index_format idiom. Single-message threads get a
        # blank so all subjects stay aligned.
        if thread.message_count > 1:
            fold = "▾" if thread.id in self._expanded else "▸"
            count_str = f"({thread.message_count})"
        else:
            fold = " "
            count_str = ""

        triage = self._triage.get(thread.id)
        if triage:
            style = self._TRIAGE_STYLE[triage]
        else:
            style = "bold" if bold else ""

        label_style = _INVITE_TAGS.get(self._invite_marks.get(thread.id))
        tag = Text(label_style[0], style=label_style[1]) if label_style else Text("")
        subject = Text.assemble(
            (f"{fold} ", "dim cyan"),
            tag,
            (_truncate_subject(thread.subject), style),
        )
        return (
            indicator,
            Text(sender, style=style),
            subject,
            Text(count_str, style=style if triage else "dim"),
            Text(_format_date(thread.date), style=style),
        )

    def _message_cells(self, thread: Thread, trow: TreeRow) -> tuple[Text, ...]:
        msg = trow.message
        bold = msg.is_unread
        star = "★" if msg.is_starred else " "
        unread_dot = "●" if msg.is_unread else " "
        indicator = Text(f"{unread_dot}{star}", style="bold cyan" if bold else "dim")

        style = "bold" if bold else ""
        # Mutt-style subject column: children show only the tree arrow when
        # their subject is just "Re: <thread subject>"; anything else (a
        # changed subject, or the root message) is spelled out.
        subj = msg.subject
        if trow.prefix and _norm_subject(subj) == _norm_subject(thread.subject):
            subj = ""
        # Two-space indent keeps the tree nested under the thread's fold
        # marker + subject.
        subject = Text.assemble(
            (f"  {trow.prefix}", "dim cyan"), (_truncate_subject(subj), style)
        )

        return (
            indicator,
            Text(_truncate(msg.display_from, 20), style=style),
            subject,
            Text(""),
            Text(_format_date(msg.date), style=style),
        )

    def _add_thread_row(self, thread: Thread) -> None:
        self.add_row(*self._row_cells(thread), key=thread.id)

    def _add_message_row(self, thread: Thread, trow: TreeRow) -> None:
        self.add_row(
            *self._message_cells(thread, trow),
            key=f"{thread.id}{_SEP}{trow.message.id}",
        )

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
        if thread.id in self._expanded:
            # Per-message rows may change too (e.g. unread dots) — rebuild.
            self._rebuild(cursor_key=self._cursor_key() or None)
            return
        try:
            for col_key, value in zip(self.COLUMN_KEYS, self._row_cells(thread)):
                self.update_cell(thread.id, col_key, value)
        except Exception:
            pass  # row no longer present (e.g. list reloaded underneath us)

    # ── Thread tree (Mutt: v / ESC-V / P) ──────────────────────────────────────

    def _cursor_key(self) -> str:
        try:
            key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
            return key or ""
        except Exception:
            return ""

    def _move_cursor_to_key(self, key: str) -> None:
        try:
            self.move_cursor(row=self.get_row_index(key))
        except Exception:
            pass

    def action_toggle_thread(self) -> None:
        """Mutt's v: expand or collapse the thread under the cursor."""
        tid = self._cursor_key().split(_SEP, 1)[0]
        thread = self._thread_map.get(tid)
        if not thread or thread.message_count <= 1:
            return
        if tid in self._expanded:
            self._expanded.discard(tid)
        else:
            self._expanded.add(tid)
        self._rebuild(cursor_key=tid)

    def action_expand_thread(self) -> None:
        """Right arrow: open (expand) the thread under the cursor."""
        tid = self._cursor_key().split(_SEP, 1)[0]
        thread = self._thread_map.get(tid)
        if not thread or thread.message_count <= 1 or tid in self._expanded:
            return
        self._expanded.add(tid)
        self._rebuild(cursor_key=tid)

    def action_collapse_thread(self) -> None:
        """Left arrow: close (collapse) the thread under the cursor. From a
        message row inside the thread, collapse the parent and land on it."""
        tid = self._cursor_key().split(_SEP, 1)[0]
        if tid not in self._expanded:
            return
        self._expanded.discard(tid)
        self._rebuild(cursor_key=tid)

    def action_toggle_all_threads(self) -> None:
        """Mutt's ESC-V: expand all threads, or collapse all if any are open."""
        expandable = {t.id for t in self._threads if t.message_count > 1}
        if not expandable:
            return
        if self._expanded:
            self._expanded.clear()
        else:
            self._expanded = expandable
        tid = self._cursor_key().split(_SEP, 1)[0]
        self._rebuild(cursor_key=tid or None)

    def action_parent_message(self) -> None:
        """Mutt's P: jump to the parent of the current message; from a root
        message, jump to its thread row."""
        key = self._cursor_key()
        if _SEP not in key:
            return
        tid, mid = key.split(_SEP, 1)
        thread = self._thread_map.get(tid)
        if not thread:
            return
        trow = next((r for r in thread_tree(thread) if r.message.id == mid), None)
        if trow and trow.parent_id:
            self._move_cursor_to_key(f"{tid}{_SEP}{trow.parent_id}")
        else:
            self._move_cursor_to_key(tid)

    # ── Events ─────────────────────────────────────────────────────────────────

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

    def _resolve_row_key(self, key: str) -> tuple[Optional[Thread], Optional[Message]]:
        if _SEP in key:
            tid, mid = key.split(_SEP, 1)
            thread = self._thread_map.get(tid)
            if thread:
                msg = next((m for m in thread.messages if m.id == mid), None)
                if msg:
                    return thread, msg
            return None, None
        return self._thread_map.get(key), None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            thread, msg = self._resolve_row_key(event.row_key.value)
            if thread and msg:
                self.post_message(self.MessageHighlighted(thread, msg))
            elif thread:
                self.post_message(self.ThreadHighlighted(thread))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            thread, msg = self._resolve_row_key(event.row_key.value)
            if thread and msg:
                self.post_message(self.MessageSelected(thread, msg))
            elif thread:
                self.post_message(self.ThreadSelected(thread))

    def action_search(self) -> None:
        self.app.action_search()


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s.ljust(max_len)
    return s[: max_len - 1] + "…"


def _truncate_subject(s: str, max_len: int = 40) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


_SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fwd?|fw)\s*:\s*)+", re.IGNORECASE)


def _norm_subject(s: str) -> str:
    return _SUBJECT_PREFIX_RE.sub("", s).strip().lower()


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
