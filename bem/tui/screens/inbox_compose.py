from __future__ import annotations

import time
import traceback
from collections import Counter
from typing import Optional

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Label, Static
from textual.worker import get_current_worker

import bem.log as bemlog
from bem.config import Config, RULES_FILE
from bem.gmail import GmailClient
from bem.gmail.models import (
    TRIAGE_STYLES, Label as GmailLabel, Message, Thread, TriageLevel
)
from bem.ai import AIAssistant
from bem.ai.commands import model_label
from bem.ai import copilot as copilot_mod
from bem.ai.copilot import CopilotBrain, CopilotExecutor
from bem.ai.agent import EmailAgent, _load_rules
from bem.calendar import parse_ics, CalendarInvite
from bem.calendar.client import (
    CalendarClient, Conflict, disposition_mark,
    MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED,
    MARK_OUTOFSYNC, MARK_PENDING, SAFE_TO_DELETE_MARKS,
)
from bem.ai.tips import Tips, load_tips
from bem.ai.tools import PlanAction
from bem.tui.widgets import (
    FolderList, MessageList, MessagePreview, AIPanel, AgentPanel, CopilotPanel,
    CommandBar
)
from bem.tui.screens.compose import (
    build_reply_draft, build_forward_draft, build_new_draft,
    launch_editor, parse_draft, AI_DRAFT_MARKER,
)
from bem.tui.screens.help import HelpScreen

log = bemlog.get()
from bem.tui.screens._inbox_shared import _match_label, _describe_error


class ComposeMixin:
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

    def _after_mutation(self, thread_id: str, op: str, note: str = "") -> None:
        destructive = op in ("archive", "trash", "move")
        if destructive:
            self._current_message = None
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
        self.notify(note or label)
        self._update_status()
        self.load_labels()  # refresh folder unread counts

    def _compose_reply(
        self, thread: Thread, reply_all: bool = False,
        message: Optional[Message] = None,
    ) -> None:
        draft = build_reply_draft(thread, reply_all=reply_all,
                                  my_address=self.gmail.email_address,
                                  message=message)
        self._open_editor_and_send(draft, thread, reply_to=message)

    def _compose_forward(self, thread: Thread, message: Optional[Message] = None) -> None:
        draft = build_forward_draft(thread, message=message)
        self._open_editor_and_send(draft, thread=None)

    def _compose_new(self) -> None:
        draft = build_new_draft()
        self._open_editor_and_send(draft, thread=None)

    def _open_editor_and_send(
        self, draft: str, thread: Optional[Thread],
        reply_to: Optional[Message] = None,
    ) -> None:
        """`reply_to` pins the In-Reply-To/References headers to a specific
        message (mid-thread reply); default is the thread's newest message."""
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
            self._save_draft_worker(to, cc, subject, body, thread, reply_to)
        else:
            self._send_worker(to, cc, subject, body, thread, reply_to)

    @work(thread=True, group="send", exit_on_error=False)
    def _save_draft_worker(
        self, to: str, cc: str, subject: str, body: str, thread: Optional[Thread],
        reply_to: Optional[Message] = None,
    ) -> None:
        worker = get_current_worker()
        try:
            self.gmail.create_draft(
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                from_address=self.gmail.email_address,
                reply_to_message=reply_to or (thread.last_message if thread else None),
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
        self, to: str, cc: str, subject: str, body: str, thread: Optional[Thread],
        reply_to: Optional[Message] = None,
    ) -> None:
        worker = get_current_worker()
        try:
            self.gmail.send(
                to=to,
                cc=cc,
                subject=subject,
                body=body,
                from_address=self.gmail.email_address,
                reply_to_message=reply_to or (thread.last_message if thread else None),
                thread_id=thread.id if thread else None,
            )
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(self.notify, f"Send failed: {e}", severity="error")
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self.notify, f"Sent to {to}")

    def _do_move(self, label_name: str) -> None:
        label_name = label_name.strip()
        if not label_name:
            self.notify("Usage: move <label>", severity="warning")
            return
        thread = self._current_thread
        if not thread:
            self.notify("No thread selected", severity="warning")
            return
        self._move_worker(thread.id, label_name)

    @work(thread=True, group="mutations", exit_on_error=False)
    def _move_worker(self, thread_id: str, label_name: str) -> None:
        worker = get_current_worker()
        try:
            labels = self._labels or self.gmail.list_labels()
            target = _match_label(labels, label_name)
            created = False
            if target is None:
                target = self.gmail.create_label(label_name)
                created = True
            remove = ["INBOX"]
            current = self._current_label_id
            if current and current != target.id and any(
                l.id == current and l.type == "user" for l in labels
            ):
                remove.append(current)  # moving out of a label folder, not just inbox
            self.gmail.modify_thread(
                thread_id, add_labels=[target.id], remove_labels=remove
            )
        except Exception as e:
            log.error("move failed: %s\n%s", e, traceback.format_exc())
            if not worker.is_cancelled:
                self.app.call_from_thread(
                    self.notify, f"Move failed: {_describe_error(e)}", severity="error")
            return
        if not worker.is_cancelled:
            note = f"Moved to {target.name}" + (" (new label)" if created else "")
            self.app.call_from_thread(self._after_mutation, thread_id, "move", note)

    @work(thread=True, exclusive=True, group="move-suggest", exit_on_error=False)
    def _suggest_move_worker(self, thread: Thread) -> None:
        worker = get_current_worker()
        user_labels = [l for l in self._labels if l.type == "user"]
        tips = load_tips()
        try:
            answer = self._ai.suggest_label(
                thread, [l.name for l in user_labels], rules=_load_rules(),
                tips=tips.content if tips else "",
            )
        except Exception as e:
            log.debug("move suggestion failed: %s", e)
            return
        match = _match_label(user_labels, answer)
        if match is not None and not worker.is_cancelled:
            self.app.call_from_thread(
                lambda: self.query_one(CommandBar).suggest("move ", match.name)
            )
