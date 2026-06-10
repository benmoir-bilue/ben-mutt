from __future__ import annotations

import json
from collections.abc import Generator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bem.gmail.models import Thread


SYSTEM_PROMPT = """You are an email assistant embedded in a terminal email client called bem.
You have access to the full text of email threads. Be concise and helpful.
Respond in plain text suitable for a terminal — no markdown headers, no bullet formatting with *.
Use short lines (under 80 chars where possible)."""


class AIAssistant:
    def __init__(
        self,
        api_key: str,
        model_fast: str = "claude-haiku-4-5-20251001",
        model_smart: str = "claude-sonnet-4-6",
    ) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model_fast = model_fast
        self._model_smart = model_smart

    def summarise(self, thread: "Thread") -> Generator[str, None, None]:
        content = _thread_to_text(thread)
        prompt = (
            "Summarise this email thread in 3-5 sentences. "
            "Who said what, what's the current status, and what (if any) action is needed?\n\n"
            + content
        )
        yield from self._stream(prompt, self._model_fast)

    def reply_draft(self, thread: "Thread", tone: str = "professional") -> Generator[str, None, None]:
        content = _thread_to_text(thread)
        prompt = (
            f"Draft a {tone} reply to this email thread. "
            "Write only the body of the reply — no subject line, no greeting preamble beyond 'Hi X,'.\n\n"
            + content
        )
        yield from self._stream(prompt, self._model_smart)

    def explain(self, thread: "Thread") -> Generator[str, None, None]:
        content = _thread_to_text(thread)
        prompt = (
            "Explain what this email thread is about in plain language, "
            "as if briefing someone who has no context.\n\n"
            + content
        )
        yield from self._stream(prompt, self._model_fast)

    def custom(self, thread: "Thread", command: str) -> Generator[str, None, None]:
        content = _thread_to_text(thread)
        prompt = f"Email thread:\n{content}\n\nInstruction: {command}"
        yield from self._stream(prompt, self._model_smart)

    def triage_structured(self, threads: list["Thread"]) -> dict[str, list]:
        """Classify every thread in a single call.

        Returns the raw {category: [{"n": 1-indexed number, "note": str}, ...]}
        mapping. This one response drives both the triage panel text and the
        colour labels, so they can never disagree.
        """
        summaries = _triage_thread_lines(threads)

        prompt = (
            "Classify each thread number into exactly one category.\n"
            "Categories: action (needs reply/action), waiting (awaiting response), "
            "fyi (low priority, informational), archive (safe to archive).\n"
            "For each thread give its number and a short note (under 8 words) "
            "saying why.\n"
            "Return ONLY valid JSON, no other text, in this shape:\n"
            '{"action": [{"n": 3, "note": "contract needs signature"}], '
            '"waiting": [{"n": 14, "note": "sent proposal Friday"}], '
            '"fyi": [], "archive": [{"n": 1, "note": "booking completed"}]}\n\n'
            "Threads:\n" + "\n".join(summaries)
        )
        resp = self._client.messages.create(
            model=self._model_fast,
            max_tokens=2048,
            system="You are an email classifier. Output only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)

    def _stream(
        self, user_prompt: str, model: str, max_tokens: int = 1024
    ) -> Generator[str, None, None]:
        with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            for text in stream.text_stream:
                yield text


def _triage_thread_lines(threads: list["Thread"]) -> list[str]:
    """One numbered summary line per thread; numbers are 1-indexed positions
    in the displayed list, which the inbox screen maps back to threads."""
    lines = []
    for i, t in enumerate(threads, 1):
        msg = t.last_message
        snippet = (msg.snippet if msg else t.snippet)[:120]
        lines.append(f"{i}. [{t.sender}] {t.subject} — {snippet}")
    return lines


def _thread_to_text(thread: "Thread") -> str:
    parts = [f"Subject: {thread.subject}\n"]
    for msg in thread.messages:
        date_str = msg.date.strftime("%a %d %b %Y %H:%M") if msg.date else ""
        parts.append(
            f"--- {msg.display_from} ({date_str}) ---\n"
            f"{msg.body[:3000]}\n"
        )
    return "\n".join(parts)
