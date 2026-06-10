from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from bem.ai.tools import PlanAction
from bem.tui.widgets.agent_panel import AgentPanel


class PanelApp(App):
    def __init__(self):
        super().__init__()
        self.posted: list = []

    def compose(self) -> ComposeResult:
        yield AgentPanel(id="agent")

    def on_agent_panel_plan_confirmed(self, event) -> None:
        self.posted.append("confirmed")

    def on_agent_panel_dismissed(self, event) -> None:
        self.posted.append("dismissed")

    def on_agent_panel_reply_decision(self, event) -> None:
        self.posted.append((event.decision, event.action.thread_id))


PLAN = [
    PlanAction(kind="file", thread_id="t1", subject="Xero invoice March",
               sender="Xero", label_name="Finance", reason="monthly invoice"),
    PlanAction(kind="archive", thread_id="t2", subject="Webinar replay",
               sender="HubSpot", reason="promotional"),
]


@pytest.mark.asyncio
async def test_full_run_confirm_apply_cycle():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        assert panel.display is False and panel.state == "idle"

        panel.begin("Sorting inbox")
        await pilot.pause()
        assert panel.display is True and panel.state == "running"

        panel.agent_event(("text", "Learning your folders."))
        panel.agent_event(("tool", "list_labels", ""))
        panel.agent_event(("tool_result", "12 labels"))
        assert panel._tool_count == 1

        panel.show_result("Queued 2 actions.", PLAN)
        await pilot.pause()
        assert panel.state == "confirm"
        assert panel.plan == PLAN

        await pilot.press("y")
        await pilot.pause()
        assert app.posted == ["confirmed"]
        assert panel.state == "applying"

        panel.mark_applied(2, 0)
        assert panel.state == "done"

        await pilot.press("escape")
        await pilot.pause()
        assert app.posted == ["confirmed", "dismissed"]


@pytest.mark.asyncio
async def test_n_discards_plan():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Sorting inbox")
        panel.show_result("done", PLAN)
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.posted == ["dismissed"]


@pytest.mark.asyncio
async def test_escape_while_running_dismisses():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Agent")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.posted == ["dismissed"]


@pytest.mark.asyncio
async def test_empty_plan_goes_straight_to_done():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Sorting inbox")
        panel.show_result("Nothing to do.", [])
        assert panel.state == "done"


def _reply_action(make_message, tid="t1", subject="Quarterly report"):
    from bem.gmail.models import Thread
    msg = make_message(id=f"m-{tid}", thread_id=tid, subject=subject)
    thread = Thread(id=tid, snippet="s", messages=[msg])
    return PlanAction(kind="reply", thread_id=tid, subject=subject,
                      sender="Alice Smith", body="Hi Alice,\n\nOn it.\n\nBen",
                      reason="needs answer", thread=thread)


@pytest.mark.asyncio
async def test_review_queue_decisions(make_message):
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Inbox zero")
        items = [_reply_action(make_message, "t1"), _reply_action(make_message, "t2")]
        panel.start_review(items, safe_mode=True)
        await pilot.pause()
        assert panel.state == "review"

        await pilot.press("y")          # accept draft 1
        await pilot.pause()
        assert app.posted == [("accept", "t1")]
        panel.review_next("accepted")   # inbox calls this after handling
        assert panel._review_idx == 1

        await pilot.press("n")          # skip draft 2
        await pilot.pause()
        assert app.posted[-1] == ("skip", "t2")
        panel.review_next("skipped")

        assert panel.state == "done"
        assert (panel._accepted, panel._skipped) == (1, 1)


@pytest.mark.asyncio
async def test_review_escape_finishes_early(make_message):
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Inbox zero")
        panel.start_review([_reply_action(make_message, "t1"),
                            _reply_action(make_message, "t2")], safe_mode=False)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert panel.state == "done"
        assert "dismissed" not in app.posted  # first Esc ends review, not panel
        await pilot.press("escape")
        await pilot.pause()
        assert "dismissed" in app.posted


@pytest.mark.asyncio
async def test_edit_decision_posted(make_message):
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Inbox zero")
        panel.start_review([_reply_action(make_message)], safe_mode=True)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert app.posted == [("edit", "t1")]


@pytest.mark.asyncio
async def test_events_after_dismiss_are_ignored():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.begin("Agent")
        await pilot.pause()
        panel.dismiss_panel()
        # late worker events must be no-ops
        panel.agent_event(("text", "late"))
        panel.agent_event(("tool", "search_threads", "in:inbox"))
        assert panel._tool_count == 0
