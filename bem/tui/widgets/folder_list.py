from __future__ import annotations

from textual.widgets import ListView, ListItem, Label
from textual.app import ComposeResult
from textual.message import Message as TMessage

from bem.gmail.models import Label as GmailLabel


class FolderList(ListView):
    DEFAULT_CSS = """
    FolderList {
        width: 22;
        border-right: solid $primary-darken-2;
        background: $surface;
        padding: 0;
    }
    FolderList > ListItem {
        padding: 0 1;
    }
    FolderList > ListItem.--highlight {
        background: $primary;
        color: $text;
    }
    FolderList > ListItem.unread Label {
        text-style: bold;
    }
    """

    class LabelSelected(TMessage):
        def __init__(self, label: GmailLabel) -> None:
            super().__init__()
            self.label = label

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._labels: list[GmailLabel] = []

    def populate(self, labels: list[GmailLabel]) -> None:
        old_index = self.index
        kept: list[GmailLabel] = []
        items: list[ListItem] = []
        for label in labels:
            if label.type == "system" and label.name not in (
                "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED"
            ):
                continue
            kept.append(label)
            text = label.display_name
            if label.messages_unread > 0:
                text = f"{label.display_name} ({label.messages_unread})"
            item = ListItem(Label(text), id=f"label-{label.id}")
            if label.messages_unread > 0:
                item.add_class("unread")
            items.append(item)
        self._labels = kept

        # clear() removes the old items asynchronously; appending new items
        # (with the same widget ids) before that lands raises DuplicateIds and
        # leaves the scroll offset pointing past the content. Rebuild in one
        # awaited sequence instead.
        async def rebuild() -> None:
            await self.clear()
            await self.extend(items)
            if kept and old_index is not None:
                self.index = min(old_index, len(kept) - 1)
            else:
                self.scroll_home(animate=False)

        self.call_later(rebuild)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Resolve by item id, not index — a rebuild may be in flight.
        label = next(
            (l for l in self._labels if f"label-{l.id}" == (event.item.id or "")),
            None,
        )
        if label is not None:
            self.post_message(self.LabelSelected(label))
        event.stop()
