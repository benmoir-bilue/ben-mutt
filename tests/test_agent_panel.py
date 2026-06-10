from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from bem.ai.tools import PlanAction
from bem.tui.widgets.agent_panel import AgentPanel


class PanelApp(App):
    def __init__(self):
        super().__init__()
        self.posted: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentPanel(id="agent")

    def on_agent_panel_plan_confirmed(self, event) -> None:
        self.posted.append("confirmed")

    def on_agent_panel_dismissed(self, event) -> None:
        self.posted.append("dismissed")


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
