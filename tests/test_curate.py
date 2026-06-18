from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from bem.ai import copilot
from bem.ai.copilot import (
    CopilotBrain, Candidate, Ranking, RankedItem,
    _decay, _curate_candidates, _extract_json, DECAY_HALF_LIFE_DAYS,
)
from bem.gmail.models import Thread


# ── helpers ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, text): self.content = [type("B", (), {"text": text})()]

class _Client:
    """Fake anthropic client whose messages.create() always returns `text`."""
    def __init__(self, text): self.messages = self; self._t = text
    def create(self, **kw): return _Resp(self._t)


def _brain(client=None):
    b = CopilotBrain.__new__(CopilotBrain)
    b._fast, b._smart, b._client = "haiku", "sonnet", client
    return b


def _thread(make_message, tid, when, subject="Subj", unread=True):
    labels = ["INBOX", "UNREAD"] if unread else ["INBOX"]
    return Thread(id=tid, snippet=f"snippet {tid}", messages=[
        make_message(id=f"m-{tid}", thread_id=tid, subject=subject,
                     date=when, label_ids=labels),
    ])


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestDecay:
    def test_now_is_full_weight(self):
        assert _decay(0) == 1.0

    def test_half_life(self):
        assert _decay(DECAY_HALF_LIFE_DAYS) == pytest.approx(0.5)

    def test_older_is_smaller(self):
        assert _decay(20) < _decay(5) < _decay(1)


class TestCandidates:
    def test_caps_and_orders_by_recency(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        threads = [_thread(make_message, f"t{i}", now - timedelta(days=i)) for i in range(30)]
        cands = _curate_candidates(threads, now=now, limit=10)
        assert len(cands) == 10
        assert cands[0].thread_id == "t0"            # most recent first
        assert cands[0].age_days < cands[-1].age_days

    def test_age_and_unread(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        c = _curate_candidates(
            [_thread(make_message, "a", now - timedelta(days=3), unread=False)], now=now
        )[0]
        assert round(c.age_days) == 3 and c.unread is False


class TestExtractJson:
    def test_array(self): assert _extract_json('[{"a":1}]') == [{"a": 1}]
    def test_object(self): assert _extract_json('{"a":1}') == {"a": 1}
    def test_fenced(self): assert _extract_json('```json\n{"a":1}\n```') == {"a": 1}
    def test_prose_wrapped(self):
        assert _extract_json('here you go: [{"x":2}] cheers') == [{"x": 2}]
    def test_garbage_is_none(self): assert _extract_json("no json here") is None


# ── passes ───────────────────────────────────────────────────────────────────

class TestScorePass:
    def test_parses_clamps_and_drops_unknown(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        cands = _curate_candidates(
            [_thread(make_message, "a", now), _thread(make_message, "x", now)], now=now
        )
        b = _brain(_Client(
            '[{"id":"a","score":150,"action":"reply"},'
            '{"id":"x","score":50,"action":"nuke"},'
            '{"id":"ghost","score":10,"action":"reply"}]'
        ))
        scores = b._score_pass(cands, "")
        assert scores["a"] == (100.0, "reply")     # clamped to 100
        assert scores["x"] == (50.0, "none")       # invalid action → none
        assert "ghost" not in scores               # not a candidate

    def test_failure_returns_empty(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        cands = _curate_candidates([_thread(make_message, "a", now)], now=now)
        assert _brain(client=None)._score_pass(cands, "") == {}


class TestComposeHero:
    def _top(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        return _curate_candidates(
            [_thread(make_message, "a", now), _thread(make_message, "x", now)], now=now
        )

    def test_happy_path(self, make_message):
        top = self._top(make_message)
        by_id = {c.thread_id: c for c in top}
        scores = {"a": (90.0, "reply"), "x": (40.0, "file")}
        b = _brain(_Client(
            '{"hero":{"id":"a","headline":"Reply to Marie on the SOW","why":"client waiting today","action":"reply"},'
            '"on_deck":[{"id":"x","headline":"File the Xero invoice","action":"file"}]}'
        ))
        r = b._compose_hero(top, by_id, scores, "")
        assert r.hero.thread_id == "a" and r.hero.action == "reply"
        assert "Marie" in r.hero.headline and r.hero.hint.startswith("press r")
        assert [i.thread_id for i in r.on_deck] == ["x"]

    def test_fallback_keeps_ranked_order(self, make_message):
        # curate passes `top` already ranked; the fallback must preserve that.
        top = list(reversed(self._top(make_message)))   # ranked order: x, then a
        by_id = {c.thread_id: c for c in top}
        scores = {"a": (40.0, "reply"), "x": (90.0, "file")}
        r = _brain(client=None)._compose_hero(top, by_id, scores, "")  # client raises → fallback
        assert r.hero.thread_id == top[0].thread_id      # first of the ranked top


# ── end-to-end ───────────────────────────────────────────────────────────────

class TestTidyTargets:
    def test_returns_only_valid_ids(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        threads = [_thread(make_message, "news", now, subject="Newsletter"),
                   _thread(make_message, "marie", now, subject="SOW question")]
        b = _brain(_Client('["news", "ghost"]'))   # ghost isn't in the inbox
        assert b.tidy_targets(threads) == ["news"]

    def test_empty_on_failure(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        threads = [_thread(make_message, "a", now)]
        assert _brain(client=None).tidy_targets(threads) == []   # client raises → []

    def test_empty_inbox(self):
        assert _brain().tidy_targets([]) == []


class TestCurate:
    def test_empty_inbox(self):
        r = _brain().curate([])
        assert isinstance(r, Ranking) and r.hero is None and r.considered == 0

    def test_decay_lets_fresh_mail_beat_stale_high_score(self, make_message):
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        old = _thread(make_message, "old", now - timedelta(days=20), subject="Old but loud")
        fresh = _thread(make_message, "fresh", now - timedelta(days=1), subject="Today")
        b = _brain(client=None)  # compose falls back to score*decay ordering
        b._score_pass = lambda cands, mem: {"old": (90.0, "reply"), "fresh": (70.0, "reply")}
        r = b.curate([old, fresh], now=now)
        # old: 90*0.5^4 ≈ 5.6 ; fresh: 70*0.5^0.2 ≈ 60.9 → fresh wins
        assert r.hero.thread_id == "fresh"
        assert r.considered == 2

    def test_items_lists_hero_then_on_deck(self):
        hero = RankedItem("h", "s", "su", "head")
        r = Ranking(hero=hero, on_deck=[RankedItem("d", "s", "su", "h2")])
        assert [i.thread_id for i in r.items] == ["h", "d"]
