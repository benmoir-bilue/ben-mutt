from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import ListItem

from bem.gmail.models import Label
from bem.tui.widgets.folder_list import FolderList


def _labels(*names: str) -> list[Label]:
    return [
        Label(id=f"Label_{n}", name=n, type="user", messages_unread=i)
        for i, n in enumerate(names)
    ]


class FolderApp(App):
    def __init__(self):
        super().__init__()
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        yield FolderList(id="folders")

    def on_folder_list_label_selected(self, event) -> None:
        self.selected.append(event.label.name)


@pytest.mark.asyncio
async def test_rapid_repopulate_does_not_duplicate_or_drop_items():
    """populate() used to fire-and-forget clear() then append items with the
    same widget ids — a second populate before the removal landed raised
    DuplicateIds and silently killed the update."""
    app = FolderApp()
    async with app.run_test() as pilot:
        fl = app.query_one(FolderList)
        # Back-to-back populates without yielding, as happens when load_labels
        # callbacks pile up after several mutations.
        fl.populate(_labels("Finance", "Travel"))
        fl.populate(_labels("Finance", "Travel", "Receipts"))
        fl.populate(_labels("Finance", "Travel", "Receipts"))
        await pilot.pause()
        await pilot.pause()
        items = fl.query(ListItem)
        assert len(items) == 3
        assert [i.id for i in items] == [
            "label-Label_Finance", "label-Label_Travel", "label-Label_Receipts"
        ]
        assert fl.scroll_y == 0


@pytest.mark.asyncio
async def test_selection_resolves_label_by_item_id():
    app = FolderApp()
    async with app.run_test() as pilot:
        fl = app.query_one(FolderList)
        fl.populate(_labels("Finance", "Travel"))
        await pilot.pause()
        fl.focus()
        await pilot.press("down", "down", "enter")  # None → Finance → Travel
        await pilot.pause()
        assert app.selected[-1] == "Travel"
