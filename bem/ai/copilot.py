"""Mutt — the live inbox copilot.

A persistent agent (distinct from the one-shot sorter in agent.py) that watches
new mail and narrates: a one-line summary, an urgency read, and a suggested next
move with the exact bem keystroke. A cheap Haiku pass triages each arrival; the
heavier Sonnet brain only runs when you talk to Mutt in the chat line.

Safe mode contract: Mutt SUGGESTS, it never files/archives/deletes/sends on its
own. When you ask it to in chat, that's you acting — it'll drive the UI for you.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bem.gmail import GmailClient
    from bem.gmail.models import Thread

try:
    from zoneinfo import ZoneInfo
    _SYD = ZoneInfo("Australia/Sydney")
except Exception:  # pragma: no cover
    _SYD = timezone.utc

NAME = "Mutt"

# Themed status words shown in the panel header while Mutt is thinking — the
# Claude-Code "Discombobulating…" trick, with a dog's nose.
STATUS_WORDS = [
    "Sniffing the inbox", "Fetching", "Digging", "Wagging", "Nosing around",
    "Following the scent", "Pawing through mail", "Ears perked", "On the scent",
    "Rooting around", "Chasing tails", "Burying bones",
]

# action -> the bem keystroke/command hint Mutt shows. Keeps the UI honest:
# the model picks the action, code maps it to the real binding.
ACTION_HINTS = {
    "reply":   "press r, or :rd to draft",
    "file":    "press s to file",
    "archive": "press e to archive",
    "delete":  "press d to delete",
    "none":    "",
}

_TRIAGE_SYSTEM = """You are Mutt, a loyal, sharp-nosed dog guarding Ben's inbox \
in the bem email client. For ONE new email, decide how Ben should handle it.

Output ONLY compact JSON, no prose:
{{"urgency":"high|normal|low","summary":"<=12 words, what it is + the ask",\
"action":"reply|file|archive|delete|none","reason":"<=10 words why"}}

Guidance:
- high = needs Ben today: a deadline, a VIP, a time-sensitive ask, a real person waiting.
- low = newsletters, automated notices, FYIs, things already handled.
- delete only when truly disposable (newsletter, self-forwarded article,
  a calendar item already handled on the diary).
- reply when a person is genuinely waiting on Ben.
- Honour Ben's standing rules below over your own judgement.

Ben's standing rules:
{rules}"""


@dataclass
class TriageNote:
    thread_id: str
    subject: str
    sender: str
    urgency: str = "normal"     # high | normal | low
    summary: str = ""
    action: str = "none"        # reply | file | archive | delete | none
    reason: str = ""

    @property
    def hint(self) -> str:
        return ACTION_HINTS.get(self.action, "")


def is_active_hours(now: Optional[datetime] = None) -> bool:
    """True during AEST/AEDT working hours (07:00–19:00 Australia/Sydney), when
    Mutt polls every minute. Outside that it idles on a slow heartbeat."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_SYD)
    return 7 <= local.hour < 19


def poll_interval(now: Optional[datetime] = None) -> float:
    """Seconds between polls: brisk in the day, lazy at night."""
    return 60.0 if is_active_hours(now) else 600.0


def status_word(i: int) -> str:
    """A themed status word; rotates by an externally supplied counter (no RNG
    so it stays deterministic and testable)."""
    return STATUS_WORDS[i % len(STATUS_WORDS)]


def _coerce_note(raw: str, thread: "Thread") -> TriageNote:
    """Parse the model's JSON into a TriageNote, degrading gracefully."""
    note = TriageNote(thread_id=thread.id, subject=thread.subject, sender=thread.sender)
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, json.JSONDecodeError):
        note.summary = thread.snippet[:80]
        return note
    note.urgency = str(data.get("urgency", "normal")).lower()
    if note.urgency not in ("high", "normal", "low"):
        note.urgency = "normal"
    note.summary = str(data.get("summary", "")).strip() or thread.snippet[:80]
    action = str(data.get("action", "none")).lower()
    note.action = action if action in ACTION_HINTS else "none"
    note.reason = str(data.get("reason", "")).strip()
    return note


class CopilotBrain:
    def __init__(self, api_key: str, fast_model: str, smart_model: str) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._fast = fast_model
        self._smart = smart_model

    def triage(
        self, thread: "Thread", rules: str = "", calendar_hint: str = "",
    ) -> TriageNote:
        """Cheap Haiku pass: classify one newly-arrived thread. calendar_hint is
        a pre-computed note (e.g. 'invite already accepted') that short-circuits
        the model's guesswork on calendar mail."""
        last = thread.last_message
        body = (last.body[:1200] if last else "")
        content = (
            f"From: {thread.sender}\nSubject: {thread.subject}\n"
            + (f"Calendar: {calendar_hint}\n" if calendar_hint else "")
            + f"\n{body}"
        )
        try:
            resp = self._client.messages.create(
                model=self._fast,
                max_tokens=300,
                system=_TRIAGE_SYSTEM.format(rules=rules or "(none)"),
                messages=[{"role": "user", "content": content}],
            )
            raw = resp.content[0].text if resp.content else ""
        except Exception:
            raw = ""
        return _coerce_note(raw, thread)

    def chat(
        self,
        messages: list[dict],
        executor: "CopilotExecutor",
        emit: Callable[[str], None],
        is_cancelled: Callable[[], bool],
        max_turns: int = 8,
    ) -> str:
        """Sonnet conversation with tools. Emits assistant commentary to the
        feed as it goes; executes read tools against Gmail and UI tools via the
        executor's callback. Returns the final reply text."""
        system = _CHAT_SYSTEM.format(rules=executor.rules)
        last_text = ""
        for _ in range(max_turns):
            if is_cancelled():
                return last_text
            try:
                resp = self._client.messages.create(
                    model=self._smart, max_tokens=2048, system=system,
                    tools=COPILOT_CHAT_TOOLS, messages=messages,
                )
            except Exception as e:
                emit(f"(Mutt tripped over a wire: {e})")
                return last_text
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    last_text = block.text.strip()
                    emit(last_text)
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return last_text
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                if is_cancelled():
                    return last_text
                out, is_err = executor.execute(tu.name, dict(tu.input))
                entry: dict = {"type": "tool_result", "tool_use_id": tu.id, "content": out}
                if is_err:
                    entry["is_error"] = True
                results.append(entry)
            messages.append({"role": "user", "content": results})
        return last_text


