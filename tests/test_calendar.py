from __future__ import annotations

from datetime import timezone

from bem.calendar.ics import parse_ics, CalendarInvite, Attendee
from bem.gmail.models import Message, Attachment, _parse_date


# A real Google Calendar invite (folded lines and all), trimmed of the VTIMEZONE
# body which the parser ignores.
MILAD_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
    "VERSION:2.0\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART;TZID=Australia/Adelaide:20260623T143000\r\n"
    "DTEND;TZID=Australia/Adelaide:20260623T150000\r\n"
    "DTSTAMP:20260615T092610Z\r\n"
    "ORGANIZER;CN=Milad Dakka:mailto:milad@colabyr.com\r\n"
    "UID:6so66cj174sj2b9lc8qj6b9k75j38bb26csj0b9l6osj4or5ccq32dhn74@google.com\r\n"
    "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=TRUE\r\n"
    " ;CN=Milad Dakka;X-NUM-GUESTS=0:mailto:milad@colabyr.com\r\n"
    "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=\r\n"
    " TRUE;CN=user@example.com;X-NUM-GUESTS=0:mailto:user@example.com\r\n"
    "LOCATION:https://meet.google.com/ttc-zqib-frq\r\n"
    "STATUS:CONFIRMED\r\n"
    "SUMMARY:Milad <> Ben\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TestParseIcs:
    def test_core_fields(self):
        inv = parse_ics(MILAD_ICS)
        assert inv is not None
        assert inv.uid == "6so66cj174sj2b9lc8qj6b9k75j38bb26csj0b9l6osj4or5ccq32dhn74@google.com"
        assert inv.summary == "Milad <> Ben"
        assert inv.method == "REQUEST"
        assert inv.status == "CONFIRMED"
        assert inv.organizer == "milad@colabyr.com"
        assert inv.location == "https://meet.google.com/ttc-zqib-frq"

    def test_dtstart_timezone_aware(self):
        inv = parse_ics(MILAD_ICS)
        assert inv.dtstart is not None
        assert inv.dtstart.tzinfo is not None
        # 14:30 in Adelaide (+09:30) == 05:00 UTC
        assert inv.dtstart.astimezone(timezone.utc).hour == 5
        assert inv.dtstart.year == 2026 and inv.dtstart.month == 6 and inv.dtstart.day == 23

    def test_folded_attendee_lines_unfolded(self):
        # The second attendee's email is split across a folded line.
        inv = parse_ics(MILAD_ICS)
        emails = {a.email for a in inv.attendees}
        assert "user@example.com" in emails
        assert "milad@colabyr.com" in emails

    def test_attendee_partstat_lookup(self):
        inv = parse_ics(MILAD_ICS)
        assert inv.attendee_partstat("user@example.com") == "NEEDS-ACTION"
        assert inv.attendee_partstat("MILAD@colabyr.com") == "ACCEPTED"  # case-insensitive
        assert inv.attendee_partstat("nobody@example.com") is None

    def test_utc_and_date_only(self):
        ics = (
            "BEGIN:VCALENDAR\nMETHOD:REQUEST\nBEGIN:VEVENT\n"
            "UID:u1\nSUMMARY:All day\n"
            "DTSTART;VALUE=DATE:20260623\nDTEND:20260624T010000Z\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        inv = parse_ics(ics)
        assert inv.dtstart.year == 2026 and inv.dtstart.hour == 0
        assert inv.dtend.tzinfo == timezone.utc and inv.dtend.hour == 1

    def test_summary_escapes_unescaped(self):
        ics = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:u\n"
            "SUMMARY:Lunch\\, then talk\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        assert parse_ics(ics).summary == "Lunch, then talk"

    def test_no_vevent_returns_none(self):
        assert parse_ics("BEGIN:VCALENDAR\nBEGIN:VFREEBUSY\nEND:VFREEBUSY\nEND:VCALENDAR") is None
        assert parse_ics("") is None
        assert parse_ics("not even ical") is None

    def test_cancellation_method_and_status(self):
        # A Google "Cancelled event" .ics — METHOD:CANCEL is the reliable signal.
        ics = (
            "BEGIN:VCALENDAR\r\nMETHOD:CANCEL\r\nBEGIN:VEVENT\r\n"
            "UID:6nc8tft0vrdceqvl0kr1cqeo47_R20260618@google.com\r\n"
            "STATUS:CANCELLED\r\nSUMMARY:ELT Meeting\r\n"
            "DTSTART;TZID=Australia/Sydney:20260618T083000\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        inv = parse_ics(ics)
        assert inv.method == "CANCEL"
        assert inv.status == "CANCELLED"
        assert inv.summary == "ELT Meeting"


class TestInviteDetection:
    def _msg(self, attachments):
        return Message(
            id="1", thread_id="1", label_ids=[], snippet="", date=_parse_date("", "0"),
            subject="Invitation", from_name="", from_address="", to=[], cc=[],
            body_plain="x", body_html="", message_id_header="", in_reply_to="",
            references="", attachments=attachments,
        )

    def test_text_calendar_is_invite(self):
        att = Attachment("invite.ics", "text/calendar", 1861, "abc")
        msg = self._msg([att])
        assert msg.is_invite is True
        assert msg.calendar_attachment is att

    def test_application_ics_is_invite(self):
        msg = self._msg([Attachment("invite.ics", "application/ics", 1861, "abc")])
        assert msg.is_invite is True

    def test_ics_filename_fallback(self):
        # Some senders use a generic mime type but an .ics filename.
        msg = self._msg([Attachment("meeting.ics", "application/octet-stream", 100, "x")])
        assert msg.is_invite is True

    def test_plain_attachment_is_not_invite(self):
        msg = self._msg([Attachment("report.pdf", "application/pdf", 100, "x")])
        assert msg.is_invite is False
        assert msg.calendar_attachment is None

    def test_no_attachments(self):
        assert self._msg([]).is_invite is False


class TestResponseStatus:
    """The attendee-matching logic, with the network lookup stubbed."""

    def _client(self, event):
        from bem.calendar.client import CalendarClient
        c = CalendarClient(credentials=None)
        c._find_event = lambda ical_uid: event  # type: ignore
        return c

    def test_accepted_attendee(self):
        event = {"attendees": [
            {"email": "user@example.com", "responseStatus": "accepted"},
            {"email": "milad@colabyr.com", "responseStatus": "accepted"},
        ]}
        c = self._client(event)
        assert c.response_status("uid", "user@example.com") == "accepted"

    def test_needs_action_attendee(self):
        event = {"attendees": [
            {"email": "user@example.com", "responseStatus": "needsAction"},
        ]}
        assert self._client(event).response_status("uid", "user@example.com") == "needsAction"

    def test_case_insensitive_email_match(self):
        event = {"attendees": [{"email": "User@Example.com", "responseStatus": "declined"}]}
        assert self._client(event).response_status("uid", "user@example.com") == "declined"

    def test_organizer_self_is_accepted(self):
        event = {"attendees": [], "organizer": {"self": True, "email": "user@example.com"}}
        assert self._client(event).response_status("uid", "user@example.com") == "accepted"

    def test_no_event_is_not_found(self):
        from bem.calendar.client import NOT_FOUND
        assert self._client(None).response_status("uid", "user@example.com") == NOT_FOUND


class TestDispositionMark:
    """Pure mapping of .ics method + calendar lookup -> decoration mark."""

    def _mark(self, method="REQUEST", found=True, cancelled=False, response="needsAction"):
        from bem.calendar.client import disposition_mark, InviteLookup
        return disposition_mark(method, InviteLookup(found, cancelled, response))

    def test_cancel_method_is_cancelled(self):
        from bem.calendar.client import MARK_CANCELLED
        # METHOD:CANCEL wins regardless of calendar state.
        assert self._mark(method="CANCEL") == MARK_CANCELLED
        assert self._mark(method="CANCEL", found=False) == MARK_CANCELLED

    def test_missing_event_is_out_of_sync(self):
        from bem.calendar.client import MARK_OUTOFSYNC
        assert self._mark(found=False) == MARK_OUTOFSYNC

    def test_cancelled_event_is_out_of_sync(self):
        # Case 1: invitation in inbox, event cancelled on calendar.
        from bem.calendar.client import MARK_OUTOFSYNC
        assert self._mark(cancelled=True, response="needsAction") == MARK_OUTOFSYNC

    def test_accepted_tentative_declined(self):
        from bem.calendar.client import MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED
        assert self._mark(response="accepted") == MARK_ACCEPTED
        assert self._mark(response="tentative") == MARK_TENTATIVE   # case 3
        assert self._mark(response="declined") == MARK_DECLINED

    def test_needs_action_is_pending(self):
        # Case 2: new invite awaiting response.
        from bem.calendar.client import MARK_PENDING
        assert self._mark(response="needsAction") == MARK_PENDING

    def test_safe_to_delete_set(self):
        from bem.calendar.client import (
            SAFE_TO_DELETE_MARKS, MARK_ACCEPTED, MARK_TENTATIVE, MARK_DECLINED,
            MARK_CANCELLED, MARK_OUTOFSYNC, MARK_PENDING,
        )
        assert MARK_ACCEPTED in SAFE_TO_DELETE_MARKS
        assert MARK_TENTATIVE in SAFE_TO_DELETE_MARKS
        assert MARK_DECLINED in SAFE_TO_DELETE_MARKS
        assert MARK_CANCELLED in SAFE_TO_DELETE_MARKS
        # Out-of-sync and pending are NOT safe to bulk-delete.
        assert MARK_OUTOFSYNC not in SAFE_TO_DELETE_MARKS
        assert MARK_PENDING not in SAFE_TO_DELETE_MARKS


class TestConflictFilter:
    """The Priyabrata case: only 'Focus time' should survive as a conflict."""

    def _events(self):
        return [
            {"summary": "Office", "start": {"date": "2026-06-22"},
             "end": {"date": "2026-06-23"}, "transparency": "transparent",
             "status": "confirmed"},
            {"summary": "Focus time",
             "start": {"dateTime": "2026-06-22T09:00:00+10:00"},
             "end": {"dateTime": "2026-06-22T12:00:00+10:00"},
             "status": "confirmed"},
            {"summary": "Priyabrata Karmakar - placeholder",
             "start": {"dateTime": "2026-06-22T10:00:00+10:00"},
             "end": {"dateTime": "2026-06-22T11:00:00+10:00"},
             "iCalUID": "2502cljngc7ubso4kkj0ta2jih@google.com", "status": "confirmed"},
            {"summary": "Declined thing",
             "start": {"dateTime": "2026-06-22T10:15:00+10:00"},
             "end": {"dateTime": "2026-06-22T10:45:00+10:00"}, "status": "confirmed",
             "attendees": [{"self": True, "responseStatus": "declined"}]},
            {"summary": "Cancelled thing",
             "start": {"dateTime": "2026-06-22T10:00:00+10:00"},
             "end": {"dateTime": "2026-06-22T10:30:00+10:00"}, "status": "cancelled"},
        ]

    def test_only_busy_timed_non_self_survives(self):
        from bem.calendar.client import _filter_conflicts
        out = _filter_conflicts(self._events(),
                                exclude_uid="2502cljngc7ubso4kkj0ta2jih@google.com")
        summaries = [c.summary for c in out]
        assert summaries == ["Focus time"]

    def test_conflict_carries_times(self):
        from bem.calendar.client import _filter_conflicts
        (c,) = _filter_conflicts(self._events(),
                                 exclude_uid="2502cljngc7ubso4kkj0ta2jih@google.com")
        assert c.start.hour == 9 and c.end.hour == 12
