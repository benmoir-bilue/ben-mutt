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
        self.clear()
        self._labels = []
        for label in labels:
            if label.type == "system" and label.name not in (
                "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED"
            ):
                continue
            self._labels.append(label)
            text = label.display_name
            if label.messages_unread > 0:
                text = f"{label.display_name} ({label.messages_unread})"
            item = ListItem(Label(text), id=f"label-{label.id}")
            if label.messages_unread > 0:
                item.add_class("unread")
            self.append(item)
        if self._labels and old_index is not None:
            try:
                self.index = min(old_index, len(self._labels) - 1)
            except Exception:
                pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.index
        if idx is not None and 0 <= idx < len(self._labels):
            label = self._labels[idx]
            self.post_message(self.LabelSelected(label))
        event.stop()
