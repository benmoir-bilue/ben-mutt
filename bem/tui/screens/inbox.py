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


def _tips_taxonomy(tips: Optional[Tips]) -> Optional[str]:
    """Step-1 goal text telling the agent to rely on saved folder tips
    instead of scanning labels; None when there are no usable tips."""
    if tips is None or not tips.content:
        return None
    date = (
        tips.generated_at.astimezone().strftime("%d %b %Y")
        if tips.generated_at else "an unknown date"
    )
    return (
        f"1. Use these saved folder tips (recorded {date}) to decide "
        "where things go — call list_labels only to confirm the folder "
        "list, and do not sample labels:\n"
        f"--- folder tips ---\n{tips.content}\n--- end tips ---\n"
    )


def _sort_goal(hint: str = "", tips: Optional[Tips] = None) -> str:
    taxonomy = _tips_taxonomy(tips) or (
        "1. Learn the folder taxonomy with list_labels. If unsure what the "
        "user files in a label, sample it (search_threads label:Name).\n"
    )
    goal = (
        "Sort the user's inbox into folders.\n"
        + taxonomy +
        "2. Search in:inbox (up to 50 threads).\n"
        "3. For each thread, queue file_thread or archive_thread when you are "
        "confident. Leave threads that likely still need the user's reply, "
        "and anything you are unsure about, in the inbox untouched."
    )
    if hint.strip():
        goal += f"\nAdditional instruction from the user: {hint.strip()}"
    return goal


def _tips_goal() -> str:
    return (
        "Build the folder-tips knowledge file, so future sorting runs do not "
        "have to re-scan every folder.\n"
        "1. list_labels to get the user's folders.\n"
        "2. For each user label, search_threads label:Name with max_results "
        "10 to read its latest threads. Use get_thread only when a subject "
        "and snippet are too vague to tell what the folder holds.\n"
        "3. For each folder, record the people involved (names, addresses), "
        "the companies (senders, domains), and what material is discussed.\n"
        "4. Call save_folder_tips ONCE with concise notes for every folder, "
        "as markdown sections: '## <Label>' followed by '- people:', "
        "'- companies:' and '- topics:' lines. Keep each folder to at most "
        "3 short lines."
    )


