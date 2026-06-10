from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label
from textual.containers import Vertical


BINDINGS_TABLE = [
    # (key, action, context)
    ("j / k",       "Next / prev thread",           "Index"),
    ("gg",          "First thread",                  "Index"),
    ("G",           "Last thread",                   "Index"),
    ("r",           "Reply",                         "Index"),
    ("R",           "Reply all",                     "Index"),
    ("f",           "Forward",                       "Index"),
    ("m",           "Compose new",                   "Index"),
    ("e",           "Archive",                       "Index"),
    ("d",           "Delete (trash)",                "Index"),
    ("u",           "Toggle read / unread",          "Index"),
    ("!",           "Toggle star",                   "Index"),
    ("c",           "Change folder",                 "Index"),
    ("/",           "Search",                        "Index"),
    ("Ctrl+R",      "Refresh",                       "Index"),
    (":",           "Command mode",                  "Index"),
    ("q",           "Quit",                          "Index"),
    ("Space",       "Page down in preview",          "Pager"),
    ("b",           "Page up in preview",            "Pager"),
    ("j / k",       "Scroll line down / up",         "Pager"),
    ("J",           "Next thread",                   "Pager"),
    ("K",           "Prev thread",                   "Pager"),
    (":summarise",  "Summarise selected thread",     "AI"),
    (":triage",     "Triage inbox by priority",      "AI"),
    (":reply-draft [tone]", "Draft a reply",         "AI"),
    (":explain",    "Explain selected thread",       "AI"),
    (":search <q>", "Search Gmail",                  "AI"),
    ("?",           "This help",                     "General"),
]


class HelpScreen(ModalScreen):
    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 68;
        height: auto;
        max-height: 80vh;
        background: $surface;
        border: solid $primary;
        padding: 0 1;
    }
    #help-title {
        text-align: center;
        text-style: bold;
        padding: 1 0 0 0;
        color: $primary;
    }
    #help-hint {
        text-align: center;
        color: $text-muted;
        padding: 0 0 1 0;
    }
    DataTable {
        height: auto;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q,escape,question_mark", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Label("bem keybindings", id="help-title")
            yield Label("press q or ? to close", id="help-hint")
            table = DataTable(show_header=True, cursor_type="none", zebra_stripes=True)
            yield table

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Key", width=24)
        table.add_column("Action", width=28)
        table.add_column("Context", width=10)
        for key, action, ctx in BINDINGS_TABLE:
            table.add_row(key, action, ctx)
