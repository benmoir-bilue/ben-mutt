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
import time
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
    "rsvp":    "press A accept / X decline",
    "read":    "press Enter to open",
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


def poll_interval(now: Optional[datetime] = None, present: bool = True) -> float:
    """Seconds between polls: brisk in the day, lazy at night — and never faster
    than every 5 minutes when Ben is away from the keyboard."""
    base = 60.0 if is_active_hours(now) else 600.0
    if not present:
        return max(base, 300.0)
    return base


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


# ── The Curator: rank the whole inbox → one hero + a short on-deck ──────────

# Old mail decays: an unactioned thread loses half its weight every this-many
# days, so last month's "urgent" can't outrank today's real ask.
DECAY_HALF_LIFE_DAYS = 5.0
MAX_CANDIDATES = 25   # most-recent threads weighed per pass (cost ceiling)
HERO_POOL = 4         # top-scored items the smart model ranks + writes up


@dataclass
class Candidate:
    thread_id: str
    sender: str
    subject: str
    snippet: str
    age_days: float
    unread: bool
    calendar_hint: str = ""


@dataclass
class RankedItem:
    thread_id: str
    sender: str
    subject: str
    headline: str          # what it is + the ask
    why: str = ""          # why it matters now
    action: str = "none"   # reply|file|archive|delete|rsvp|read|none
    score: float = 0.0

    @property
    def hint(self) -> str:
        return ACTION_HINTS.get(self.action, "")


@dataclass
class Ranking:
    hero: Optional[RankedItem] = None
    on_deck: list[RankedItem] = None  # up to 3
    considered: int = 0

    def __post_init__(self):
        if self.on_deck is None:
            self.on_deck = []

    @property
    def items(self) -> list[RankedItem]:
        """Hero first, then on-deck — the numbered list Ben refers to in chat."""
        return ([self.hero] if self.hero else []) + list(self.on_deck)


def _decay(age_days: float, half_life: float = DECAY_HALF_LIFE_DAYS) -> float:
    return 0.5 ** (max(0.0, age_days) / half_life)


