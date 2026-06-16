from __future__ import annotations

import base64
import email
import email.utils
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class TriageLevel(Enum):
    ACTION_NEEDED = "action"
    WAITING_REPLY = "waiting"
    FYI_LOW       = "fyi"
    CAN_ARCHIVE   = "archive"


# Single source of truth for triage colours — used for message-list rows and
# for the headings in the :triage panel, so the two always match.
TRIAGE_STYLES: dict[TriageLevel, str] = {
    TriageLevel.ACTION_NEEDED: "bold red",
    TriageLevel.WAITING_REPLY: "yellow",
    TriageLevel.FYI_LOW:       "dim",
    TriageLevel.CAN_ARCHIVE:   "dim italic",
}


@dataclass
class Label:
    id: str
    name: str
    type: str  # "system" | "user"
    messages_total: int = 0
    messages_unread: int = 0

    @property
    def display_name(self) -> str:
        # Shorten system label names
        _map = {
            "INBOX": "Inbox",
            "SENT": "Sent",
            "DRAFT": "Drafts",
            "TRASH": "Trash",
            "SPAM": "Spam",
            "STARRED": "Starred",
            "IMPORTANT": "Important",
            "UNREAD": "Unread",
        }
        return _map.get(self.name, self.name.replace("CATEGORY_", "").title())

    @property
    def sort_key(self) -> tuple:
        order = ["INBOX", "STARRED", "SENT", "DRAFT", "TRASH", "SPAM"]
        try:
            return (0, order.index(self.name), "")
        except ValueError:
            return (1, 0, self.display_name.lower())


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size: int
    attachment_id: str


@dataclass
class Message:
    id: str
    thread_id: str
    label_ids: list[str]
    snippet: str
    date: datetime
    subject: str
    from_name: str
    from_address: str
    to: list[str]
    cc: list[str]
    body_plain: str
    body_html: str
    message_id_header: str  # The Message-ID header for threading
    in_reply_to: str
    references: str
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.label_ids

    @property
    def is_starred(self) -> bool:
        return "STARRED" in self.label_ids

    @property
    def display_from(self) -> str:
        return self.from_name or self.from_address

    @property
    def body(self) -> str:
        if self.body_plain:
            return self.body_plain
        if self.body_html:
            return _html_to_text(self.body_html)
        return self.snippet

    @property
    def body_is_html(self) -> bool:
        """True when `body` is markdown converted from an HTML-only message —
        the preview renders these through Rich Markdown rather than as raw text."""
        return not self.body_plain and bool(self.body_html)

    @property
    def calendar_attachment(self) -> Optional["Attachment"]:
        """The calendar (.ics) part of a meeting invite, if any. Google sends it
        both as text/calendar and a duplicate application/ics — either works."""
        for att in self.attachments:
            mime = att.mime_type.lower()
            if mime in ("text/calendar", "application/ics") or \
                    att.filename.lower().endswith(".ics"):
                return att
        return None

    @property
    def is_invite(self) -> bool:
        return self.calendar_attachment is not None


