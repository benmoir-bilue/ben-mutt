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


class CalendarMixin:
    @property
    def _cal(self) -> CalendarClient:
        if self._calendar is None:
            self._calendar = CalendarClient(self.gmail.credentials)
        return self._calendar

    @work(thread=True, exclusive=True, group="invites", exit_on_error=False)
    def _scan_invites(self, thread_ids: list[str]) -> None:
        """Resolve, per visible thread, whether a meeting invite has already
        been accepted on the calendar, then decorate the accepted ones. The
        listed-thread metadata carries no attachments, so invites are found via
        a search and intersected with what's on screen."""
        worker = get_current_worker()
        visible = set(thread_ids)
        if not visible:
            return
        try:
            invite_threads, _ = self.gmail.list_threads(
                query="filename:ics", max_results=50,
            )
        except Exception as e:
            log.debug("invite scan: list failed: %s", e)
            return
        email = self.gmail.email_address
        # Seed from cache so this view's already-resolved threads keep their mark
        # (apply replaces the whole set). Empty-string entries are 'resolved, no
        # decoration' — kept in the cache to avoid re-querying, dropped here.
        marks = {
            tid: m for tid, m in self._invite_status.items()
            if tid in visible and m
        }
        for t in invite_threads:
            if worker.is_cancelled:
                return
            if t.id not in visible or t.id in self._invite_status:
                continue
            mark = self._resolve_invite(t.id, email)
            if mark is None:
                continue  # couldn't resolve — leave uncached so a later scan retries
            self._invite_status[t.id] = mark
            if mark:
                marks[t.id] = mark
        if not worker.is_cancelled:
            self.app.call_from_thread(self._apply_invites, marks)

    def _resolve_invite(self, thread_id: str, email: str) -> Optional[str]:
        """Classify a thread's invite into a decoration mark:
          MARK_CANCELLED  — the .ics is a cancellation (METHOD:CANCEL).
          MARK_OUTOFSYNC  — an invitation whose calendar event is gone/cancelled.
          MARK_ACCEPTED / MARK_TENTATIVE / MARK_DECLINED — your response.
          MARK_PENDING    — awaiting your response (conflicts stashed for preview).
          None            — couldn't resolve; a later scan should retry."""
        full = self.gmail.get_thread(thread_id)
        if full is None:
            return None
        for msg in full.messages:
            att = msg.calendar_attachment
            if att is None or not att.attachment_id:
                continue
            data = self.gmail.get_attachment(msg.id, att.attachment_id)
            if not data:
                continue
            invite = parse_ics(data.decode("utf-8", "replace"))
            if invite is None:
                continue
            self._invite_objs[thread_id] = invite
            if invite.method == "CANCEL":
                return MARK_CANCELLED
            if not invite.uid:
                return ""
            try:
                info = self._cal.lookup(invite.uid, email)
            except Exception as e:
                log.debug("invite scan: calendar lookup failed: %s", e)
                return None
            mark = disposition_mark(invite.method, info)
            # Awaiting a response: pre-compute conflicts for the preview.
            if mark == MARK_PENDING:
                try:
                    self._invite_conflicts[thread_id] = self._cal.conflicts(
                        invite.dtstart, invite.dtend, exclude_uid=invite.uid,
                    )
                except Exception as e:
                    log.debug("invite scan: conflict lookup failed: %s", e)
                    self._invite_conflicts[thread_id] = []
            return mark
        return None

    def _apply_invites(self, marks: dict[str, str]) -> None:
        try:
            self.query_one(MessageList).apply_invite_marks(marks)
        except Exception:
            pass
        # If the user is already looking at one of these, surface the banner now.
        if self._current_thread is not None:
            self._sync_invite_banner(self._current_thread.id)

    def _banner_for(
        self, thread_id: Optional[str]
    ) -> tuple[Optional[CalendarInvite], Optional[str], list[Conflict]]:
        """The (invite, mark, conflicts) to banner for a thread, or empty."""
        mark = self._invite_status.get(thread_id) if thread_id else None
        if mark in self._BANNER_MARKS:
            return (
                self._invite_objs.get(thread_id),
                mark,
                self._invite_conflicts.get(thread_id, []),
            )
        return None, None, []

    def _sync_invite_banner(self, thread_id: Optional[str]) -> None:
        """Tell the preview about the shown thread's invite state, then redraw
        so the banner appears or clears."""
        preview = self.query_one(MessagePreview)
        if preview.set_invite_banner(*self._banner_for(thread_id)):
            if self._current_message is not None:
                preview.show_message(self._current_message)
            elif self._current_thread is not None:
                preview.show_thread(self._current_thread)

    def action_rsvp_accept(self) -> None:
        self._rsvp("accepted", MARK_ACCEPTED, "accepted")

    def action_rsvp_maybe(self) -> None:
        self._rsvp("tentative", MARK_TENTATIVE, "maybe")

    def action_rsvp_decline(self) -> None:
        self._rsvp("declined", MARK_DECLINED, "declined")

    def _rsvp(self, response: str, new_mark: str, label: str) -> None:
        thread = self._current_thread
        if thread is None:
            return
        invite = self._invite_objs.get(thread.id)
        mark = self._invite_status.get(thread.id)
        if invite is None or not invite.uid or mark not in self._RSVP_ABLE:
            self.notify("Not a calendar invite you can respond to.", severity="warning")
            return
        self.notify(f"RSVP “{label}”…")
        self._rsvp_worker(thread.id, invite.uid, response, new_mark, label)

    @work(thread=True, group="mutations", exit_on_error=False)
    def _rsvp_worker(
        self, thread_id: str, uid: str, response: str, new_mark: str, label: str
    ) -> None:
        worker = get_current_worker()
        try:
            ok = self._cal.respond_to_event(uid, self.gmail.email_address, response)
        except Exception as e:
            log.error("rsvp failed: %s", e)
            ok = False
        if not worker.is_cancelled:
            self.app.call_from_thread(self._after_rsvp, thread_id, new_mark, label, ok)

    def _after_rsvp(self, thread_id: str, new_mark: str, label: str, ok: bool) -> None:
        if not ok:
            self.notify(f"Couldn't set “{label}” — calendar update failed.",
                        severity="error")
            return
        self._invite_status[thread_id] = new_mark
        self._invite_conflicts.pop(thread_id, None)  # no longer pending
        self.query_one(MessageList).apply_invite_marks(self._current_marks())
        if self._current_thread is not None:
            self._sync_invite_banner(self._current_thread.id)
        self.notify(f"Marked “{label}” — organiser notified.", timeout=6)

    def _current_marks(self) -> dict[str, str]:
        """All loaded threads' invite marks (for re-decorating the list)."""
        marks: dict[str, str] = {}
        for t in self._threads:
            m = self._invite_status.get(t.id)
            if m:
                marks[t.id] = m
        return marks

    def _safe_to_delete_ids(self) -> list[str]:
        """Loaded threads the calendar has handled (accepted, tentative, declined
        or cancelled), in list order — the set :cal-clean trashes. Out-of-sync
        and still-pending invites are deliberately excluded."""
        return [
            t.id for t in self._threads
            if self._invite_status.get(t.id) in SAFE_TO_DELETE_MARKS
        ]

    def _cal_clean(self, override: bool) -> None:
        """Trash every calendar email marked safe to delete. The bare command
        only reports the count; :cal-clean! confirms and performs the delete."""
        ids = self._safe_to_delete_ids()
        if not ids:
            self.notify(
                "No calendar emails marked safe to delete in this view.",
                severity="warning",
            )
            return
        if not override:
            counts = Counter(self._invite_status.get(i) for i in ids)
            breakdown = ", ".join(
                f"{counts[m]} {m}" for m in
                (MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED)
                if counts.get(m)
            )
            self.notify(
                f"{len(ids)} calendar email{'s' if len(ids) != 1 else ''} safe to "
                f"delete ({breakdown}) — run :cal-clean! to move them to Trash.",
                timeout=12,
            )
            return
        self.notify(f"Trashing {len(ids)} calendar email{'s' if len(ids) != 1 else ''}…")
        self._bulk_trash(ids)

    @work(thread=True, group="mutations", exit_on_error=False)
    def _bulk_trash(self, thread_ids: list[str]) -> None:
        worker = get_current_worker()
        done: list[str] = []
        errors = 0
        for tid in thread_ids:
            if worker.is_cancelled:
                break
            try:
                self.gmail.trash(tid)
                done.append(tid)
            except Exception as e:
                log.error("cal-clean: trash failed for %s: %s", tid, e)
                errors += 1
        if not worker.is_cancelled:
            self.app.call_from_thread(self._after_bulk_trash, done, errors)

    def _after_bulk_trash(self, thread_ids: list[str], errors: int) -> None:
        removed = set(thread_ids)
        if removed:
            self._current_message = None
            for tid in removed:
                self._thread_cache.pop(tid, None)
                self._invite_status.pop(tid, None)
                self._invite_objs.pop(tid, None)
                self._invite_conflicts.pop(tid, None)
            msg_list = self.query_one(MessageList)
            cursor_row = msg_list.cursor_row or 0
            self._threads = [t for t in self._threads if t.id not in removed]
            msg_list.populate(self._threads, cursor_row=cursor_row)
            if self._threads:
                next_thread = self._threads[min(cursor_row, len(self._threads) - 1)]
                self._current_thread = next_thread
                self.query_one(MessagePreview).set_invite_banner(*self._banner_for(next_thread.id))
                self.query_one(MessagePreview).show_thread(next_thread)
                self._load_full_thread(next_thread.id)
            else:
                self._current_thread = None
                self.query_one(MessagePreview).clear_preview()
        note = f"Trashed {len(thread_ids)} calendar email{'s' if len(thread_ids) != 1 else ''}"
        if errors:
            note += f", {errors} failed"
        self.notify(note, timeout=6)
        self._update_status()
