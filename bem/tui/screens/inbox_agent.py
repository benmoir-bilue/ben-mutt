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
from bem.tui.screens._inbox_shared import _describe_error


class AgentMixin:
    def _start_agent(self, title: str, goal: str) -> None:
        if not self.config.anthropic_api_key:
            self.notify("ANTHROPIC_API_KEY not set — agent unavailable", severity="warning")
            return
        panel = self.query_one(AgentPanel)
        if panel.is_busy:
            self.notify("Agent already running — Esc in the panel to cancel", severity="warning")
            return
        # The agent panel and Mutt's copilot share the right column; showing
        # both stacks two 42%-wide panels and crushes the list/preview. Tuck
        # Mutt away for the duration of the run and restore him on dismiss.
        copilot = self.query_one(CopilotPanel)
        self._copilot_hidden_for_agent = bool(copilot.display)
        copilot.display = False
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
        self._restore_copilot_panel()
        self.query_one(MessageList).focus()
        event.stop()

    def _restore_copilot_panel(self) -> None:
        """Bring Mutt's panel back if an agent run tucked it away."""
        if getattr(self, "_copilot_hidden_for_agent", False):
            self._copilot_hidden_for_agent = False
            if getattr(self, "_copilot_on", False):
                self.query_one(CopilotPanel).display = True

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
