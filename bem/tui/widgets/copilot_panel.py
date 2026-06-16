"""Mutt's panel — a persistent right-hand feed + chat line.

Unlike AgentPanel (transient, one-shot plan runs), this stays docked while
copilot mode is on: a scrolling feed of triage notes and chat, an animated
"Sniffing…" status, and an Input at the bottom to talk back to Mutt.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message as TMessage
from textual.widgets import Input, Label, RichLog

from bem.ai import copilot

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_URGENCY_STYLE = {"high": "bold red", "normal": "", "low": "dim"}
_URGENCY_GLYPH = {"high": "🔴", "normal": "•", "low": "·"}


class CopilotPanel(Vertical):
    """States: off (hidden) → idle (watching) → thinking (animated)."""

    can_focus = False

    DEFAULT_CSS = """
    CopilotPanel {
        display: none;
        width: 42%;
        min-width: 46;
        max-width: 92;
        border-left: heavy $accent;
        background: $surface;
        padding: 0;
    }
    #copilot-title { height: 1; padding: 0 1; text-style: bold; color: $accent; }
    #copilot-feed  { height: 1fr; padding: 0 1; background: $surface; }
    #copilot-input { dock: bottom; border: round $accent; }
    """

    class ChatSubmitted(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = "off"
        self._frame = 0
        self._status = ""
        self._word_i = 0
        self._started = 0.0

    def compose(self) -> ComposeResult:
        yield Label("🐕 Mutt — off", id="copilot-title")
        yield RichLog(id="copilot-feed", wrap=True, markup=False, highlight=False)
        yield Input(placeholder="talk to Mutt…  (Esc to leave)", id="copilot-input")

    def on_mount(self) -> None:
        self.set_interval(0.25, self._tick)

    @property
    def _feed(self) -> RichLog:
        return self.query_one("#copilot-feed", RichLog)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self.state = "idle"
        self.display = True
        self._feed.write(Text("🐕 Mutt is on watch. I'll bark when something matters.",
                              style="bold $accent"))
        self._feed.write(Text(
            "Tell me what to do — e.g. \"archive 4 and the invoice\", "
            "\"reply to Marie\", \"delete that\". Press t to talk, Esc to leave.",
            style="dim",
        ))
        self._set_title()

    def stop(self) -> None:
        self.state = "off"
        self.display = False

    @property
    def is_on(self) -> bool:
        return self.state != "off"

    def begin_thinking(self, word_i: int = 0) -> None:
        self.state = "thinking"
        self._word_i = word_i
        self._started = time.monotonic()

    def end_thinking(self) -> None:
        if self.state == "thinking":
            self.state = "idle"
        self._set_title()

    def _tick(self) -> None:
        if self.state != "thinking":
            return
        self._frame = (self._frame + 1) % len(SPINNER)
        elapsed = int(time.monotonic() - self._started)
        word = copilot.status_word(self._word_i)
        self.query_one("#copilot-title", Label).update(
            f"🐕 {SPINNER[self._frame]} {word}… ({elapsed}s)"
        )

    def _set_title(self, suffix: str = "watching") -> None:
        self.query_one("#copilot-title", Label).update(f"🐕 Mutt — {suffix}")

    # ── Feed ────────────────────────────────────────────────────────────────

    def post_triage(self, note: "copilot.TriageNote", number: int) -> None:
        glyph = _URGENCY_GLYPH.get(note.urgency, "•")
        style = _URGENCY_STYLE.get(note.urgency, "")
        self._feed.write(Text(""))
        self._feed.write(Text.assemble(
            (f"[{number}] ", "bold cyan"),
            (f"{glyph} ", style or "dim"),
            (f"{_clip(note.sender, 22)}  ", "bold"),
            (note.summary, style),
        ))
        if note.action != "none":
            # A suggestion, not a keystroke — tell Mutt "archive {number}" to act.
            self._feed.write(Text.assemble(
                ("   ↳ suggest ", "dim"),
                (note.action, "bold cyan"),
                (f" — {note.reason}" if note.reason else "", "dim italic"),
            ))

    def post_mutt(self, text: str) -> None:
        self._feed.write(Text(""))
        for i, line in enumerate(text.splitlines() or [""]):
            prefix = "🐕 " if i == 0 else "   "
            self._feed.write(Text(f"{prefix}{line}", style="$accent" if i == 0 else ""))

    def post_user(self, text: str) -> None:
        self._feed.write(Text(""))
        self._feed.write(Text(f"› {text}", style="bold"))

    def post_note(self, text: str, style: str = "dim") -> None:
        self._feed.write(Text(text, style=style))

    def focus_input(self) -> None:
        if self.is_on:
            self.query_one("#copilot-input", Input).focus()

    # ── Chat input ────────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        event.stop()
        if not text:
            return
        self.post_user(text)
        self.post_message(self.ChatSubmitted(text))

    def on_key(self, event: events.Key) -> None:
        # Esc from the chat line hands focus back to the inbox.
        if event.key == "escape":
            inp = self.query_one("#copilot-input", Input)
            if inp.has_focus:
                self.screen.focus_next()
                event.stop()


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
