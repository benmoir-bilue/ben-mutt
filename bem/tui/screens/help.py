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
    ("v",           "Expand / collapse thread",      "Index"),
    ("V",           "Expand / collapse all threads", "Index"),
    ("P",           "Jump to parent message",        "Index"),
    ("Enter",       "Open thread + focus preview",   "Index"),
    ("r",           "Reply (to selected message)",   "Index"),
    ("R",           "Reply all",                     "Index"),
    ("f",           "Forward",                       "Index"),
    ("m",           "Compose new",                   "Index"),
    ("e",           "Archive",                       "Index"),
    ("s",           "Move to folder (AI suggests)",  "Index"),
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
    ("↑ / ↓",       "Move between links",            "Pager"),
    ("Enter",       "Open selected link in browser",  "Pager"),
    ("J",           "Next thread",                   "Pager"),
    ("K",           "Prev thread",                   "Pager"),
    (":summarise",  "Summarise selected thread",     "AI"),
    (":triage",     "Triage inbox by priority",      "AI"),
    (":reply-draft, :rd [tone]", "Draft a reply",    "AI"),
    (":explain",    "Explain selected thread",       "AI"),
    (":ai <prompt>", "Free-form prompt on thread",    "AI"),
    (":tips",       "Agent: scan folders, save tips", "Agent"),
    (":sort [hint]", "Agent: file inbox into folders", "Agent"),
    (":sort!",      "Sort even if tips are stale",   "Agent"),
    (":zero [hint]", "Agent: file, archive + draft replies", "Agent"),
    (":zero!",      "Zero even if tips are stale",   "Agent"),
    (":agent <goal>", "Agent: free-form goal",       "Agent"),
    (":rule <text>", "Save a standing filing rule",  "Agent"),
    (":copilot / :mutt", "Toggle Mutt, the live inbox copilot", "Copilot"),
    (":mutt <message>", "Ask Mutt something",          "Copilot"),
    ("t",            "Talk to Mutt (focus chat)",     "Copilot"),
    (":focus <text>", "Set what Mutt ranks against (:focus clear)", "Copilot"),
    (":brief",       "Show top priority + on-deck now", "Copilot"),
    (":tidy / :tidy!", "Propose / archive disposable noise", "Copilot"),
    (":vip <who>",   "Mark a sender VIP (:vip to list)", "Copilot"),
    ("(to Mutt)",    "\"show me how you can control the TUI\" — autopilot demo", "Copilot"),
    ("A / M / X",    "Invite: accept / maybe / decline", "Calendar"),
    (":cal-clean",   "Count calendar emails safe to delete", "Calendar"),
    (":cal-clean!",  "Trash handled invites (✓ ~ ⊘ ✗)", "Calendar"),
    (":move <label>", "Move thread (creates label)", "General"),
    (":folder <name>", "Switch to a folder (Mutt can too)", "General"),
    (":search <q>", "Search Gmail",                  "General"),
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
        table.add_column("Key", width=26)
        table.add_column("Action", width=28)
        table.add_column("Context", width=10)
        for key, action, ctx in BINDINGS_TABLE:
            table.add_row(key, action, ctx)
