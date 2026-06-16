from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult

from bem.ai.commands import AIAssistant
from bem.gmail.models import Label, Thread
from bem.tui.screens.inbox import _match_label
from bem.tui.widgets.command_bar import CommandBar

LABELS = [
    Label(id="L1", name="Finance", type="user"),
    Label(id="L2", name="Clients/Acme", type="user"),
    Label(id="INBOX", name="INBOX", type="system"),
]


class TestMatchLabel:
    def test_exact(self):
        assert _match_label(LABELS, "Finance").id == "L1"

    def test_case_insensitive(self):
        assert _match_label(LABELS, "finance").id == "L1"
        assert _match_label(LABELS, "clients/acme").id == "L2"

    def test_whitespace_trimmed(self):
        assert _match_label(LABELS, "  Finance  ").id == "L1"

    def test_no_match(self):
        assert _match_label(LABELS, "Taxes") is None
        assert _match_label(LABELS, "") is None


class TestSuggestLabel:
    def _assistant_returning(self, text):
        a = AIAssistant.__new__(AIAssistant)  # skip __init__ (no real client)
        a._model_fast = "claude-haiku-4-5"
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)])

        a._client = SimpleNamespace(messages=SimpleNamespace(create=create))
        return a, captured

    def _thread(self, make_message):
        return Thread(id="t1", snippet="invoice attached",
                      messages=[make_message(subject="Xero invoice March")])

    def test_returns_model_answer_stripped(self, make_message):
        a, captured = self._assistant_returning("  Finance \n")
        answer = a.suggest_label(self._thread(make_message), ["Finance", "Clients"])
        assert answer == "Finance"
        prompt = captured["messages"][0]["content"]
        assert "Finance, Clients" in prompt
        assert "Xero invoice March" in prompt

    def test_rules_included_when_present(self, make_message):
        a, captured = self._assistant_returning("Finance")
        a.suggest_label(self._thread(make_message), ["Finance"],
                        rules="- invoices from Xero -> Finance")
        assert "invoices from Xero" in captured["messages"][0]["content"]

    def test_uses_fast_model_with_small_budget(self, make_message):
        a, captured = self._assistant_returning("Finance")
        a.suggest_label(self._thread(make_message), ["Finance"])
        assert captured["model"] == "claude-haiku-4-5"
        assert captured["max_tokens"] <= 64


class BarApp(App):
    def __init__(self):
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield CommandBar(id="command")

    def on_command_bar_command_submitted(self, event) -> None:
        self.submitted.append(event.command)


@pytest.mark.asyncio
async def test_suggestion_fills_untouched_prefix():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.show("move ")
        await pilot.pause()
        bar.suggest("move ", "Finance")
        assert bar._buffer == "move Finance"
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["move Finance"]


@pytest.mark.asyncio
async def test_suggestion_never_clobbers_user_typing():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.show("move ")
        await pilot.pause()
        await pilot.press("c")  # user starts typing "Clients"
        bar.suggest("move ", "Finance")  # late suggestion arrives
        assert bar._buffer == "move c"


@pytest.mark.asyncio
async def test_user_can_replace_suggestion():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.show("move ")
        bar.suggest("move ", "Finance")
        await pilot.pause()
        for _ in range(len("Finance")):
            await pilot.press("backspace")
        for ch in "Travel":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["move Travel"]


class TestSuggestLabelTips:
    def test_tips_included_when_present(self, make_message):
        helper = TestSuggestLabel()
        a, captured = helper._assistant_returning("Finance")
        a.suggest_label(
            helper._thread(make_message), ["Finance"],
            tips="## Finance\n- companies: Xero",
        )
        prompt = captured["messages"][0]["content"]
        assert "What lives in each folder" in prompt
        assert "- companies: Xero" in prompt

    def test_no_tips_section_when_absent(self, make_message):
        helper = TestSuggestLabel()
        a, captured = helper._assistant_returning("Finance")
        a.suggest_label(helper._thread(make_message), ["Finance"])
        assert "What lives in each folder" not in captured["messages"][0]["content"]
