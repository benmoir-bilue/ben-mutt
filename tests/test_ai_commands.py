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


def _capture_reply_prompt(make_thread, make_message, target):
    """Run reply_draft with _stream stubbed, returning the prompt it built."""
    from bem.ai.commands import AIAssistant
    ai = AIAssistant.__new__(AIAssistant)  # skip __init__ (no anthropic client needed)
    ai._model_smart = "test-model"
    captured = {}

    def fake_stream(prompt, model):
        captured["prompt"] = prompt
        return iter(())

    ai._stream = fake_stream
    thread = make_thread(
        make_message(id="m1", from_name="Dana", from_address="dana@example.com",
                     body_plain="my CV"),
        make_message(id="m2", from_name="Ben", from_address="ben@example.com",
                     body_plain="fwd to cam"),
    )
    list(ai.reply_draft(thread, target=target))
    return captured["prompt"]


def test_reply_draft_targets_selected_message(make_thread, make_message):
    target = make_message(from_name="Dana", from_address="dana@example.com")
    prompt = _capture_reply_prompt(make_thread, make_message, target)
    assert "dana@example.com" in prompt
    assert "not the most recent" in prompt


def test_reply_draft_without_target_has_no_focus(make_thread, make_message):
    prompt = _capture_reply_prompt(make_thread, make_message, target=None)
    assert "not the most recent" not in prompt


def _capture_voice_prompt(make_thread, make_message, **kw):
    from bem.ai.commands import AIAssistant
    ai = AIAssistant.__new__(AIAssistant)
    ai._model_smart = "test-model"
    captured = {}
    ai._stream = lambda prompt, model: (captured.__setitem__("p", prompt), iter(()))[1]
    thread = make_thread(make_message(id="m1", body_plain="hi"))
    list(ai.reply_draft(thread, **kw))
    return captured["p"]


def test_reply_draft_injects_voice_signature_rules(make_thread, make_message):
    prompt = _capture_voice_prompt(
        make_thread, make_message,
        voice="Warm, concise, Australian.",
        signature="Ben Moir\nCTO",
        samples=["Hi Jackie, thanks for the clear next steps."],
        rules="Politely defer cold partnership pitches.",
    )
    assert "Warm, concise, Australian." in prompt          # voice
    assert "Ben Moir\nCTO" in prompt                         # signature
    assert "thanks for the clear next steps" in prompt       # sample
    assert "Politely defer cold partnership" in prompt       # rules
    assert "[bracketed placeholder]" in prompt               # no-fabrication guard


def test_reply_draft_skips_placeholder_rules_text(make_thread, make_message):
    # The _load_rules "none yet" sentinel must not be injected as a real rule.
    prompt = _capture_voice_prompt(
        make_thread, make_message,
        rules="(none yet — the user has not recorded any rules)",
    )
    assert "none yet" not in prompt
    assert "Standing rules" not in prompt


def test_model_label():
    from bem.ai.commands import model_label
    assert model_label("claude-sonnet-4-6") == "Claude Sonnet 4.6"
    assert model_label("some-future-model") == "some-future-model"
