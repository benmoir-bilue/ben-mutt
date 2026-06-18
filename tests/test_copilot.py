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

    def test_driver_tools_route_to_ui(self):
        ex, calls = self._ex()
        for tool, args in (("move_cursor", {"direction": "down"}),
                           ("scroll_preview", {"direction": "up"}),
                           ("expand_thread", {})):
            out, err = ex.execute(tool, args)
            assert not err and out == f"did {tool}"
        assert [c[0] for c in calls] == ["move_cursor", "scroll_preview", "expand_thread"]


    def test_check_calendar_without_calendar(self):
        ex, _ = self._ex(calendar=None)
        out, err = ex.execute("check_calendar", {"thread_id": "t1"})
        assert err and "calendar" in out.lower()


class TestCopilotTools:
    """build_copilot_tools wraps the executor as Strands @tool functions."""

    def _ex(self):
        calls = []
        class Ex:
            rules = "(none)"
            def execute(self, name, args):
                calls.append((name, args))
                if name == "boom":
                    return ("nope", True)
                return (f"ok {name}", False)
        return Ex(), calls

    def test_specs_cover_every_tool(self):
        from bem.ai.copilot import build_copilot_tools
        ex, _ = self._ex()
        names = {t.tool_name for t in build_copilot_tools(ex)}
        assert names == {
            "search_threads", "get_thread", "check_calendar", "open_thread",
            "change_folder", "move_cursor", "scroll_preview", "expand_thread",
            "archive_thread", "trash_thread", "file_thread", "undo_last",
            "run_command",
        }

    def test_file_thread_schema_requires_label(self):
        from bem.ai.copilot import build_copilot_tools
        ex, _ = self._ex()
        ft = next(t for t in build_copilot_tools(ex) if t.tool_name == "file_thread")
        schema = ft.tool_spec["inputSchema"]["json"]
        assert set(schema["required"]) == {"thread_id", "label"}

    @pytest.mark.asyncio
    async def test_tool_delegates_and_maps_error(self):
        from bem.ai.copilot import build_copilot_tools
        ex, calls = self._ex()
        archive = next(t for t in build_copilot_tools(ex) if t.tool_name == "archive_thread")
        last = None
        async for ev in archive.stream(
            {"toolUseId": "u1", "name": "archive_thread", "input": {"thread_id": "t9"}}, {}
        ):
            last = ev
        assert ("archive_thread", {"thread_id": "t9"}) in calls
        assert last["tool_result"]["status"] == "success"


def _text_turn(text):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


