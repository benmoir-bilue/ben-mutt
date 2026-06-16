"""Google Calendar API v3 client.

Mirrors GmailClient's threading model: httplib2 is not thread-safe, so each
thread builds its own service via threading.local(). Used to answer "have I
already responded to this invite?" by looking the event up by its iCalUID.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

REQUEST_TIMEOUT = 15  # seconds

# Response states, mapped from the Calendar API's responseStatus values.
ACCEPTED = "accepted"
DECLINED = "declined"
TENTATIVE = "tentative"
NEEDS_ACTION = "needsAction"
NOT_FOUND = "notFound"      # no matching event on the user's calendar

# Inbox decoration marks. The first four mean "the calendar has handled this,
# safe to delete"; the last two flag invites that still need attention.
MARK_ACCEPTED = "accepted"
MARK_TENTATIVE = "tentative"
MARK_DECLINED = "declined"
MARK_CANCELLED = "cancelled"
MARK_OUTOFSYNC = "out-of-sync"   # invite present but event cancelled/removed
MARK_PENDING = "pending"          # awaiting your response

SAFE_TO_DELETE_MARKS = frozenset(
    {MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED, MARK_CANCELLED}
)


@dataclass
class InviteLookup:
    """What the calendar knows about an invite, looked up by iCalUID."""
    found: bool                  # an event with this UID exists
    cancelled: bool              # ...and its status is "cancelled"
    response: str                # the user's responseStatus (NEEDS_ACTION if none)
    start: Optional[datetime] = None
    end: Optional[datetime] = None


@dataclass
class Conflict:
    summary: str
    start: Optional[datetime]
    end: Optional[datetime]


def _event_dt(d: dict) -> Optional[datetime]:
    """Parse a Calendar event's start/end ({'dateTime': ...} or {'date': ...})."""
    s = d.get("dateTime") or d.get("date")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def disposition_mark(method: str, info: "InviteLookup") -> str:
    """Map an invite's .ics METHOD plus its live calendar lookup to a decoration
    mark. Pure — the IO (fetching the .ics and the lookup) happens elsewhere."""
    if method == "CANCEL":
        return MARK_CANCELLED
    if not info.found or info.cancelled:
        return MARK_OUTOFSYNC
    return {
        ACCEPTED: MARK_ACCEPTED,
        TENTATIVE: MARK_TENTATIVE,
        DECLINED: MARK_DECLINED,
    }.get(info.response, MARK_PENDING)


def _filter_conflicts(items: list[dict], exclude_uid: str = "") -> list["Conflict"]:
    """Reduce raw calendar events to genuine time conflicts: drop the invite's
    own event, all-day and free/transparent blocks, cancelled events, and
    anything the user has declined."""
    out: list[Conflict] = []
    for e in items:
        if exclude_uid and e.get("iCalUID", "") == exclude_uid:
            continue
        if e.get("status") == "cancelled":
            continue
        if e.get("transparency") == "transparent":
            continue
        st = e.get("start", {})
        if "date" in st and "dateTime" not in st:
            continue  # all-day event — not a hard time conflict
        if any(a.get("self") and a.get("responseStatus") == DECLINED
               for a in e.get("attendees", [])):
            continue
        out.append(Conflict(
            summary=e.get("summary", "(no title)"),
            start=_event_dt(st),
            end=_event_dt(e.get("end", {})),
        ))
    return out


class CalendarClient:
    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._local = threading.local()

    def _svc(self):
        if not hasattr(self._local, "service"):
            http = AuthorizedHttp(
                self._credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT)
            )
            self._local.service = build("calendar", "v3", http=http)
        return self._local.service

    def _find_event(self, ical_uid: str) -> Optional[dict]:
        """The calendar event matching an invite's iCalUID, or None. Cancelled
        (showDeleted) events count — a declined invite may be marked deleted."""
        if not ical_uid:
            return None
        try:
            resp = self._svc().events().list(
                calendarId="primary",
                iCalUID=ical_uid,
                showDeleted=True,
                maxResults=1,
            ).execute()
        except HttpError:
            return None
        items = resp.get("items", [])
        return items[0] if items else None

    def lookup(self, ical_uid: str, user_email: str) -> InviteLookup:
        """Everything bem needs about an invite's live calendar state: whether
        the event still exists, whether it's been cancelled, the user's response,
        and the event's time window."""
        event = self._find_event(ical_uid)
        if event is None:
            return InviteLookup(found=False, cancelled=False, response=NEEDS_ACTION)
        user_email = user_email.lower()
        response = NEEDS_ACTION
        for att in event.get("attendees", []):
            if att.get("email", "").lower() == user_email:
                response = att.get("responseStatus", NEEDS_ACTION)
                break
        else:
            # No attendee row usually means the user is the organiser or it's a
            # personal event they created — treat as accepted.
            organizer = event.get("organizer", {})
            if organizer.get("self") or organizer.get("email", "").lower() == user_email:
                response = ACCEPTED
        return InviteLookup(
            found=True,
            cancelled=event.get("status") == "cancelled",
            response=response,
            start=_event_dt(event.get("start", {})),
            end=_event_dt(event.get("end", {})),
        )

    def response_status(self, ical_uid: str, user_email: str) -> str:
        """The user's live response to an invite, or NOT_FOUND. The invite's own
        .ics PARTSTAT is the organiser's stale view; this is the source of truth."""
        info = self.lookup(ical_uid, user_email)
        return info.response if info.found else NOT_FOUND

    def conflicts(
        self, start: Optional[datetime], end: Optional[datetime],
        exclude_uid: str = "",
    ) -> list[Conflict]:
        """Busy, timed events overlapping [start, end) — the things a pending
        invite would clash with. Skips the invite's own event, all-day and
        free/transparent blocks (e.g. 'Office' working-location), cancelled
        events, and anything the user has declined."""
        if start is None or end is None:
            return []
        try:
            resp = self._svc().events().list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=25,
            ).execute()
        except HttpError:
            return []
        return _filter_conflicts(resp.get("items", []), exclude_uid)

    def respond_to_event(self, ical_uid: str, user_email: str, status: str) -> bool:
        """RSVP to an invite: set the user's responseStatus (accepted /
        tentative / declined) on the matching event. sendUpdates='all' so the
        organiser is notified of the response (a responseStatus change isn't
        broadcast to every guest the way an edit would be). Returns True on
        success. Requires the calendar.events write scope."""
        event = self._find_event(ical_uid)
        if event is None:
            return False
        user_email = user_email.lower()
        attendees = event.get("attendees", [])
        found = False
        for att in attendees:
            if att.get("email", "").lower() == user_email:
                att["responseStatus"] = status
                found = True
                break
        if not found:
            return False
        try:
            self._svc().events().patch(
                calendarId="primary",
                eventId=event["id"],
                body={"attendees": attendees},
                sendUpdates="all",
            ).execute()
        except HttpError:
            return False
        return True
