from __future__ import annotations

from bem.ai.commands import _thread_to_text, _triage_thread_lines
from bem.gmail.models import Thread


def _threads(n: int, make_message) -> list[Thread]:
    return [
        Thread(
            id=f"t{i}",
            snippet=f"snippet {i}",
            messages=[make_message(id=f"m{i}", thread_id=f"t{i}", subject=f"Subject {i}")],
        )
        for i in range(1, n + 1)
    ]


def test_all_threads_included_not_just_first_20(make_message):
    # Regression: triage used to silently cap at threads[:20]
    lines = _triage_thread_lines(_threads(50, make_message))
    assert len(lines) == 50
    assert lines[0].startswith("1. ")
    assert lines[-1].startswith("50. ")
    assert "Subject 50" in lines[-1]


def test_lines_are_numbered_by_display_position(make_message):
    lines = _triage_thread_lines(_threads(3, make_message))
    assert [l.split(".")[0] for l in lines] == ["1", "2", "3"]


def test_snippet_truncated(make_message):
    t = Thread(id="t1", snippet="x" * 500,
               messages=[make_message(snippet="y" * 500)])
    (line,) = _triage_thread_lines([t])
    assert len(line) < 250


def test_thread_to_text_includes_all_messages(make_message, make_thread):
    thread = make_thread(
        make_message(id="m1", body_plain="first message"),
        make_message(id="m2", body_plain="second message"),
    )
    text = _thread_to_text(thread)
    assert "first message" in text
    assert "second message" in text
