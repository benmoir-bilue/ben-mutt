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
from textual.widgets import Input, Label, RichLog, Static

from bem.ai import copilot

# A mood emoji sits next to "Mutt" to show what he's up to. No animation — it
# just changes with his state (and drifts through the idle moods so he has a bit
# of personality when the inbox is quiet).
SNIFF_MOODS = ["🐽", "👃", "🔍", "🕵️", "🤔"]            # actively working a pass
AWAY_MOODS = ["😴", "💤"]                              # you're gone — napping on the job
IDLE_MOODS = [
    "👀",   # watching the inbox
    "🦴",   # gnawing a bone (all quiet)
    "🥱",   # bored
    "🐿️",   # distracted by a squirrel
    "🎾",   # wants to play
    "🧦",   # nicked a sock
    "👅",   # panting happily
    "🍖",   # snack break
    "🐾",   # pacing
    "💭",   # daydreaming
]
NEW_MAIL_MOOD = "👂"   # ears perked — fresh mail just landed

# Rotate idle moods every this-many ticks (ticks are 0.25s → ~10s per mood).
IDLE_MOOD_TICKS = 40


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
    #copilot-hero  { height: auto; padding: 0 1; border-bottom: dashed $accent; }
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
        # Heartbeat: proof Mutt is alive and watching, shown in the idle title.
        self._present = True
        self._new_count = 0
        self._last_sniff: float | None = None
        self._next_sniff_at: float | None = None

    def compose(self) -> ComposeResult:
        yield Label("🐕 Mutt — off", id="copilot-title")
        yield Static("", id="copilot-hero")   # pinned hero + on-deck, never scrolls
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
        self._new_count = 0
        self._last_sniff = time.monotonic()
        self.query_one("#copilot-hero", Static).update(
            Text("finding your top priority… 🐽", style="dim")
        )
        self._feed.write(Text("🐕 Mutt is on watch. I'll bark when something matters.",
                              style="bold $accent"))
        self._feed.write(Text(
            "Tell me what to do — e.g. \"archive 4 and the invoice\", "
            "\"reply to Marie\", \"delete that\". Press t to talk, Esc to leave.",
            style="dim",
        ))
        self._feed.write(Text(
            "New? Try: \"show me how you can control the TUI\".", style="dim italic",
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

    def mark_sniff(self, new_count: int, present: bool, next_in: float) -> None:
        """Record a completed poll so the heartbeat shows fresh activity: bumps
        the 'new' tally, resets the 'sniffed Ns ago' clock, and the countdown to
        the next sniff."""
        self._new_count += max(0, new_count)
        self._present = present
        now = time.monotonic()
        self._last_sniff = now
        self._next_sniff_at = now + max(0.0, next_in)

    def set_present(self, present: bool) -> None:
        self._present = present

    def _tick(self) -> None:
        if self.state == "off":
            return
        self._frame += 1
        self.query_one("#copilot-title", Label).update(self._render_title())

    def _render_title(self) -> str:
        """🐕 Mutt {mood}  {caption} — a mood emoji for what he's doing, plus a
        live caption (countdown ticks so it never looks frozen)."""
        if self.state == "thinking":
            word = copilot.status_word(self._word_i)
            elapsed = int(time.monotonic() - self._started)
            caption = f"{word} the inbox… ({elapsed}s)"
        else:
            caption = self._heartbeat_suffix()
        return f"🐕 Mutt {self._mood()}  {caption}"

    def _mood(self) -> str:
        """The emoji that says what Mutt's up to right now."""
        if self.state == "thinking":
            return SNIFF_MOODS[self._word_i % len(SNIFF_MOODS)]
        if not self._present:
            return AWAY_MOODS[(self._frame // 8) % len(AWAY_MOODS)]
        return IDLE_MOODS[(self._frame // IDLE_MOOD_TICKS) % len(IDLE_MOODS)]

    def _heartbeat_suffix(self) -> str:
        bits = ["watching" if self._present else "away"]
        if self._new_count:
            bits.append(f"{self._new_count} new")
        if self._last_sniff is not None:
            bits.append(f"sniffed {int(time.monotonic() - self._last_sniff)}s ago")
        if not self._present:
            bits.append("back soon? I'll brief you")
        elif self._next_sniff_at is not None:
            bits.append(f"next {max(0, int(self._next_sniff_at - time.monotonic()))}s")
        return " · ".join(bits)

    def _set_title(self, suffix: str = "watching") -> None:
        self.query_one("#copilot-title", Label).update(self._render_title())

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

    def set_ranking(self, ranking) -> None:
        """Update the pinned hero card (one hero + on-deck). Stays put above the
        chat feed instead of scrolling away."""
        self.query_one("#copilot-hero", Static).update(self._hero_renderable(ranking))

    @staticmethod
    def _hero_renderable(ranking) -> Text:
        t = Text()
        if ranking is None or ranking.hero is None:
            t.append("nothing urgent — inbox looks calm 🦴", style="dim")
            return t
        h = ranking.hero
        t.append("▶ DO THIS\n", style="bold cyan")
        t.append(f"  {h.headline}\n", style="bold")
        if h.subject:
            t.append(f"  ✉ {h.subject}\n", style="dim")
        if h.why:
            t.append(f"  why: {h.why}\n", style="dim italic")
        if h.action != "none":
            t.append("  ↳ ", style="dim")
            t.append(h.action, style="bold cyan")
            if h.hint:
                t.append(f" — {h.hint}", style="dim")
            t.append("\n")
        if ranking.on_deck:
            t.append("on deck\n", style="dim")
            for i, item in enumerate(ranking.on_deck, start=2):
                t.append(f"  {i}. ", style="cyan")
                t.append(item.headline)
                if item.action != "none":
                    t.append(f"  ({item.action})", style="dim")
                t.append("\n")
        t.rstrip()   # in place — drops the trailing newline
        return t

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
