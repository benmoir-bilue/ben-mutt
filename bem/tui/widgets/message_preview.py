from __future__ import annotations

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

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=True, wrap=True, highlight=False, **kwargs)
        self._thread: Thread | None = None
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
        self.clear()
        self._write_invite_banner()
        for i, msg in enumerate(thread.messages):
            if i > 0:
                self.write("─" * 72)
            _write_message(self, msg)
        self.call_after_refresh(self.scroll_home, animate=False)

    def show_message(self, message: Message) -> None:
        self.clear()
        self._write_invite_banner()
        _write_message(self, message)
        self.call_after_refresh(self.scroll_home, animate=False)

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
        self.clear()

    def on_key(self, event: events.Key) -> None:
        if event.key == "j":
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


def _write_message(log: RichLog, msg: Message) -> None:
    dt_str = ""
    if msg.date:
        try:
            dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
            dt_str = dt.astimezone().strftime("%a %d %b %Y %H:%M")
        except Exception:
            pass

    log.write(f"[bold]From:[/bold]    {_e(msg.from_name)} <{_e(msg.from_address)}>")
    if msg.to:
        log.write(f"[bold]To:[/bold]      {_e(', '.join(msg.to))}")
    if msg.cc:
        log.write(f"[bold]Cc:[/bold]      {_e(', '.join(msg.cc))}")
    log.write(f"[bold]Date:[/bold]    {dt_str}")
    log.write(f"[bold]Subject:[/bold] {_e(msg.subject)}")
    if msg.attachments:
        names = ", ".join(a.filename for a in msg.attachments)
        log.write(f"[bold]Attach:[/bold]  [yellow]{_e(names)}[/yellow]")
    log.write("")

    if msg.body_is_html:
        # HTML emails arrive as Markdown — render it so headings, bold, lists,
        # links and quotes display formatted instead of as raw syntax.
        log.write(Markdown(msg.body))
        return

    for line in msg.body.splitlines():
        if line.startswith(">"):
            log.write(f"[dim]{_e(line)}[/dim]")
        else:
            log.write(_e(line) if line else "")
