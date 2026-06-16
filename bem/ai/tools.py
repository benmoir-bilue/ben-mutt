"""Tool surface for the email agent.

Read-only tools execute immediately against Gmail. Mutating tools are
DEFERRED: calling one records a PlanAction and returns "queued" — nothing
touches the mailbox until the user approves the plan in the agent panel.
A confused or buggy agent run therefore costs nothing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bem.gmail import GmailClient
    from bem.gmail.models import Thread

MAX_SEARCH_RESULTS = 50
MAX_BODY_CHARS = 2500
TRANSIENT_RETRIES = 2
RETRY_BASE_DELAY = 1.0  # seconds; grows linearly per attempt

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
    {
        "name": "save_folder_tips",
        "description": (
            "Save the folder-tips knowledge file: per-folder notes on the "
            "people, companies and topics found there. Future sorting runs "
            "read this file instead of re-scanning every folder. Writes a "
            "local file immediately — nothing touches the mailbox. Call it "
            "ONCE, with notes for every folder, as markdown sections:\n"
            "## <Label name>\n"
            "- people: <names / addresses seen here>\n"
            "- companies: <senders, domains>\n"
            "- topics: <what material is discussed>"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tips": {
                    "type": "string",
                    "description": "The complete tips document, one section per folder",
                },
            },
            "required": ["tips"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_reply",
        "description": (
            "Queue a reply draft for a thread. You MUST call get_thread on "
            "the thread first and read it in full. Write only the reply body "
            "(greeting, content, sign-off) in the user's own voice — study "
            "their tone first via their sent mail. No subject line, no "
            "quoted text. The user reviews every draft before anything is "
            "sent, and can edit or skip it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "body": {
                    "type": "string",
                    "description": "The reply body, written as the user would write it",
                },
                "reason": {
                    "type": "string",
                    "description": "Very short justification, under 8 words",
                },
            },
            "required": ["thread_id", "body", "reason"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class PlanAction:
    kind: str  # "file" | "archive" | "reply"
    thread_id: str
    subject: str
    sender: str
    label_name: str = ""
    reason: str = ""
    body: str = ""  # reply actions: the drafted reply text
    thread: "Thread | None" = None  # reply actions: full thread, for send headers

    @property
    def description(self) -> str:
        if self.kind == "file":
            return f"→ {self.label_name}  {self.subject}"
        if self.kind == "reply":
            return f"→ reply  {self.subject}"
        return f"→ archive  {self.subject}"


class ToolExecutor:
    """Executes agent tool calls. Read-only tools hit Gmail; mutating tools
    accumulate into self.plan for later user approval."""

    def __init__(self, gmail: "GmailClient", threads: list["Thread"] | None = None):
        self.gmail = gmail
        self.plan: list[PlanAction] = []
        self._known: dict[str, "Thread"] = {t.id: t for t in (threads or [])}
        self._full: set[str] = set()  # thread ids fetched in full via get_thread

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool call. Returns (result_text, is_error).

        Transient network failures (socket errors, rate limiting) are retried
        with a short backoff rather than burning an agent turn on the error.
        """
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"Unknown tool: {name}", True
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                return handler(**args)
            except TypeError as e:
                return f"Bad arguments for {name}: {e}", True
            except Exception as e:
                if attempt < TRANSIENT_RETRIES and _is_transient(e):
                    time.sleep(RETRY_BASE_DELAY * (attempt + 1))
                    continue
                return f"{type(e).__name__}: {e}", True
        return f"{name} failed after retries", True  # unreachable

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
        self._full.add(thread.id)
        parts = [f"Subject: {thread.subject}"]
        for msg in thread.messages:
            date = msg.date.strftime("%d %b %H:%M") if msg.date else ""
            parts.append(f"--- {msg.display_from} ({date}) ---")
            parts.append(msg.body[:MAX_BODY_CHARS])
        return "\n".join(parts), False

    def _tool_save_folder_tips(self, tips: str) -> tuple[str, bool]:
        from bem.ai.tips import save_tips
        if not tips.strip():
            return "tips must not be empty", True
        save_tips(tips)
        return "folder tips saved", False

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
        # Replace any earlier queued move for the same thread (replies coexist)
        self._drop(thread_id, kinds=("file", "archive"))
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
        self._drop(thread_id, kinds=("file", "archive"))
        self.plan.append(PlanAction(
            kind="archive", thread_id=thread_id, subject=info[0], sender=info[1],
            reason=reason.strip(),
        ))
        return f"queued: archive '{info[0]}'", False

    def _tool_draft_reply(
        self, thread_id: str, body: str, reason: str = ""
    ) -> tuple[str, bool]:
        if thread_id not in self._full:
            return (
                f"Call get_thread on {thread_id} and read it in full before "
                "drafting a reply.",
                True,
            )
        if not body.strip():
            return "body must not be empty", True
        thread = self._known[thread_id]
        self._drop(thread_id, kinds=("reply",))
        self.plan.append(PlanAction(
            kind="reply", thread_id=thread_id, subject=thread.subject,
            sender=thread.sender, body=body.strip(), reason=reason.strip(),
            thread=thread,
        ))
        return f"queued: reply to '{thread.subject}'", False

    def _drop(self, thread_id: str, kinds: tuple[str, ...]) -> None:
        self.plan = [
            a for a in self.plan
            if not (a.thread_id == thread_id and a.kind in kinds)
        ]


def _is_transient(e: Exception) -> bool:
    """Errors worth retrying: socket-level failures (e.g. macOS EADDRNOTAVAIL
    under connection bursts) and Gmail rate-limit / server errors."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(e, HttpError):
            return getattr(e.resp, "status", 0) in (429, 500, 502, 503, 504)
    except ImportError:
        pass
    return isinstance(e, OSError)
