from __future__ import annotations

import pytest

from bem.gchat import ChatClient
from bem.tui.screens.inbox_copilot import _is_vip


# ── Fake Google Chat discovery service ────────────────────────────────────────
class _Exec:
    def __init__(self, result): self._r = result
    def execute(self): return self._r


class _Messages:
    def __init__(self, sink, msg_pages, calls):
        self.sink = sink
        self._msg_pages = msg_pages
        self.calls = calls
    def create(self, parent, body):
        self.sink.append((parent, body))
        return _Exec({"name": f"{parent}/messages/1"})
    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Exec(self._msg_pages.pop(0) if self._msg_pages else {"messages": []})


class _Spaces:
    def __init__(self, sink, pages, msg_pages, msg_calls):
        self._messages = _Messages(sink, msg_pages, msg_calls)
        self._pages = pages
        self.tokens = []
    def messages(self): return self._messages
    def list(self, pageSize=None, pageToken=None):
        self.tokens.append(pageToken)
        return _Exec(self._pages.pop(0))


class _Service:
    def __init__(self, sink, pages, msg_pages, msg_calls):
        self._spaces = _Spaces(sink, pages, msg_pages, msg_calls)
    def spaces(self): return self._spaces


def _client(sink=None, pages=None, msg_pages=None, msg_calls=None):
    c = ChatClient(credentials=None)
    c._local.service = _Service(
        sink if sink is not None else [], pages or [],
        msg_pages or [], msg_calls if msg_calls is not None else [],
    )
    return c


def test_send_posts_text_to_space():
    sink = []
    c = _client(sink=sink)
    name = c.send("spaces/AAA", "hello")
    assert sink == [("spaces/AAA", {"text": "hello"})]
    assert name == "spaces/AAA/messages/1"


def test_send_without_space_raises():
    with pytest.raises(ValueError):
        _client().send("", "hi")


def test_send_webhook_posts_json(monkeypatch):
    import bem.gchat.client as gc
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"name": "spaces/A/messages/wh1"}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(gc.urllib.request, "urlopen", fake_urlopen)
    name = ChatClient.send_webhook("https://chat.googleapis.com/v1/spaces/A/messages?key=k", "hi there")
    assert name == "spaces/A/messages/wh1"
    assert captured["method"] == "POST"
    assert b'"text": "hi there"' in captured["data"]


def test_send_webhook_without_url_raises():
    with pytest.raises(ValueError):
        ChatClient.send_webhook("", "hi")


def test_list_spaces_paginates_and_maps_fields():
    pages = [
        {"spaces": [{"name": "spaces/A", "displayName": "Team", "spaceType": "SPACE"}],
         "nextPageToken": "t2"},
        {"spaces": [{"name": "spaces/B", "spaceType": "DIRECT_MESSAGE"}],
         "nextPageToken": ""},
    ]
    c = _client(pages=pages)
    out = c.list_spaces()
    assert [s.name for s in out] == ["spaces/A", "spaces/B"]
    assert out[0].display == "Team" and out[0].type == "SPACE"
    assert out[1].display == "" and out[1].type == "DIRECT_MESSAGE"


def test_list_messages_builds_filter_and_maps():
    calls = []
    pages = [{"messages": [
        {"name": "spaces/A/messages/1", "text": "archive 1",
         "createTime": "2026-06-23T01:00:00.000000Z",
         "sender": {"name": "users/123"}},
    ]}]
    c = _client(msg_pages=pages, msg_calls=calls)
    out = c.list_messages("spaces/A", after="2026-06-23T00:00:00.000000Z")
    assert calls[0]["parent"] == "spaces/A"
    assert calls[0]["orderBy"] == "createTime asc"
    assert calls[0]["filter"] == 'createTime > "2026-06-23T00:00:00.000000Z"'
    assert out[0].name == "spaces/A/messages/1" and out[0].text == "archive 1"
    assert out[0].sender == "users/123"


def test_list_messages_without_after_omits_filter():
    calls = []
    c = _client(msg_pages=[{"messages": []}], msg_calls=calls)
    c.list_messages("spaces/A")
    assert "filter" not in calls[0]


def test_is_vip_matches_name_or_address():
    assert _is_vip("Marie Curie", "marie@lab.org", ["marie"])
    assert _is_vip("Whoever", "ceo@acme.com", ["acme.com"])
    assert not _is_vip("Random", "a@b.com", ["marie", "acme.com"])
    assert not _is_vip("Marie", "marie@x.com", ["  "])   # blank matcher ignored


