from __future__ import annotations

import base64
import email

from bem.gmail.client import _build_raw_mime


def _decode(raw: str) -> email.message.Message:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_basic_headers():
    raw = _build_raw_mime(
        to="a@b.com", cc="", subject="Hi", body="Body text",
        from_address="me@example.com",
    )
    msg = _decode(raw)
    assert msg["To"] == "a@b.com"
    assert msg["Cc"] is None
    assert msg["From"] == "me@example.com"
    assert msg["Subject"] == "Hi"
    assert msg.get_payload(decode=True).decode() == "Body text"


def test_cc_header_set_when_present():
    raw = _build_raw_mime(
        to="a@b.com", cc="c@d.com, e@f.com", subject="Hi", body="x",
        from_address="me@example.com",
    )
    assert _decode(raw)["Cc"] == "c@d.com, e@f.com"


def test_reply_threading_headers(make_message):
    original = make_message(
        message_id_header="<msg-1@example.com>",
        references="<msg-0@example.com>",
    )
    raw = _build_raw_mime(
        to="a@b.com", cc="", subject="Re: Hi", body="x",
        from_address="me@example.com", reply_to_message=original,
    )
    msg = _decode(raw)
    assert msg["In-Reply-To"] == "<msg-1@example.com>"
    assert msg["References"] == "<msg-0@example.com> <msg-1@example.com>"


def test_no_threading_headers_when_original_lacks_message_id(make_message):
    original = make_message(message_id_header="", references="")
    raw = _build_raw_mime(
        to="a@b.com", cc="", subject="Re: Hi", body="x",
        from_address="me@example.com", reply_to_message=original,
    )
    msg = _decode(raw)
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None
