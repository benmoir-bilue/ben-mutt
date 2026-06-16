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


def test_turn_limit_sets_flag_and_injects_wrap_up_nudge(agent, monkeypatch):
    import bem.ai.agent as agent_mod
    monkeypatch.setattr(agent_mod, "MAX_TURNS", 6)  # WRAP_UP_AT=4 → nudge at turn 2

    a, threads = agent
    client = ScriptedClient([
        _response([_tool_use(f"tu{i}", "search_threads", {"query": "in:inbox"})])
        for i in range(6)
    ])
    a._client = client

    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)

    assert result is not None
    assert result.turns == 6
    assert "turn limit" in result.warning

    # The agent mutates one messages list in place (the recorded requests all
    # alias it), so inspect its final state: exactly one nudge, riding along
    # with turn 2's tool results — messages run goal, asst1, results1, asst2,
    # results2+nudge, putting the nudge in the message at index 4.
    messages = client.requests[-1]["messages"]
    nudges = [
        (i, b) for i, m in enumerate(messages) if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "text"
        and "turns remain" in b.get("text", "")
    ]
    assert len(nudges) == 1  # injected once, not every turn
    assert nudges[0][0] == 4


def test_finishing_normally_sets_no_warning(agent):
    a, threads = agent
    a._client = ScriptedClient([_response([_text("done")], stop_reason="end_turn")])
    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)
    assert result is not None
    assert result.warning == ""


def test_truncated_response_is_retried_once(agent):
    """A response cut off by max_tokens has no tool calls and no text — it
    must not be mistaken for a finished run."""
    a, threads = agent
    client = ScriptedClient([
        _response([], stop_reason="max_tokens"),               # thinking ate the budget
        _response([_text("all sorted")], stop_reason="end_turn"),
    ])
    a._client = client
    events = []
    result = a.run("sort", threads, emit=events.append, is_cancelled=lambda: False)
    assert result is not None
    assert result.warning == ""
    assert result.summary == "all sorted"
    assert len(client.requests) == 2
    assert any("token cap" in e[1] for e in events if e[0] == "text")


def test_repeated_truncation_ends_with_warning(agent):
    a, threads = agent
    a._client = ScriptedClient([
        _response([], stop_reason="max_tokens"),
        _response([], stop_reason="max_tokens"),
    ])
    result = a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)
    assert result is not None
    assert "token cap" in result.warning


def test_thinking_runs_get_a_larger_output_budget(agent):
    import bem.ai.agent as agent_mod
    a, threads = agent
    client = ScriptedClient([_response([_text("done")], stop_reason="end_turn")])
    a._client = client
    a.run("sort", threads, emit=lambda e: None, is_cancelled=lambda: False)
    assert client.requests[0]["max_tokens"] == agent_mod.MAX_TOKENS_THINKING
