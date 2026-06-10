from __future__ import annotations

import base64
from datetime import timezone

from bem.gmail.models import (
    _extract_body,
    _html_to_text,
    _parse_date,
    _strip_html,
    parse_message,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class TestParseDate:
    def test_rfc2822_header(self):
        dt = _parse_date("Mon, 01 Jun 2026 09:30:00 +1000", None)
        assert dt.tzinfo is not None
        assert dt.astimezone(timezone.utc).hour == 23  # previous day 23:30 UTC

    def test_falls_back_to_internal_date_as_utc(self):
        dt = _parse_date("not a date", "1764547200000")
        assert dt.tzinfo == timezone.utc

    def test_missing_everything_still_returns_aware(self):
        dt = _parse_date("", None)
        assert dt.tzinfo is not None

    def test_naive_header_date_assumed_utc(self):
        dt = _parse_date("Mon, 01 Jun 2026 09:30:00 -0000", None)
        assert dt.tzinfo is not None


class TestExtractBody:
    def test_simple_plain(self):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("hello")}}
        plain, html, atts = _extract_body(payload)
        assert plain == "hello"
        assert not atts

    def test_multipart_alternative(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
            ],
        }
        plain, html, atts = _extract_body(payload)
        assert plain == "plain"
        assert html == "<p>html</p>"

    def test_attachment_with_id(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("body")}},
                {"mimeType": "application/pdf", "filename": "doc.pdf",
                 "body": {"attachmentId": "att-1", "size": 1234}},
            ],
        }
        plain, _, atts = _extract_body(payload)
        assert plain == "body"
        assert [a.filename for a in atts] == ["doc.pdf"]

    def test_inline_text_attachment_is_not_mistaken_for_body(self):
        # Small attachments arrive inline with a filename but no attachmentId
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "filename": "notes.txt",
                 "body": {"data": _b64("ATTACHMENT CONTENT"), "size": 18}},
                {"mimeType": "text/plain", "filename": "",
                 "body": {"data": _b64("real body")}},
            ],
        }
        plain, _, atts = _extract_body(payload)
        assert plain == "real body"
        assert [a.filename for a in atts] == ["notes.txt"]

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("nested")}},
                    ],
                },
            ],
        }
        plain, _, _ = _extract_body(payload)
        assert plain == "nested"


class TestParseMessage:
    def test_minimal_message(self):
        raw = {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "snip",
            "internalDate": "1764547200000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alice <alice@example.com>"},
                    {"name": "Subject", "value": "Hi"},
                    {"name": "To", "value": "ben@example.com"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("hello")},
            },
        }
        msg = parse_message(raw)
        assert msg.from_address == "alice@example.com"
        assert msg.from_name == "Alice"
        assert msg.date.tzinfo is not None
        assert msg.body == "hello"


class TestStripHtml:
    def test_strips_tags_and_unescapes(self):
        assert _strip_html("<p>a &amp; b</p>") == "a & b"

    def test_removes_style_blocks(self):
        assert "color" not in _strip_html("<style>p {color: red}</style><p>hi</p>")


class TestHtmlToText:
    def test_paragraphs_and_entities(self):
        text = _html_to_text("<p>First &amp; second</p><p>Third</p>")
        assert "First & second" in text
        assert "Third" in text

    def test_links_keep_their_urls(self):
        text = _html_to_text('<a href="https://example.com/x">Click here</a>')
        assert "Click here" in text
        assert "https://example.com/x" in text

    def test_lists_get_bullets(self):
        text = _html_to_text("<ul><li>one</li><li>two</li></ul>")
        assert "* one" in text
        assert "* two" in text

    def test_headings_emphasised(self):
        text = _html_to_text("<h1>Big News</h1><p>body</p>")
        assert "Big News" in text

    def test_images_and_styles_dropped(self):
        html = (
            "<style>p {color: red}</style>"
            '<img src="https://t.example/pixel.gif" alt="">'
            "<p>visible</p>"
        )
        text = _html_to_text(html)
        assert "visible" in text
        assert "pixel.gif" not in text
        assert "color" not in text

    def test_no_excessive_blank_lines(self):
        text = _html_to_text("<p>a</p><br><br><br><p>b</p>")
        assert "\n\n\n" not in text

    def test_blockquotes_use_quote_prefix(self):
        # The preview pane dims lines starting with ">" — html2text's
        # blockquote convention lines up with that.
        text = _html_to_text("<blockquote>quoted reply</blockquote>")
        assert "> quoted reply" in text
