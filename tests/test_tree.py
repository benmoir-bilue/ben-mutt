from __future__ import annotations

from datetime import datetime, timezone

from bem.gmail.models import Thread
from bem.tui.tree import thread_tree


def _at(minute: int) -> datetime:
    return datetime(2026, 6, 1, 9, minute, tzinfo=timezone.utc)


def _thread(*messages) -> Thread:
    return Thread(id="t1", snippet="", messages=list(messages))


class TestThreadTree:
    def test_single_message_is_a_bare_root(self, make_message):
        rows = thread_tree(_thread(make_message()))
        assert [(r.message.id, r.prefix, r.parent_id) for r in rows] == [("m1", "", "")]

    def test_linear_chain(self, make_message):
        a = make_message(id="a", date=_at(0), message_id_header="<a@x>",
                         references="")
        b = make_message(id="b", date=_at(1), message_id_header="<b@x>",
                         in_reply_to="<a@x>", references="<a@x>")
        c = make_message(id="c", date=_at(2), message_id_header="<c@x>",
                         in_reply_to="<b@x>", references="<a@x> <b@x>")
        rows = thread_tree(_thread(a, b, c))
        assert [(r.message.id, r.prefix) for r in rows] == [
            ("a", ""),
            ("b", "└─>"),
            ("c", "  └─>"),
        ]

    def test_branching_replies_draw_rails(self, make_message):
        a = make_message(id="a", date=_at(0), message_id_header="<a@x>",
                         references="")
        b = make_message(id="b", date=_at(1), message_id_header="<b@x>",
                         in_reply_to="<a@x>", references="<a@x>")
        c = make_message(id="c", date=_at(3), message_id_header="<c@x>",
                         in_reply_to="<a@x>", references="<a@x>")
        d = make_message(id="d", date=_at(2), message_id_header="<d@x>",
                         in_reply_to="<b@x>", references="<a@x> <b@x>")
        rows = thread_tree(_thread(a, b, c, d))
        assert [(r.message.id, r.prefix) for r in rows] == [
            ("a", ""),
            ("b", "├─>"),
            ("d", "│ └─>"),
            ("c", "└─>"),
        ]
        assert {r.message.id: r.parent_id for r in rows} == {
            "a": "", "b": "a", "d": "b", "c": "a",
        }

    def test_references_fallback_when_in_reply_to_missing(self, make_message):
        # Mailing-list style: no In-Reply-To, but References names the parent.
        a = make_message(id="a", date=_at(0), message_id_header="<a@x>",
                         references="")
        b = make_message(id="b", date=_at(1), message_id_header="<b@x>",
                         in_reply_to="", references="<elsewhere@x> <a@x>")
        rows = thread_tree(_thread(a, b))
        assert [(r.message.id, r.prefix) for r in rows] == [("a", ""), ("b", "└─>")]

    def test_unknown_parent_becomes_second_root(self, make_message):
        a = make_message(id="a", date=_at(0), message_id_header="<a@x>",
                         references="")
        b = make_message(id="b", date=_at(1), message_id_header="<b@x>",
                         in_reply_to="<gone@x>", references="<gone@x>")
        rows = thread_tree(_thread(a, b))
        assert [(r.message.id, r.prefix) for r in rows] == [("a", ""), ("b", "")]

    def test_reference_cycle_does_not_hang(self, make_message):
        a = make_message(id="a", date=_at(0), message_id_header="<a@x>",
                         in_reply_to="<b@x>", references="")
        b = make_message(id="b", date=_at(1), message_id_header="<b@x>",
                         in_reply_to="<a@x>", references="")
        rows = thread_tree(_thread(a, b))
        assert {r.message.id for r in rows} == {"a", "b"}
