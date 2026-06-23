from __future__ import annotations

import re
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
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

# Shown the instant Ben hits inbox zero, before (and instead of) any AI plan.
_DEFAULT_ZERO_PLAN = [
    "skim new mail as it lands — don't let it pile",
    "archive or file each one the moment it's done",
    "say :tidy if newsletters start creeping back",
]


def _is_vip(sender_name: str, sender_address: str, matchers: list[str]) -> bool:
    """True if the sender matches any VIP matcher (case-insensitive substring of
    the name or address) — the same loose matching Mutt's memory uses."""
    hay = f"{sender_name} {sender_address}".lower()
    return any(m.strip() and m.strip().lower() in hay for m in matchers)


# Adaptive Chat polling: after Mutt sends (or while you're replying), check every
# CHAT_FAST_INTERVAL seconds for up to CHAT_FAST_CYCLES idle rounds, then ramp back
# to the mail cadence (60s present / longer when away).
CHAT_FAST_INTERVAL = 5.0
CHAT_FAST_CYCLES = 10

# "send chat message …" / "test chat …" in the talk window → fire a test Chat send.
_CHAT_TEST_RE = re.compile(r"^\s*(send(\s+a)?|test)\s+chat\b", re.IGNORECASE)


def _is_chat_test_request(text: str) -> bool:
    return bool(_CHAT_TEST_RE.match(text or ""))


def _chat_test_payload(text: str) -> str:
    """The message body to send, after stripping the trigger + filler words."""
    body = _CHAT_TEST_RE.sub("", text or "", count=1).strip()
    body = re.sub(r"^(message|msg|saying|that says|to chat)\b\s*[:,-]?\s*", "",
                  body, flags=re.IGNORECASE).strip()
    return body