def _curate_candidates(
    threads: list["Thread"], now: Optional[datetime] = None,
    hints: Optional[dict] = None, limit: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """The most-recent inbox threads, compacted for the scoring pass."""
    now = now or datetime.now(timezone.utc)
    hints = hints or {}
    ordered = sorted(
        threads, key=lambda t: t.date or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:limit]
    out = []
    for t in ordered:
        age = (now - t.date).total_seconds() / 86400 if t.date else 30.0
        out.append(Candidate(
            thread_id=t.id, sender=t.sender, subject=t.subject,
            snippet=(t.snippet or "")[:140], age_days=max(0.0, age),
            unread=t.is_unread, calendar_hint=hints.get(t.id, ""),
        ))
    return out


def _extract_json(raw: str):
    """Best-effort parse of a model reply that should be JSON (tolerates ``` fences
    and surrounding prose). Returns the parsed object or None."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    # Parse around whichever delimiter appears FIRST, so an object that contains
    # an array (e.g. the hero with its on_deck list) isn't mis-read as the array.
    starts = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]")) if text.find(o) != -1]
    if not starts:
        return None
    i, _open, close_c = min(starts)
    j = text.rfind(close_c)
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except (ValueError, json.JSONDecodeError):
            return None
    return None


_CURATE_SCORE_SYSTEM = """You are Mutt, ranking Ben's inbox. For EACH email, score \
how much it deserves Ben's attention RIGHT NOW from 0-100.

Weigh up:
- matches Ben's current focus → much higher
- VIP sender (board/investor/key customer/family) → higher
- a real person genuinely waiting on Ben's reply → higher
- a deadline or time-sensitive ask → higher
- newsletters, automated notices, receipts, notifications → much lower

Output ONLY a JSON array, no prose:
[{{"id":"<thread_id>","score":<0-100>,"action":"reply|file|archive|delete|rsvp|read|none"}}]

What Mutt knows:
{memory}"""

_CURATE_HERO_SYSTEM = """You are Mutt, Ben's sharp chief of staff. From these top \
candidates, pick THE single most important thing for Ben to do right now (the hero) \
and the next three (on-deck).

For the hero: a crisp headline (what it is + the ask, <=14 words), why it matters now \
(<=12 words), and the action verb. For each on-deck item: its id, a one-line headline, \
and the action.

Output ONLY JSON, no prose:
{{"hero":{{"id":"...","headline":"...","why":"...","action":"reply|file|archive|delete|rsvp|read|none"}},
"on_deck":[{{"id":"...","headline":"...","action":"..."}}]}}

Honour Ben's focus, VIPs and rules below.
{memory}"""

_TIDY_SYSTEM = """You are Mutt, tidying Ben's inbox toward zero. From these emails, \
list ONLY the ids that are clearly disposable and safe to archive WITHOUT Ben reading \
them — newsletters, automated notifications, receipts, marketing blasts.

NEVER include: anything where a real person is waiting on Ben, anything matching his \
focus or VIPs, calendar invites, or anything you're unsure about. When in doubt, leave \
it out.

Output ONLY a JSON array of ids, e.g. ["id1","id2"]. Empty array if nothing's safe.

What Mutt knows:
{memory}"""


class CopilotBrain:
    def __init__(self, api_key: str, fast_model: str, smart_model: str) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._api_key = api_key
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

    def curate(
        self,
        threads: list["Thread"],
        memory_ctx: str = "",
        calendar_hints: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> Ranking:
        """Rank the whole inbox into one hero + 3 on-deck. Cheap Haiku pass scores
        every candidate; old mail is decayed in code; the smart model then writes
        the hero + on-deck for the top few. Degrades gracefully at each step."""
        candidates = _curate_candidates(threads, now=now, hints=calendar_hints)
        if not candidates:
            return Ranking(considered=0)
        by_id = {c.thread_id: c for c in candidates}
        scores = self._score_pass(candidates, memory_ctx)  # id -> (score, action)
        ranked = sorted(
            candidates,
            key=lambda c: scores.get(c.thread_id, (0, "none"))[0] * _decay(c.age_days),
            reverse=True,
        )
        top = ranked[:HERO_POOL]
        ranking = self._compose_hero(top, by_id, scores, memory_ctx)
        ranking.considered = len(candidates)
        return ranking

    def _score_pass(self, candidates: list[Candidate], memory_ctx: str) -> dict:
        """Haiku scores every candidate. Returns {thread_id: (score, action)};
        empty on failure (curate then falls back to recency)."""
        lines = "\n".join(
            f"{c.thread_id} | {c.sender} | {c.subject} | {int(c.age_days)}d old"
            + (" | unread" if c.unread else "")
            + (f" | calendar: {c.calendar_hint}" if c.calendar_hint else "")
            + f" | {c.snippet}"
            for c in candidates
        )
        try:
            resp = self._client.messages.create(
                model=self._fast, max_tokens=1500,
                system=_CURATE_SCORE_SYSTEM.format(memory=memory_ctx or "(nothing yet)"),
                messages=[{"role": "user", "content": lines}],
            )
            data = _extract_json(resp.content[0].text if resp.content else "")
        except Exception:
            data = None
        out: dict = {}
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                tid = str(row.get("id", ""))
                if tid not in {c.thread_id for c in candidates}:
                    continue
                try:
                    score = max(0.0, min(100.0, float(row.get("score", 0))))
                except (TypeError, ValueError):
                    score = 0.0
                action = str(row.get("action", "none")).lower()
                out[tid] = (score, action if action in ACTION_HINTS else "none")
        return out

    def _compose_hero(
        self, top: list[Candidate], by_id: dict, scores: dict, memory_ctx: str,
    ) -> Ranking:
        """Smart model writes the hero + on-deck for the top candidates. Falls
        back to a recency/score ordering if the model output is unusable."""
        def fallback() -> Ranking:
            items = [
                RankedItem(
                    thread_id=c.thread_id, sender=c.sender, subject=c.subject,
                    headline=c.subject, why="", action=scores.get(c.thread_id, (0, "none"))[1],
                    score=scores.get(c.thread_id, (0, "none"))[0] * _decay(c.age_days),
                )
                for c in top
            ]
            return Ranking(hero=items[0] if items else None, on_deck=items[1:4])

        if not top:
            return Ranking()
        digest = "\n".join(
            f"{c.thread_id} | {c.sender} | {c.subject}"
            + (f" | calendar: {c.calendar_hint}" if c.calendar_hint else "")
            + f"\n    {c.snippet}"
            for c in top
        )
        try:
            resp = self._client.messages.create(
                model=self._smart, max_tokens=800,
                system=_CURATE_HERO_SYSTEM.format(memory=memory_ctx or "(nothing yet)"),
                messages=[{"role": "user", "content": digest}],
            )
            data = _extract_json(resp.content[0].text if resp.content else "")
        except Exception:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("hero"), dict):
            return fallback()

        def build(raw: dict) -> Optional[RankedItem]:
            tid = str(raw.get("id", ""))
            c = by_id.get(tid)
            if c is None:
                return None
            action = str(raw.get("action", "none")).lower()
            return RankedItem(
                thread_id=tid, sender=c.sender, subject=c.subject,
                headline=str(raw.get("headline", "")).strip() or c.subject,
                why=str(raw.get("why", "")).strip(),
                action=action if action in ACTION_HINTS else "none",
                score=scores.get(tid, (0, "none"))[0] * _decay(c.age_days),
            )

        hero = build(data["hero"])
        if hero is None:
            return fallback()
        on_deck = []
        for raw in (data.get("on_deck") or [])[:3]:
            if isinstance(raw, dict):
                item = build(raw)
                if item and item.thread_id != hero.thread_id:
                    on_deck.append(item)
        return Ranking(hero=hero, on_deck=on_deck)

    def tidy_targets(self, threads: list["Thread"], memory_ctx: str = "") -> list[str]:
        """Thread ids that are disposable noise, safe to archive unread. Empty on
        any failure — tidy must never guess threads into the bin."""
        candidates = _curate_candidates(threads)
        if not candidates:
            return []
        lines = "\n".join(
            f"{c.thread_id} | {c.sender} | {c.subject} | {c.snippet}" for c in candidates
        )
        try:
            resp = self._client.messages.create(
                model=self._fast, max_tokens=800,
                system=_TIDY_SYSTEM.format(memory=memory_ctx or "(nothing yet)"),
                messages=[{"role": "user", "content": lines}],
            )
            data = _extract_json(resp.content[0].text if resp.content else "")
        except Exception:
            data = None
        valid = {c.thread_id for c in candidates}
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if str(x) in valid]

    def chat(
        self,
        messages: list[dict],
        executor: "CopilotExecutor",
        emit: Callable[[str], None],
        is_cancelled: Callable[[], bool],
        context: str = "",
        max_turns: int = 8,
    ) -> str:
        """Sonnet conversation, run as a Strands agent with tools. Emits each
        assistant line to the feed as it lands; the Strands event loop drives
        tool use, calling read tools against Gmail and action/UI tools via the
        executor. `context` is the numbered feed + open thread so Mutt can
        resolve 'archive 4' / 'the invoice'. Returns the final reply.

        The last entry in `messages` is Ben's new turn (the prompt); anything
        before it is prior conversation seeded into the agent's history."""
        from strands import Agent
        from strands.models.anthropic import AnthropicModel
        from strands.types.agent import Limits

        system = _CHAT_SYSTEM.format(rules=executor.rules, context=context or "(inbox quiet)")
        prompt = messages[-1]["content"] if messages else ""
        history = _to_strands_messages(messages[:-1])

        state = {"last": ""}
        agent: Optional[Agent] = None

        def on_event(**kw) -> None:
            # Emit each *complete* assistant text block (not token deltas) so the
            # panel shows whole lines, the way the old loop did per content block.
            msg = kw.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for block in msg.get("content", []):
                    text = (block.get("text") or "").strip() if isinstance(block, dict) else ""
                    if text:
                        state["last"] = text
                        emit(text)
            # Cooperative cancel: Ben hit a key / switched Mutt off mid-think.
            if agent is not None and is_cancelled():
                agent.cancel()

        if is_cancelled():
            return ""
        agent = Agent(
            model=AnthropicModel(
                client_args={"api_key": self._api_key},
                model_id=self._smart, max_tokens=2048,
            ),
            system_prompt=system,
            tools=build_copilot_tools(executor),
            messages=history,
            callback_handler=on_event,
        )
        try:
            agent(prompt, limits=Limits(turns=max_turns))
        except Exception as e:
            emit(f"(Mutt tripped over a wire: {e})")
        return state["last"]


_CHAT_SYSTEM = """You are Mutt, Ben's loyal inbox dog in the bem terminal email \
client. You chat with Ben in the side panel — warm, brief, a little playful, and \
sharp about email. Plain text, short lines.

Ben refers to emails by their [n] number in the feed, by sender, or by subject. \
Work out which thread he means from the NUMBERED FEED and INBOX below and act on \
that thread's id. If it is genuinely ambiguous (e.g. two from the same person), \
ask a short clarifying question rather than guessing.

Your tools:
- archive_thread / trash_thread / file_thread — act on ONE specific thread by id.
  These are how you do what Ben asks. file_thread needs a label.
- undo_last — reverse your most recent archive/file/trash.
- open_thread (show him an email), check_calendar (his diary),
  search_threads / get_thread (read), run_command (other bem commands like
  ':summarise'; it tells you if the command wasn't recognised).
- DRIVE the screen so Ben can watch you work: change_folder to switch which
  folder is showing (e.g. 'Inbox', 'Sent', 'Finance' — see FOLDERS below),
  move_cursor (down/up/top/bottom) to scroll the inbox selection, scroll_preview
  (down/up) to read an open email, expand_thread to unfold a conversation.
  Narrate briefly as you move ("opening Finance…", "scrolling down…") so it
  reads like an autopilot.

Acting: Ben asking IS his consent. When he tells you to archive / file / trash, \
do it with the tool, then report in one short line and mention undo, e.g. \
"Archived Miro + invoice — say 'undo' to restore." Act ONLY on what he asked \
for; never touch anything else. To reply, draft it right here in his voice \
(warm, concise, Australian; sign off 'Cheers, Ben') and offer to open the editor \
with run_command ':rd'. Sends always become drafts (safe mode) — never claim you \
sent anything.

When unsure what he means, ask. When he asks a question, just answer it.

Ben's standing rules:
{rules}

{context}"""


def _to_strands_messages(turns: list[dict]) -> list[dict]:
    """Convert bem's stored chat history ({'role', 'content': str}) into the
    content-block form Strands seeds an agent's conversation with."""
    out = []
    for t in turns:
        content = t.get("content", "")
        blocks = content if isinstance(content, list) else [{"text": str(content)}]
        out.append({"role": t.get("role", "user"), "content": blocks})
    return out


def build_copilot_tools(executor: "CopilotExecutor") -> list:
    """Mutt's chat tools as Strands @tool functions. Each is a thin wrapper that
    delegates to the executor (read tools hit Gmail; action/UI tools run on the
    main thread). The executor's (text, is_error) result is mapped to a Strands
    tool result so the model still sees failures as errors."""
    from strands import tool

    def run(name: str, **args) -> object:
        out, is_err = executor.execute(name, args)
        if is_err:
            return {"status": "error", "content": [{"text": out}]}
        return out

    @tool
    def search_threads(query: str) -> object:
        """Search Gmail (Gmail query syntax). One line per thread: id, date, sender, subject, snippet."""
        return run("search_threads", query=query)

    @tool
    def get_thread(thread_id: str) -> object:
        """Fetch one thread in full, including message bodies."""
        return run("get_thread", thread_id=thread_id)

    @tool
    def check_calendar(thread_id: str) -> object:
        """For an invite thread, report Ben's live response (accepted/declined/etc.) and any conflicts."""
        return run("check_calendar", thread_id=thread_id)

    @tool
    def open_thread(thread_id: str) -> object:
        """Open a thread in Ben's list + preview so he can see it."""
        return run("open_thread", thread_id=thread_id)

    @tool
    def change_folder(folder: str) -> object:
        """Switch the visible folder (e.g. 'Inbox', 'Sent', 'Finance') so Ben sees that folder's mail. Use a name from the FOLDERS list."""
        return run("change_folder", folder=folder)

    @tool
    def move_cursor(direction: str) -> object:
        """Drive the inbox selection so Ben watches it move. direction is one of 'down', 'up', 'top', 'bottom'."""
        return run("move_cursor", direction=direction)

    @tool
    def scroll_preview(direction: str) -> object:
        """Scroll the currently open email in the preview pane. direction is 'down' or 'up'."""
        return run("scroll_preview", direction=direction)

    @tool
    def expand_thread() -> object:
        """Expand or collapse the highlighted thread in the list."""
        return run("expand_thread")

    @tool
    def archive_thread(thread_id: str) -> object:
        """Archive a specific thread out of the inbox (Ben must have asked). Reversible with undo_last."""
        return run("archive_thread", thread_id=thread_id)

    @tool
    def trash_thread(thread_id: str) -> object:
        """Move a specific thread to Trash (Ben must have asked). Reversible with undo_last."""
        return run("trash_thread", thread_id=thread_id)

    @tool
    def file_thread(thread_id: str, label: str) -> object:
        """File a specific thread under a label and archive it (Ben must have asked)."""
        return run("file_thread", thread_id=thread_id, label=label)

    @tool
    def undo_last() -> object:
        """Reverse your most recent archive/file/trash."""
        return run("undo_last")

    @tool
    def run_command(command: str) -> object:
        """Fire a non-mutating bem command (e.g. ':summarise', ':rd friendly'). Returns whether it was recognised."""
        return run("run_command", command=command)

    return [
        search_threads, get_thread, check_calendar, open_thread, change_folder,
        move_cursor, scroll_preview, expand_thread, archive_thread, trash_thread,
        file_thread, undo_last, run_command,
    ]


class CopilotExecutor:
    """Executes Mutt's chat tools. Read tools hit Gmail directly; action and UI
    tools are handed to a main-thread callback `ui_action(name, args) -> str`
    supplied by the inbox, whose return string is fed back to the model."""

    # Tools delegated to the inbox's main-thread handler.
    _UI_TOOLS = (
        "open_thread", "change_folder", "archive_thread", "trash_thread",
        "file_thread", "undo_last", "run_command", "move_cursor",
        "scroll_preview", "expand_thread",
    )
    # Movement tools get a short pause after each so Ben can watch the autopilot
    # move, rather than the screen jumping to its final state instantly.
    _PACED = ("open_thread", "change_folder", "move_cursor", "scroll_preview", "expand_thread")

    def __init__(
        self, gmail: "GmailClient", calendar, ui_action: Callable[[str, dict], str],
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
        if name in self._UI_TOOLS:
            try:
                out = self._ui(name, args) or "done"
            except Exception as e:
                return f"couldn't {name}: {e}", True
            if name in self._PACED:
                time.sleep(0.45)  # let Ben see the movement before the next step
            return out, False
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

