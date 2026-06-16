from __future__ import annotations

from datetime import datetime, timezone

import pytest
from textual.app import App, ComposeResult

from bem.gmail.models import Thread
from bem.tui.widgets.message_list import MessageList


def _at(minute: int) -> datetime:
    return datetime(2026, 6, 1, 9, minute, tzinfo=timezone.utc)


class ListApp(App):
    def __init__(self, threads):
        super().__init__()
        self.threads = threads
        self.events: list = []

    def compose(self) -> ComposeResult:
        yield MessageList(id="messages")

    def on_message_list_thread_highlighted(self, event) -> None:
        self.events.append(("thread", event.thread.id))

    def on_message_list_message_highlighted(self, event) -> None:
        self.events.append(("message", event.message.id))


@pytest.fixture
def threads(make_message):
    a = make_message(id="a", thread_id="t1", date=_at(0),
                     message_id_header="<a@x>", references="")
    b = make_message(id="b", thread_id="t1", date=_at(1),
                     message_id_header="<b@x>", in_reply_to="<a@x>")
    c = make_message(id="c", thread_id="t1", date=_at(2),
                     message_id_header="<c@x>", in_reply_to="<a@x>")
    single = make_message(id="s", thread_id="t2",
                          message_id_header="<s@x>", references="")
    return [
        Thread(id="t1", snippet="", messages=[a, b, c]),
        Thread(id="t2", snippet="", messages=[single]),
    ]


@pytest.mark.asyncio
async def test_v_expands_and_collapses_thread(threads):
    app = ListApp(threads)
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate(threads)
        ml.focus()
        await pilot.pause()
        assert ml.row_count == 2

        await pilot.press("v")  # expand t1 under cursor
        await pilot.pause()
        assert ml.row_count == 5  # 2 thread rows + 3 message rows

        await pilot.press("j")  # onto the root message row
        await pilot.pause()
        assert app.events[-1] == ("message", "a")

        await pilot.press("v")  # collapse from a message row
        await pilot.pause()
        assert ml.row_count == 2
        assert app.events[-1] == ("thread", "t1")  # cursor lands on thread row


@pytest.mark.asyncio
async def test_parent_key_walks_up_the_tree(threads):
    app = ListApp(threads)
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate(threads)
        ml.focus()
        await pilot.pause()
        await pilot.press("v")
        await pilot.press("j", "j")  # root message row, then first child "b"
        await pilot.pause()
        assert app.events[-1] == ("message", "b")

        await pilot.press("P")  # parent of b is the root message a
        await pilot.pause()
        assert app.events[-1] == ("message", "a")

        await pilot.press("P")  # parent of a root is the thread row itself
        await pilot.pause()
        assert app.events[-1] == ("thread", "t1")


@pytest.mark.asyncio
async def test_shift_v_toggles_all_threads(threads):
    app = ListApp(threads)
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate(threads)
        ml.focus()
        await pilot.pause()

        await pilot.press("V")
        await pilot.pause()
        assert ml.row_count == 5  # t2 is single-message: nothing to expand

        await pilot.press("V")
        await pilot.pause()
        assert ml.row_count == 2


@pytest.mark.asyncio
async def test_expansion_survives_repopulate(threads):
    app = ListApp(threads)
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate(threads)
        ml.focus()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()

        ml.populate(threads)  # e.g. a refresh
        await pilot.pause()
        assert ml.row_count == 5  # t1 stays expanded

        ml.populate(threads[1:])  # t1 gone: expansion state dropped
        await pilot.pause()
        assert ml.row_count == 1


@pytest.mark.asyncio
async def test_fold_marker_tracks_expansion(threads):
    app = ListApp(threads)
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate(threads)
        ml.focus()
        await pilot.pause()
        assert ml.get_cell("t1", "subject").plain.startswith("▸ ")
        assert ml.get_cell("t1", "n").plain == "(3)"
        # Single-message thread: blank marker keeps subjects aligned.
        assert ml.get_cell("t2", "subject").plain.startswith("  Quarterly")
        assert ml.get_cell("t2", "n").plain == ""

        await pilot.press("v")
        await pilot.pause()
        assert ml.get_cell("t1", "subject").plain.startswith("▾ ")

        await pilot.press("v")
        await pilot.pause()
        assert ml.get_cell("t1", "subject").plain.startswith("▸ ")


@pytest.mark.asyncio
async def test_long_subjects_truncate_at_40_chars(make_message):
    long_subject = "A very long subject line that just keeps going and going"
    thread = Thread(
        id="t1", snippet="",
        messages=[make_message(id="a", subject=long_subject, references="")],
    )
    app = ListApp([thread])
    async with app.run_test() as pilot:
        ml = app.query_one(MessageList)
        ml.populate([thread])
        await pilot.pause()
        subj = ml.get_cell("t1", "subject").plain
        assert subj == "  " + long_subject[:39] + "…"
