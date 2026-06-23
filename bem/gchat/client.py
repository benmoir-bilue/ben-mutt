"""Google Chat API client — lets Mutt message Ben when he's away from the desk.

Mirrors GmailClient/CalendarClient's threading model: httplib2 isn't thread-safe,
so each thread builds its own service via threading.local(). Shares the same
OAuth credentials as the Gmail and Calendar clients.

Sending uses user credentials, so the target must be a space Ben already belongs
to (you can't DM yourself in Chat) — configure its id with `bem chat-spaces`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

REQUEST_TIMEOUT = 15  # seconds


@dataclass
class ChatSpace:
    name: str           # resource name, e.g. "spaces/AAAA1234"
    display: str        # human-readable name (DMs may be blank)
    type: str           # "SPACE" | "DIRECT_MESSAGE" | "GROUP_CHAT" | ...


@dataclass
class ChatMessage:
    name: str           # resource name, e.g. "spaces/A/messages/B"
    text: str           # plain-text body
    create_time: str    # RFC3339, e.g. "2026-06-23T01:02:03.456Z"
    sender: str         # sender resource name (users/…)


class ChatClient:
    def __init__(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._local = threading.local()

    def _svc(self):
        if not hasattr(self._local, "service"):
            http = AuthorizedHttp(
                self._credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT)
            )
            # static_discovery=False: the Chat discovery doc isn't bundled with
            # older googleapiclient releases, so fetch it at runtime.
            self._local.service = build(
                "chat", "v1", http=http, static_discovery=False, cache_discovery=False
            )
        return self._local.service

    def send(self, space: str, text: str) -> str:
        """Post a plain-text message to `space`. Returns the created message name."""
        if not space:
            raise ValueError("no Google Chat space configured")
        resp = (
            self._svc().spaces().messages()
            .create(parent=space, body={"text": text})
            .execute()
        )
        return resp.get("name", "")

    def list_messages(
        self, space: str, after: str | None = None, limit: int = 25
    ) -> list[ChatMessage]:
        """Messages in `space`, oldest first. `after` is an RFC3339 timestamp —
        only messages created strictly after it are returned (for polling)."""
        if not space:
            return []
        kwargs = dict(parent=space, pageSize=limit, orderBy="createTime asc")
        if after:
            kwargs["filter"] = f'createTime > "{after}"'
        resp = self._svc().spaces().messages().list(**kwargs).execute()
        out: list[ChatMessage] = []
        for m in resp.get("messages", []):
            out.append(ChatMessage(
                name=m.get("name", ""),
                text=m.get("text", ""),
                create_time=m.get("createTime", ""),
                sender=(m.get("sender") or {}).get("name", ""),
            ))
        return out

    def list_spaces(self) -> list[ChatSpace]:
        """Every space Ben belongs to, for picking one to put in config."""
        out: list[ChatSpace] = []
        page_token = ""
        while True:
            resp = self._svc().spaces().list(
                pageSize=100, pageToken=page_token or None
            ).execute()
            for s in resp.get("spaces", []):
                out.append(ChatSpace(
                    name=s.get("name", ""),
                    display=s.get("displayName", ""),
                    type=s.get("spaceType", s.get("type", "")),
                ))
            page_token = resp.get("nextPageToken", "")
            if not page_token:
                break
        return out
