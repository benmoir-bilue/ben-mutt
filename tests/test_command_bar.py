from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from bem.tui.widgets.command_bar import CommandBar


class BarApp(App):
    def compose(self) -> ComposeResult:
        yield CommandBar(id="command")


FOLDERS = ["Finance", "Family", "Projects", "Receipts"]


@pytest.mark.asyncio
async def test_tab_completes_folder_prefix():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.set_completions(FOLDERS)
        bar.show("move ")
        await pilot.pause()
        # type "Rec" then Tab → unique match completes
        for ch in "Rec":
            await pilot.press(ch)
        await pilot.press("tab")
        await pilot.pause()
        assert bar._buffer == "move Receipts"


@pytest.mark.asyncio
async def test_tab_cycles_through_matches():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.set_completions(FOLDERS)
        bar.show("move ")
        await pilot.pause()
        await pilot.press("F")          # matches Finance, Family
        await pilot.press("tab")
        await pilot.pause()
        first = bar._buffer
        await pilot.press("tab")        # cycle to the next match
        await pilot.pause()
        second = bar._buffer
        assert {first, second} == {"move Finance", "move Family"}
        await pilot.press("tab")        # wraps back round
        await pilot.pause()
        assert bar._buffer == first


@pytest.mark.asyncio
async def test_typing_resets_tab_cycle():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.set_completions(FOLDERS)
        bar.show("move ")
        await pilot.pause()
        await pilot.press("F", "tab")
        await pilot.pause()
        # backspace the whole arg and retype a different stem
        for _ in range(len(bar._buffer) - len("move ")):
            await pilot.press("backspace")
        await pilot.press("P", "tab")
        await pilot.pause()
        assert bar._buffer == "move Projects"


@pytest.mark.asyncio
async def test_tab_does_nothing_for_non_folder_command():
    app = BarApp()
    async with app.run_test() as pilot:
        bar = app.query_one(CommandBar)
        bar.set_completions(FOLDERS)
        bar.show("search ")
        await pilot.pause()
        await pilot.press("F", "tab")
        await pilot.pause()
        assert bar._buffer == "search F"   # untouched — search isn't a folder cmd
