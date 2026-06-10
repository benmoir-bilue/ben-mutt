from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Label

from bem.tui.widgets.ai_panel import AIPanel


class PanelApp(App):
    pass


def _make_panel(confirm: bool, confirm_calls: list) -> AIPanel:
    return AIPanel(
        title="Triage Inbox",
        work_fn=lambda p: None,  # no worker; tests drive the panel directly
        confirm_label="Apply colour labels to inbox?" if confirm else None,
        confirm_fn=(lambda panel: confirm_calls.append(panel)) if confirm else None,
    )


@pytest.mark.asyncio
async def test_footer_is_tall_enough_to_render_text():
    # Regression: height 1 + padding-bottom 1 left 0 rows for the text,
    # so the footer (and the y/n confirm prompt) was invisible.
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=False, confirm_calls=[])
        app.push_screen(panel)
        await pilot.pause()
        footer = panel.query_one("#ai-footer", Label)
        assert footer.region.height >= 2


@pytest.mark.asyncio
async def test_confirm_prompt_appears_and_y_applies():
    app = PanelApp()
    confirm_calls: list = []
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=True, confirm_calls=confirm_calls)
        app.push_screen(panel)
        await pilot.pause()

        panel.append_text("ACTION NEEDED\n1 - something\n")
        panel.mark_done()
        await pilot.pause()
        assert panel._awaiting_confirm is True

        await pilot.press("y")
        await pilot.pause()
        assert confirm_calls == [panel]  # confirm_fn receives the panel
        assert app.screen is not panel  # panel dismissed


@pytest.mark.asyncio
async def test_confirm_prompt_n_cancels():
    app = PanelApp()
    confirm_calls: list = []
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=True, confirm_calls=confirm_calls)
        app.push_screen(panel)
        await pilot.pause()

        panel.mark_done()
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert confirm_calls == []
        assert app.screen is not panel


@pytest.mark.asyncio
async def test_no_confirm_when_not_configured():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=False, confirm_calls=[])
        app.push_screen(panel)
        await pilot.pause()
        panel.mark_done()
        await pilot.pause()
        assert panel._awaiting_confirm is False


@pytest.mark.asyncio
async def test_full_text_collects_streamed_chunks():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=False, confirm_calls=[])
        app.push_screen(panel)
        await pilot.pause()
        panel.append_text("Hi Alice,\n\nThanks for")
        panel.append_text(" the update.\n")
        panel.mark_done()
        assert panel.full_text == "Hi Alice,\n\nThanks for the update.\n"


@pytest.mark.asyncio
async def test_line_style_applied_to_headings():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = AIPanel(
            title="T",
            work_fn=lambda p: None,
            line_style=lambda line: "bold red" if line.startswith("HEAD") else None,
        )
        app.push_screen(panel)
        await pilot.pause()
        heading = panel._render_line("HEADING")
        plain = panel._render_line("1 — some thread")
        assert heading.style == "bold red"
        assert plain.style == ""
        # And the streaming path accepts styled lines without error
        panel.append_text("HEADING\n1 — some thread\n")
        panel.mark_done()


@pytest.mark.asyncio
async def test_append_after_dismiss_does_not_crash():
    app = PanelApp()
    async with app.run_test() as pilot:
        panel = _make_panel(confirm=False, confirm_calls=[])
        app.push_screen(panel)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        # Worker chunks arriving after dismissal must be no-ops
        panel.append_text("late chunk\n")
        panel.set_error("late error")
        panel.mark_done()