def _match_label(labels: list[GmailLabel], name: str) -> Optional[GmailLabel]:
    """Resolve a user-typed (or model-suggested) label name, case-insensitively."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    return next((l for l in labels if l.name.lower() == wanted), None)


def _zero_goal(hint: str = "", tips: Optional[Tips] = None) -> str:
    taxonomy = _tips_taxonomy(tips) or (
        "1. Learn the folder taxonomy with list_labels.\n"
    )
    goal = (
        "Get the user's inbox to zero.\n"
        + taxonomy +
        "2. Learn the user's writing voice: search_threads in:sent, then "
        "get_thread on 2-3 recent sent threads. Note their tone, greeting, "
        "sign-off, and typical length.\n"
        "3. Search in:inbox (up to 50 threads).\n"
        "4. Handle every thread: if it needs a reply from the user, "
        "get_thread it and draft_reply in their voice (match their tone, "
        "keep it as short as they would). If it is noise, archive_thread. "
        "If it belongs in a folder, file_thread. Leave anything you cannot "
        "handle confidently and say why in your summary."
    )
    if hint.strip():
        goal += f"\nAdditional instruction from the user: {hint.strip()}"
    return goal


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
        Binding("s", "move", "Move", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("u", "toggle_unread", "Unread", show=False),
        Binding("!", "toggle_star", "Star", show=False),
        Binding("c", "change_folder", "Folder", show=False),
        Binding("A", "rsvp_accept", "Accept invite", show=False),
        Binding("M", "rsvp_maybe", "Maybe (tentative)", show=False),
        Binding("X", "rsvp_decline", "Decline invite", show=False),
        Binding("t", "talk_to_mutt", "Talk to Mutt", show=False),
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
        # Set when the cursor is on a message row inside an expanded thread;
        # reply/forward then target this message instead of the newest one.
        self._current_message: Optional[Message] = None
        self._restore_cursor_row: Optional[int] = None
        self._thread_cache: dict[str, Thread] = {}
        self._preview_timer: Optional[Timer] = None
        self._pending_replies: list[PlanAction] = []
        self._labels: list[GmailLabel] = []
        self._calendar: Optional[CalendarClient] = None
        # thread_id -> invite status ("accepted" | "declined" | ...). Caches
        # calendar lookups so re-loading a folder doesn't re-query everything.
        self._invite_status: dict[str, str] = {}
        self._invite_objs: dict[str, "CalendarInvite"] = {}  # thread_id -> parsed .ics
        self._invite_conflicts: dict[str, list[Conflict]] = {}  # pending invites only
        self._voice_samples: Optional[list[str]] = None  # cached Sent-folder samples
        # ── Mutt, the live copilot ──
        self._copilot: Optional[CopilotBrain] = None
        self._copilot_on = False
        self._copilot_timer: Optional[Timer] = None
        self._seen_thread_ids: set[str] = set()      # for new-mail delta
        self._copilot_chat: list[dict] = []           # rolling chat history
        self._last_activity: float = 0.0              # monotonic; for idle detection
        self._copilot_word = 0                        # rotates status words
        self._copilot_feed: list = []                 # ordered TriageNotes (the [n] refs)
        self._copilot_undo: list[dict] = []           # reversible actions Mutt took

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
            yield AgentPanel(id="agent")
            yield CopilotPanel(id="copilot")
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
        self._labels = labels
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
            if self.query_one(AgentPanel).state == "idle":
                msg_list.focus()  # don't steal focus from an active agent panel
            self._update_status()
            log.debug("_on_threads_loaded: populate complete")
        except Exception as e:
            log.error("_on_threads_loaded: %s\n%s", e, traceback.format_exc())
        self._scan_invites([t.id for t in threads])

    # ── Calendar invites ────────────────────────────────────────────────────────

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

    _BANNER_MARKS = (
        MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED,
        MARK_OUTOFSYNC, MARK_PENDING,
    )

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

    # ── RSVP (accept / maybe / decline) ──────────────────────────────────────────

    # Marks that represent a real, RSVP-able calendar event (you can set or
    # change your response). Cancelled/out-of-sync/non-invites are excluded.
    _RSVP_ABLE = (MARK_PENDING, MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED)

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

    # ── Mutt — the live copilot ───────────────────────────────────────────────

    def _toggle_copilot(self) -> None:
        panel = self.query_one(CopilotPanel)
        if panel.is_on:
            self._copilot_on = False
            if self._copilot_timer is not None:
                self._copilot_timer.stop()
                self._copilot_timer = None
            panel.stop()
            self.notify("Mutt is off")
            return
        if not self.config.anthropic_api_key:
            self.notify("ANTHROPIC_API_KEY not set — Mutt needs it.", severity="warning")
            return
        if self._copilot is None:
            self._copilot = CopilotBrain(
                self.config.anthropic_api_key,
                self.config.ai_model_fast, self.config.ai_model_smart,
            )
        self._copilot_on = True
        self._copilot_feed = []
        self._copilot_undo = []
        # Seed 'seen' with the current inbox so Mutt reacts only to NEW mail,
        # then give an initial digest of the top few threads already sitting here.
        self._seen_thread_ids = {t.id for t in self._threads}
        panel.start()
        self.notify("Mutt is on watch 🐕")
        self._copilot_triage_batch(self._threads[:5])
        self._schedule_copilot_poll()

    def _schedule_copilot_poll(self) -> None:
        if not self._copilot_on:
            return
        self._copilot_timer = self.set_timer(
            copilot_mod.poll_interval(), self._copilot_poll
        )

    def _copilot_poll(self) -> None:
        if not self._copilot_on:
            return
        self._copilot_fetch()
        self._schedule_copilot_poll()

    @work(thread=True, exclusive=True, group="copilot-poll", exit_on_error=False)
    def _copilot_fetch(self) -> None:
        """Quietly list the inbox to spot new mail — independent of which folder
        the user is currently viewing."""
        worker = get_current_worker()
        try:
            threads, token = self.gmail.list_threads(
                label_id="INBOX", max_results=self.config.threads_per_page,
            )
        except Exception as e:
            log.debug("copilot fetch failed: %s", e)
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_copilot_fetch, threads, token)

    def _on_copilot_fetch(self, threads: list[Thread], token: Optional[str]) -> None:
        new = [t for t in threads if t.id not in self._seen_thread_ids]
        self._seen_thread_ids |= {t.id for t in threads}
        if not new:
            return
        # Refresh the visible list only when the user is on the inbox AND idle,
        # so new mail appears without yanking the view mid-navigation.
        if self._viewing_inbox() and self._is_idle():
            self._threads = threads
            self._next_page_token = token
            cursor = self.query_one(MessageList).cursor_row or 0
            self.query_one(MessageList).populate(threads, cursor_row=cursor)
            self._scan_invites([t.id for t in threads])
        self._copilot_triage_batch(new[:5])

    def _viewing_inbox(self) -> bool:
        return self._current_label_id == "INBOX" and not self._current_query

    def _is_idle(self) -> bool:
        return (time.monotonic() - self._last_activity) > 8.0

    def _copilot_calendar_hint(self, thread: Thread) -> str:
        mark = self._invite_status.get(thread.id)
        if mark in SAFE_TO_DELETE_MARKS:
            return f"already handled on your calendar ({mark}) — safe to delete"
        if mark == MARK_OUTOFSYNC:
            return "event was cancelled/removed from your calendar"
        if mark == MARK_PENDING:
            return "a calendar invite awaiting your RSVP"
        return ""

    @work(thread=True, exclusive=True, group="copilot-triage", exit_on_error=False)
    def _copilot_triage_batch(self, threads: list[Thread]) -> None:
        worker = get_current_worker()
        rules = _load_rules()
        for t in threads:
            if worker.is_cancelled or not self._copilot_on:
                break
            self._copilot_word += 1
            self.app.call_from_thread(self._copilot_begin_thinking, self._copilot_word)
            hint = self._copilot_calendar_hint(t)
            try:
                note = self._copilot.triage(t, rules=rules, calendar_hint=hint)
            except Exception as e:
                log.debug("copilot triage failed: %s", e)
                continue
            if not worker.is_cancelled:
                self.app.call_from_thread(self._on_triage_note, note)
        self.app.call_from_thread(self._copilot_end_thinking)

    def _copilot_begin_thinking(self, word_i: int) -> None:
        self.query_one(CopilotPanel).begin_thinking(word_i)

    def _copilot_end_thinking(self) -> None:
        self.query_one(CopilotPanel).end_thinking()

    def _on_triage_note(self, note: "copilot_mod.TriageNote") -> None:
        self._copilot_feed.append(note)
        number = len(self._copilot_feed)
        self.query_one(CopilotPanel).post_triage(note, number)
        # Auto-open only the genuinely urgent, and only when the user is idle.
        if note.urgency == "high" and self._is_idle():
            self._open_thread_by_id(note.thread_id)

    def _copilot_context(self) -> str:
        """The numbered feed + currently-open thread, so Mutt can resolve
        'archive 4' or 'the invoice' to a real thread id."""
        lines = ["NUMBERED FEED (most recent at the bottom):"]
        feed = self._copilot_feed
        for idx in range(max(0, len(feed) - 20), len(feed)):
            n = feed[idx]
            lines.append(
                f"[{idx + 1}] id={n.thread_id} | {n.sender} | {n.subject} — {n.summary}"
            )
        if self._current_thread is not None:
            lines.append(
                f"\nCURRENTLY OPEN: id={self._current_thread.id} | "
                f"{self._current_thread.subject}"
            )
        return "\n".join(lines)

    def _open_thread_by_id(self, thread_id: str) -> None:
        ml = self.query_one(MessageList)
        try:
            ml.move_cursor(row=ml.get_row_index(thread_id))
            ml.focus()
            return
        except Exception:
            pass
        thread = self._thread_cache.get(thread_id)
        if thread is None:
            try:
                thread = self.gmail.get_thread(thread_id)
            except Exception:
                thread = None
        if thread is not None:
            self._current_thread = thread
            self.query_one(MessagePreview).set_invite_banner(*self._banner_for(thread_id))
            self.query_one(MessagePreview).show_thread(thread)

    # ── Chat ─────────────────────────────────────────────────────────────────

    def action_talk_to_mutt(self) -> None:
        panel = self.query_one(CopilotPanel)
        if not panel.is_on:
            self.notify("Mutt is off — :copilot to wake him.", severity="warning")
            return
        panel.focus_input()

    def on_copilot_panel_chat_submitted(self, event: CopilotPanel.ChatSubmitted) -> None:
        self._copilot_chat_worker(event.text)
        event.stop()

    def _copilot_say(self, message: str) -> None:
        """':mutt <message>' — talk to Mutt from the command bar, waking him first."""
        if not self._copilot_on:
            self._toggle_copilot()
            if not self._copilot_on:
                return  # no API key
        self.query_one(CopilotPanel).post_user(message)
        self._copilot_chat_worker(message)

    @work(thread=True, exclusive=True, group="copilot-chat", exit_on_error=False)
    def _copilot_chat_worker(self, text: str) -> None:
        worker = get_current_worker()
        self._copilot_word += 1
        self.app.call_from_thread(self._copilot_begin_thinking, self._copilot_word)
        convo = list(self._copilot_chat)
        convo.append({"role": "user", "content": text})
        context = self.app.call_from_thread(self._copilot_context)
        ui = lambda name, args: self.app.call_from_thread(self._apply_copilot_ui, name, args)
        emit = lambda t: self.app.call_from_thread(self.query_one(CopilotPanel).post_mutt, t)
        executor = CopilotExecutor(
            self.gmail, self._cal, ui, self._threads, rules=_load_rules(),
        )
        try:
            reply = self._copilot.chat(
                convo, executor, emit,
                lambda: worker.is_cancelled or not self._copilot_on,
                context=context,
            )
        except Exception as e:
            log.debug("copilot chat failed: %s", e)
            reply = ""
        if reply:
            self._copilot_chat.append({"role": "user", "content": text})
            self._copilot_chat.append({"role": "assistant", "content": reply})
            self._copilot_chat = self._copilot_chat[-20:]
        self.app.call_from_thread(self._copilot_end_thinking)

    _COPILOT_KNOWN_CMDS = {
        "summarise", "summarize", "summary", "triage", "explain", "reply-draft",
        "draft-reply", "rd", "ai", "ask", "search", "move", "mv", "cal-clean",
        "cal-clean!", "sort", "zero", "tips",
    }

    def _apply_copilot_ui(self, name: str, args: dict) -> str:
        """Action + UI tools Mutt fires, run on the main thread. Returns an
        honest result string fed back to Mutt."""
        tid = args.get("thread_id", "")
        if name == "open_thread":
            self._open_thread_by_id(tid)
            return f"opened '{self._thread_subject(tid)}'"
        if name == "archive_thread":
            self._mutate_thread(tid, "archive")
            self._copilot_undo.append({"op": "archive", "thread_id": tid})
            return f"archived '{self._thread_subject(tid)}' (say 'undo' to restore)"
        if name == "trash_thread":
            self._mutate_thread(tid, "trash")
            self._copilot_undo.append({"op": "trash", "thread_id": tid})
            return f"trashed '{self._thread_subject(tid)}' (in Trash; say 'undo' to restore)"
        if name == "file_thread":
            label = args.get("label", "").strip()
            if not label:
                return "need a label to file under"
            self._move_worker(tid, label)
            self._copilot_undo.append({"op": "file", "thread_id": tid})
            return f"filed '{self._thread_subject(tid)}' under {label} (say 'undo' to restore)"
        if name == "undo_last":
            return self._copilot_undo_last()
        if name == "run_command":
            cmd = args.get("command", "").strip().lstrip(":")
            head = cmd.split(None, 1)[0].lower() if cmd else ""
            if head not in self._COPILOT_KNOWN_CMDS:
                return f"'{cmd}' isn't a bem command I can run"
            self._run_command(cmd)
            return f"ran :{cmd}"
        return f"unknown action: {name}"

    def _thread_subject(self, thread_id: str) -> str:
        for n in self._copilot_feed:
            if n.thread_id == thread_id:
                return n.subject
        t = self._thread_by_id(thread_id) or self._thread_cache.get(thread_id)
        return t.subject if t else thread_id[:12]

    def _copilot_undo_last(self) -> str:
        if not self._copilot_undo:
            return "nothing to undo"
        entry = self._copilot_undo.pop()
        self._copilot_undo_worker(entry["thread_id"], entry["op"])
        return f"undoing the last {entry['op']}"

    @work(thread=True, group="mutations", exit_on_error=False)
    def _copilot_undo_worker(self, thread_id: str, op: str) -> None:
        worker = get_current_worker()
        try:
            if op == "trash":
                self.gmail.untrash(thread_id)
            else:  # archive / file -> put it back in the inbox
                self.gmail.modify_thread(thread_id, add_labels=["INBOX"])
        except Exception as e:
            log.error("copilot undo failed: %s", e)
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self.notify, "Restored to inbox 🐕")

    def on_key(self, event: events.Key) -> None:
        # Track activity so Mutt knows when the user is busy (don't grab focus
        # or yank the view) versus idle (safe to auto-open urgent mail).
        self._last_activity = time.monotonic()

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
            # Pass every loaded id (not just the new page) so the replace in
            # apply_invite_marks keeps earlier pages' tags; cached threads
            # are skipped from re-resolving, so this stays cheap.
            self._scan_invites([t.id for t in self._threads])
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
        self._last_activity = time.monotonic()
        preview = self.query_one(MessagePreview)
        preview.set_invite_banner(*self._banner_for(event.thread.id))
        self._current_message = None
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
        self._current_message = None
        self._load_full_thread(event.thread.id, preview_only=False)
        self.query_one(MessagePreview).focus()
        event.stop()

    @staticmethod
    def _message_in(thread: Thread, message: Message) -> Message:
        """Prefer the thread's own copy of a message — after the full fetch it
        carries the body, where the listed (metadata) copy has only headers."""
        return next((m for m in thread.messages if m.id == message.id), message)

    def on_message_list_message_highlighted(
        self, event: MessageList.MessageHighlighted
    ) -> None:
        preview = self.query_one(MessagePreview)
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        cached = self._thread_cache.get(event.thread.id)
        thread = cached if cached is not None else event.thread
        self._current_thread = thread
        self._current_message = self._message_in(thread, event.message)
        preview.set_invite_banner(*self._banner_for(event.thread.id))
        preview.show_message(self._current_message)
        if cached is None:
            thread_id = event.thread.id
            self._preview_timer = self.set_timer(
                0.2, lambda: self._load_full_thread(thread_id)
            )
        if self._threads and event.thread.id == self._threads[-1].id:
            self._maybe_load_more()
        self._update_status()
        event.stop()

    def on_message_list_message_selected(
        self, event: MessageList.MessageSelected
    ) -> None:
        self._current_message = event.message
        self._load_full_thread(event.thread.id, preview_only=False)
        self.query_one(MessagePreview).focus()
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
        preview = self.query_one(MessagePreview)
        preview.set_invite_banner(*self._banner_for(thread.id))
        if self._current_message is not None:
            # Cursor is on a message row: swap in the full-bodied copy of
            # that message and keep the preview focused on it.
            self._current_message = self._message_in(thread, self._current_message)
            preview.show_message(self._current_message)
        else:
            preview.show_thread(thread)
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
            self._compose_reply(
                self._current_thread, reply_all=False, message=self._current_message
            )

    def action_reply_all(self) -> None:
        if self._current_thread:
            self._compose_reply(
                self._current_thread, reply_all=True, message=self._current_message
            )

    def action_forward(self) -> None:
        if self._current_thread:
            self._compose_forward(self._current_thread, message=self._current_message)

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

    def action_move(self) -> None:
        """Open command mode pre-filled with `move `, then let a fast model
        suggest the most logical destination while the user can keep typing."""
        thread = self._current_thread
        if not thread:
            return
        self.action_command_mode("move ")
        if self._ai and any(l.type == "user" for l in self._labels):
            self._suggest_move_worker(thread)

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

    # ── Compose ────────────────────────────────────────────────────────────────

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
        if cmd in ("move", "mv"):
            self._do_move(arg)
            return
        if cmd in ("sort", "sort!"):
            tips, proceed = self._gated_tips("sort", override=cmd.endswith("!"))
            if proceed:
                self._start_agent("Sorting inbox", _sort_goal(arg, tips=tips))
            return
        if cmd in ("zero", "zero!"):
            tips, proceed = self._gated_tips("zero", override=cmd.endswith("!"))
            if proceed:
                self._start_agent("Inbox zero", _zero_goal(arg, tips=tips))
            return
        if cmd == "tips":
            self._start_agent("Learning folders", _tips_goal())
            return
        if cmd in ("cal-clean", "cal-clean!"):
            self._cal_clean(override=cmd.endswith("!"))
            return
        if cmd in ("copilot", "mutt"):
            if arg.strip():
                self._copilot_say(arg.strip())   # ":mutt <message>" one-liner
            else:
                self._toggle_copilot()
            return
        if cmd == "agent":
            if not arg:
                self.notify("Usage: agent <goal>", severity="warning")
                return
            self._start_agent("Agent", arg)
            return
        if cmd == "rule":
            self._add_rule(arg)
            return
        if cmd in ("summarise", "summarize", "summary"):
            self._ai_command("summarise")
            return
        if cmd == "triage":
            self._ai_command("triage")
            return
        if cmd in ("reply-draft", "draft-reply", "rd"):
            self._ai_command("reply_draft", arg or "professional")
            return
        if cmd == "explain":
            self._ai_command("explain")
            return
        if cmd in ("ai", "ask"):
            if not arg:
                self.notify("Usage: ai <instruction>", severity="warning")
                return
            self._ai_command("custom", arg)
            return
        # Unknown command — report it and reopen the (cleared) bar to retry,
        # rather than silently running it as a free-form AI prompt.
        self.notify(f"Unknown command: :{cmd}", severity="warning")
        self.action_command_mode()

    def _gated_tips(self, cmd: str, override: bool) -> tuple[Optional[Tips], bool]:
        """Load folder tips for an agent run. Returns (tips, proceed): stale
        tips block the run unless overridden with :<cmd>!."""
        tips = load_tips()
        if tips is not None and tips.is_stale() and not override:
            age = tips.age_days()
            age_str = f"{age} days old" if age is not None else "undated"
            self.notify(
                f"Folder tips are {age_str} — run :tips to refresh them, "
                f"or :{cmd}! to continue with the stale tips.",
                severity="warning", timeout=10,
            )
            return None, False
        if tips is None:
            self.notify(
                "Tip: run :tips once to record your folders — future "
                "runs skip the folder scan.", timeout=8,
            )
        return tips, True

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
        # Capture the selected message now: when the cursor is on an earlier
        # message in an expanded thread, the reply targets that message's sender
        # (Mutt-style), not the newest message in the thread.
        target = self._current_message if is_reply else None
        panel = AIPanel(
            title=f"AI — {title_map.get(cmd, cmd)}",
            work_fn=lambda p: self._run_ai_worker(p, cmd, thread, arg, target),
            confirm_label="Open as reply in editor?" if is_reply else None,
            confirm_fn=(
                (lambda p: self._open_ai_reply(thread, p, target)) if is_reply else None
            ),
        )
        self.app.push_screen(panel)

    def _get_voice_samples(self) -> list[str]:
        """A few de-quoted Sent-folder messages as writing-voice samples, fetched
        once per session. Best-effort — never blocks drafting."""
        if self._voice_samples is None:
            try:
                self._voice_samples = self.gmail.recent_sent_replies(limit=3)
            except Exception as e:
                log.debug("voice samples fetch failed: %s", e)
                self._voice_samples = []
        return self._voice_samples

    def _open_ai_reply(
        self, thread: Thread, panel: AIPanel, target: Optional[Message] = None
    ) -> None:
        body = panel.full_text.strip()
        if not body:
            return
        # Prepend a disclaimer the editor shows but parse_draft strips before
        # sending, so the recipient never sees it.
        note = (
            f"{AI_DRAFT_MARKER} First draft by {model_label(self.config.ai_model_smart)}"
            " — review & edit before sending; this line is removed automatically."
        )
        draft = build_reply_draft(
            thread, my_address=self.gmail.email_address,
            body=f"{note}\n\n{body}", message=target,
        )
        # Let the modal finish closing before suspending for the editor. Pass the
        # target so the In-Reply-To/References headers point at the right message.
        self.call_after_refresh(self._open_editor_and_send, draft, thread, target)

    @work(thread=True, group="ai", exit_on_error=False)
    def _run_ai_worker(
        self, panel: AIPanel, cmd: str, thread: Optional[Thread], arg: str,
        target: Optional[Message] = None,
    ) -> None:
        worker = get_current_worker()
        try:
            ai = self._ai
            if cmd == "summarise":
                gen = ai.summarise(thread)
            elif cmd == "reply_draft":
                gen = ai.reply_draft(
                    thread, tone=arg or "professional", target=target,
                    voice=self.config.voice_notes,
                    signature=self.config.signature,
                    samples=self._get_voice_samples(),
                    rules=_load_rules(),
                )
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

    # ── Email agent (:sort / :agent) ───────────────────────────────────────────

    def _start_agent(self, title: str, goal: str) -> None:
        if not self.config.anthropic_api_key:
            self.notify("ANTHROPIC_API_KEY not set — agent unavailable", severity="warning")
            return
        panel = self.query_one(AgentPanel)
        if panel.is_busy:
            self.notify("Agent already running — Esc in the panel to cancel", severity="warning")
            return
        panel.begin(title)
        self._run_agent_worker(goal, panel)

    @work(thread=True, exclusive=True, group="agent", exit_on_error=False)
    def _run_agent_worker(self, goal: str, panel: AgentPanel) -> None:
        worker = get_current_worker()
        agent = EmailAgent(
            api_key=self.config.anthropic_api_key,
            model=self.config.ai_model_agent,
            gmail=self.gmail,
        )

        def emit(event: tuple) -> None:
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.agent_event, event)

        try:
            result = agent.run(
                goal,
                threads=list(self._threads),
                emit=emit,
                is_cancelled=lambda: worker.is_cancelled,
            )
        except Exception as e:
            log.error("agent run failed: %s\n%s", e, traceback.format_exc())
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.show_error, _describe_error(e))
            return
        if result is not None and not worker.is_cancelled:
            log.debug("agent finished: %d turns, %d plan actions",
                      result.turns, len(result.plan))
            self.app.call_from_thread(
                panel.show_result, result.summary, result.plan, result.warning
            )

    def on_agent_panel_plan_confirmed(self, event: AgentPanel.PlanConfirmed) -> None:
        panel = self.query_one(AgentPanel)
        plan = list(panel.plan)
        mutations = [a for a in plan if a.kind in ("file", "archive")]
        self._pending_replies = [a for a in plan if a.kind == "reply"]
        if mutations:
            self._apply_plan_worker(mutations, panel)
        elif self._pending_replies:
            replies, self._pending_replies = self._pending_replies, []
            panel.start_review(replies, safe_mode=self.config.safe_mode)
        event.stop()

    def on_agent_panel_dismissed(self, event: AgentPanel.Dismissed) -> None:
        self.app.workers.cancel_group(self, "agent")
        self._pending_replies = []
        self.query_one(AgentPanel).dismiss_panel()
        self.query_one(MessageList).focus()
        event.stop()

    @work(thread=True, group="agent-apply", exit_on_error=False)
    def _apply_plan_worker(self, plan: list[PlanAction], panel: AgentPanel) -> None:
        worker = get_current_worker()
        applied = 0
        errors = 0
        try:
            labels = {l.name.lower(): l.id for l in self.gmail.list_labels()}
        except Exception as e:
            if not worker.is_cancelled:
                self.app.call_from_thread(panel.show_error, _describe_error(e))
            return
        for action in plan:
            if worker.is_cancelled:
                return
            try:
                if action.kind == "file":
                    label_id = labels.get(action.label_name.lower())
                    if label_id is None:
                        new_label = self.gmail.create_label(action.label_name)
                        labels[new_label.name.lower()] = new_label.id
                        label_id = new_label.id
                    self.gmail.modify_thread(
                        action.thread_id,
                        add_labels=[label_id],
                        remove_labels=["INBOX"],
                    )
                else:
                    self.gmail.archive(action.thread_id)
                applied += 1
            except Exception as e:
                log.error("plan apply failed for %s: %s", action.thread_id, e)
                errors += 1
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_plan_applied, applied, errors)

    def _on_plan_applied(self, applied: int, errors: int) -> None:
        panel = self.query_one(AgentPanel)
        if self._pending_replies:
            replies, self._pending_replies = self._pending_replies, []
            suffix = f", {errors} failed" if errors else ""
            self.notify(f"Applied {applied} actions{suffix}")
            panel.start_review(replies, safe_mode=self.config.safe_mode)
        else:
            panel.mark_applied(applied, errors)
        self._restore_cursor_row = self.query_one(MessageList).cursor_row
        self.load_threads(label_id=self._current_label_id)
        self.load_labels()

    def on_agent_panel_reply_decision(self, event: AgentPanel.ReplyDecision) -> None:
        panel = self.query_one(AgentPanel)
        action, decision = event.action, event.decision
        event.stop()
        if decision == "skip":
            panel.review_next("skipped")
            return
        thread = action.thread
        if thread is None or thread.last_message is None:
            self.notify("Draft is missing its thread context", severity="error")
            panel.review_next("skipped")
            return
        draft_text = build_reply_draft(
            thread, my_address=self.gmail.email_address, body=action.body
        )
        content: Optional[str] = draft_text
        if decision == "edit":
            with self.app.suspend():
                content = launch_editor(
                    draft_text, self.config.editor, treat_unchanged_as_cancel=False
                )
            if content is None:
                panel.review_next("skipped")
                return
        to, cc, subject, body = parse_draft(content)
        if not to or not body.strip():
            self.notify("Skipped (empty To or body)", severity="warning")
            panel.review_next("skipped")
            return
        if self.config.safe_mode:
            self._save_draft_worker(to, cc, subject, body, thread)
        else:
            self._send_worker(to, cc, subject, body, thread)
        panel.review_next("accepted")

    def _add_rule(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.notify("Usage: rule <filing rule, e.g. invoices from Xero -> Finance>",
                        severity="warning")
            return
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RULES_FILE, "a", encoding="utf-8") as f:
            f.write(f"- {text}\n")
        self.notify(f"Rule saved to {RULES_FILE.name}")

    # ── Status bar ─────────────────────────────────────────────────────────────

    def _update_status(self) -> None:
        total = len(self._threads)
        unread = sum(1 for t in self._threads if t.is_unread)
        msg_list = self.query_one(MessageList)
        # With expanded threads, table rows outnumber threads — count by the
        # highlighted thread, not the cursor row.
        current_id = self._current_thread.id if self._current_thread else None
        idx = next(
            (i + 1 for i, t in enumerate(self._threads) if t.id == current_id),
            (msg_list.cursor_row or 0) + 1,
        )

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
