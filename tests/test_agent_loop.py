from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import anthropic

from bem.ai.agent import EmailAgent
from bem.gmail.models import Thread

from tests.test_agent_tools import FakeGmail


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _response(blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class ScriptedClient:
    """Stands in for anthropic.Anthropic — returns canned responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture
def agent(make_message):
    threads = [
        Thread(id="t1", snippet="s",
               messages=[make_message(id="m1", thread_id="t1", subject="Xero invoice")]),
    ]
    a = EmailAgent(api_key="test-key", model="claude-opus-4-8",
                   gmail=FakeGmail(threads=threads))
    return a, threads


def test_loop_executes_tools_and_returns_plan(agent):
    a, threads = agent
    client = ScriptedClient([
        _response([
            _text("Filing the invoice."),
            _tool_use("tu1", "file_thread",
                      {"thread_id": "t1", "label_name": "Finance", "reason": "invoice"}),
        ]),
        _response([_text("Done — queued 1 action.")], stop_reason="end_turn"),
    ])
    a._client = client

    events = []
    result = a.run("sort", threads, emit=events.append, is_cancelled=lambda: False)

    assert result is not None
    assert len(result.plan) == 1
    assert result.plan[0].label_name == "Finance"
    assert result.turns == 2
    kinds = [e[0] for e in events]
    assert kinds == ["text", "tool", "tool_result", "text"]

    # Second request must carry the assistant turn + the tool result back
    second = client.requests[1]
    assert second["messages"][1]["role"] == "assistant"
    tool_results = second["messages"][2]["content"]
    assert tool_results[0]["tool_use_id"] == "tu1"
    assert "queued" in tool_results[0]["content"]


def test_tool_errors_are_reported_not_fatal(agent):
    a, threads = agent
    client = ScriptedClient([
        _response([_tool_use("tu1", "file_thread",
                             {"thread_id": "bogus", "label_name": "X", "reason": "r"})]),
        _response([_text("Could not find that one.")], stop_reason="end_turn"),
    ])
    a._client = client
    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)
    assert result is not None
    assert result.plan == []
    tool_results = client.requests[1]["messages"][2]["content"]
    assert tool_results[0]["is_error"] is True


def test_cancellation_returns_none(agent):
    a, threads = agent
    a._client = ScriptedClient([])  # must never be called
    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: True)
    assert result is None


def test_thinking_passed_and_fallback_on_400(agent):
    a, threads = agent

    final = _response([_text("done")], stop_reason="end_turn")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "thinking" in kwargs:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                "thinking not supported",
                response=httpx.Response(400, request=request),
                body=None,
            )
        return final

    a._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)

    assert result is not None
    assert "thinking" in calls[0]          # tried adaptive thinking first
    assert "thinking" not in calls[1]      # retried without
    assert a._thinking_ok is False         # and won't try again


def test_haiku_models_skip_thinking(make_message):
    threads = [Thread(id="t1", snippet="s",
                      messages=[make_message(id="m1", thread_id="t1")])]
    a = EmailAgent(api_key="k", model="claude-haiku-4-5",
                   gmail=FakeGmail(threads=threads))
    client = ScriptedClient([_response([_text("hi")], stop_reason="end_turn")])
    a._client = client
    a.run("goal", threads, emit=lambda e: None, is_cancelled=lambda: False)
    assert "thinking" not in client.requests[0]
