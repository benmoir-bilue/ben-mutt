from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bem.gmail.models import Message, Thread


@pytest.fixture
def make_message():
    def _make(**overrides) -> Message:
        base = dict(
            id="m1",
            thread_id="t1",
            label_ids=["INBOX", "UNREAD"],
            snippet="snippet text",
            date=datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc),
            subject="Quarterly report",
            from_name="Alice Smith",
            from_address="alice@example.com",
            to=["ben@example.com", "carol@example.com"],
            cc=["dave@example.com"],
            body_plain="Hello Ben,\nPlease review the attached numbers.",
            body_html="",
            message_id_header="<msg-1@example.com>",
            in_reply_to="",
            references="<msg-0@example.com>",
        )
        base.update(overrides)
        return Message(**base)

    return _make


@pytest.fixture
def make_thread(make_message):
    def _make(*messages: Message) -> Thread:
        msgs = list(messages) or [make_message()]
        return Thread(id="t1", snippet="snippet text", messages=msgs)

    return _make
