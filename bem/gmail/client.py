from __future__ import annotations

import base64
import email.mime.text
import threading
from typing import Optional

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from .models import Label, Thread, Message, parse_thread, parse_message

REQUEST_TIMEOUT = 15  # seconds


class GmailClient:
    """Thread-safe Gmail API client.

    httplib2 is not thread-safe, so each thread gets its own Http + service
    instance via threading.local(). The discovery document is cached to disk
    by googleapiclient, so per-thread build() calls after the first are fast.
    """

    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._local = threading.local()
        self._user = "me"
        self._profile: dict = {}

    @property
    def credentials(self) -> Credentials:
        """The OAuth credentials, shared with sibling clients (e.g. Calendar)."""
        return self._credentials

    def _svc(self):
        if not hasattr(self._local, "service"):
            http = AuthorizedHttp(
                self._credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT)
            )
            self._local.service = build("gmail", "v1", http=http)
        return self._local.service

    def get_profile(self) -> dict:
        if not self._profile:
            self._profile = self._svc().users().getProfile(userId=self._user).execute()
        return self._profile

    @property
    def email_address(self) -> str:
        return self.get_profile().get("emailAddress", "")

    # ── Labels ────────────────────────────────────────────────────────────────

    def list_labels(self) -> list[Label]:
        svc = self._svc()
        resp = svc.users().labels().list(userId=self._user).execute()
        raw_labels = resp.get("labels", [])
        if not raw_labels:
            return []

        details: list[Optional[dict]] = [None] * len(raw_labels)

        def _make_cb(idx: int):
            def cb(request_id, response, exception):
                if exception is None and response:
                    details[idx] = response
            return cb

        batch = svc.new_batch_http_request()
        for i, raw in enumerate(raw_labels):
            batch.add(
                svc.users().labels().get(userId=self._user, id=raw["id"]),
                callback=_make_cb(i),
            )
        batch.execute()

        labels = []
        for raw, d in zip(raw_labels, details):
            # The batch can partially fail (rate limiting); fall back to the
            # list() entry so the folder still appears, just without counts.
            d = d or raw
            labels.append(Label(
                id=d["id"],
                name=d["name"],
                type=d.get("type", "user"),
                messages_total=d.get("messagesTotal", 0),
                messages_unread=d.get("messagesUnread", 0),
            ))
        return sorted(labels, key=lambda l: l.sort_key)

    def create_label(self, name: str) -> Label:
        raw = self._svc().users().labels().create(
            userId=self._user,
            body={"name": name, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        return Label(id=raw["id"], name=raw["name"], type=raw.get("type", "user"))

    # ── Thread listing ─────────────────────────────────────────────────────────

    def list_threads(
        self,
        label_id: str = "INBOX",
        max_results: int = 50,
        page_token: Optional[str] = None,
        query: str = "",
    ) -> tuple[list[Thread], Optional[str]]:
        """Return (threads, next_page_token). Uses batch requests for speed."""
        svc = self._svc()
        kwargs: dict = {
            "userId": self._user,
            "maxResults": max_results,
            "labelIds": [label_id],
        }
        if page_token:
            kwargs["pageToken"] = page_token
        if query:
            kwargs["q"] = query
            del kwargs["labelIds"]

        resp = svc.users().threads().list(**kwargs).execute()
        raw_threads = resp.get("threads", [])
        next_token = resp.get("nextPageToken")

        if not raw_threads:
            return [], next_token

        results: list[Optional[Thread]] = [None] * len(raw_threads)

        def _make_cb(idx: int):
            def cb(request_id, response, exception):
                if exception is None and response:
                    results[idx] = parse_thread(response)
            return cb

        batch = svc.new_batch_http_request()
        for i, rt in enumerate(raw_threads):
            batch.add(
                svc.users().threads().get(
                    userId=self._user,
                    id=rt["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date", "To", "Cc",
                                     "Message-ID", "In-Reply-To", "References"],
                ),
                callback=_make_cb(i),
            )
        batch.execute()

        return [t for t in results if t is not None], next_token

    def get_thread(self, thread_id: str) -> Optional[Thread]:
        try:
            raw = self._svc().users().threads().get(
                userId=self._user,
                id=thread_id,
                format="full",
            ).execute()
            return parse_thread(raw)
        except HttpError:
            return None

    def recent_sent_replies(self, limit: int = 3, max_scan: int = 8) -> list[str]:
        """A few of the user's own recent Sent messages, de-quoted — few-shot
        writing-voice samples for AI reply drafts. Best-effort; returns [] on
        any failure so drafting never blocks on it."""
        from .models import dequote_reply
        try:
            threads, _ = self.list_threads(label_id="SENT", max_results=max_scan)
        except HttpError:
            return []
        me = self.email_address.lower()
        out: list[str] = []
        for t in threads:
            full = self.get_thread(t.id)
            if full is None:
                continue
            mine = [m for m in full.messages if m.from_address.lower() == me]
            if not mine:
                continue
            text = dequote_reply(mine[-1].body)
            if len(text) >= 40:
                out.append(text[:800])
            if len(out) >= limit:
                break
        return out

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Fetch and decode an attachment's bytes. Small attachments arrive
        inline (no attachmentId); callers should read those from the part data
        directly. Returns b'' on failure."""
        if not attachment_id:
            return b""
        try:
            att = self._svc().users().messages().attachments().get(
                userId=self._user, messageId=message_id, id=attachment_id,
            ).execute()
        except HttpError:
            return b""
        data = att.get("data", "")
        if not data:
            return b""
        return base64.urlsafe_b64decode(data + "==")

    # ── Send / Reply ───────────────────────────────────────────────────────────

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        from_address: str,
        cc: str = "",
        reply_to_message: Optional[Message] = None,
        thread_id: Optional[str] = None,
    ) -> Message:
        raw_bytes = _build_raw_mime(to, cc, subject, body, from_address, reply_to_message)
        body_payload: dict = {"raw": raw_bytes}
        if thread_id:
            body_payload["threadId"] = thread_id

        raw = self._svc().users().messages().send(
            userId=self._user, body=body_payload
        ).execute()
        return parse_message(raw)

    # ── Mutations ──────────────────────────────────────────────────────────────

    def modify_thread(
        self,
        thread_id: str,
        add_labels: Optional[list[str]] = None,
        remove_labels: Optional[list[str]] = None,
    ) -> None:
        body: dict = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels
        self._svc().users().threads().modify(
            userId=self._user, id=thread_id, body=body
        ).execute()

    def archive(self, thread_id: str) -> None:
        self.modify_thread(thread_id, remove_labels=["INBOX"])

    def trash(self, thread_id: str) -> None:
        self._svc().users().threads().trash(userId=self._user, id=thread_id).execute()

    def untrash(self, thread_id: str) -> None:
        self._svc().users().threads().untrash(userId=self._user, id=thread_id).execute()

    def mark_read(self, thread_id: str) -> None:
        self.modify_thread(thread_id, remove_labels=["UNREAD"])

    def mark_unread(self, thread_id: str) -> None:
        self.modify_thread(thread_id, add_labels=["UNREAD"])

    def star(self, thread_id: str) -> None:
        self.modify_thread(thread_id, add_labels=["STARRED"])

    def unstar(self, thread_id: str) -> None:
        self.modify_thread(thread_id, remove_labels=["STARRED"])

    # ── Drafts ─────────────────────────────────────────────────────────────────

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        from_address: str,
        cc: str = "",
        reply_to_message: Optional[Message] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        raw_bytes = _build_raw_mime(to, cc, subject, body, from_address, reply_to_message)
        msg_payload: dict = {"raw": raw_bytes}
        if thread_id:
            msg_payload["threadId"] = thread_id

        resp = self._svc().users().drafts().create(
            userId=self._user, body={"message": msg_payload}
        ).execute()
        return resp["id"]


def _build_raw_mime(
    to: str,
    cc: str,
    subject: str,
    body: str,
    from_address: str,
    reply_to_message: Optional[Message] = None,
) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API."""
    mime = email.mime.text.MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    if cc:
        mime["Cc"] = cc
    mime["From"] = from_address
    mime["Subject"] = subject

    if reply_to_message and reply_to_message.message_id_header:
        mime["In-Reply-To"] = reply_to_message.message_id_header
        refs = (reply_to_message.references + " "
                + reply_to_message.message_id_header).strip()
        mime["References"] = refs

    return base64.urlsafe_b64encode(mime.as_bytes()).decode()
