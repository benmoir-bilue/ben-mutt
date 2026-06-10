"""Full-height agent side panel, Claude Code style.

Docks on the right of the inbox. While the agent runs it streams annotated
activity (⏺ tool calls, ⎿ results, commentary) under an animated header;
when the agent finishes it renders the queued plan and waits for y/n.
"""
from __future__ import annotations

import time
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message as TMessage
from textual.widgets import Label, RichLog

from bem.ai.tools import PlanAction

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class AgentPanel(Vertical):
    """States: idle (hidden) → running → confirm/done → applying → done."""

    can_focus = True

    DEFAULT_CSS = """
    AgentPanel {
        display: none;
        width: 42%;
        min-width: 44;
        max-width: 90;
        border-left: heavy $accent;
        background: $surface;
        padding: 0;
    }
    #agent-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
        background: $surface;
    }
    #agent-log {
        height: 1fr;
        padding: 0 1;
        background: $surface;
    }
    #agent-footer {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    class PlanConfirmed(TMessage):
        pass

    class Dismissed(TMessage):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state: str = "idle"
        self.plan: list[PlanAction] = []
        self._title = ""
        self._frame = 0
        self._tool_count = 0
        self._started = 0.0

    def compose(self) -> ComposeResult:
        yield Label("", id="agent-title")
        yield RichLog(id="agent-log", wrap=True, markup=False, highlight=False)
        yield Label("", id="agent-footer")

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    # ── Lifecycle (called from the inbox screen / via call_from_thread) ────

    @property
    def is_busy(self) -> bool:
        return self.state in ("running", "applying")

    @property
    def _log(self) -> RichLog:
        return self.query_one("#agent-log", RichLog)

    def _footer(self, text: str) -> None:
        self.query_one("#agent-footer", Label).update(text)

    def begin(self, title: str) -> None:
        self.state = "running"
        self.plan = []
        self._title = title
        self._tool_count = 0
        self._started = time.monotonic()
        self.display = True
        self._log.clear()
        self._footer("Esc cancel   j/k scroll")
        self.focus()

    def _tick(self) -> None:
        if self.state not in ("running", "applying"):
            return
        self._frame = (self._frame + 1) % len(SPINNER)
        elapsed = int(time.monotonic() - self._started)
        verb = "Applying plan" if self.state == "applying" else self._title
        tools = f" · {self._tool_count} tools" if self._tool_count else ""
        self.query_one("#agent-title", Label).update(
            f"{SPINNER[self._frame]} {verb}… ({elapsed}s{tools})"
        )

    def agent_event(self, event: tuple) -> None:
        if not self.is_mounted or self.state != "running":
            return
        kind = event[0]
        if kind == "text":
            self._log.write(Text(""))
            self._log.write(Text(event[1]))
        elif kind == "tool":
            _, name, args = event
            self._tool_count += 1
            line = Text.assemble(
                ("⏺ ", "green"),
                (name, "bold"),
                ((f"({args})" if args else ""), "dim"),
            )
            self._log.write(line)
        elif kind == "tool_result":
            self._log.write(Text(f"  ⎿ {event[1]}", style="dim"))
        elif kind == "error":
            self._log.write(Text(f"✗ {event[1]}", style="bold red"))

    def show_result(self, summary: str, plan: list[PlanAction]) -> None:
        if not self.is_mounted:
            return
        self.plan = plan
        log = self._log
        if plan:
            log.write(Text(""))
            log.write(Text(f"Plan — {len(plan)} action{'s' if len(plan) != 1 else ''}",
                           style="bold underline"))
            for i, action in enumerate(plan, 1):
                if action.kind == "file":
                    line = Text.assemble(
                        (f"{i:>3} ", "dim"),
                        ("file    ", "green"),
                        (_clip(action.subject, 42)),
                        (" → ", "dim"),
                        (action.label_name, "bold cyan"),
                    )
                else:
                    line = Text.assemble(
                        (f"{i:>3} ", "dim"),
                        ("archive ", "yellow"),
                        (_clip(action.subject, 48)),
                    )
                log.write(line)
                if action.reason:
                    log.write(Text(f"      {action.reason}", style="dim italic"))
            self.state = "confirm"
            self.query_one("#agent-title", Label).update(
                f"⏸ Plan ready — {len(plan)} actions"
            )
            self._footer("y apply   n/Esc discard   j/k scroll")
        else:
            self.state = "done"
            self.query_one("#agent-title", Label).update("✓ Done — nothing to apply")
            self._footer("Esc close")

    def begin_apply(self) -> None:
        self.state = "applying"
        self._started = time.monotonic()
        self._tool_count = 0
        self._footer("")

    def mark_applied(self, applied: int, errors: int) -> None:
        if not self.is_mounted:
            return
        self.state = "done"
        suffix = f", {errors} failed" if errors else ""
        self.query_one("#agent-title", Label).update(f"✓ Applied {applied} actions{suffix}")
        self._log.write(Text(""))
        style = "bold yellow" if errors else "bold green"
        self._log.write(Text(f"✓ {applied} applied{suffix}", style=style))
        self._footer("Esc close")

    def show_error(self, message: str) -> None:
        if not self.is_mounted:
            return
        self.state = "done"
        self.query_one("#agent-title", Label).update("✗ Agent failed")
        self._log.write(Text(f"✗ {message}", style="bold red"))
        self._footer("Esc close")

    def dismiss_panel(self) -> None:
        self.state = "idle"
        self.plan = []
        self.display = False

    # ── Keys ────────────────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("j", "down"):
            self._log.scroll_down(animate=False)
        elif key in ("k", "up"):
            self._log.scroll_up(animate=False)
        elif key == "space":
            self._log.scroll_page_down(animate=False)
        elif key == "b":
            self._log.scroll_page_up(animate=False)
        elif self.state == "confirm" and key == "y":
            self.begin_apply()
            self.post_message(self.PlanConfirmed())
        elif self.state == "confirm" and key in ("n", "escape"):
            self.post_message(self.Dismissed())
        elif key == "escape" or (self.state == "done" and key == "q"):
            self.post_message(self.Dismissed())
        else:
            return
        event.stop()


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
