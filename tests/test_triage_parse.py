from __future__ import annotations

from bem.gmail.models import Thread, TriageLevel
from bem.tui.screens.inbox import _parse_triage, _triage_entries, _triage_heading_style


def _threads(n: int, make_message) -> list[Thread]:
    return [
        Thread(
            id=f"t{i}",
            snippet=f"snippet {i}",
            messages=[make_message(id=f"m{i}", thread_id=f"t{i}", subject=f"Subject {i}")],
        )
        for i in range(1, n + 1)
    ]


class TestTriageEntries:
    def test_accepts_objects_with_notes(self):
        assert _triage_entries([{"n": 3, "note": "needs reply"}]) == [(3, "needs reply")]

    def test_accepts_bare_numbers(self):
        assert _triage_entries([3, 9]) == [(3, ""), (9, "")]

    def test_missing_note_is_empty(self):
        assert _triage_entries([{"n": 2}]) == [(2, "")]

    def test_garbage_skipped(self):
        assert _triage_entries([{"note": "no number"}, "x", None]) == []

    def test_non_list_input(self):
        assert _triage_entries(None) == []
        assert _triage_entries("3") == []


class TestParseTriage:
    RAW = {
        "action": [{"n": 1, "note": "deadline in 20 days"}],
        "waiting": [{"n": 2, "note": "awaiting your response"}],
        "fyi": [],
        "archive": [{"n": 3, "note": "completed"}],
    }

    def test_levels_keyed_by_thread_id(self, make_message):
        levels, _ = _parse_triage(self.RAW, _threads(3, make_message))
        assert levels == {
            "t1": TriageLevel.ACTION_NEEDED,
            "t2": TriageLevel.WAITING_REPLY,
            "t3": TriageLevel.CAN_ARCHIVE,
        }

    def test_text_contains_subjects_and_notes(self, make_message):
        _, text = _parse_triage(self.RAW, _threads(3, make_message))
        assert "1 — Subject 1 (deadline in 20 days)" in text
        assert "2 — Subject 2 (awaiting your response)" in text

    def test_text_headings_match_panel_colour_matcher(self, make_message):
        # The headings we render must be the ones the colour matcher styles —
        # this is what makes the panel a truthful legend.
        _, text = _parse_triage(self.RAW, _threads(3, make_message))
        styled = [line for line in text.splitlines() if _triage_heading_style(line)]
        assert styled == ["ACTION NEEDED", "WAITING FOR REPLY", "CAN ARCHIVE"]

    def test_empty_categories_omitted(self, make_message):
        _, text = _parse_triage(self.RAW, _threads(3, make_message))
        assert "FYI / LOW PRIORITY" not in text

    def test_out_of_range_numbers_ignored(self, make_message):
        raw = {"action": [{"n": 99, "note": "x"}, {"n": 0, "note": "y"}]}
        levels, text = _parse_triage(raw, _threads(2, make_message))
        assert levels == {}

    def test_unclassified_threads_reported(self, make_message):
        raw = {"action": [{"n": 1, "note": ""}]}
        levels, text = _parse_triage(raw, _threads(3, make_message))
        assert "(not classified: 2, 3)" in text

    def test_all_classified_no_missing_note(self, make_message):
        _, text = _parse_triage(self.RAW, _threads(3, make_message))
        assert "not classified" not in text
