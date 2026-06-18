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
from bem.tui.screens._inbox_shared import _match_label

log = bemlog.get()


class CopilotMixin:
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
        if self.query_one(AgentPanel).display:
            # They share the right column — don't stack Mutt on a live agent run.
            self.notify("Agent panel is open — press Esc to close it first.", severity="warning")
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
        from bem.ai import presence
        self._present = presence.is_present()
        panel.start()
        # Show the heartbeat immediately (countdown to first sniff) rather than
        # leaving a static title until the first poll lands.
        panel.mark_sniff(0, self._present, copilot_mod.poll_interval(present=self._present))
        self.notify("Mutt is on watch 🐕")
        self._copilot_triage_batch(self._threads[:5])
        self._schedule_copilot_poll()

    def _schedule_copilot_poll(self) -> None:
        if not self._copilot_on:
            return
        self._copilot_timer = self.set_timer(
            copilot_mod.poll_interval(present=self._present), self._copilot_poll
        )

    def _copilot_poll(self) -> None:
        if not self._copilot_on:
            return
        self._copilot_fetch()
        self._schedule_copilot_poll()

    @work(thread=True, exclusive=True, group="copilot-poll", exit_on_error=False)
    def _copilot_fetch(self) -> None:
        """Quietly list the inbox to spot new mail — independent of which folder
        the user is currently viewing. Also samples presence (off the UI thread)."""
        worker = get_current_worker()
        from bem.ai import presence
        present = presence.is_present()
        try:
            threads, token = self.gmail.list_threads(
                label_id="INBOX", max_results=self.config.threads_per_page,
            )
        except Exception as e:
            log.debug("copilot fetch failed: %s", e)
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_copilot_fetch, threads, token, present)

    def _on_copilot_fetch(
        self, threads: list[Thread], token: Optional[str], present: bool = True,
    ) -> None:
        self._present = present
        new = [t for t in threads if t.id not in self._seen_thread_ids]
        self._seen_thread_ids |= {t.id for t in threads}
        # Every sniff updates the heartbeat — even a quiet one — so Mutt visibly
        # stays alive. next_in mirrors how the next poll will be scheduled.
        next_in = copilot_mod.poll_interval(present=present)
        self.query_one(CopilotPanel).mark_sniff(len(new), present, next_in)
        if not new:
            return
        # Repaint the inbox in place, keeping the cursor on the same email by id
        # so new mail lands without yanking the view mid-navigation.
        if self._viewing_inbox():
            ml = self.query_one(MessageList)
            keep = ml.selected_key()
            self._threads = threads
            self._next_page_token = token
            ml.populate(threads, cursor_key=keep)
            self._scan_invites([t.id for t in threads])
        self._copilot_triage_batch(new[:5])

    def _viewing_inbox(self) -> bool:
        return self._current_label_id == "INBOX" and not self._current_query

    def _ensure_inbox_view(self) -> int:
        """Main thread: make the inbox the live list, returning the
        threads-generation to wait on. The folder switch reloads asynchronously,
        so a caller needing the list ready should wait for the generation to
        advance past the returned value (see ``_await_inbox_view``)."""
        if not self._viewing_inbox():
            self._current_label_id = "INBOX"
            self._current_label_name = "Inbox"
            self._current_query = ""
            self.load_threads(label_id="INBOX")
            self._update_status()
        return self._threads_generation

    def _await_inbox_view(self, alive) -> bool:
        """From a worker thread: bring the inbox into view before Mutt drives it
        so what he narrates matches what's on screen. Returns True once the inbox
        is the live list (it may already have been)."""
        if self.app.call_from_thread(self._viewing_inbox):
            return True
        gen = self.app.call_from_thread(self._ensure_inbox_view)
        for _ in range(40):  # wait up to ~4s for the reload to land
            if not alive():
                return False
            if (self._threads_generation > gen
                    and self.app.call_from_thread(self._viewing_inbox)):
                return True
            time.sleep(0.1)
        return self.app.call_from_thread(self._viewing_inbox)

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
        lines.append(f"\nNOW SHOWING FOLDER: {self._current_label_name}")
        if self._labels:
            names = ", ".join(l.display_name for l in self._labels[:40])
            lines.append(f"FOLDERS YOU CAN OPEN (change_folder): {names}")
        return "\n".join(lines)

    def _open_thread_by_id(self, thread_id: str) -> None:
        ml = self.query_one(MessageList)
        try:
            ml.move_cursor(row=ml.get_row_index(thread_id))
            ml.focus()
            return
        except Exception:
            pass
        # Not in the current list. Mutt's feed comes from the inbox, so if this
        # is a feed thread, bring the inbox back into view and select it there
        # once it loads — keeping the list and preview on the same folder rather
        # than previewing an inbox thread over some other folder's list.
        if (not self._viewing_inbox()
                and any(n.thread_id == thread_id for n in self._copilot_feed)):
            self._pending_select_id = thread_id
            self._ensure_inbox_view()
            return
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

    def action_talk_to_mutt(self) -> None:
        panel = self.query_one(CopilotPanel)
        if not panel.is_on:
            self.notify("Mutt is off — :copilot to wake him.", severity="warning")
            return
        panel.focus_input()

    def _set_focus(self, arg: str) -> None:
        """':focus <text>' sets the week's priority Mutt ranks against; ':focus'
        shows the current one; ':focus clear' resets it."""
        from bem.ai import memory
        text = arg.strip()
        if not text:
            foc = memory.load_focus()
            if foc and foc.text:
                age = foc.age_days()
                when = f" (set {age}d ago)" if age is not None else ""
                stale = " — getting stale, reset it?" if foc.is_stale() else ""
                self.notify(f"Focus{when}: {foc.text}{stale}")
            else:
                self.notify("No focus set — ':focus <what you're on>' to set one.")
            return
        if text.lower() in ("clear", "none", "off", "reset"):
            memory.clear_focus()
            self.notify("Focus cleared 🐕")
            return
        memory.save_focus(text)
        self.notify(f"Focus set: {text} 🐕")

    def on_copilot_panel_chat_submitted(self, event: CopilotPanel.ChatSubmitted) -> None:
        if self._is_demo_request(event.text):
            self._copilot_demo_worker()
        else:
            self._copilot_chat_worker(event.text)
        event.stop()

    def _copilot_say(self, message: str) -> None:
        """':mutt <message>' — talk to Mutt from the command bar, waking him first."""
        if not self._copilot_on:
            self._toggle_copilot()
            if not self._copilot_on:
                return  # no API key
        self.query_one(CopilotPanel).post_user(message)
        if self._is_demo_request(message):
            self._copilot_demo_worker()
        else:
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

    def _apply_copilot_ui(self, name: str, args: dict) -> str:
        """Action + UI tools Mutt fires, run on the main thread. Returns an
        honest result string fed back to Mutt."""
        tid = args.get("thread_id", "")
        if name == "open_thread":
            self._open_thread_by_id(tid)
            return f"opened '{self._thread_subject(tid)}'"
        if name == "change_folder":
            return self._drive_change_folder(args.get("folder", ""))
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
        if name == "move_cursor":
            direction = args.get("direction", "down")
            self._drive_cursor(direction)
            return f"moved selection {direction}"
        if name == "scroll_preview":
            direction = args.get("direction", "down")
            self._drive_scroll(direction)
            return f"scrolled preview {direction}"
        if name == "expand_thread":
            self._drive_expand()
            return "toggled thread expand"
        return f"unknown action: {name}"

    # ── TUI driver primitives (main thread) ──────────────────────────────────

    def _drive_change_folder(self, name: str) -> str:
        """Switch the visible folder by name, the way pressing `c` and picking a
        folder would. Returns an honest result string fed back to Mutt."""
        name = (name or "").strip()
        if not name:
            return "which folder? (e.g. Inbox, Sent, Finance)"
        # Match the typed name against the raw label name first (what :move
        # uses), then the friendly display name Mutt sees in its context
        # (so 'Drafts'/'Trash' resolve, not just 'DRAFT'/'TRASH').
        label = _match_label(self._labels, name)
        if label is None:
            wanted = name.lower()
            label = next(
                (l for l in self._labels if l.display_name.lower() == wanted), None
            )
        if label is None:
            known = ", ".join(l.display_name for l in self._labels) or "(none loaded)"
            return f"no folder called '{name}'. Folders: {known}"
        self._restore_cursor_row = None
        self._current_label_id = label.id
        self._current_label_name = label.display_name
        self._current_query = ""
        self.load_threads(label_id=label.id)
        self._update_status()
        return f"opened the {label.display_name} folder"

    def _drive_cursor(self, direction: str) -> None:
        ml = self.query_one(MessageList)
        ml.focus()
        if direction == "up":
            ml.action_cursor_up()
        elif direction == "top":
            ml.move_cursor(row=0)
        elif direction == "bottom":
            ml.action_cursor_bottom()
        else:
            ml.action_cursor_down()

    def _drive_scroll(self, direction: str) -> None:
        preview = self.query_one(MessagePreview)
        (preview.scroll_up if direction == "up" else preview.scroll_down)()

    def _drive_expand(self) -> None:
        self.query_one(MessageList).action_toggle_thread()

    def _mutt_say(self, text: str) -> None:
        self.query_one(CopilotPanel).post_mutt(text)

    # ── "Show me how you can control the TUI" — scripted autopilot demo ───────

    @staticmethod
    def _is_demo_request(text: str) -> bool:
        t = text.lower().strip().strip(":/ ")
        if t in ("demo", "autopilot"):
            return True
        if "control the tui" in t or "control the interface" in t or "drive the tui" in t:
            return True
        return ("show" in t and ("control" in t or "drive" in t)
                and ("tui" in t or "interface" in t or "screen" in t or "you" in t))

    @work(thread=True, exclusive=True, group="copilot-demo", exit_on_error=False)
    def _copilot_demo_worker(self) -> None:
        """A paced choreography proving Mutt can drive the TUI: move the inbox
        selection, preview, open/expand, scroll — narrating each step."""
        worker = get_current_worker()

        def alive() -> bool:
            return not worker.is_cancelled and self._copilot_on

        def do(fn, *a) -> None:
            if alive():
                self.app.call_from_thread(fn, *a)

        def say(text: str) -> None:
            do(self._mutt_say, text)

        # Drive what we narrate: if Ben wandered off to another folder, fold the
        # inbox back into view first so the list on screen matches the demo.
        if not self._await_inbox_view(alive):
            return
        if not self._threads:
            say("Inbox's empty right now, so there's nothing to drive — load "
                "some mail and ask me again. 🐕")
            return
        say("Watch this — I'll take the wheel and drive your inbox. 🐕")
        time.sleep(1.3)
        do(self._drive_cursor, "top")
        say("Jumping to the top of the inbox…")
        time.sleep(1.1)
        for _ in range(min(3, max(1, len(self._threads) - 1))):
            if not alive():
                return
            do(self._drive_cursor, "down")
            time.sleep(0.9)
        say("…scrolling down, previewing each one as I pass it.")
        time.sleep(1.1)
        say("Let me unfold this conversation and read down it…")
        do(self._drive_expand)
        time.sleep(0.9)
        for _ in range(2):
            if not alive():
                return
            do(self._drive_scroll, "down")
            time.sleep(0.7)
        time.sleep(0.5)
        say("That's the gist: I can move the selection, open & preview, scroll, "
            "and expand threads — and act (archive, file, reply, RSVP) the moment "
            "you ask. Try: \"archive 1\", \"open the one from Anna\", or \"what's "
            "urgent?\"")

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
        # "Esc to leave" (as Mutt's welcome line promises): close the panel when
        # it's on screen. The chat input's own Esc yields focus first and stops
        # the event, so this only fires from the inbox — one Esc shuts Mutt.
        if event.key == "escape" and self._copilot_on:
            panel = self.query_one(CopilotPanel)
            if panel.display:
                self._toggle_copilot()
                event.stop()
