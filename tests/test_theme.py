from __future__ import annotations

import pytest

from bem.config import Config
from bem.tui.app import BemApp, resolve_theme, GREEN_SCREEN_THEME


class _FakeGmail:
    email_address = "ben@example.com"
    credentials = None
    def get_profile(self): return {"emailAddress": self.email_address}
    def list_labels(self): return []
    def list_threads(self, **kw): return ([], None)
    def get_thread(self, tid): return None


def test_resolve_theme_maps_names():
    assert resolve_theme("dark") == "textual-dark"
    assert resolve_theme("light") == "textual-light"
    assert resolve_theme("green") == "green-screen"
    assert resolve_theme("green-screen") == "green-screen"
    assert resolve_theme(None) == "textual-dark"
    assert resolve_theme("nonsense") == "textual-dark"   # safe fallback


@pytest.mark.asyncio
async def test_config_theme_applied_on_mount():
    app = BemApp(gmail=_FakeGmail(), config=Config(theme="green"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "green-screen"
        # the phosphor palette is live
        assert app.current_theme.background == GREEN_SCREEN_THEME.background


@pytest.mark.asyncio
async def test_theme_command_switches_and_persists(tmp_path, monkeypatch):
    import bem.config as cfg
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    app = BemApp(gmail=_FakeGmail(), config=Config(theme="dark"))
    async with app.run_test() as pilot:
        await pilot.pause()
        scr = app.screen
        scr._set_theme("green")
        await pilot.pause()
        assert app.theme == "green-screen"
        assert scr.config.theme == "green"
        assert (tmp_path / "config.toml").read_text().find('theme = "green"') != -1
        # an unknown theme is rejected, leaving the current one untouched
        scr._set_theme("chartreuse")
        await pilot.pause()
        assert app.theme == "green-screen"