@dataclass
class Thread:
    id: str
    snippet: str
    messages: list[Message] = field(default_factory=list)

    @property
    def subject(self) -> str:
        if self.messages:
            return self.messages[0].subject
        return "(no subject)"

    @property
    def is_unread(self) -> bool:
        return any(m.is_unread for m in self.messages)

    @property
    def sender(self) -> str:
        if not self.messages:
            return ""
        return self.messages[0].display_from

    @property
    def date(self) -> Optional[datetime]:
        if not self.messages:
            return None
        return self.messages[-1].date

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def last_message(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None


def parse_thread(raw: dict) -> Thread:
    messages = [parse_message(m) for m in raw.get("messages", [])]
    return Thread(
        id=raw["id"],
        snippet=raw.get("snippet", ""),
        messages=messages,
    )


def parse_message(raw: dict) -> Message:
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    date = _parse_date(headers.get("date", ""), raw.get("internalDate"))

    from_raw = headers.get("from", "")
    from_name, from_addr = email.utils.parseaddr(from_raw)

    to_raw = headers.get("to", "")
    to_list = [addr for _, addr in email.utils.getaddresses([to_raw])] if to_raw else []

    cc_raw = headers.get("cc", "")
    cc_list = [addr for _, addr in email.utils.getaddresses([cc_raw])] if cc_raw else []

    body_plain, body_html, attachments = _extract_body(payload)

    return Message(
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        label_ids=raw.get("labelIds", []),
        snippet=raw.get("snippet", ""),
        date=date,
        subject=headers.get("subject", "(no subject)"),
        from_name=from_name,
        from_address=from_addr,
        to=to_list,
        cc=cc_list,
        body_plain=body_plain,
        body_html=body_html,
        message_id_header=headers.get("message-id", ""),
        in_reply_to=headers.get("in-reply-to", ""),
        references=headers.get("references", ""),
        attachments=attachments,
    )


def _parse_date(date_header: str, internal_date: Optional[str]) -> datetime:
    """Parse a message date, always returning an aware UTC datetime."""
    date = None
    if date_header:
        try:
            date = email.utils.parsedate_to_datetime(date_header)
        except Exception:
            date = None
    if date is None:
        try:
            date = datetime.fromtimestamp(int(internal_date or 0) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            date = datetime.fromtimestamp(0, tz=timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date


def _decode_body(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> tuple[str, str, list[Attachment]]:
    plain = ""
    html_body = ""
    attachments = []

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    parts = payload.get("parts", [])

    if mime_type == "text/plain":
        plain = _decode_body(body.get("data", ""))
    elif mime_type == "text/html":
        html_body = _decode_body(body.get("data", ""))
    elif parts:
        for part in parts:
            p_mime = part.get("mimeType", "")
            p_body = part.get("body", {})
            p_parts = part.get("parts", [])
            filename = part.get("filename", "")

            if filename:
                # Any named part is an attachment — small ones arrive inline
                # with no attachmentId, and must not be mistaken for the body.
                attachments.append(Attachment(
                    filename=filename,
                    mime_type=p_mime,
                    size=p_body.get("size", 0),
                    attachment_id=p_body.get("attachmentId", ""),
                ))
            elif p_mime == "text/plain" and not plain:
                plain = _decode_body(p_body.get("data", ""))
            elif p_mime == "text/html" and not html_body:
                html_body = _decode_body(p_body.get("data", ""))
            elif p_mime.startswith("multipart/") and p_parts:
                sub_plain, sub_html, sub_att = _extract_body(part)
                plain = plain or sub_plain
                html_body = html_body or sub_html
                attachments.extend(sub_att)

    return plain, html_body, attachments


def _strip_html_chrome(h: str) -> str:
    """Drop the parts of an HTML email that carry no readable content but wreck
    conversion: MS Office conditional comments, <head> (title + CSS), inline
    <style>/<script>, and ordinary comments."""
    h = re.sub(r"<!--\[if[^\]]*\]>.*?<!\[endif\]-->", "", h, flags=re.S | re.I)
    h = re.sub(r"<head\b.*?</head>", "", h, flags=re.S | re.I)
    h = re.sub(r"<style\b.*?</style>", "", h, flags=re.S | re.I)
    h = re.sub(r"<script\b.*?</script>", "", h, flags=re.S | re.I)
    h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
    return h


def _collapse_blank_lines(text: str) -> str:
    """Trim trailing whitespace and squeeze runs of blank (incl. space-only)
    lines down to one — layout tables emit hundreds of space-only lines."""
    out: list[str] = []
    blank = False
    for line in text.splitlines():
        line = line.rstrip()
        if line:
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


_QUOTE_BOUNDARY = re.compile(
    r"^\s*(On .+ wrote:|From:\s|-+\s*Original Message|________+|"
    r"This e-mail, and any attachment, is confidential)",
    re.IGNORECASE,
)


def dequote_reply(body: str) -> str:
    """Return just the author's own text from a reply — everything above the
    quoted history. Used to harvest writing-voice samples from the Sent folder."""
    out: list[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith(">") or _QUOTE_BOUNDARY.match(line):
            break
        out.append(line)
    return "\n".join(out).strip()


def _html_to_text(h: str) -> str:
    """Convert an HTML email body to clean Markdown via html2text, falling back
    to the regex stripper if conversion blows up. Layout chrome is stripped
    first; the result is rendered as Markdown in the preview pane."""
    h = _strip_html_chrome(h)
    try:
        import html2text
        conv = html2text.HTML2Text()
        conv.body_width = 0          # no hard wrapping — the preview pane wraps
        conv.ignore_images = True    # tracking pixels and logos are just noise
        conv.ignore_tables = True    # flatten layout tables to their cell text
        conv.unicode_snob = True     # real unicode instead of ASCII approximations
        conv.single_line_break = True
        text = conv.handle(h)
    except Exception:
        return _strip_html(h)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)  # empty links left by dropped images
    # Collapse linked buttons (a link wrapping a link) — invalid Markdown that
    # would otherwise render as raw [ [text](url) ](url) syntax.
    text = re.sub(r"\[\s*(\[[^\]]*\]\([^)]*\))\s*\]\([^)]*\)", r"\1", text)
    return _collapse_blank_lines(text)


def _strip_html(h: str) -> str:
    h = re.sub(r"<style[^>]*>.*?</style>", "", h, flags=re.S | re.I)
    h = re.sub(r"<script[^>]*>.*?</script>", "", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<p[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"</p>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()
