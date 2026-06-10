from __future__ import annotations

from bem.tui.screens.compose import (
    build_forward_draft,
    build_new_draft,
    build_reply_draft,
    parse_draft,
)

MY_ADDRESS = "ben@example.com"


def _headers(draft: str) -> dict[str, str]:
    to, cc, subject, body = parse_draft(draft)
    return {"to": to, "cc": cc, "subject": subject, "body": body}


class TestBuildReplyDraft:
    def test_plain_reply_addresses_sender_only(self, make_thread):
        draft = build_reply_draft(make_thread(), my_address=MY_ADDRESS)
        h = _headers(draft)
        assert h["to"] == "alice@example.com"
        assert h["cc"] == ""

    def test_reply_all_ccs_other_recipients(self, make_thread):
        draft = build_reply_draft(make_thread(), reply_all=True, my_address=MY_ADDRESS)
        h = _headers(draft)
        assert h["to"] == "alice@example.com"
        cc = {a.strip() for a in h["cc"].split(",")}
        assert cc == {"carol@example.com", "dave@example.com"}

    def test_reply_all_excludes_my_address(self, make_thread):
        draft = build_reply_draft(make_thread(), reply_all=True, my_address=MY_ADDRESS)
        assert MY_ADDRESS not in _headers(draft)["cc"]

    def test_reply_all_dedupes_case_insensitively(self, make_thread, make_message):
        msg = make_message(
            to=["Carol@Example.com", "carol@example.com"],
            cc=["alice@example.com"],  # sender already in To
        )
        draft = build_reply_draft(make_thread(msg), reply_all=True, my_address=MY_ADDRESS)
        h = _headers(draft)
        assert h["cc"] == "Carol@Example.com"

    def test_reply_to_own_message_keeps_recipients(self, make_thread, make_message):
        msg = make_message(
            from_name="Ben Moir",
            from_address=MY_ADDRESS,
            to=["alice@example.com", "carol@example.com"],
        )
        draft = build_reply_draft(make_thread(msg), my_address=MY_ADDRESS)
        h = _headers(draft)
        assert "alice@example.com" in h["to"]
        assert "carol@example.com" in h["to"]
        assert MY_ADDRESS not in h["to"]

    def test_subject_gets_re_prefix_once(self, make_thread, make_message):
        draft = build_reply_draft(make_thread(make_message(subject="Re: hello")),
                                  my_address=MY_ADDRESS)
        assert _headers(draft)["subject"] == "Re: hello"

    def test_body_is_quoted(self, make_thread):
        draft = build_reply_draft(make_thread(), my_address=MY_ADDRESS)
        body = _headers(draft)["body"]
        assert "> Hello Ben," in body
        assert "Alice Smith" in body  # quote attribution line

    def test_prefilled_body_sits_above_quote(self, make_thread):
        ai_text = "Hi Alice,\n\nHappy to review — will send notes today."
        draft = build_reply_draft(make_thread(), my_address=MY_ADDRESS, body=ai_text)
        body = _headers(draft)["body"]
        assert body.startswith("Hi Alice,")
        assert body.index("will send notes today.") < body.index("> Hello Ben,")


class TestBuildForwardDraft:
    def test_subject_gets_fwd_prefix(self, make_thread):
        draft = build_forward_draft(make_thread())
        h = _headers(draft)
        assert h["subject"] == "Fwd: Quarterly report"
        assert h["to"] == ""

    def test_includes_original_body(self, make_thread):
        draft = build_forward_draft(make_thread())
        assert "Please review the attached numbers." in _headers(draft)["body"]


class TestParseDraft:
    def test_round_trip_of_new_draft(self):
        content = build_new_draft().replace("To: ", "To: x@y.com") + "Hello there"
        to, cc, subject, body = parse_draft(content)
        assert to == "x@y.com"
        assert cc == ""
        assert body == "Hello there"

    def test_parses_all_headers_and_body(self):
        content = (
            "To: a@b.com\n"
            "Cc: c@d.com, e@f.com\n"
            "Subject: Test subject\n"
            "\n"
            "Line one.\n"
            "Line two: with a colon.\n"
        )
        to, cc, subject, body = parse_draft(content)
        assert to == "a@b.com"
        assert cc == "c@d.com, e@f.com"
        assert subject == "Test subject"
        assert body == "Line one.\nLine two: with a colon."

    def test_missing_cc_header(self):
        content = "To: a@b.com\nSubject: s\n\nbody"
        to, cc, subject, body = parse_draft(content)
        assert cc == ""
        assert body == "body"

    def test_missing_subject_defaults(self):
        content = "To: a@b.com\n\nbody"
        _, _, subject, _ = parse_draft(content)
        assert subject == "(no subject)"

    def test_empty_to_returned_as_empty(self):
        to, cc, subject, body = parse_draft("To: \nCc: \nSubject: s\n\nbody")
        assert to == ""
