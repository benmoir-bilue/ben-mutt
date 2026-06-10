from __future__ import annotations

import traceback
from typing import Optional

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Label, Static
from textual.worker import get_current_worker

import bem.log as bemlog
from bem.config import Config
from bem.gmail import GmailClient
from bem.gmail.models import TRIAGE_STYLES, Label as GmailLabel, Thread, TriageLevel
from bem.ai import AIAssistant
from bem.tui.widgets import (
    FolderList, MessageList, MessagePreview, AIPanel, CommandBar
)
from bem.tui.screens.compose import (
    build_reply_draft, build_forward_draft, build_new_draft,
    launch_editor, parse_draft,
)
from bem.tui.screens.help import HelpScreen

log = bemlog.get()


# JSON category key → triage level, in display order
_TRIAGE_CATEGORIES: dict[str, TriageLevel] = {
    "action":  TriageLevel.ACTION_NEEDED,
    "waiting": TriageLevel.WAITING_REPLY,
    "fyi":     TriageLevel.FYI_LOW,
    "archive": TriageLevel.CAN_ARCHIVE,
}

_TRIAGE_DISPLAY: dict[TriageLevel, str] = {
    TriageLevel.ACTION_NEEDED: "ACTION NEEDED",
    TriageLevel.WAITING_REPLY: "WAITING FOR REPLY",
    TriageLevel.FYI_LOW:       "FYI / LOW PRIORITY",
    TriageLevel.CAN_ARCHIVE:   "CAN ARCHIVE",
}

# Headings as they appear in the panel, condensed (no spaces/colons,
# uppercased) so minor formatting drift still matches.
_TRIAGE_HEADINGS: dict[str, TriageLevel] = {
    "ACTIONNEEDED":    TriageLevel.ACTION_NEEDED,
    "WAITINGFORREPLY": TriageLevel.WAITING_REPLY,
    "FYI":             TriageLevel.FYI_LOW,
    "CANARCHIVE":      TriageLevel.CAN_ARCHIVE,
}


def _triage_entries(numbers: object) -> list[tuple[int, str]]:
    """Normalise one category's JSON list to (1-indexed number, note) pairs.

    Accepts both bare numbers (3) and {"n": 3, "note": "..."} objects, so a
    model that ignores the note instruction still parses.
    """
    out: list[tuple[int, str]] = []
    if not isinstance(numbers, list):
        return out
    for item in numbers:
        note = ""
        n = item
        if isinstance(item, dict):
            n = item.get("n")
            note = str(item.get("note") or "")
        try:
            out.append((int(n), note))
        except (TypeError, ValueError):
            continue
    return out


def _parse_triage(
    raw: dict, threads: list[Thread]
) -> tuple[dict[str, TriageLevel], str]:
    """Turn the structured triage response into ({thread_id: level}, panel text)."""
    levels: dict[str, TriageLevel] = {}
    lines: list[str] = []
    for category, level in _TRIAGE_CATEGORIES.items():
        section: list[str] = []
        for n, note in _triage_entries(raw.get(category)):
            idx = n - 1
            if not 0 <= idx < len(threads):
                continue
            levels[threads[idx].id] = level
            suffix = f" ({note})" if note else ""
            section.append(f"{n} — {threads[idx].subject}{suffix}")
        if section:
            lines.append(_TRIAGE_DISPLAY[level])
            lines.extend(section)
            lines.append("")
    missing = [str(i + 1) for i, t in enumerate(threads) if t.id not in levels]
    if missing:
        lines.append(f"(not classified: {', '.join(missing)})")
        lines.append("")
    return levels, "\n".join(lines)


def _triage_heading_style(line: str) -> Optional[str]:
    """Style for a :triage output line: heading lines get the same colour the
    message-list rows will get when the labels are applied."""
    condensed = "".join(line.split()).rstrip(":").upper()
    if not condensed or condensed[0].isdigit():
        return None
    for prefix, level in _TRIAGE_HEADINGS.items():
        if condensed.startswith(prefix):
            return TRIAGE_STYLES[level]
    return None


