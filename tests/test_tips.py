from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import bem.ai.tips as tips_mod
from bem.ai.tips import Tips, load_tips, save_tips


NOW = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def tips_file(tmp_path, monkeypatch):
    path = tmp_path / "folder_tips.md"
    monkeypatch.setattr(tips_mod, "TIPS_FILE", path)
    return path


class TestSaveLoad:
    def test_roundtrip_preserves_content_and_stamp(self):
        save_tips("## Finance\n- people: Alice\n- topics: invoices", now=NOW)
        tips = load_tips()
        assert tips is not None
        assert tips.generated_at == NOW
        assert tips.content.startswith("## Finance")
        assert "invoices" in tips.content

    def test_missing_file_returns_none(self):
        assert load_tips() is None

    def test_file_without_stamp_loads_undated(self, tips_file):
        tips_file.write_text("## Finance\n- topics: invoices", encoding="utf-8")
        tips = load_tips()
        assert tips is not None
        assert tips.generated_at is None
        assert tips.is_stale()  # undated counts as stale

    def test_corrupt_stamp_loads_undated(self, tips_file):
        tips_file.write_text(
            "<!-- generated: not-a-date -->\n## Finance", encoding="utf-8"
        )
        tips = load_tips()
        assert tips is not None
        assert tips.generated_at is None
        assert tips.content == "## Finance"


class TestStaleness:
    def test_fresh_tips_are_not_stale(self):
        t = Tips(content="x", generated_at=NOW - timedelta(days=5))
        assert t.age_days(NOW) == 5
        assert not t.is_stale(NOW)

    def test_tips_older_than_a_month_are_stale(self):
        t = Tips(content="x", generated_at=NOW - timedelta(days=31))
        assert t.is_stale(NOW)

    def test_boundary_thirty_days_is_still_fresh(self):
        t = Tips(content="x", generated_at=NOW - timedelta(days=30))
        assert not t.is_stale(NOW)

    def test_future_stamp_clamps_to_zero_age(self):
        t = Tips(content="x", generated_at=NOW + timedelta(days=2))
        assert t.age_days(NOW) == 0


class TestSortGoal:
    def test_sort_goal_embeds_fresh_tips(self):
        from bem.tui.screens.inbox import _sort_goal
        tips = Tips(content="## Finance\n- topics: invoices", generated_at=NOW)
        goal = _sort_goal(tips=tips)
        assert "## Finance" in goal
        assert "do not sample labels" in goal
        assert "list_labels only to confirm" in goal

    def test_sort_goal_without_tips_keeps_sampling_instructions(self):
        from bem.tui.screens.inbox import _sort_goal
        goal = _sort_goal()
        assert "sample it (search_threads label:Name)" in goal

    def test_tips_goal_mentions_save_tool(self):
        from bem.tui.screens.inbox import _tips_goal
        goal = _tips_goal()
        assert "save_folder_tips" in goal
        assert "max_results 10" in goal.replace("\n", " ")


class TestZeroGoal:
    def test_zero_goal_embeds_fresh_tips(self):
        from bem.tui.screens.inbox import _zero_goal
        tips = Tips(content="## Finance\n- topics: invoices", generated_at=NOW)
        goal = _zero_goal(tips=tips)
        assert "## Finance" in goal
        assert "do not sample labels" in goal
        # the rest of the zero flow is intact
        assert "writing voice" in goal
        assert "draft_reply" in goal

    def test_zero_goal_without_tips_keeps_taxonomy_step(self):
        from bem.tui.screens.inbox import _zero_goal
        goal = _zero_goal()
        assert "Learn the folder taxonomy with list_labels" in goal
