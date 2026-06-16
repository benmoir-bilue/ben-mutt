from __future__ import annotations

from datetime import datetime, timezone

import pytest
from textual.app import App, ComposeResult

from bem.ai import copilot
from bem.ai.copilot import TriageNote, _coerce_note
from bem.tui.widgets.copilot_panel import CopilotPanel


class TestCadence:
    def test_active_during_sydney_daytime(self):
        # 00:00 UTC == 10:00 AEST (winter, +10) → active
        assert copilot.is_active_hours(datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc))

    def test_inactive_overnight(self):
        # 16:00 UTC == 02:00 AEST next day → inactive
        assert not copilot.is_active_hours(datetime(2026, 6, 16, 16, 0, tzinfo=timezone.utc))

    def test_poll_interval_brisk_then_lazy(self):
        assert copilot.poll_interval(datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)) == 60.0
        assert copilot.poll_interval(datetime(2026, 6, 16, 16, 0, tzinfo=timezone.utc)) == 600.0


class TestStatusWords:
    def test_rotates(self):
        assert copilot.status_word(0) == copilot.STATUS_WORDS[0]
        assert copilot.status_word(len(copilot.STATUS_WORDS)) == copilot.STATUS_WORDS[0]


class TestTriageNote:
    def test_hint_maps_action(self):
        assert TriageNote("i", "s", "f", action="reply").hint.startswith("press r")
        assert TriageNote("i", "s", "f", action="delete").hint == "press d to delete"
        assert TriageNote("i", "s", "f", action="none").hint == ""

    def test_coerce_valid_json(self, make_thread, make_message):
        thread = make_thread(make_message(id="m1"))
        raw = '{"urgency":"high","summary":"deadline today","action":"reply","reason":"client waiting"}'
        note = _coerce_note(raw, thread)
        assert note.urgency == "high"
        assert note.action == "reply"
        assert note.summary == "deadline today"
        assert note.thread_id == thread.id

    def test_coerce_fenced_json(self, make_thread, make_message):
        thread = make_thread(make_message(id="m1"))
        raw = '```json\n{"urgency":"low","summary":"newsletter","action":"delete","reason":"noise"}\n```'
        note = _coerce_note(raw, thread)
        assert note.urgency == "low"
        assert note.action == "delete"

    def test_coerce_garbage_falls_back_to_snippet(self, make_thread, make_message):
        thread = make_thread(make_message(id="m1", snippet="hello there"))
        note = _coerce_note("the model rambled with no json", thread)
        assert note.action == "none"
        assert note.urgency == "normal"
        assert note.summary  # non-empty fallback

    def test_coerce_clamps_bad_enums(self, make_thread, make_message):
        thread = make_thread(make_message(id="m1"))
        raw = '{"urgency":"EXTREME","action":"nuke","summary":"x","reason":"y"}'
        note = _coerce_note(raw, thread)
        assert note.urgency == "normal"   # invalid → normal
        assert note.action == "none"      # invalid → none


class TestCopilotExecutor:
    def _ex(self, calendar=None):
        from bem.ai.copilot import CopilotExecutor
        calls = []
        def fake_ui(name, args):
            calls.append((name, args))
            return f"did {name}"
        ex = CopilotExecutor(gmail=None, calendar=calendar, ui_action=fake_ui, threads=[])
        return ex, calls

    def test_action_tools_route_to_ui(self):
        ex, calls = self._ex()
        for tool in ("archive_thread", "trash_thread", "open_thread", "undo_last", "run_command"):
            out, err = ex.execute(tool, {"thread_id": "t1"})
            assert not err and out == f"did {tool}"
        assert [c[0] for c in calls] == [
            "archive_thread", "trash_thread", "open_thread", "undo_last", "run_command",
        ]

    def test_file_thread_carries_label(self):
        ex, calls = self._ex()
        ex.execute("file_thread", {"thread_id": "t1", "label": "Recruiting"})
        assert calls[-1] == ("file_thread", {"thread_id": "t1", "label": "Recruiting"})

    def test_unknown_tool_is_error(self):
        ex, _ = self._ex()
        _, err = ex.execute("frobnicate", {})
        assert err

    def test_check_calendar_without_calendar(self):
        ex, _ = self._ex(calendar=None)
        out, err = ex.execute("check_calendar", {"thread_id": "t1"})
        assert err and "calendar" in out.lower()


class _FakeGmail:
    email_address = "ben@example.com"
    credentials = None
    def __init__(self):
        self.archived, self.trashed, self.modified = [], [], []
    def get_profile(self):
        return {"emailAddress": self.email_address}
    def list_labels(self):
        return []
    def list_threads(self, **kw):
        return ([], None)
    def get_thread(self, tid):
        return None
    def archive(self, tid):
        self.archived.append(tid)
    def trash(self, tid):
        self.trashed.append(tid)
    def untrash(self, tid):
        self.modified.append(("untrash", tid))
    def modify_thread(self, tid, add_labels=None, remove_labels=None):
        self.modified.append((tid, add_labels))


@pytest.mark.asyncio
async def test_inbox_copilot_actions_and_undo():
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.ai.copilot import TriageNote
    g = _FakeGmail()
    app = BemApp(gmail=g, config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_feed = [TriageNote("t1", "Invoice 16 Jun", "ben@example.com", summary="invoice")]
        # archive by id, with subject in the report + undo tracked
        r = scr._apply_copilot_ui("archive_thread", {"thread_id": "t1"})
        await pilot.pause()
        assert "archived" in r.lower() and "Invoice" in r
        assert g.archived == ["t1"]
        assert scr._copilot_undo[-1]["op"] == "archive"
        # honest run_command feedback
        assert "isn't a bem command" in scr._apply_copilot_ui("run_command", {"command": "status"})
        assert scr._apply_copilot_ui("run_command", {"command": ":summarise"}).startswith("ran")
        # undo puts it back in the inbox
        scr._apply_copilot_ui("undo_last", {})
        await pilot.pause()
        assert ("t1", ["INBOX"]) in g.modified


class _Host(App):
    def compose(self) -> ComposeResult:
        yield CopilotPanel(id="copilot")


@pytest.mark.asyncio
async def test_panel_mounts_and_posts():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(CopilotPanel)
        assert not panel.is_on
        panel.start()
        assert panel.is_on
        # All feed methods should run without error against a mounted widget.
        panel.post_triage(TriageNote(
            "t1", "Subject", "alice@example.com", urgency="high",
            summary="needs a reply today", action="reply", reason="client waiting",
        ), number=1)
        panel.post_mutt("Woof — that one looks urgent.")
        panel.post_user("what's urgent?")
        panel.begin_thinking(0)
        await pilot.pause()
        panel.end_thinking()
        panel.stop()
        assert not panel.is_on
