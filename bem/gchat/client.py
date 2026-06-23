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