# ── Away-ping trigger wiring ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_away_ping_fires_for_vip_then_dedupes(make_message, tmp_path, monkeypatch):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.ai import memory
    from tests.test_copilot import _FakeGmail

    monkeypatch.setattr(memory, "VIPS_FILE", tmp_path / "vips.md")
    (tmp_path / "vips.md").write_text("marie\n")

    app = BemApp(gmail=_FakeGmail(), config=Config(google_chat_space="spaces/X"))
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._copilot_curate = lambda *a, **k: None
        sent = []
        scr._send_chat_ping = lambda sender, subject, reason, note: sent.append((sender, reason))

        vip = Thread(id="t1", snippet="", messages=[make_message(
            id="m1", thread_id="t1", from_name="Marie", from_address="marie@x.com",
            subject="need your call")])
        # present -> away on this sniff, with a VIP arrival
        scr._on_copilot_fetch([vip], None, present=False)
        await pilot.pause()
        assert sent == [("Marie", "VIP")]

        # same thread next sniff: no duplicate ping
        scr._on_copilot_fetch([vip], None, present=False)
        await pilot.pause()
        assert sent == [("Marie", "VIP")]


@pytest.mark.asyncio
async def test_away_ping_skips_ordinary_mail(make_message, tmp_path, monkeypatch):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.ai import memory
    from tests.test_copilot import _FakeGmail

    monkeypatch.setattr(memory, "VIPS_FILE", tmp_path / "vips.md")
    (tmp_path / "vips.md").write_text("marie\n")

    app = BemApp(gmail=_FakeGmail(), config=Config(google_chat_space="spaces/X"))
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._copilot_curate = lambda *a, **k: None
        # no brain → urgency can't be judged; non-VIP must not ping
        scr._copilot = None
        sent = []
        scr._send_chat_ping = lambda *a: sent.append(a)

        junk = Thread(id="t9", snippet="", messages=[make_message(
            id="m9", thread_id="t9", from_name="Newsletter", from_address="news@list.com",
            subject="weekly digest")])
        scr._on_copilot_fetch([junk], None, present=False)
        await pilot.pause()
        assert sent == []


@pytest.mark.asyncio
async def test_away_ping_fires_for_high_urgency(make_message, tmp_path, monkeypatch):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.ai import memory
    from bem.ai.copilot import TriageNote
    from tests.test_copilot import _FakeGmail

    monkeypatch.setattr(memory, "VIPS_FILE", tmp_path / "vips.md")
    (tmp_path / "vips.md").write_text("")   # no VIPs — urgency must carry it

    app = BemApp(gmail=_FakeGmail(), config=Config(google_chat_space="spaces/X"))
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._copilot_curate = lambda *a, **k: None
        # fake brain: triage says this one is high urgency
        scr._copilot = type("B", (), {
            "triage": lambda _self, thread, rules="": TriageNote(
                thread.id, thread.subject, thread.sender, urgency="high",
                summary="deadline today")
        })()
        sent = []
        scr._send_chat_ping = lambda sender, subject, reason, note: sent.append((reason, note))

        t = Thread(id="t5", snippet="", messages=[make_message(
            id="m5", thread_id="t5", from_name="Client", from_address="c@firm.com",
            subject="contract due")])
        scr._on_copilot_fetch([t], None, present=False)
        await pilot.pause()
        assert sent == [("urgent", "deadline today")]


@pytest.mark.asyncio
async def test_no_ping_without_configured_space(make_message, monkeypatch, tmp_path):
    from bem.config import Config
    from bem.tui.app import BemApp
    from bem.gmail.models import Thread
    from bem.ai import memory
    from tests.test_copilot import _FakeGmail

    monkeypatch.setattr(memory, "VIPS_FILE", tmp_path / "vips.md")
    (tmp_path / "vips.md").write_text("marie\n")

    app = BemApp(gmail=_FakeGmail(), config=Config())   # no google_chat_space
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._scan_invites = lambda *a, **k: None
        scr._copilot_curate = lambda *a, **k: None
        sent = []
        scr._send_chat_ping = lambda *a: sent.append(a)
        vip = Thread(id="t1", snippet="", messages=[make_message(
            id="m1", thread_id="t1", from_name="Marie", from_address="marie@x.com", subject="hi")])
        scr._on_copilot_fetch([vip], None, present=False)
        await pilot.pause()
        assert sent == []   # feature off when no space configured