_CHAT_SYSTEM = """You are Mutt, Ben's loyal inbox dog in the bem terminal email \
client. You're chatting with Ben in the side panel. Be warm, brief and a little \
playful — a good dog who's sharp about email. Plain text, short lines.

What you can do:
- Read mail (search_threads, get_thread) and check his diary (check_calendar).
- Drive his screen: open_thread shows him an email; run_command fires a bem command.
- Draft replies right here in chat for him to read.

bem commands for run_command (single letters act on the currently open thread):
  r = reply, :rd <tone> = AI-draft a reply, s = file (suggests a label),
  e = archive, d = delete/trash, :summarise, :move <label>, :cal-clean,
  A/M/X = accept/maybe/decline a calendar invite.

SAFE MODE IS ON. You may open and read anything freely. You may run mutating \
commands (file/archive/delete/send) ONLY when Ben asks you to in chat — that is \
him acting through you. Never mutate on your own initiative; suggest instead.

When Ben asks you to do something, do it with tools then confirm in one line. \
When he asks a question, answer it. When he wants a reply drafted, write it in \
his voice (warm, concise, Australian; sign off 'Cheers, Ben'), show it, and \
offer to open it with :rd.

Ben's standing rules:
{rules}"""


COPILOT_CHAT_TOOLS: list[dict] = [
    {
        "name": "search_threads",
        "description": "Search Gmail (Gmail query syntax). One line per thread: id, date, sender, subject, snippet.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "get_thread",
        "description": "Fetch one thread in full, including message bodies.",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"], "additionalProperties": False,
        },
    },
    {
        "name": "check_calendar",
        "description": "For a thread that is a meeting invite, report Ben's live response (accepted/declined/etc.) and any conflicts around that time.",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"], "additionalProperties": False,
        },
    },
    {
        "name": "open_thread",
        "description": "Open a thread in Ben's message list + preview so he can see it. Use before suggesting an action on it.",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"], "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": "Fire a bem command in the main UI (e.g. ':summarise', ':rd friendly', 's', 'd', ':move Recruiting'). Mutating commands only when Ben asked for them.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"], "additionalProperties": False,
        },
    },
]


class CopilotExecutor:
    """Executes Mutt's chat tools. Read tools hit Gmail; UI tools (open_thread,
    run_command) are handed to a main-thread callback supplied by the inbox."""

    def __init__(
        self, gmail: "GmailClient", calendar, ui_action: Callable[[str, str], None],
        threads: list["Thread"], rules: str = "",
    ) -> None:
        from bem.ai.tools import ToolExecutor
        self._read = ToolExecutor(gmail, threads)
        self._gmail = gmail
        self._calendar = calendar
        self._ui = ui_action
        self.rules = rules or "(none)"

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        if name in ("search_threads", "get_thread"):
            return self._read.execute(name, args)
        if name == "check_calendar":
            return self._check_calendar(args.get("thread_id", ""))
        if name == "open_thread":
            self._ui("open_thread", args.get("thread_id", ""))
            return "opened in Ben's view", False
        if name == "run_command":
            self._ui("run_command", args.get("command", ""))
            return f"ran: {args.get('command', '')}", False
        return f"Unknown tool: {name}", True

    def _check_calendar(self, thread_id: str) -> tuple[str, bool]:
        from bem.calendar import parse_ics
        if self._calendar is None:
            return "calendar not connected", True
        thread = self._gmail.get_thread(thread_id)
        if thread is None:
            return "thread not found", True
        for m in thread.messages:
            att = m.calendar_attachment
            if not att or not att.attachment_id:
                continue
            data = self._gmail.get_attachment(m.id, att.attachment_id)
            invite = parse_ics(data.decode("utf-8", "replace")) if data else None
            if invite is None or not invite.uid:
                continue
            info = self._calendar.lookup(invite.uid, self._gmail.email_address)
            if not info.found:
                return f"'{invite.summary}': not on your calendar (out of sync)", False
            parts = [f"'{invite.summary}': your response = {info.response}"]
            conflicts = self._calendar.conflicts(invite.dtstart, invite.dtend, invite.uid)
            if conflicts:
                parts.append("conflicts with " + ", ".join(c.summary for c in conflicts))
            return "; ".join(parts), False
        return "no calendar invite in this thread", False

