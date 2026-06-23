from __future__ import annotations

import pytest

from bem.gchat import ChatClient
from bem.tui.screens.inbox_copilot import _is_vip


# ── Fake Google Chat discovery service ────────────────────────────────────────
class _Exec:
    def __init__(self, result): self._r = result
    def execute(self): return self._r


class _Messages:
    def __init__(self, sink): self.sink = sink
    def create(self, parent, body):
        self.sink.append((parent, body))
        return _Exec({"name": f"{parent}/messages/1"})


class _Spaces:
    def __init__(self, sink, pages):
        self._messages = _Messages(sink)
        self._pages = pages
        self.tokens = []
    def messages(self): return self._messages
    def list(self, pageSize=None, pageToken=None):
        self.tokens.append(pageToken)
        return _Exec(self._pages.pop(0))


class _Service:
    def __init__(self, sink, pages): self._spaces = _Spaces(sink, pages)
    def spaces(self): return self._spaces


def _client(sink=None, pages=None):
    c = ChatClient(credentials=None)
    c._local.service = _Service(sink if sink is not None else [], pages or [])
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
