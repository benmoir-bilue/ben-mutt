from __future__ import annotations

from datetime import datetime, timezone

from bem.ai import presence
from bem.ai import copilot


class TestParseIdle:
    def test_parses_hid_idle_nanoseconds(self):
        out = '    | |   "HIDIdleTime" = 4200000000\n    | |   "EventFlags" = 0\n'
        assert presence._parse_idle_seconds(out) == 4.2

    def test_missing_returns_none(self):
        assert presence._parse_idle_seconds("nothing here") is None

    def test_garbage_value_returns_none(self):
        assert presence._parse_idle_seconds('"HIDIdleTime" = not-a-number') is None


class TestIsPresent:
    def test_present_when_recently_active(self):
        assert presence.is_present(idle=5.0) is True

    def test_away_when_idle_past_threshold(self):
        assert presence.is_present(idle=presence.AWAY_IDLE_SECS + 1) is False

    def test_unknown_defaults_to_present(self, monkeypatch):
        # A probe that can't read idle time (non-mac / failure) → assume present.
        # Stub the probe so the result doesn't depend on this machine's real idle.
        monkeypatch.setattr(presence, "idle_seconds", lambda: None)
        assert presence.is_present(idle=None) is True


class TestPollCadence:
    def _day(self):  # 00:00 UTC == 10:00 AEST → active hours
        return datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)

    def test_present_is_brisk_in_active_hours(self):
        assert copilot.poll_interval(self._day(), present=True) == 60.0

    def test_away_is_throttled(self):
        assert copilot.poll_interval(self._day(), present=False) == 300.0

    def test_backwards_compatible_default(self):
        # Old call sites (no `present`) still get the daytime interval.
        assert copilot.poll_interval(self._day()) == 60.0