# ── Two-way: polling Ben's replies as instructions ────────────────────────────
class _FakeChat:
    """Stand-in for ChatClient on the screen: records sends, serves messages."""
    def __init__(self, messages):
        self._messages = messages
        self.sent = []
        self.webhooked = []
    def list_messages(self, space, after=None, limit=25):
        return [m for m in self._messages if not after or m.create_time > after]
    def send(self, space, text):
        self.sent.append(text)
        return f"{space}/messages/sent{len(self.sent)}"
    def send_webhook(self, url, text):
        self.webhooked.append((url, text))
        return f"spaces/W/messages/wh{len(self.webhooked)}"


def _screen_app():
    from bem.config import Config
    from bem.tui.app import BemApp
    from tests.test_copilot import _FakeGmail
    return BemApp(gmail=_FakeGmail(), config=Config(google_chat_space="spaces/X"))


@pytest.mark.asyncio
async def test_first_poll_baselines_without_dispatch():
    import asyncio
    from bem.gchat import ChatMessage
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        seen = []
        scr._on_chat_instruction = lambda text: seen.append(text)
        scr._chat = _FakeChat([ChatMessage("spaces/X/messages/old", "ignore me",
                                           "2020-01-01T00:00:00.000000Z", "users/1")])
        assert scr._chat_after is None
        await asyncio.to_thread(scr._poll_chat_instructions)
        await pilot.pause()
        assert scr._chat_after is not None    # baseline set
        assert seen == []                      # nothing dispatched on the first poll


@pytest.mark.asyncio
async def test_poll_dispatches_new_and_skips_own():
    import asyncio
    from bem.gchat import ChatMessage
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        seen = []
        scr._on_chat_instruction = lambda text: seen.append(text)
        scr._chat_after = "2026-06-23T00:00:00.000000Z"          # past baseline → polls now
        scr._chat_sent_names = {"spaces/X/messages/own"}          # Mutt's own ping
        scr._chat = _FakeChat([
            ChatMessage("spaces/X/messages/own", "🐕 Mutt ping",
                        "2026-06-23T01:00:00.000000Z", "users/1"),
            ChatMessage("spaces/X/messages/u1", "archive the invoice",
                        "2026-06-23T01:05:00.000000Z", "users/1"),
        ])
        await asyncio.to_thread(scr._poll_chat_instructions)
        await pilot.pause()
        assert seen == ["archive the invoice"]                    # own ping skipped
        assert scr._chat_after == "2026-06-23T01:05:00.000000Z"   # advanced past newest


@pytest.mark.asyncio
async def test_on_chat_instruction_runs_brain_to_chat():
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._is_demo_request = lambda t: False
        routed = []
        scr._copilot_chat_worker = lambda text, to_chat=False: routed.append((text, to_chat))
        scr._on_chat_instruction("file it under Finance")
        await pilot.pause()
        assert routed == [("file it under Finance", True)]        # answered back on Chat


@pytest.mark.asyncio
async def test_send_chat_reply_tracks_name():
    import asyncio
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._chat = _FakeChat([])
        await asyncio.to_thread(scr._send_chat_reply, "done — archived it")
        await pilot.pause()
        assert scr._chat.sent == ["done — archived it"]
        assert "spaces/X/messages/sent1" in scr._chat_sent_names   # won't be read back


@pytest.mark.asyncio
async def test_chat_poll_interval_fast_then_normal():
    from bem.tui.screens.inbox_copilot import CHAT_FAST_INTERVAL
    from bem.ai import copilot
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._present = True
        scr._chat_fast_remaining = 4
        assert scr._chat_poll_interval() == CHAT_FAST_INTERVAL     # brisk while fast window open
        scr._chat_fast_remaining = 0
        assert scr._chat_poll_interval() == copilot.poll_interval(present=True)   # back to mail cadence