class CopilotMixin:
    def _toggle_copilot(self) -> None:
        panel = self.query_one(CopilotPanel)
        if panel.is_on:
            self._copilot_on = False
            if self._copilot_timer is not None:
                self._copilot_timer.stop()
                self._copilot_timer = None
            if self._chat_timer is not None:
                self._chat_timer.stop()
                self._chat_timer = None
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
        self._inbox_sig = self._inbox_signature(self._threads)
        from bem.ai import presence
        self._present = presence.is_present()
        panel.start()
        # Show the heartbeat immediately (countdown to first sniff) rather than
        # leaving a static title until the first poll lands.
        panel.mark_sniff(0, self._present, copilot_mod.poll_interval(present=self._present))
        self.notify("Mutt is on watch 🐕")
        self._copilot_curate(self._threads)
        self._schedule_copilot_poll()
        self._start_chat_polling()

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

    @staticmethod
    def _inbox_signature(threads: list[Thread]) -> frozenset:
        """A fingerprint of the inbox that changes when mail arrives, leaves
        (archived/filed/trashed), is read, or gains a reply — so the Curator
        re-ranks after Ben acts on something, not only on brand-new mail."""
        return frozenset(
            (t.id, t.is_unread, t.last_message.id if t.last_message else "")
            for t in threads
        )

    def _on_copilot_fetch(
        self, threads: list[Thread], token: Optional[str], present: bool = True,
    ) -> None:
        was_present = self._present
        self._present = present
        new = [t for t in threads if t.id not in self._seen_thread_ids]
        self._seen_thread_ids |= {t.id for t in threads}
        # Every sniff updates the heartbeat — even a quiet one — so Mutt visibly
        # stays alive. next_in mirrors how the next poll will be scheduled.
        next_in = copilot_mod.poll_interval(present=present)
        self.query_one(CopilotPanel).mark_sniff(len(new), present, next_in)

        # Presence transitions drive away-mode + the welcome-back briefing.
        if was_present and not present:
            self._enter_away()
        elif not was_present and present:
            self._return_from_away()
        if not present:
            self._away_new.extend((t.sender, t.subject) for t in new)
            if new:
                self._maybe_chat_ping(new)

        sig = self._inbox_signature(threads)
        if sig == self._inbox_sig:
            return  # nothing moved — heartbeat only, no re-rank
        self._inbox_sig = sig

        # The inbox changed (new mail, or Ben actioned/read/replied to something).
        # Repaint in place, keeping the cursor on the same email by id, then
        # re-rank so a finished hero drops and the next priority surfaces.
        if self._viewing_inbox():
            ml = self.query_one(MessageList)
            keep = ml.selected_key()
            self._threads = threads
            self._next_page_token = token
            ml.populate(threads, cursor_key=keep)
            self._scan_invites([t.id for t in threads])

        if not threads:
            self._enter_inbox_zero()
            return
        if self._inbox_zero:
            # Mail just landed on a clear inbox — Mutt perks up and thinks it over.
            self._inbox_zero = False
            self.query_one(CopilotPanel).set_inbox_zero(False)
            if new:
                self.query_one(CopilotPanel).post_note(
                    "📨 something just landed — let me think how to handle it.", "dim"
                )
        self._copilot_curate(threads)

    def _enter_inbox_zero(self) -> None:
        """Inbox hit zero: switch Mutt to the content heart, clear stale rankings,
        celebrate once, and ask him for a short plan to keep it here."""
        first = not self._inbox_zero
        self._inbox_zero = True
        self._copilot_ranking = None
        self._copilot_feed = []
        panel = self.query_one(CopilotPanel)
        panel.set_inbox_zero(True)
        panel.show_inbox_zero(self._last_zero_plan or _DEFAULT_ZERO_PLAN)
        if self._viewing_inbox():
            self.query_one(MessagePreview).show_inbox_zero()
        if first:
            panel.post_note(
                "💕 Inbox zero — nice work. I'll keep watch and help you stay here.",
                "bold cyan",
            )
            if self._copilot and self._ai:
                self._copilot_plan_zero()

    @work(thread=True, exclusive=True, group="copilot-zero", exit_on_error=False)
    def _copilot_plan_zero(self) -> None:
        """Worker: ask Mutt for a short 'stay at zero' plan, then pin it."""
        worker = get_current_worker()
        if not self._copilot or not self._copilot_on:
            return
        from bem.ai import memory
        try:
            plan = self._copilot.inbox_zero_plan(memory_ctx=memory.memory_context())
        except Exception as e:
            log.debug("inbox-zero plan failed: %s", e)
            return
        if plan and not worker.is_cancelled and self._inbox_zero:
            self._last_zero_plan = plan
            self.app.call_from_thread(
                lambda: self.query_one(CopilotPanel).show_inbox_zero(plan)
            )

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

    @work(thread=True, exclusive=True, group="copilot-curate", exit_on_error=False)
    def _copilot_curate(self, threads: list[Thread]) -> None:
        """Rank the whole inbox into one hero + on-deck (replaces the old
        per-email triage feed). Runs on a worker; renders on the main thread."""
        worker = get_current_worker()
        if not threads or not self._copilot_on:
            return
        from bem.ai import memory
        self._copilot_word += 1
        self.app.call_from_thread(self._copilot_begin_thinking, self._copilot_word)
        hints = {}
        for t in threads:
            hint = self._copilot_calendar_hint(t)
            if hint:
                hints[t.id] = hint
        try:
            ranking = self._copilot.curate(
                threads, memory_ctx=memory.memory_context(), calendar_hints=hints,
            )
        except Exception as e:
            log.debug("copilot curate failed: %s", e)
            ranking = None
        if not worker.is_cancelled and ranking is not None:
            self.app.call_from_thread(self._on_ranking, ranking)
        self.app.call_from_thread(self._copilot_end_thinking)

    def _on_ranking(self, ranking: "copilot_mod.Ranking") -> None:
        # The ranked items are also the numbered list Ben refers to in chat
        # ("archive 1", "open 2") — hero is [1].
        prev = self._copilot_ranking
        self._copilot_ranking = ranking
        self._copilot_feed = list(ranking.items)
        panel = self.query_one(CopilotPanel)
        panel.set_ranking(ranking)
        # Quiet nudge (no focus steal): a one-line trace in the chat feed when the
        # top priority actually changes, so Ben notices without being yanked.
        new_id = ranking.hero.thread_id if ranking.hero else None
        old_id = prev.hero.thread_id if (prev and prev.hero) else None
        if ranking.hero and new_id != old_id:
            panel.post_note(f"↑ new top: {ranking.hero.headline}", "dim")

    # ── Away mode + "while you were out" briefing ─────────────────────────────

    def _enter_away(self) -> None:
        """Ben stepped away — go quiet (read-only) and start collecting a briefing."""
        self._away_since = time.monotonic()
        self._away_new = []
        self.query_one(CopilotPanel).post_note(
            "💤 you're away — I'll keep watch and brief you when you're back.", "dim"
        )

    # ── Google Chat pings (Mutt reaches Ben when he's away) ───────────────────

    def _chat_client(self):
        """Lazily build the Chat client, sharing the Gmail OAuth credentials."""
        if self._chat is None:
            from bem.gchat import ChatClient
            self._chat = ChatClient(self.gmail.credentials)
        return self._chat

    def _ping_reason(self, thread: Thread, vips: list[str]) -> Optional[tuple[str, str]]:
        """Should Mutt ping Ben about this while-away arrival? Returns
        (reason, note) for a VIP sender or a high-urgency triage, else None."""
        last = thread.last_message
        addr = last.from_address if last else ""
        if _is_vip(thread.sender, addr, vips):
            return ("VIP", "")
        if self._copilot is None:
            return None  # can't judge urgency without the brain; VIP is enough
        try:
            note = self._copilot.triage(thread, rules=_load_rules())
        except Exception as e:
            log.debug("chat-ping triage failed: %s", e)
            return None
        if note.urgency == "high":
            return ("urgent", note.summary)
        return None

    def _chat_send_enabled(self) -> bool:
        """Can Mutt send to Chat? A webhook (preferred, notifies you) or a space."""
        return bool(self.config.google_chat_webhook or self.config.google_chat_space)

    def _maybe_chat_ping(self, new_threads: list[Thread]) -> None:
        """While away, message Ben on Google Chat about urgent/VIP arrivals."""
        if not self._chat_send_enabled() or not self._copilot_on:
            return
        from bem.ai import memory
        vips = memory.load_vips()
        for thread in new_threads[:5]:           # cap work on a burst of mail
            if thread.id in self._chat_pinged:
                continue
            reason = self._ping_reason(thread, vips)
            if reason is None:
                continue
            self._chat_pinged.add(thread.id)
            self._send_chat_ping(thread.sender, thread.subject, reason[0], reason[1])

    @work(thread=True, group="chat-ping", exit_on_error=False)
    def _send_chat_ping(self, sender: str, subject: str, reason: str, note: str) -> None:
        worker = get_current_worker()
        tag = "⭐ VIP" if reason == "VIP" else "🔴 urgent"
        lines = [
            f"🐕 Mutt here — you're away and something {tag} just landed:",
            f"• {sender} — {subject}",
        ]
        if note:
            lines.append(f"  {note}")
        lines.append("I'll hold it up top for you. Reply/return when you can.")
        self._chat_send("\n".join(lines))   # also echoes the outgoing in the talk panel

    def _chat_send(self, text: str, echo_full: bool = True) -> bool:
        """Worker-thread: post `text` to Chat, track its id so the poll won't read
        it back, and surface it in the talk panel. Prefers the webhook (authored
        by the app, so your phone notifies) over the user-authored API send."""
        try:
            client = self._chat_client()
            if self.config.google_chat_webhook:
                name = client.send_webhook(self.config.google_chat_webhook, text)
            else:
                name = client.send(self.config.google_chat_space, text)
            self._chat_sent_names.add(name)
        except Exception as e:
            log.debug("chat send failed: %s", e)
            return False
        if echo_full:
            self.app.call_from_thread(self._panel_chat_out, text)
        else:
            self.app.call_from_thread(self._panel_chat_sent)
        # We just messaged Ben — watch briskly for his reply.
        self.app.call_from_thread(self._bump_chat_polling)
        return True

    def _panel_chat_out(self, text: str) -> None:
        self.query_one(CopilotPanel).post_chat_out(text)

    def _panel_chat_sent(self) -> None:
        self.query_one(CopilotPanel).post_note("↗ sent to Chat", "dim")

    def _panel_say(self, text: str) -> None:
        self.query_one(CopilotPanel).post_mutt(text)

    # ── Adaptive Chat polling ─────────────────────────────────────────────────
    # Right after Mutt sends (or while you're replying) check briskly; when the
    # conversation goes quiet, ramp back to the mail cadence so we don't burn API
    # calls watching a dead thread.

    def _start_chat_polling(self) -> None:
        """Begin the Chat poll loop (only meaningful when a space is set — reading
        replies needs the API; a webhook-only setup is send-only)."""
        if not self.config.google_chat_space:
            return
        self._chat_fast_remaining = 0
        self._chat_after = None        # re-baseline so we don't replay backlog
        self._schedule_chat_poll()

    def _chat_poll_interval(self) -> float:
        if self._chat_fast_remaining > 0:
            return CHAT_FAST_INTERVAL
        return copilot_mod.poll_interval(present=self._present)

    def _schedule_chat_poll(self) -> None:
        if not self._copilot_on or not self.config.google_chat_space:
            return
        self._chat_timer = self.set_timer(self._chat_poll_interval(), self._chat_poll)

    def _chat_poll(self) -> None:
        if self._copilot_on and self.config.google_chat_space:
            self._chat_poll_worker()

    @work(thread=True, exclusive=True, group="chat-poll", exit_on_error=False)
    def _chat_poll_worker(self) -> None:
        got = self._poll_chat_instructions()
        self.app.call_from_thread(self._after_chat_poll, got)

    def _after_chat_poll(self, got: int) -> None:
        """Main thread: adapt the cadence, then schedule the next poll. Replies
        coming in keep it brisk; an idle round counts down the fast window."""
        if got:
            self._chat_fast_remaining = CHAT_FAST_CYCLES
        elif self._chat_fast_remaining > 0:
            self._chat_fast_remaining -= 1
        self._schedule_chat_poll()

    def _bump_chat_polling(self) -> None:
        """A message just went out — poll briskly for a reply, starting now."""
        self._chat_fast_remaining = CHAT_FAST_CYCLES
        if not self._copilot_on or not self.config.google_chat_space:
            return
        if self._chat_timer is not None:
            self._chat_timer.stop()
        self._schedule_chat_poll()

    def _poll_chat_instructions(self) -> int:
        """Worker thread: read new messages in the Chat space and treat Ben's
        replies as instructions for Mutt. Returns how many were dispatched."""
        if not self.config.google_chat_space or not self._copilot_on:
            return 0
        if self._chat_after is None:
            # First poll: baseline at "now" so we don't replay old history.
            self._chat_after = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            return 0
        try:
            msgs = self._chat_client().list_messages(
                self.config.google_chat_space, after=self._chat_after
            )
        except Exception as e:
            log.debug("chat poll failed: %s", e)
            return 0
        got = 0
        for m in msgs:
            if m.create_time and m.create_time > self._chat_after:
                self._chat_after = m.create_time
            if m.name in self._chat_sent_names:
                continue        # Mutt's own ping/reply — never act on it
            text = (m.text or "").strip()
            if text:
                got += 1
                self.app.call_from_thread(self._on_chat_instruction, text)
        return got

    def _on_chat_instruction(self, text: str) -> None:
        """Main thread: an instruction arrived from Chat — show it in the talk
        panel and let Mutt act, sending his reply back to the space."""
        if not self._copilot_on:
            return
        self.query_one(CopilotPanel).post_chat_in(text)
        if self._is_demo_request(text):
            self._copilot_demo_worker()
        else:
            self._copilot_chat_worker(text, to_chat=True)

    def _send_chat_reply(self, text: str) -> None:
        """Send Mutt's reply to the Chat space. The reply is already shown in the
        panel by the chat worker, so just mark that it went out (echo_full=False)."""
        self._chat_send(text, echo_full=False)

    def _return_from_away(self) -> None:
        """Ben's back — lead with what changed while he was out."""
        mins = int((time.monotonic() - self._away_since) / 60) if self._away_since else 0
        self._last_brief = self._compose_brief(mins, self._away_new)
        self.query_one(CopilotPanel).post_mutt(self._last_brief)
        self._away_since = None
        self._away_new = []

    def _compose_brief(self, mins: int, new_items: list) -> str:
        when = f"~{mins}m" if mins else "a moment"
        if not new_items:
            return f"Welcome back 🐕 Away {when} — nothing new, all quiet."
        lines = [f"Welcome back 🐕 Away {when} — {len(new_items)} new while you were out:"]
        for sender, subject in new_items[:5]:
            lines.append(f" • {sender} — {subject}")
        if len(new_items) > 5:
            lines.append(f" • …and {len(new_items) - 5} more")
        r = self._copilot_ranking
        if r and r.hero:
            lines.append(f"Top now: {r.hero.headline}")
        return "\n".join(lines)

    def _show_brief(self) -> None:
        """':brief' — show the current priorities on demand."""
        if not self._copilot_on:
            self.notify("Mutt's off — :mutt to wake him.", severity="warning")
            return
        panel = self.query_one(CopilotPanel)
        r = self._copilot_ranking
        if r and r.hero:
            lines = [f"🐕 Top priority: {r.hero.headline}"]
            if r.hero.action != "none":
                lines.append(f"   ↳ {r.hero.action} — {r.hero.hint}")
            for i, it in enumerate(r.on_deck, start=2):
                lines.append(f"   {i}. {it.headline}")
            panel.post_mutt("\n".join(lines))
        else:
            panel.post_mutt("All quiet — nothing urgent in the inbox right now. 🦴")

    # ── Tidy toward inbox-zero (markdown-native, undo-able) ───────────────────

    def _tidy(self, execute: bool = False) -> None:
        """':tidy' proposes disposable noise to clear; ':tidy!' archives it."""
        if not self._copilot_on:
            self.notify("Mutt's off — :mutt to wake him.", severity="warning")
            return
        if not self._viewing_inbox():
            self.notify("Open the inbox first, then :tidy.", severity="warning")
            return
        if execute:
            self._copilot_tidy_execute()
        else:
            self._copilot_tidy_propose()

    @work(thread=True, exclusive=True, group="copilot-tidy", exit_on_error=False)
    def _copilot_tidy_propose(self) -> None:
        worker = get_current_worker()
        from bem.ai import memory
        self._copilot_word += 1
        self.app.call_from_thread(self._copilot_begin_thinking, self._copilot_word)
        try:
            ids = self._copilot.tidy_targets(self._threads, memory.memory_context())
        except Exception as e:
            log.debug("tidy propose failed: %s", e)
            ids = []
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_tidy_proposed, ids)
        self.app.call_from_thread(self._copilot_end_thinking)

    def _on_tidy_proposed(self, ids: list[str]) -> None:
        by_id = {t.id: t for t in self._threads}
        items = [by_id[i] for i in ids if i in by_id]
        self._tidy_proposed = [t.id for t in items]
        panel = self.query_one(CopilotPanel)
        if not items:
            panel.post_mutt("Nothing obvious to tidy — inbox's already tight. 🦴")
            return
        lines = [f"I can clear {len(items)} bit(s) of noise:"]
        lines += [f" • {t.sender} — {t.subject}" for t in items[:8]]
        if len(items) > 8:
            lines.append(f" • …and {len(items) - 8} more")
        lines.append("Run :tidy! to archive them (undo-able).")
        panel.post_mutt("\n".join(lines))

    @work(thread=True, group="copilot-tidy-exec", exit_on_error=False)
    def _copilot_tidy_execute(self) -> None:
        ids = list(self._tidy_proposed)
        if not ids:
            self.app.call_from_thread(
                self.notify, "Nothing proposed — run :tidy first.", severity="warning"
            )
            return
        done = 0
        for tid in ids:
            try:
                self.gmail.modify_thread(tid, remove_labels=["INBOX"])
                done += 1
                self.app.call_from_thread(
                    self._copilot_undo.append, {"op": "archive", "thread_id": tid}
                )
            except Exception as e:
                log.debug("tidy archive failed for %s: %s", tid, e)
        self._tidy_proposed = []
        self.app.call_from_thread(self._after_tidy, done)

    def _after_tidy(self, done: int) -> None:
        self.notify(f"Tidied {done} away 🐕 — say 'undo' to restore the last.")
        if self._viewing_inbox():
            self.load_threads(label_id="INBOX")

    # ── Learning (markdown): VIP senders ──────────────────────────────────────

    def _add_vip(self, arg: str) -> None:
        """':vip <email/name>' adds a VIP; ':vip' lists them."""
        from bem.ai import memory
        m = arg.strip()
        if not m:
            vips = memory.load_vips()
            self.notify("VIPs: " + (", ".join(vips) if vips else "(none) — ':vip <who>' to add"))
            return
        if memory.add_vip(m):
            self.notify(f"VIP added: {m} 🐕")
            if self._copilot_on and self._viewing_inbox():
                self._copilot_curate(self._threads)
        else:
            self.notify(f"{m} is already a VIP.")

    def _copilot_begin_thinking(self, word_i: int) -> None:
        self.query_one(CopilotPanel).begin_thinking(word_i)

    def _copilot_end_thinking(self) -> None:
        self.query_one(CopilotPanel).end_thinking()

    def _copilot_context(self) -> str:
        """The numbered feed + currently-open thread, so Mutt can resolve
        'archive 4' or 'the invoice' to a real thread id."""
        lines = ["NUMBERED FEED (most recent at the bottom):"]
        feed = self._copilot_feed
        for idx in range(max(0, len(feed) - 20), len(feed)):
            n = feed[idx]
            lines.append(
                f"[{idx + 1}] id={n.thread_id} | {n.sender} | {n.subject} — {n.headline}"
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
        # Re-rank against the new focus right away when Mutt's watching the inbox.
        if self._copilot_on and self._viewing_inbox():
            self._copilot_curate(self._threads)

    def on_copilot_panel_chat_submitted(self, event: CopilotPanel.ChatSubmitted) -> None:
        self._route_copilot_input(event.text)
        event.stop()

    def _copilot_say(self, message: str) -> None:
        """':mutt <message>' — talk to Mutt from the command bar, waking him first."""
        if not self._copilot_on:
            self._toggle_copilot()
            if not self._copilot_on:
                return  # no API key
        self.query_one(CopilotPanel).post_user(message)
        self._route_copilot_input(message)

    def _route_copilot_input(self, text: str) -> None:
        """Send what Ben typed in the talk window to the right handler: a Chat
        test send, the autopilot demo, or the chat brain."""
        if _is_chat_test_request(text):
            self._chat_test_from_panel(text)
        elif self._is_demo_request(text):
            self._copilot_demo_worker()
        else:
            self._copilot_chat_worker(text)

    def _chat_test_from_panel(self, text: str) -> None:
        """'send chat message …' in the talk window → post it to the Chat space
        so Ben can test the round-trip; his reply comes back via the poll."""
        panel = self.query_one(CopilotPanel)
        if not self._chat_send_enabled():
            panel.post_mutt("No Chat target set — add google_chat_webhook (or google_chat_space).")
            return
        msg = _chat_test_payload(text) or "🐕 test from the talk window — reply here and I'll bring it back."
        panel.post_note("sending to Chat…", "dim")
        self._chat_test_worker(msg)

    @work(thread=True, group="chat-ping", exit_on_error=False)
    def _chat_test_worker(self, msg: str) -> None:
        if not self._chat_send(msg, echo_full=True):
            self.app.call_from_thread(
                self._panel_say, "Couldn't reach Chat — check the API/space setup."
            )

    @work(thread=True, exclusive=True, group="copilot-chat", exit_on_error=False)
    def _copilot_chat_worker(self, text: str, to_chat: bool = False) -> None:
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
            if to_chat:                       # instruction came from Chat — answer there
                self._send_chat_reply(reply)
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
