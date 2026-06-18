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


from bem.tui.screens.inbox_calendar import CalendarMixin
from bem.tui.screens.inbox_copilot import CopilotMixin
from bem.tui.screens.inbox_compose import ComposeMixin
from bem.tui.screens.inbox_agent import AgentMixin
from bem.tui.screens._inbox_shared import _describe_error, _match_label  # noqa: F401 (re-export)


class InboxScreen(
    CalendarMixin, CopilotMixin, ComposeMixin, AgentMixin, Screen,
):
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
        # Bumped on every completed folder/search load. Lets a background worker
        # (e.g. Mutt's autopilot) tell when a reload it triggered has landed.
        self._threads_generation = 0
        # Set to a thread id when an action wants the cursor on that thread once
        # the next load completes (used to re-sync the list after a folder swap).
        self._pending_select_id: Optional[str] = None
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
        self._copilot_feed: list = []                 # ranked items, the [n] refs in chat
        self._copilot_ranking = None                   # latest Curator Ranking
        self._inbox_sig = None                          # inbox fingerprint, to re-rank on change
        self._copilot_undo: list[dict] = []           # reversible actions Mutt took
        self._copilot_hidden_for_agent = False         # Mutt tucked away during an agent run
        self._present = True                            # is Ben at the keyboard? (presence)
        self._away_since: Optional[float] = None        # monotonic when he stepped away
        self._away_new: list = []                       # (sender, subject) seen while away
        self._last_brief = ""                           # last while-you-were-out briefing

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
        self._threads_generation += 1
        self._next_page_token = next_token
        self._thread_cache.clear()
        cursor_row = self._restore_cursor_row or 0
        self._restore_cursor_row = None
        # An action asked to open a thread that lived in another folder; now the
        # inbox is loaded, land the cursor on it so the list and preview agree.
        if self._pending_select_id is not None:
            idx = next((i for i, t in enumerate(threads)
                        if t.id == self._pending_select_id), None)
            self._pending_select_id = None
            if idx is not None:
                cursor_row = idx
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





    _BANNER_MARKS = (
        MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED,
        MARK_OUTOFSYNC, MARK_PENDING,
    )



    # ── RSVP (accept / maybe / decline) ──────────────────────────────────────────

    # Marks that represent a real, RSVP-able calendar event (you can set or
    # change your response). Cancelled/out-of-sync/non-invites are excluded.
    _RSVP_ABLE = (MARK_PENDING, MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED)












    # ── Mutt — the live copilot ───────────────────────────────────────────────















    # ── Chat ─────────────────────────────────────────────────────────────────





    _COPILOT_KNOWN_CMDS = {
        "summarise", "summarize", "summary", "triage", "explain", "reply-draft",
        "draft-reply", "rd", "ai", "ask", "search", "move", "mv", "cal-clean",
        "cal-clean!", "sort", "zero", "tips", "folder", "cd", "go", "refresh", "r",
    }






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


    # How each non-destructive op changes a thread's labels locally, so the UI
    # reflects the mutation immediately instead of waiting for a refresh.
    _LOCAL_LABEL_OPS = {
        "mark_read":   ("remove", "UNREAD"),
        "mark_unread": ("add", "UNREAD"),
        "star":        ("add", "STARRED"),
        "unstar":      ("remove", "STARRED"),
    }




    # ── Compose ────────────────────────────────────────────────────────────────







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
        if cmd in ("folder", "cd", "go"):
            self.notify(self._drive_change_folder(arg))
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
        if cmd == "focus":
            self._set_focus(arg)
            return
        if cmd in ("brief", "briefing"):
            self._show_brief()
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
