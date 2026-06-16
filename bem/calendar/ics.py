"""A small, dependency-free iCalendar (RFC 5545) reader.

bem only needs a handful of fields from meeting invites — UID, summary, start,
organiser and attendee response state — so this parses those rather than pulling
in a full iCal library. It is deliberately lenient: unknown properties are
ignored and malformed values degrade to None instead of raising.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None  # type: ignore


@dataclass
class Attendee:
    email: str
    partstat: str = ""   # ACCEPTED | DECLINED | TENTATIVE | NEEDS-ACTION
    cn: str = ""         # display name


@dataclass
class CalendarInvite:
    uid: str = ""
    summary: str = ""
    method: str = ""     # REQUEST | REPLY | CANCEL ...
    status: str = ""     # CONFIRMED | CANCELLED ...
    organizer: str = ""  # email address
    location: str = ""
    dtstart: Optional[datetime] = None
    dtend: Optional[datetime] = None
    attendees: list[Attendee] = field(default_factory=list)

    def attendee_partstat(self, email: str) -> Optional[str]:
        """The PARTSTAT the organiser recorded for `email` in this invite, if
        present. Note this is the state at send time, not the live calendar."""
        email = email.lower()
        for a in self.attendees:
            if a.email.lower() == email:
                return a.partstat or "NEEDS-ACTION"
        return None


def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding: a CRLF followed by a space or tab continues
    the previous logical line."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """Split 'NAME;PARAM=v;PARAM2=v2:value' into (name, params, value)."""
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def _mailto(value: str) -> str:
    v = value.strip()
    if v.lower().startswith("mailto:"):
        v = v[len("mailto:"):]
    return v.strip()


def _parse_dt(value: str, params: dict[str, str]) -> Optional[datetime]:
    """Parse DTSTART/DTEND in the common forms: UTC (…Z), floating/local with a
    TZID param, or a date-only value. Returns an aware datetime when a zone is
    known, otherwise a naive one."""
    value = value.strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return dt.replace(tzinfo=timezone.utc)
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        else:  # VALUE=DATE
            dt = datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid))
        except Exception:
            return dt
    return dt


def parse_ics(text: str) -> Optional[CalendarInvite]:
    """Parse the first VEVENT out of an iCalendar document. Returns None when no
    event is present (e.g. a VFREEBUSY or unparseable blob)."""
    if not text or "BEGIN:VEVENT" not in text:
        return None
    invite = CalendarInvite()
    in_event = False
    for line in _unfold(text):
        name, params, value = _split_line(line)
        if name == "METHOD":
            invite.method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            in_event = True
        elif name == "END" and value.strip().upper() == "VEVENT":
            break
        elif not in_event:
            continue
        elif name == "UID":
            invite.uid = value.strip()
        elif name == "SUMMARY":
            invite.summary = _unescape(value)
        elif name == "STATUS":
            invite.status = value.strip().upper()
        elif name == "LOCATION":
            invite.location = _unescape(value)
        elif name == "ORGANIZER":
            invite.organizer = _mailto(value)
        elif name == "DTSTART":
            invite.dtstart = _parse_dt(value, params)
        elif name == "DTEND":
            invite.dtend = _parse_dt(value, params)
        elif name == "ATTENDEE":
            invite.attendees.append(Attendee(
                email=_mailto(value),
                partstat=params.get("PARTSTAT", "").upper(),
                cn=params.get("CN", ""),
            ))
    return invite


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
        .strip()
    )