def _tool_turn(tuid, name, inp):
    import json
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0,
            "start": {"toolUse": {"toolUseId": tuid, "name": name}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0,
            "delta": {"toolUse": {"input": json.dumps(inp)}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


from strands.models.model import Model as _StrandsModel


class _FakeModel(_StrandsModel):
    """A scripted Strands model: each call to stream() replays the next turn."""
    def __init__(self, scripts):
        self._scripts, self._i = scripts, 0
    def get_config(self): return {}
    def update_config(self, **k): pass
    async def structured_output(self, *a, **k):  # pragma: no cover
        yield {}
    async def stream(self, messages, tool_specs=None, system_prompt=None, **k):
        script = self._scripts[self._i]
        self._i += 1
        for ev in script:
            yield ev


class _ChatExec:
    rules = "(none)"
    def __init__(self): self.calls = []
    def execute(self, name, args):
        self.calls.append((name, args))
        return (f"queued {name}", False)


def _brain():
    brain = copilot.CopilotBrain.__new__(copilot.CopilotBrain)
    brain._api_key = "x"
    brain._smart = "claude-sonnet-4-6"
    return brain


class TestCopilotChatLoop:
    """CopilotBrain.chat drives a Strands agent over the copilot tools."""

    def test_dispatches_tool_then_returns_reply(self, monkeypatch):
        from strands.models import anthropic as anth
        model = _FakeModel([
            _tool_turn("u1", "archive_thread", {"thread_id": "t1"}),
            _text_turn("Archived it — say 'undo' to restore."),
        ])
        monkeypatch.setattr(anth, "AnthropicModel", lambda **kw: model)
        ex = _ChatExec()
        emitted = []
        reply = _brain().chat(
            messages=[{"role": "user", "content": "archive 1"}],
            executor=ex, emit=emitted.append, is_cancelled=lambda: False,
            context="(test)",
        )
        assert ("archive_thread", {"thread_id": "t1"}) in ex.calls
        assert reply == "Archived it — say 'undo' to restore."
        assert reply in emitted

    def test_cancelled_before_start_returns_empty(self, monkeypatch):
        from strands.models import anthropic as anth
        monkeypatch.setattr(anth, "AnthropicModel",
                            lambda **kw: _FakeModel([_text_turn("hi")]))
        ex = _ChatExec()
        reply = _brain().chat(
            messages=[{"role": "user", "content": "hello"}],
            executor=ex, emit=lambda t: None, is_cancelled=lambda: True,
        )
        assert reply == "" and ex.calls == []


class TestDemoTrigger:
    def _is(self, s):
        from bem.tui.screens.inbox import InboxScreen
        return InboxScreen._is_demo_request(s)

    def test_recognises_demo_phrases(self):
        assert self._is("Show me how you can control the TUI")
        assert self._is("show me how you control the interface")
        assert self._is("drive the tui")
        assert self._is("demo")
        assert self._is("autopilot")

    def test_rejects_normal_chat(self):
        assert not self._is("archive 4 and the invoice")
        assert not self._is("what's urgent today?")
        assert not self._is("reply to Marie")
        assert not self._is("show me the urgent ones")


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


@pytest.mark.asyncio
async def test_inbox_copilot_drives_selection(make_message):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.tui.widgets import MessageList
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        ml = scr.query_one(MessageList)
        ml.populate([
            Thread(id="t1", snippet="a", messages=[make_message(id="m1", thread_id="t1", subject="One")]),
            Thread(id="t2", snippet="b", messages=[make_message(id="m2", thread_id="t2", subject="Two")]),
        ])
        await pilot.pause()
        scr._drive_cursor("top")
        await pilot.pause()
        assert ml.cursor_row == 0
        # move_cursor tool drives the selection down a row
        r = scr._apply_copilot_ui("move_cursor", {"direction": "down"})
        await pilot.pause()
        assert "moved selection down" in r and ml.cursor_row == 1
        # scroll + expand drivers run without error
        assert "scrolled" in scr._apply_copilot_ui("scroll_preview", {"direction": "down"})
        assert "toggled" in scr._apply_copilot_ui("expand_thread", {})


@pytest.mark.asyncio
async def test_ensure_inbox_view_switches_and_bumps_generation():
    from bem.config import Config
    from bem.tui.app import BemApp
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._current_label_id = "Label_Finance"
        scr._current_query = ""
        gen = scr._threads_generation
        # Returns the pre-reload generation so a worker can wait for it to pass.
        assert scr._ensure_inbox_view() == gen
        await pilot.pause()
        await pilot.pause()
        assert scr._viewing_inbox()
        assert scr._threads_generation > gen


@pytest.mark.asyncio
async def test_change_folder_tool_switches_folder():
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Label

    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._labels = [
            Label(id="INBOX", name="INBOX", type="system"),
            Label(id="DRAFT", name="DRAFT", type="system"),
            Label(id="Label_42", name="Finance", type="user"),
        ]
        # By raw/user name…
        r = scr._apply_copilot_ui("change_folder", {"folder": "Finance"})
        await pilot.pause()
        assert "Finance" in r
        assert scr._current_label_id == "Label_42" and not scr._viewing_inbox()
        # …and by friendly display name (DRAFT -> "Drafts").
        assert "Drafts" in scr._apply_copilot_ui("change_folder", {"folder": "Drafts"})
        assert scr._current_label_id == "DRAFT"
        # Unknown folder is reported honestly, with the options.
        miss = scr._apply_copilot_ui("change_folder", {"folder": "Nope"})
        assert "no folder called" in miss and "Finance" in miss


@pytest.mark.asyncio
async def test_change_folder_routes_through_executor():
    """The executor delegates change_folder to the UI handler (not an error)."""
    from bem.ai.copilot import CopilotExecutor
    calls = []
    ex = CopilotExecutor(_FakeGmail(), None, lambda n, a: calls.append((n, a)) or "opened Sent", [])
    out, is_err = ex.execute("change_folder", {"folder": "Sent"})
    assert calls == [("change_folder", {"folder": "Sent"})]
    assert out == "opened Sent" and is_err is False


@pytest.mark.asyncio
async def test_open_feed_thread_refolds_to_inbox(make_message):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.tui.widgets import MessageList

    inbox_thread = Thread(id="t1", snippet="invoice",
                          messages=[make_message(id="m1", thread_id="t1", subject="Invoice")])

    class _G(_FakeGmail):
        def list_threads(self, label_id="INBOX", **kw):
            return ([inbox_thread], None) if label_id == "INBOX" else ([], None)

    app = BemApp(gmail=_G(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        # Ben wandered into another folder showing an unrelated thread.
        scr._current_label_id = "Label_Finance"
        scr._current_label_name = "Finance"
        scr._current_query = ""
        fin = Thread(id="f9", snippet="receipt",
                     messages=[make_message(id="mf", thread_id="f9", subject="Receipt")])
        scr._threads = [fin]
        scr.query_one(MessageList).populate([fin])
        await pilot.pause()
        # Mutt's feed references an inbox thread that isn't in the Finance list.
        scr._copilot_feed = [TriageNote("t1", "Invoice", "ben@example.com", summary="invoice")]

        scr._open_thread_by_id("t1")
        assert scr._pending_select_id == "t1"   # deferred until the reload lands
        await pilot.pause()
        await pilot.pause()
        # Refolded to the inbox and selected the feed thread there.
        assert scr._viewing_inbox()
        ml = scr.query_one(MessageList)
        assert ml.get_row_index("t1") == ml.cursor_row
        assert scr._pending_select_id is None


class _Host(App):
    def compose(self) -> ComposeResult:
        yield CopilotPanel(id="copilot")


@pytest.mark.asyncio
async def test_agent_run_tucks_mutt_away_then_restores(monkeypatch):
    """:tips/:sort/:zero and :mutt share the right column — never stack them.
    Starting an agent hides Mutt; dismissing the agent brings him back."""
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.tui.widgets import AgentPanel, CopilotPanel
    cfg = Config(); cfg.anthropic_api_key = "x"
    app = BemApp(gmail=_FakeGmail(), config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        cop, ag = scr.query_one(CopilotPanel), scr.query_one(AgentPanel)
        scr._toggle_copilot()
        await pilot.pause()
        assert cop.display and scr._copilot_on
        # Start an agent run without touching the API.
        monkeypatch.setattr(scr, "_run_agent_worker", lambda *a, **k: None)
        scr._start_agent("Learning folders", "goal")
        await pilot.pause()
        # Agent shown, Mutt tucked away — never both at once.
        assert ag.display and not cop.display and scr._copilot_hidden_for_agent
        # Dismissing the agent restores Mutt.
        scr.on_agent_panel_dismissed(AgentPanel.Dismissed())
        await pilot.pause()
        assert not ag.display and cop.display and not scr._copilot_hidden_for_agent


@pytest.mark.asyncio
async def test_focus_command_sets_shows_and_clears(tmp_path, monkeypatch):
    from bem.ai import memory
    monkeypatch.setattr(memory, "FOCUS_FILE", tmp_path / "focus.md")
    from bem.config import Config
    from bem.tui.app import BemApp
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._set_focus("closing Globex, Acme onboarding")
        assert memory.load_focus().text == "closing Globex, Acme onboarding"
        scr._set_focus("")            # show — must not change or error
        assert memory.load_focus().text == "closing Globex, Acme onboarding"
        scr._set_focus("clear")
        assert memory.load_focus() is None


@pytest.mark.asyncio
async def test_esc_closes_mutt_from_the_inbox():
    """Mutt promises 'Esc to leave' — pressing Esc with focus on the inbox
    (not the chat line) closes the panel."""
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.tui.widgets import CopilotPanel, MessageList
    cfg = Config(); cfg.anthropic_api_key = "x"
    app = BemApp(gmail=_FakeGmail(), config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._toggle_copilot()
        await pilot.pause()
        cop = scr.query_one(CopilotPanel)
        assert cop.display and scr._copilot_on
        scr.query_one(MessageList).focus()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not cop.display and not scr._copilot_on


@pytest.mark.asyncio
async def test_mutt_refuses_to_open_over_a_live_agent(monkeypatch):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.tui.widgets import AgentPanel, CopilotPanel
    cfg = Config(); cfg.anthropic_api_key = "x"
    app = BemApp(gmail=_FakeGmail(), config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        cop, ag = scr.query_one(CopilotPanel), scr.query_one(AgentPanel)
        monkeypatch.setattr(scr, "_run_agent_worker", lambda *a, **k: None)
        scr._start_agent("Sorting inbox", "goal")   # Mutt was off
        await pilot.pause()
        assert ag.display and not cop.display
        # :mutt while the agent panel is up must not stack a second panel.
        scr._toggle_copilot()
        await pilot.pause()
        assert not cop.display and not scr._copilot_on


def test_heartbeat_renders_liveness():
    """The title caption is a live heartbeat — proof Mutt is watching."""
    from bem.tui.widgets.copilot_panel import CopilotPanel
    p = CopilotPanel()
    p.mark_sniff(2, present=True, next_in=60)
    hb = p._heartbeat_suffix()
    assert "watching" in hb and "2 new" in hb and "next" in hb and "sniffed" in hb
    p.set_present(False)
    away = p._heartbeat_suffix()
    assert "away" in away and "brief you" in away


def test_mood_emoji_reflects_state():
    """A mood emoji next to Mutt shows what he's doing — sniffing while working,
    asleep while away, a drifting idle mood while watching."""
    from bem.tui.widgets.copilot_panel import (
        CopilotPanel, SNIFF_MOODS, AWAY_MOODS, IDLE_MOODS, IDLE_MOOD_TICKS,
    )
    p = CopilotPanel()
    p.state = "thinking"
    assert p._mood() in SNIFF_MOODS
    p.state = "idle"
    p.set_present(False)
    assert p._mood() in AWAY_MOODS
    p.set_present(True)
    assert p._mood() in IDLE_MOODS
    # Idle mood drifts over time (no frozen face) but isn't a frantic animation.
    p._frame = 0
    first = p._mood()
    p._frame = IDLE_MOOD_TICKS
    assert p._mood() != first or len(IDLE_MOODS) == 1
    # The rendered title carries the dog, a mood, and the live caption.
    p.mark_sniff(2, present=True, next_in=60)
    title = p._render_title()
    assert title.startswith("🐕 Mutt ") and "watching" in title and "next" in title


@pytest.mark.asyncio
async def test_background_refresh_keeps_selection(make_message):
    """New mail repaints the inbox in place without moving the cursor off the
    email Ben is on — the fix for 'the inbox isn't updating' without yanking."""
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.tui.widgets import MessageList
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._copilot_curate = lambda *a, **k: None
        scr._current_label_id, scr._current_query = "INBOX", ""
        t1 = Thread(id="t1", snippet="a", messages=[make_message(id="m1", thread_id="t1", subject="One")])
        t2 = Thread(id="t2", snippet="b", messages=[make_message(id="m2", thread_id="t2", subject="Two")])
        ml = scr.query_one(MessageList)
        ml.populate([t1, t2])
        scr._threads = [t1, t2]
        scr._seen_thread_ids = {"t1", "t2"}
        ml.move_cursor(row=ml.get_row_index("t2"))
        await pilot.pause()
        assert ml.selected_key() == "t2"
        # New mail lands at the top.
        t3 = Thread(id="t3", snippet="c", messages=[make_message(id="m3", thread_id="t3", subject="Three")])
        scr._on_copilot_fetch([t3, t1, t2], None, present=True)
        await pilot.pause()
        assert ml.get_row_index("t3") >= 0          # refreshed: new thread is shown
        assert ml.selected_key() == "t2"            # cursor stayed put


@pytest.mark.asyncio
async def test_sniff_rerank_on_inbox_change_not_just_new_mail(make_message):
    """The reported bug: after Ben actions the hero, the next sniff finds no NEW
    mail — but the inbox changed, so the Curator must still re-rank. And an
    unchanged inbox must NOT burn a re-rank (heartbeat only)."""
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._current_label_id, scr._current_query = "Other", "x"   # not viewing inbox
        recurated = []
        scr._copilot_curate = lambda threads: recurated.append(list(threads))
        mk = lambda tid: Thread(id=tid, snippet=tid,
                                messages=[make_message(id=f"m{tid}", thread_id=tid)])
        t1, t2, t3 = mk("t1"), mk("t2"), mk("t3")
        scr._seen_thread_ids = {"t1", "t2", "t3"}
        scr._inbox_sig = scr._inbox_signature([t1, t2, t3])
        # Ben archived t1 — next poll returns the inbox without it, no NEW mail.
        scr._on_copilot_fetch([t2, t3], None, present=True)
        assert recurated, "inbox shrank (archived) → re-rank even with no new mail"
        # A subsequent identical sniff must not re-rank again.
        recurated.clear()
        scr._on_copilot_fetch([t2, t3], None, present=True)
        assert not recurated, "nothing moved → heartbeat only, no re-rank"


@pytest.mark.asyncio
async def test_on_ranking_populates_feed_and_chat_refs():
    """The Curator's ranking becomes the panel display AND the numbered list Ben
    refers to in chat (hero = [1])."""
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.tui.widgets import CopilotPanel
    from bem.ai.copilot import Ranking, RankedItem
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr.query_one(CopilotPanel).start()
        await pilot.pause()
        r = Ranking(
            hero=RankedItem("h", "Marie", "SOW", "Reply to Marie on the SOW", "client waiting", "reply"),
            on_deck=[RankedItem("d", "Xero", "Invoice", "File the Xero invoice", "", "file")],
        )
        scr._on_ranking(r)
        await pilot.pause()
        assert scr._copilot_ranking is r
        assert [i.thread_id for i in scr._copilot_feed] == ["h", "d"]
        ctx = scr._copilot_context()
        assert "[1]" in ctx and "Reply to Marie on the SOW" in ctx


@pytest.mark.asyncio
async def test_focus_change_recurates_when_watching(tmp_path, monkeypatch):
    from bem.ai import memory
    monkeypatch.setattr(memory, "FOCUS_FILE", tmp_path / "focus.md")
    from bem.config import Config
    from bem.tui.app import BemApp
    app = BemApp(gmail=_FakeGmail(), config=Config())
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._current_label_id, scr._current_query = "INBOX", ""
        recurated = []
        scr._copilot_curate = lambda threads: recurated.append(threads)
        scr._set_focus("closing Globex")
        assert recurated, "setting focus should re-rank while watching the inbox"


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