def _describe_error(e: Exception) -> str:
    """Turn a worker exception into a message that says what to do about it."""
    from googleapiclient.errors import HttpError

    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", None)
        if status in (401, 403):
            return "Gmail authorisation failed — run `bem auth` to re-authenticate"
        return f"Gmail API error ({status}): {e.reason if hasattr(e, 'reason') else e}"
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return f"Network error: {e}"
    return f"{type(e).__name__}: {e}"


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $primary;
        color: $background;
        padding: 0 1;
    }
    """


class InboxScreen(Screen):
    DEFAULT_CSS = """
    InboxScreen {
        layout: vertical;
    }
    #layout {
        height: 1fr;
    }
    #main-pane {
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("r", "reply", "Reply", show=False),
        Binding("R", "reply_all", "Reply All", show=False),
        Binding("f", "forward", "Forward", show=False),
        Binding("m", "compose", "Compose", show=False),
        Binding("e", "archive", "Archive", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("u", "toggle_unread", "Unread", show=False),
        Binding("!", "toggle_star", "Star", show=False),
        Binding("c", "change_folder", "Folder", show=False),
        Binding("colon", "command_mode", "Command", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, gmail: GmailClient, config: Config, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gmail = gmail
        self.config = config
        self._ai: Optional[AIAssistant] = None
        self._current_label_id = "INBOX"
        self._current_label_name = "Inbox"
        self._current_query = ""
        self._next_page_token: Optional[str] = None
        self._threads: list[Thread] = []
        self._current_thread: Optional[Thread] = None
        self._restore_cursor_row: Optional[int] = None
        self._thread_cache: dict[str, Thread] = {}
        self._preview_timer: Optional[Timer] = None

        self._pending_triage: dict[str, TriageLevel] = {}
        if config.anthropic_api_key:
            self._ai = AIAssistant(
                config.anthropic_api_key,
                model_fast=config.ai_model_fast,
                model_smart=config.ai_model_smart,
            )

    def compose(self) -> ComposeResult:
        with Horizontal(id="layout"):
            yield FolderList(id="folders")
            with Vertical(id="main-pane"):
                yield MessageList(id="messages")
                yield MessagePreview(id="preview")
        yield CommandBar(id="command")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.load_labels()
        self.load_threads()
        self.query_one(StatusBar).update(" bem  Loading…")

    # ── Data loading ───────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="labels", exit_on_error=False)
    def load_labels(self) -> None:
        worker = get_current_worker()
        log.debug("load_labels: starting")
        try:
            labels = self.gmail.list_labels()
            log.debug("load_labels: got %d labels", len(labels))
        except Exception as e:
            log.error("load_labels: %s\n%s", e, traceback.format_exc())
            if not worker.is_cancelled:
                self.app.call_from_thread(self.app.notify, f"Labels: {e}", severity="error")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_labels_loaded, labels)

    @work(thread=True, exclusive=True, group="threads", exit_on_error=False)
    def load_threads(self, label_id: str = "INBOX", query: str = "") -> None:
        worker = get_current_worker()
        log.debug("load_threads: starting label_id=%s query=%r", label_id, query)
        try:
            threads, next_token = self.gmail.list_threads(
                label_id=label_id,
                max_results=self.config.threads_per_page,
                query=query,
            )
            log.debug("load_threads: got %d threads", len(threads))
        except Exception as e:
            log.error("load_threads: %s\n%s", e, traceback.format_exc())
            if not worker.is_cancelled:
                self.app.call_from_thread(self._on_load_failed, _describe_error(e))
            return
        if not worker.is_cancelled:
            log.debug("load_threads: calling _on_threads_loaded")
            self.app.call_from_thread(self._on_threads_loaded, threads, next_token)

    def _on_labels_loaded(self, labels: list[GmailLabel]) -> None:
        log.debug("_on_labels_loaded: %d labels", len(labels))
        self.query_one(FolderList).populate(labels)

    def _on_threads_loaded(
        self, threads: list[Thread], next_token: Optional[str] = None
    ) -> None:
        log.debug("_on_threads_loaded: %d threads", len(threads))
        self._threads = threads
        self._next_page_token = next_token
        self._thread_cache.clear()
        cursor_row = self._restore_cursor_row or 0
        self._restore_cursor_row = None
        try:
            msg_list = self.query_one(MessageList)
            msg_list.populate(threads, cursor_row=cursor_row)
            msg_list.focus()
            self._update_status()
            log.debug("_on_threads_loaded: populate complete")
        except Exception as e:
            log.error("_on_threads_loaded: %s\n%s", e, traceback.format_exc())

    def _maybe_load_more(self) -> None:
        """Fetch the next page when the cursor reaches the end of the list."""
        if not self._next_page_token:
            return
        # Take the token so repeated highlights can't refetch the same page
        token, self._next_page_token = self._next_page_token, None
        self._load_more_threads(token)

    @work(thread=True, exclusive=True, group="threads-more", exit_on_error=False)
    def _load_more_threads(self, page_token: str) -> None:
        worker = get_current_worker()
        try:
            if self._current_query:
                threads, next_token = self.gmail.list_threads(
                    label_id="",
                    max_results=self.config.threads_per_page,
                    page_token=page_token,
                    query=self._current_query,
                )
            else:
                threads, next_token = self.gmail.list_threads(
                    label_id=self._current_label_id,
                    max_results=self.config.threads_per_page,
                    page_token=page_token,
                )
        except Exception as e:
            log.error("load_more: %s\n%s", e, traceback.format_exc())
            if not worker.is_cancelled:
                # Put the token back so the user can retry by scrolling
                self.app.call_from_thread(
                    setattr, self, "_next_page_token", page_token
                )
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_more_threads_loaded, threads, next_token)

    def _on_more_threads_loaded(
        self, threads: list[Thread], next_token: Optional[str]
    ) -> None:
        self._next_page_token = next_token
        seen = {t.id for t in self._threads}
        new = [t for t in threads if t.id not in seen]
        if new:
            self._threads.extend(new)
            self.query_one(MessageList).append_threads(new)
        self._update_status()

    def _on_load_failed(self, error: str) -> None:
        self.query_one(StatusBar).update(
            f" bem  {self._current_label_name}  ⚠ load failed"
        )
        self.app.notify(error, severity="error", timeout=8)

    # ── Widget message handlers ────────────────────────────────────────────────

    def on_folder_list_label_selected(self, event: FolderList.LabelSelected) -> None:
        self._current_label_id = event.label.id
        self._current_label_name = event.label.display_name
        self._current_query = ""
        self.load_threads(label_id=event.label.id)
        self.query_one(MessageList).focus()
        self._update_status()
        event.stop()

    def on_message_list_thread_highlighted(self, event: MessageList.ThreadHighlighted) -> None:
        preview = self.query_one(MessagePreview)
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        cached = self._thread_cache.get(event.thread.id)
        if cached is not None:
            self._current_thread = cached
            preview.show_thread(cached)
        else:
            # Show metadata immediately; fetch the full thread only once the
            # cursor settles, so j/k scrolling doesn't fire a request per row.
            self._current_thread = event.thread
            preview.show_thread(event.thread)
            thread_id = event.thread.id
            self._preview_timer = self.set_timer(
                0.2, lambda: self._load_full_thread(thread_id)
            )
        if self._threads and event.thread.id == self._threads[-1].id:
            self._maybe_load_more()
        self._update_status()
        event.stop()

    def on_message_list_thread_selected(self, event: MessageList.ThreadSelected) -> None:
        self._load_full_thread(event.thread.id, preview_only=False)
        event.stop()

    def on_command_bar_command_submitted(self, event: CommandBar.CommandSubmitted) -> None:
        self._restore_status_bar()
        self._run_command(event.command)
        event.stop()

    def on_command_bar_dismissed(self, event: CommandBar.Dismissed) -> None:
        self._restore_status_bar()
        event.stop()

    def _restore_status_bar(self) -> None:
        self.query_one(StatusBar).display = True
        self.query_one(MessageList).focus()
        self._update_status()

    # ── Thread loading ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="thread-detail", exit_on_error=False)
    def _load_full_thread(self, thread_id: str, preview_only: bool = True) -> None:
        """Fetch the full thread. preview_only=False also marks it read."""
        worker = get_current_worker()
        try:
            thread = self.gmail.get_thread(thread_id)
        except Exception:
            return
        if thread and not worker.is_cancelled:
            self.app.call_from_thread(
                self._on_full_thread_loaded, thread, preview_only=preview_only
            )

    def _on_full_thread_loaded(self, thread: Thread, preview_only: bool = False) -> None:
        self._thread_cache[thread.id] = thread
        if self._current_thread and self._current_thread.id != thread.id:
            return  # cursor moved on while this fetch was in flight
        self._current_thread = thread
        self.query_one(MessagePreview).show_thread(thread)
        if thread.is_unread and not preview_only:
            self._mark_read_background(thread.id)
            self._apply_local_label_change(thread.id, "mark_read")
        self._update_status()

    @work(thread=True, group="mutations", exit_on_error=False)
    def _mark_read_background(self, thread_id: str) -> None:
        worker = get_current_worker()
        try:
            self.gmail.mark_read(thread_id)
        except Exception as e:
            log.error("mark_read failed for %s: %s", thread_id, e)
            return
        if not worker.is_cancelled:
            # Refresh folder unread counts now the server reflects the change
            self.app.call_from_thread(self.load_labels)

    # ── Actions ────────────────────────────────────────────────────────────────

    def action_reply(self) -> None:
        if self._current_thread:
            self._compose_reply(self._current_thread, reply_all=False)

    def action_reply_all(self) -> None:
        if self._current_thread:
            self._compose_reply(self._current_thread, reply_all=True)

    def action_forward(self) -> None:
        if self._current_thread:
            self._compose_forward(self._current_thread)

    def action_compose(self) -> None:
        self._compose_new()

    def action_archive(self) -> None:
        thread = self._current_thread
        if not thread:
            return
        self._mutate_thread(thread.id, "archive")

    def action_delete(self) -> None:
        thread = self._current_thread
        if not thread:
            return
        self._mutate_thread(thread.id, "trash")

    def action_toggle_unread(self) -> None:
        thread = self._current_thread
        if not thread:
            return
        if thread.is_unread:
            self._mutate_thread(thread.id, "mark_read")
        else:
            self._mutate_thread(thread.id, "mark_unread")

    def action_toggle_star(self) -> None:
        thread = self._current_thread
        if not thread:
            return
        if thread.last_message and thread.last_message.is_starred:
            self._mutate_thread(thread.id, "unstar")
        else:
            self._mutate_thread(thread.id, "star")

    def action_change_folder(self) -> None:
        self.query_one(FolderList).focus()

    def action_command_mode(self, prefill: str = "") -> None:
        self.query_one(StatusBar).display = False
        self.query_one(CommandBar).show(prefill)

    def action_search_mode(self) -> None:
        self.action_command_mode("search ")

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_refresh(self) -> None:
        self.notify("Refreshing…")
        self._restore_cursor_row = self.query_one(MessageList).cursor_row
        if self._current_query:
            self.load_threads(label_id="", query=self._current_query)
        else:
            self.load_threads(label_id=self._current_label_id)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def on_message_preview_next_thread(self, event: MessagePreview.NextThread) -> None:
        msg_list = self.query_one(MessageList)
        msg_list.action_cursor_down()
        msg_list.focus()
        event.stop()

    def on_message_preview_prev_thread(self, event: MessagePreview.PrevThread) -> None:
        msg_list = self.query_one(MessageList)
        msg_list.action_cursor_up()
        msg_list.focus()
        event.stop()

    # ── Mutation worker ────────────────────────────────────────────────────────

    @work(thread=True, group="mutations", exit_on_error=False)
    def _mutate_thread(self, thread_id: str, op: str) -> None:
        worker = get_current_worker()
        try:
            getattr(self.gmail, op)(thread_id)
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(self.notify, f"Error: {e}", severity="error")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._after_mutation, thread_id, op)

    # How each non-destructive op changes a thread's labels locally, so the UI
    # reflects the mutation immediately instead of waiting for a refresh.
    _LOCAL_LABEL_OPS = {
        "mark_read":   ("remove", "UNREAD"),
        "mark_unread": ("add", "UNREAD"),
        "star":        ("add", "STARRED"),
        "unstar":      ("remove", "STARRED"),
    }

    def _thread_by_id(self, thread_id: str) -> Optional[Thread]:
        return next((t for t in self._threads if t.id == thread_id), None)

    def _apply_local_label_change(self, thread_id: str, op: str) -> None:
        action = self._LOCAL_LABEL_OPS.get(op)
        if not action:
            return
        kind, label = action
        # The listed, previewed, and cached threads can be distinct objects
        # (metadata vs full fetch) — keep them all in sync.
        targets = [self._thread_by_id(thread_id), self._thread_cache.get(thread_id)]
        if self._current_thread and self._current_thread.id == thread_id:
            targets.append(self._current_thread)
        for thread in targets:
            if not thread:
                continue
            for msg in thread.messages:
                if kind == "add" and label not in msg.label_ids:
                    msg.label_ids.append(label)
                elif kind == "remove" and label in msg.label_ids:
                    msg.label_ids.remove(label)
        listed = self._thread_by_id(thread_id)
        if listed:
            self.query_one(MessageList).update_thread(listed)

    def _after_mutation(self, thread_id: str, op: str) -> None:
        destructive = op in ("archive", "trash")
        if destructive:
            self._thread_cache.pop(thread_id, None)
            msg_list = self.query_one(MessageList)
            cursor_row = msg_list.cursor_row or 0
            self._threads = [t for t in self._threads if t.id != thread_id]
            msg_list.populate(self._threads, cursor_row=cursor_row)
            if self._threads:
                next_thread = self._threads[min(cursor_row, len(self._threads) - 1)]
                self._current_thread = next_thread
                self.query_one(MessagePreview).show_thread(next_thread)
                self._load_full_thread(next_thread.id)
            else:
                self._current_thread = None
                self.query_one(MessagePreview).clear_preview()
        else:
            self._apply_local_label_change(thread_id, op)
        label = {"archive": "Archived", "trash": "Deleted", "mark_read": "Marked read",
                 "mark_unread": "Marked unread", "star": "Starred", "unstar": "Unstarred"}.get(op, op)
        self.notify(label)
        self._update_status()
        self.load_labels()  # refresh folder unread counts

    # ── Compose ────────────────────────────────────────────────────────────────

    def _compose_reply(self, thread: Thread, reply_all: bool = False) -> None:
        draft = build_reply_draft(thread, reply_all=reply_all,
                                  my_address=self.gmail.email_address)
        self._open_editor_and_send(draft, thread)

    def _compose_forward(self, thread: Thread) -> None:
        draft = build_forward_draft(thread)
        self._open_editor_and_send(draft, thread=None)

    def _compose_new(self) -> None:
        draft = build_new_draft()
        self._open_editor_and_send(draft, thread=None)

    def _open_editor_and_send(self, draft: str, thread: Optional[Thread]) -> None:
        editor = self.config.editor
        with self.app.suspend():
            content = launch_editor(draft, editor)
        if not content:
            return
        to, cc, subject, body = parse_draft(content)
        if not to or not body.strip():
            self.notify("Compose cancelled (empty To or body)", severity="warning")
            return
        if self.config.safe_mode:
            self._save_draft_worker(to, cc, subject, body, thread)
        else:
            self._send_worker(to, cc, subject, body, thread)

    @work(thread=True, group="send", exit_on_error=False)
    def _save_draft_worker(
        self, to: str, cc: str, subject: str, body: str, thread: Optional[Thread]
    ) -> None:
        worker = get_current_worker()
        try:
            self.gmail.create_draft(
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                from_address=self.gmail.email_address,
                reply_to_message=thread.last_message if thread else None,
                thread_id=thread.id if thread else None,
            )
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(self.notify, f"Draft save failed: {e}", severity="error")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(
                self.notify,
                f"[SAFE] Saved as draft — review and send from Gmail",
                timeout=6,
            )

    @work(thread=True, group="send", exit_on_error=False)
    def _send_worker(
        self, to: str, cc: str, subject: str, body: str, thread: Optional[Thread]
    ) -> None:
        worker = get_current_worker()
        try:
            self.gmail.send(
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                from_address=self.gmail.email_address,
                reply_to_message=thread.last_message if thread else None,
                thread_id=thread.id if thread else None,
            )
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(self.notify, f"Send failed: {e}", severity="error")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self.notify, f"Sent to {to}")

    # ── AI commands ────────────────────────────────────────────────────────────

    def _run_command(self, raw: str) -> None:
        parts = raw.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("q", "quit"):
            self.app.exit()
            return
        if cmd in ("r", "refresh"):
            self.action_refresh()
            return
        if cmd in ("a", "archive"):
            self.action_archive()
            return
        if cmd in ("d", "delete"):
            self.action_delete()
            return
        if cmd == "search":
            self._do_search(arg)
            return
        if cmd in ("summarise", "summarize", "summary"):
            self._ai_command("summarise")
            return
        if cmd == "triage":
            self._ai_command("triage")
            return
        if cmd == "reply-draft":
            self._ai_command("reply_draft", arg or "professional")
            return
        if cmd == "explain":
            self._ai_command("explain")
            return
        # Free-form command against current thread
        if self._current_thread:
            self._ai_command("custom", raw)
        else:
            self.notify(f"Unknown command: {cmd}", severity="warning")

    def _do_search(self, query: str) -> None:
        if not query:
            return
        self._current_query = query
        self._current_label_name = f"search: {query}"
        self.load_threads(label_id="", query=query)
        self._update_status()

    def _ai_command(self, cmd: str, arg: str = "") -> None:
        if not self._ai:
            self.notify("ANTHROPIC_API_KEY not set — AI features unavailable", severity="warning")
            return

        if cmd == "triage":
            if not self._threads:
                self.notify("No threads to triage", severity="warning")
                return
            panel = AIPanel(
                title="AI — Triage Inbox",
                work_fn=self._run_triage_worker,
                confirm_label="Apply colour labels to inbox?",
                confirm_fn=self._apply_pending_triage,
                line_style=_triage_heading_style,
            )
            self.app.push_screen(panel)
            return

        thread = self._current_thread
        if not thread:
            self.notify("No thread selected", severity="warning")
            return

        title_map = {
            "summarise": "Summarise",
            "reply_draft": f"Draft Reply ({arg})",
            "explain": "Explain",
            "custom": f": {arg}",
        }
        is_reply = cmd == "reply_draft"
        panel = AIPanel(
            title=f"AI — {title_map.get(cmd, cmd)}",
            work_fn=lambda p: self._run_ai_worker(p, cmd, thread, arg),
            confirm_label="Open as reply in editor?" if is_reply else None,
            confirm_fn=(
                (lambda p: self._open_ai_reply(thread, p)) if is_reply else None
            ),
        )
        self.app.push_screen(panel)

    def _open_ai_reply(self, thread: Thread, panel: AIPanel) -> None:
        body = panel.full_text.strip()
        if not body:
            return
        draft = build_reply_draft(
            thread, my_address=self.gmail.email_address, body=body
        )
        # Let the modal finish closing before suspending for the editor
        self.call_after_refresh(self._open_editor_and_send, draft, thread)

    @work(thread=True, group="ai", exit_on_error=False)
    def _run_ai_worker(
        self, panel: AIPanel, cmd: str, thread: Optional[Thread], arg: str
    ) -> None:
        worker = get_current_worker()
        try:
            ai = self._ai
            if cmd == "summarise":
                gen = ai.summarise(thread)
            elif cmd == "reply_draft":
                gen = ai.reply_draft(thread, tone=arg or "professional")
            elif cmd == "explain":
                gen = ai.explain(thread)
            else:
                gen = ai.custom(thread, arg)

            for chunk in gen:
                if worker.is_cancelled:
                    break
                self.app.call_from_thread(panel.append_text, chunk)
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.set_error, str(e))
        finally:
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.mark_done)

    @work(thread=True, group="ai", exit_on_error=False)
    def _run_triage_worker(self, panel: AIPanel) -> None:
        """One structured call classifies every loaded thread; the result is
        rendered into the panel AND stored for the y=apply confirmation, so
        the colours always match the text."""
        worker = get_current_worker()
        threads = list(self._threads)  # snapshot: numbering must stay stable
        self.app.call_from_thread(
            panel.append_text, f"Classifying {len(threads)} threads…\n"
        )
        try:
            raw = self._ai.triage_structured(threads)
            levels, text = _parse_triage(raw, threads)
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.set_error, str(e))
                self.app.call_from_thread(panel.mark_done)
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_triage_result, panel, levels, text)

    def _on_triage_result(
        self, panel: AIPanel, levels: dict[str, TriageLevel], text: str
    ) -> None:
        self._pending_triage = levels
        body = panel.body
        if body is not None:
            body.clear()  # drop the "Classifying…" progress line
        panel.append_text(text)
        panel.mark_done()

    def _apply_pending_triage(self, _panel: AIPanel) -> None:
        if not self._pending_triage:
            self.notify("No triage results to apply", severity="warning")
            return
        self._on_triage_labels_ready(self._pending_triage)

    def _on_triage_labels_ready(self, triage: dict[str, TriageLevel]) -> None:
        self._pending_triage = triage
        self.query_one(MessageList).apply_triage(triage)
        counts = {}
        for lvl in triage.values():
            counts[lvl.name] = counts.get(lvl.name, 0) + 1
        summary = "  ".join(f"{v} {k.replace('_', ' ').lower()}" for k, v in counts.items())
        self.notify(f"Triage applied: {summary}")

    # ── Status bar ─────────────────────────────────────────────────────────────

    def _update_status(self) -> None:
        total = len(self._threads)
        unread = sum(1 for t in self._threads if t.is_unread)
        msg_list = self.query_one(MessageList)
        idx = (msg_list.cursor_row or 0) + 1

        safe_indicator = " [SAFE]" if self.config.safe_mode else ""
        ai_indicator = " [AI]" if self._ai else ""
        more = "+" if self._next_page_token else ""
        status = (
            f" bem{safe_indicator}  {self._current_label_name}  "
            f"{idx}/{total}{more}  "
            f"{unread} unread"
            f"{ai_indicator}"
        )
        self.query_one(StatusBar).update(status)
