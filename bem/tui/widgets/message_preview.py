from __future__ import annotations

import re
import webbrowser
from datetime import datetime, timezone

from rich.markup import escape as _e
from rich.markdown import Markdown
from rich.panel import Panel
from textual.binding import Binding
from textual.message import Message as TMessage
from textual.widgets import RichLog
from textual import events

from typing import TYPE_CHECKING

from bem.gmail.models import Thread, Message
from bem.calendar.client import (
    MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED,
    MARK_OUTOFSYNC, MARK_PENDING,
)

if TYPE_CHECKING:
    from bem.calendar import CalendarInvite


class MessagePreview(RichLog):
    DEFAULT_CSS = """
    MessagePreview {
        height: 1fr;
        background: $background;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("space", "scroll_down", "Page down", show=False),
        Binding("b", "scroll_up", "Page up", show=False),
    ]

    class NextThread(TMessage):
        pass

    class PrevThread(TMessage):
        pass

    class LinkFocused(TMessage):
        """Posted as the user arrows between links, so the screen can surface the
        target URL (markdown links hide it) in the status bar."""
        def __init__(self, url: str, index: int, total: int) -> None:
            super().__init__()
            self.url = url
            self.index = index
            self.total = total

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, wrap=True, highlight=False, **kwargs)
        self._thread: Thread | None = None
        self._message: Message | None = None          # single-message view
        self._links: list[str] = []                   # URLs, in document order
        self._sel: int = -1                            # selected link, -1 = none
        self._invite: "CalendarInvite | None" = None  # invite → banner
        self._invite_kind: str | None = None          # disposition mark
        self._invite_conflicts: list = []             # for pending invites

    def set_invite_banner(
        self, invite: "CalendarInvite | None", kind: str | None = None,
        conflicts: list | None = None,
    ) -> bool:
        """Set (or clear) the invite banner for the shown thread. Returns True if
        it changed, so the caller can decide whether a redraw is needed."""
        conflicts = conflicts or []
        if (invite is self._invite and kind == self._invite_kind
                and conflicts == self._invite_conflicts):
            return False
        self._invite = invite
        self._invite_kind = kind
        self._invite_conflicts = conflicts
        return True

    def show_thread(self, thread: Thread) -> None:
        self._thread = thread
        self._message = None
        self._sel = -1
        self._render()
        self.call_after_refresh(self.scroll_home, animate=False)

    def show_message(self, message: Message) -> None:
        self._thread = None
        self._message = message
        self._sel = -1
        self._render()
        self.call_after_refresh(self.scroll_home, animate=False)

    def _render(self) -> None:
        """(Re)draw the current thread/message. Rebuilds the link index, marking
        the selected link so arrow-key navigation has something to highlight."""
        self._links = []
        self.clear()
        self._write_invite_banner()
        if self._thread is not None:
            messages = self._thread.messages
        elif self._message is not None:
            messages = [self._message]
        else:
            messages = []
        for i, msg in enumerate(messages):
            if i > 0:
                self.write("─" * 72)
            _write_message(self, msg)

    def _write_invite_banner(self) -> None:
        if self._invite is None or self._invite_kind is None:
            return
        when = _fmt_when(self._invite.dtstart, self._invite.dtend)
        lines = _invite_banner_lines(self._invite_kind, when, self._invite_conflicts)
        if not lines:
            return
        color = _INVITE_BANNER_COLORS.get(self._invite_kind, "green")
        self.write(Panel("\n".join(lines), style=color, border_style=color,
                         expand=False, padding=(0, 1)))
        self.write("")

    def clear_preview(self) -> None:
        self._thread = None
        self._message = None
        self._links = []
        self._sel = -1
        self.clear()

    # ── Link navigation ──────────────────────────────────────────────────────

    def _move_link(self, delta: int) -> None:
        """Move the link selection by `delta`, wrapping, and redraw."""
        n = len(self._links)
        if n == 0:
            return
        if self._sel < 0:
            self._sel = 0 if delta > 0 else n - 1
        else:
            self._sel = (self._sel + delta) % n
        self._render()
        self.post_message(self.LinkFocused(self._links[self._sel], self._sel, n))
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        """Bring the highlighted link into view by finding its marker line."""
        for y, strip in enumerate(self.lines):
            if _MARK in strip.text:
                self.scroll_to(y=max(0, y - 2), animate=False)
                return

    def _open_selected(self) -> None:
        if not (0 <= self._sel < len(self._links)):
            return
        url = self._links[self._sel]
        try:
            webbrowser.open(url)
            self.app.notify(f"Opening {url}", timeout=3)
        except Exception as e:  # pragma: no cover - platform dependent
            self.app.notify(f"Couldn't open link: {e}", severity="error")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("down", "right") and self._links:
            self._move_link(1)
            event.prevent_default()
            event.stop()
        elif event.key in ("up", "left") and self._links:
            self._move_link(-1)
            event.prevent_default()
            event.stop()
        elif event.key == "enter" and self._links:
            if self._sel < 0:
                self._move_link(1)
            else:
                self._open_selected()
            event.prevent_default()
            event.stop()
        elif event.key == "j":
            self.scroll_down()
            event.prevent_default()
        elif event.key == "k":
            self.scroll_up()
            event.prevent_default()
        elif event.key == "J":
            self.post_message(self.NextThread())
            event.prevent_default()
        elif event.key == "K":
            self.post_message(self.PrevThread())
            event.prevent_default()


_INVITE_BANNER_COLORS = {
    MARK_ACCEPTED: "green",
    MARK_TENTATIVE: "yellow",
    MARK_DECLINED: "grey50",
    MARK_CANCELLED: "red",
    MARK_OUTOFSYNC: "dark_orange",
    MARK_PENDING: "cyan",
}


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    try:
        d = dt.astimezone()
    except Exception:
        return ""
    return d.strftime("%a %d %b %Y %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _fmt_time(dt) -> str:
    if dt is None:
        return ""
    try:
        d = dt.astimezone()
    except Exception:
        return ""
    return d.strftime("%-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _fmt_when(start, end) -> str:
    s = _fmt_dt(start)
    if not s:
        return ""
    e = _fmt_time(end)
    return f"{s}–{e}" if e else s


def _invite_banner_lines(kind: str, when: str, conflicts: list) -> list[str]:
    """The text lines for an invite banner, by disposition mark."""
    safe = f"{when} — safe to delete" if when else "Safe to delete"
    if kind == MARK_ACCEPTED:
        return ["✓ Invite accepted on your calendar", safe]
    if kind == MARK_TENTATIVE:
        return ["~ Tentatively accepted (maybe)", safe]
    if kind == MARK_DECLINED:
        return ["⊘ You declined this invite", safe]
    if kind == MARK_CANCELLED:
        return ["✗ Event cancelled — removed from calendar", safe]
    if kind == MARK_OUTOFSYNC:
        return [
            "⚠ Not in sync — the event was cancelled or removed",
            "from your calendar, but the invite is still here.",
        ]
    if kind == MARK_PENDING:
        lines = ["◷ Invitation · awaiting your response"]
        if when:
            lines.append(when)
        if conflicts:
            lines.append("⚠ Conflicts with:")
            for c in conflicts[:4]:
                span = f" {_fmt_time(c.start)}–{_fmt_time(c.end)}" if c.start else ""
                lines.append(f"   • {c.summary}{span}")
        else:
            lines.append("✓ No conflicts — you're free")
        lines.append("A accept · M maybe · X decline")
        return lines
    return []


# Prefix stamped on the selected link in both Markdown and plain bodies. Doubles
# as the sentinel _scroll_to_selected() scans the rendered lines for.
_MARK = "➤ "

# One pass finds either a Markdown link [text](url) or a bare http(s) URL.
_SCAN = re.compile(
    r"\[(?P<text>[^\]]+)\]\((?P<mdurl>https?://[^)\s]+)\)"
    r"|(?P<bare>https?://[^\s<>)\]\"']+)"
)


def _split_trailing(url: str) -> tuple[str, str]:
    """Peel trailing punctuation off a bare URL (so 'see https://x.com.' doesn't
    swallow the full stop). Returns (clean_url, trailing_text)."""
    i = len(url)
    while i and url[i - 1] in ".,;:!?'\"":
        i -= 1
    if i and url[i - 1] == ")" and "(" not in url[:i]:
        i -= 1
    return url[:i], url[i:]


def _write_message(preview: "MessagePreview", msg: Message) -> None:
    dt_str = ""
    if msg.date:
        try:
            dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            dt_str = dt.astimezone().strftime("%a %d %b %Y %H:%M")
        except Exception:
            pass

    preview.write(f"[bold]From:[/bold]    {_e(msg.from_name)} <{_e(msg.from_address)}>")
    if msg.to:
        preview.write(f"[bold]To:[/bold]      {_e(', '.join(msg.to))}")
    if msg.cc:
        preview.write(f"[bold]Cc:[/bold]      {_e(', '.join(msg.cc))}")
    preview.write(f"[bold]Date:[/bold]    {dt_str}")
    preview.write(f"[bold]Subject:[/bold] {_e(msg.subject)}")
    if msg.attachments:
        names = ", ".join(a.filename for a in msg.attachments)
        preview.write(f"[bold]Attach:[/bold]  [yellow]{_e(names)}[/yellow]")
    preview.write("")

    if msg.body_is_html:
        # HTML emails arrive as Markdown — render it so headings, bold, lists,
        # links and quotes display formatted instead of as raw syntax. Rewrite
        # the link syntax first to register each URL and mark the selected one.
        preview.write(Markdown(_mark_markdown(msg.body, preview)))
        return

    for line in msg.body.splitlines():
        if not line:
            preview.write("")
            continue
        marked = _mark_plain(line, preview)
        preview.write(f"[dim]{marked}[/dim]" if line.startswith(">") else marked)


def _mark_markdown(body: str, preview: "MessagePreview") -> str:
    """Register every link in `body` (in order) on the preview, normalise bare
    URLs into Markdown links so they're navigable too, and stamp the selected
    one with the marker + bold."""
    def repl(m: re.Match) -> str:
        trail = ""
        if m.group("mdurl"):
            url, text = m.group("mdurl"), m.group("text")
        else:
            url, trail = _split_trailing(m.group("bare"))
            text = url
        idx = len(preview._links)
        preview._links.append(url)
        if idx == preview._sel:
            return f"[{_MARK}**{text}**]({url}){trail}"
        return f"[{text}]({url}){trail}"

    return _SCAN.sub(repl, body)


def _mark_plain(line: str, preview: "MessagePreview") -> str:
    """Build Rich markup for one plain-text line, styling URLs as links and
    highlighting the selected one."""
    out: list[str] = []
    pos = 0
    for m in _SCAN.finditer(line):
        trail = ""
        if m.group("mdurl"):
            url, disp = m.group("mdurl"), m.group("text")
        else:
            url, trail = _split_trailing(m.group("bare"))
            disp = url
        out.append(_e(line[pos:m.start()]))
        idx = len(preview._links)
        preview._links.append(url)
        if idx == preview._sel:
            out.append(f"[b reverse]{_MARK}{_e(disp)}[/]")
        else:
            out.append(f"[u #5fafff]{_e(disp)}[/]")
        out.append(_e(trail))
        pos = m.end()
    out.append(_e(line[pos:]))
    return "".join(out)
