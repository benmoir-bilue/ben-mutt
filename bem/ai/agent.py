"""The email agent: a manual tool-use loop over the Claude API.

The loop is manual (not the SDK tool runner) because mutations must be
deferred for user approval, progress streams into the agent panel, and the
worker needs a cancellation check between turns.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from bem.ai.tools import AGENT_TOOLS, PlanAction, ToolExecutor
from bem.config import RULES_FILE

if TYPE_CHECKING:
    from bem.gmail import GmailClient
    from bem.gmail.models import Thread

MAX_TURNS = 25

AGENT_SYSTEM = """You are the email agent inside bem, a terminal email client.
You act on the user's Gmail through tools to save them time.

How to work:
- Work autonomously to complete the goal; never ask the user questions.
- Mutating tools (file_thread, archive_thread) only QUEUE actions into a plan
  the user reviews and approves — so act decisively once you have evidence.
- Start by learning the folder taxonomy (list_labels). Prefer existing labels.
  When unsure what belongs in a label, sample it: search_threads label:Name.
- Only propose a new label when nothing existing fits; reuse the same new
  name for similar threads rather than inventing many labels.
- Leave a thread untouched when genuinely unsure — say so in your summary.
- Keep visible commentary brief: one short line when you start a new phase of
  work. Finish with a 2-4 line summary of what you queued and anything you
  deliberately left alone.

The user's standing filing rules (follow these over your own judgement):
{rules}"""

# Event tuples emitted to the UI:
#   ("text", str)                 visible assistant commentary
#   ("tool", name, arg_summary)   a tool call is starting
#   ("tool_result", summary)      that call finished
#   ("error", str)                fatal error


@dataclass
class AgentResult:
    summary: str = ""
    plan: list[PlanAction] = field(default_factory=list)
    turns: int = 0


def _load_rules() -> str:
    try:
        text = RULES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "(none yet — the user has not recorded any rules)"


def _arg_summary(name: str, args: dict) -> str:
    if name == "search_threads":
        return str(args.get("query", ""))
    if name == "get_thread":
        return str(args.get("thread_id", ""))[:18]
    if name == "file_thread":
        return f"{str(args.get('thread_id', ''))[:12]}… → {args.get('label_name', '')}"
    if name == "archive_thread":
        return f"{str(args.get('thread_id', ''))[:12]}…"
    return ""


def _result_summary(name: str, result: str, is_error: bool) -> str:
    if is_error:
        return result[:120]
    if name in ("search_threads", "list_labels"):
        n = 0 if result.startswith("(no ") else len(result.splitlines())
        noun = "labels" if name == "list_labels" else "threads"
        return f"{n} {noun}"
    if name == "get_thread":
        return f"{len(result)} chars"
    return result[:120]


class EmailAgent:
    def __init__(self, api_key: str, model: str, gmail: "GmailClient") -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._gmail = gmail
        self._thinking_ok = True  # flips off if the API rejects adaptive thinking

    def run(
        self,
        goal: str,
        threads: list["Thread"],
        emit: Callable[[tuple], None],
        is_cancelled: Callable[[], bool],
    ) -> Optional[AgentResult]:
        """Run the agent loop. Returns None if cancelled mid-run."""
        executor = ToolExecutor(self._gmail, threads)
        system = AGENT_SYSTEM.format(rules=_load_rules())
        messages: list[dict] = [{"role": "user", "content": goal}]
        summary_parts: list[str] = []
        use_thinking = "haiku" not in self._model

        for turn in range(1, MAX_TURNS + 1):
            if is_cancelled():
                return None
            response = self._create(system, messages, use_thinking)

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    summary_parts.append(block.text.strip())
                    emit(("text", block.text.strip()))

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return AgentResult(
                    summary="\n".join(summary_parts[-3:]),
                    plan=executor.plan,
                    turns=turn,
                )

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tu in tool_uses:
                if is_cancelled():
                    return None
                emit(("tool", tu.name, _arg_summary(tu.name, dict(tu.input))))
                result, is_error = executor.execute(tu.name, dict(tu.input))
                emit(("tool_result", _result_summary(tu.name, result, is_error)))
                entry: dict = {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                }
                if is_error:
                    entry["is_error"] = True
                results.append(entry)
            messages.append({"role": "user", "content": results})

        return AgentResult(
            summary="Stopped at the turn limit — plan may be incomplete.",
            plan=executor.plan,
            turns=MAX_TURNS,
        )

    def _create(self, system: str, messages: list[dict], use_thinking: bool):
        import anthropic
        kwargs: dict = dict(
            model=self._model,
            max_tokens=4096,
            system=system,
            tools=AGENT_TOOLS,
            messages=messages,
        )
        if use_thinking and self._thinking_ok:
            kwargs["thinking"] = {"type": "adaptive"}
        try:
            return self._client.messages.create(**kwargs)
        except anthropic.BadRequestError:
            if "thinking" not in kwargs:
                raise
            # Model/API combination without adaptive thinking — retry plain
            # and stop sending it for the rest of the run.
            self._thinking_ok = False
            kwargs.pop("thinking", None)
            return self._client.messages.create(**kwargs)
