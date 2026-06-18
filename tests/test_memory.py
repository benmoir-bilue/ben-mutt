from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bem.ai import memory


@pytest.fixture
def mem_files(tmp_path, monkeypatch):
    """Point the memory module at throwaway files."""
    f, v, r = tmp_path / "focus.md", tmp_path / "vips.md", tmp_path / "rules.md"
    monkeypatch.setattr(memory, "FOCUS_FILE", f)
    monkeypatch.setattr(memory, "VIPS_FILE", v)
    monkeypatch.setattr(memory, "RULES_FILE", r)
    return f, v, r


class TestFocus:
    def test_save_load_roundtrip(self, mem_files):
        memory.save_focus("closing Globex, Acme onboarding")
        foc = memory.load_focus()
        assert foc is not None
        assert foc.text == "closing Globex, Acme onboarding"
        assert foc.set_at is not None and foc.set_at.tzinfo is not None

    def test_load_none_when_missing_or_empty(self, mem_files):
        assert memory.load_focus() is None
        mem_files[0].write_text("<!-- set: 2026-06-18T09:00:00+00:00 -->\n\n")
        assert memory.load_focus() is None  # stamp but no body

    def test_clear(self, mem_files):
        memory.save_focus("x")
        memory.clear_focus()
        assert memory.load_focus() is None
        memory.clear_focus()  # idempotent, no error

    def test_staleness(self, mem_files):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        memory.save_focus("fresh", now=now)
        foc = memory.load_focus()
        assert not foc.is_stale(now=now)
        assert foc.is_stale(now=now + timedelta(days=memory.FOCUS_STALE_DAYS + 1))

    def test_age_days(self, mem_files):
        set_at = datetime(2026, 6, 10, tzinfo=timezone.utc)
        memory.save_focus("x", now=set_at)
        foc = memory.load_focus()
        assert foc.age_days(now=datetime(2026, 6, 18, tzinfo=timezone.utc)) == 8


class TestVips:
    def test_parses_bullets_strips_notes_ignores_headings(self, mem_files):
        mem_files[1].write_text(
            "# VIPs\n"
            "<!-- a comment -->\n"
            "- priya@northwind.example — runs the Globex deal\n"
            "- @globex.example\n"
            "* Marie\n"
            "\n"
            "plain-line@example.com\n"
        )
        vips = memory.load_vips()
        assert vips == ["priya@northwind.example", "@globex.example", "Marie", "plain-line@example.com"]

    def test_empty_when_no_file(self, mem_files):
        assert memory.load_vips() == []


class TestAddVip:
    def test_adds_and_dedupes(self, mem_files):
        assert memory.add_vip("priya@northwind.example") is True
        assert memory.add_vip("priya@northwind.example") is False   # dedupe
        assert memory.add_vip("- Marie") is True                    # strips bullet
        vips = memory.load_vips()
        assert "priya@northwind.example" in vips and "Marie" in vips

    def test_blank_is_noop(self, mem_files):
        assert memory.add_vip("   ") is False
        assert memory.load_vips() == []


class TestMemoryContext:
    def test_includes_focus_vips_rules(self, mem_files):
        memory.save_focus("ship the Q3 release")
        mem_files[1].write_text("- ceo@board.example\n")
        mem_files[2].write_text("Newsletters -> archive\n")
        ctx = memory.memory_context()
        assert "ship the Q3 release" in ctx
        assert "ceo@board.example" in ctx
        assert "Newsletters -> archive" in ctx

    def test_no_focus_says_so(self, mem_files):
        ctx = memory.memory_context()
        assert "none set" in ctx.lower()
