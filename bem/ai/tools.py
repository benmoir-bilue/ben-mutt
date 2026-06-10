"""Tool surface for the email agent.

Read-only tools execute immediately against Gmail. Mutating tools are
DEFERRED: calling one records a PlanAction and returns "queued" — nothing
touches the mailbox until the user approves the plan in the agent panel.
A confused or buggy agent run therefore costs nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bem.gmail import GmailClient
    from bem.gmail.models import Thread

MAX_SEARCH_RESULTS = 50
MAX_BODY_CHARS = 2500

AGENT_TOOLS: list[dict] = [
    {
        "name": "list_labels",
        "description": (
            "List the user's Gmail labels (folders) with message counts. "
            "Call this first when filing email, to learn the user's existing "
            "folder taxonomy before proposing where anything goes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "search_threads",
        "description": (
            "Search Gmail threads using Gmail query syntax (e.g. 'in:inbox', "
            "'from:xero.com', 'label:Finance newer_than:30d'). Returns one "
            "line per thread: thread_id, date, sender, subject, snippet. "
            "Call this to see what is in the inbox, or to sample a label and "
            "learn what the user files there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Max threads to return (default 25, max 50)",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_thread",
        "description": (
            "Fetch one thread in full, including message bodies. Use when a "
            "subject/snippet is not enough to decide how to handle a thread."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "file_thread",
        "description": (
            "Queue a plan action: move a thread into a label (folder) and "
            "archive it out of the inbox. Deferred — the user reviews and "
            "approves the full plan before anything is applied. Prefer the "
            "user's existing labels; only propose a new label name when "
            "nothing fits, and reuse the same new name for similar threads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "label_name": {
                    "type": "string",
                    "description": "Exact existing label name, or a proposed new one",
                },
                "reason": {
                    "type": "string",
                    "description": "Very short justification, under 8 words",
                },
            },
            "required": ["thread_id", "label_name", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_thread",
        "description": (
            "Queue a plan action: archive a thread out of the inbox without "
            "filing it under a label (for noise that needs no folder). "
            "Deferred until the user approves the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Very short justification, under 8 words",
                },
            },
            "required": ["thread_id", "reason"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class PlanAction:
    kind: str  # "file" | "archive"
    thread_id: str
    subject: str
    sender: str
    label_name: str = ""
    reason: str = ""

    @property
    def description(self) -> str:
        target = f"→ {self.label_name}" if self.kind == "file" else "→ archive"
        return f"{target}  {self.subject}"


class ToolExecutor:
    """Executes agent tool calls. Read-only tools hit Gmail; mutating tools
    accumulate into self.plan for later user approval."""

    def __init__(self, gmail: "GmailClient", threads: list["Thread"] | None = None):
        self.gmail = gmail
        self.plan: list[PlanAction] = []
        self._known: dict[str, "Thread"] = {t.id: t for t in (threads or [])}

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool call. Returns (result_text, is_error)."""
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"Unknown tool: {name}", True
            return handler(**args)
        except TypeError as e:
            return f"Bad arguments for {name}: {e}", True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True

    # ── Read-only ──────────────────────────────────────────────────────────

    def _tool_list_labels(self) -> tuple[str, bool]:
        labels = self.gmail.list_labels()
        lines = []
        for l in labels:
            kind = "system" if l.type == "system" else "user"
            lines.append(
                f"{l.name}  [{kind}, {l.messages_total} msgs, {l.messages_unread} unread]"
            )
        return "\n".join(lines) or "(no labels)", False

    def _tool_search_threads(self, query: str, max_results: int = 25) -> tuple[str, bool]:
        max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        threads, _ = self.gmail.list_threads(
            label_id="", query=query, max_results=max_results
        )
        for t in threads:
            self._known[t.id] = t
        lines = []
        for t in threads:
            date = t.date.strftime("%d %b") if t.date else "?"
            snippet = t.snippet[:80]
            lines.append(f"{t.id} | {date} | {t.sender} | {t.subject} | {snippet}")
        return "\n".join(lines) or "(no results)", False

    def _tool_get_thread(self, thread_id: str) -> tuple[str, bool]:
        thread = self.gmail.get_thread(thread_id)
        if thread is None:
            return f"Thread not found: {thread_id}", True
        self._known[thread.id] = thread
        parts = [f"Subject: {thread.subject}"]
        for msg in thread.messages:
            date = msg.date.strftime("%d %b %H:%M") if msg.date else ""
            parts.append(f"--- {msg.display_from} ({date}) ---")
            parts.append(msg.body[:MAX_BODY_CHARS])
        return "\n".join(parts), False

    # ── Deferred mutations ─────────────────────────────────────────────────

    def _describe(self, thread_id: str) -> tuple[str, str] | None:
        thread = self._known.get(thread_id)
        if thread is None:
            return None
        return thread.subject, thread.sender

    def _tool_file_thread(
        self, thread_id: str, label_name: str, reason: str = ""
    ) -> tuple[str, bool]:
        info = self._describe(thread_id)
        if info is None:
            return (
                f"Unknown thread_id {thread_id} — only file threads you have "
                "seen via search_threads or get_thread.",
                True,
            )
        if not label_name.strip():
            return "label_name must not be empty", True
        # Replace any earlier queued action for the same thread
        self.plan = [a for a in self.plan if a.thread_id != thread_id]
        self.plan.append(PlanAction(
            kind="file", thread_id=thread_id, subject=info[0], sender=info[1],
            label_name=label_name.strip(), reason=reason.strip(),
        ))
        return f"queued: file '{info[0]}' under {label_name}", False

    def _tool_archive_thread(self, thread_id: str, reason: str = "") -> tuple[str, bool]:
        info = self._describe(thread_id)
        if info is None:
            return (
                f"Unknown thread_id {thread_id} — only archive threads you "
                "have seen via search_threads or get_thread.",
                True,
            )
        self.plan = [a for a in self.plan if a.thread_id != thread_id]
        self.plan.append(PlanAction(
            kind="archive", thread_id=thread_id, subject=info[0], sender=info[1],
            reason=reason.strip(),
        ))
        return f"queued: archive '{info[0]}'", False