@pytest.mark.asyncio
async def test_cadence_stays_fast_on_reply_ramps_down_when_quiet():
    from bem.tui.screens.inbox_copilot import CHAT_FAST_CYCLES
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._chat_fast_remaining = 3
        scr._after_chat_poll(1)                       # a reply landed
        assert scr._chat_fast_remaining == CHAT_FAST_CYCLES   # re-armed to full fast window
        scr._after_chat_poll(0)                       # quiet round
        scr._after_chat_poll(0)
        assert scr._chat_fast_remaining == CHAT_FAST_CYCLES - 2   # counting down
        # drain the rest → never goes negative, settles at 0 (normal cadence)
        for _ in range(CHAT_FAST_CYCLES):
            scr._after_chat_poll(0)
        assert scr._chat_fast_remaining == 0


@pytest.mark.asyncio
async def test_sending_bumps_to_fast_polling():
    import asyncio
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        scr._chat = _FakeChat([])
        assert scr._chat_fast_remaining == 0
        await asyncio.to_thread(scr._chat_send, "ping me")   # a send re-arms fast polling
        await pilot.pause()
        from bem.tui.screens.inbox_copilot import CHAT_FAST_CYCLES
        assert scr._chat_fast_remaining == CHAT_FAST_CYCLES


@pytest.mark.asyncio
async def test_chat_send_prefers_webhook_when_set():
    import asyncio
    from bem.config import Config
    from bem.tui.app import BemApp
    from tests.test_copilot import _FakeGmail
    app = BemApp(gmail=_FakeGmail(), config=Config(
        google_chat_space="spaces/X",
        google_chat_webhook="https://chat.googleapis.com/v1/spaces/X/messages?key=k"))
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._chat = _FakeChat([])
        await asyncio.to_thread(scr._chat_send, "ping me")
        await pilot.pause()
        assert scr._chat.webhooked and scr._chat.webhooked[0][1] == "ping me"
        assert scr._chat.sent == []                          # API send not used
        assert "spaces/W/messages/wh1" in scr._chat_sent_names


# ── Test-send + conversation shown in the talk panel ──────────────────────────
def test_chat_test_trigger_and_payload():
    from bem.tui.screens.inbox_copilot import _is_chat_test_request, _chat_test_payload
    assert _is_chat_test_request("send chat message hello")
    assert _is_chat_test_request("send a chat saying hi")
    assert _is_chat_test_request("test chat")
    assert not _is_chat_test_request("archive 1")
    assert _chat_test_payload("send chat message hello world") == "hello world"
    assert _chat_test_payload("send a chat saying: hi there") == "hi there"
    assert _chat_test_payload("test chat") == ""


@pytest.mark.asyncio
async def test_talk_input_routes_send_chat_to_test():
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        tested, brained = [], []
        scr._chat_test_from_panel = lambda text: tested.append(text)
        scr._copilot_chat_worker = lambda text, to_chat=False: brained.append(text)
        scr._is_demo_request = lambda t: False
        scr._route_copilot_input("send chat message ping")
        scr._route_copilot_input("archive 1")
        assert tested == ["send chat message ping"]   # test send
        assert brained == ["archive 1"]                # ordinary talk → brain


@pytest.mark.asyncio
async def test_chat_test_from_panel_dispatches_payload():
    app = _screen_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._copilot_on = True
        sent = []
        scr._chat_test_worker = lambda msg: sent.append(msg)
        scr._chat_test_from_panel("send chat message ping me")
        assert sent == ["ping me"]


@pytest.mark.asyncio
async def test_chat_test_from_panel_warns_without_space():
    from bem.config import Config
    from bem.tui.app import BemApp
    from tests.test_copilot import _FakeGmail
    app = BemApp(gmail=_FakeGmail(), config=Config())   # no google_chat_space
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        sent = []
        scr._chat_test_worker = lambda msg: sent.append(msg)
        scr._chat_test_from_panel("send chat message hi")
        assert sent == []   # no space → nothing dispatched


@pytest.mark.asyncio
async def test_panel_shows_chat_in_and_out():
    from textual.app import App, ComposeResult
    from bem.tui.widgets.copilot_panel import CopilotPanel
    class P(App):
        def compose(self) -> ComposeResult:
            yield CopilotPanel(id="copilot")
    app = P()
    async with app.run_test() as pilot:
        panel = app.query_one(CopilotPanel)
        panel.display = True            # size the feed so writes flush (start() would add $accent)
        await pilot.pause()
        panel.post_chat_out("hello space")
        panel.post_chat_in("got it")
        await pilot.pause()
        text = "\n".join(s.text for s in panel.query_one("#copilot-feed").lines)
        assert "to Chat: hello space" in text
        assert "from Chat: got it" in text
