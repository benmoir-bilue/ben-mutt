# bem — a Mutt-inspired terminal email client for Google Workspace

> *"All mail clients suck. This one just sucks less."*
> — Michael Elkins, author of [Mutt](http://www.mutt.org/), circa 1995

**bem** stands for **Ben-Mutt** — a personal, [Mutt](http://www.mutt.org/)-inspired
email client for the terminal, rebuilt for Gmail and Google Workspace and wired
end-to-end with Claude.

It keeps the things that made Mutt great — a keyboard-driven index/pager, vim
muscle memory, a `:` command line, everything one keystroke away — and adds the
things Mutt never had: native Gmail threads and labels, Google Calendar invite
handling, AI triage, reply drafting in your own voice, an agentic inbox-zero
mode, and **Mutt**, a live copilot that watches your inbox and can drive the UI
for you.

```
┌ Inbox ────────────────────────────────┬ Mutt ──────────────────┐
│ ● Xero          Invoice INV-0412   red │ 1. Invoice from Xero    │
│   Jane Doe      Re: Q3 planning  yellow│    high · reply         │
│ ● Calendar      Standup ◷ pending      │ 2. Standup invite       │
│   GitHub        [PR] inbox mixins  dim │    normal · accept?     │
│                                        │ > archive 4             │
├────────────────────────────────────────┴────────────────────────┤
│ From: billing@xero.com                                            │
│ Subject: Invoice INV-0412                                         │
│ ...                                                               │
├───────────────────────────────────────────────────────────────────┤
│ [SAFE] [AI]  Inbox  3/altered  :sort to clear inbox               │
└───────────────────────────────────────────────────────────────────┘
```

---

## Why bem?

Mutt's design has aged better than almost any other mail client: the index of
threads, the pager for the current message, a modal command line, and a hand
that never has to leave the home row. What aged *poorly* was everything around
it — mbox files, procmail, fetchmail, IMAP gymnastics, and HTML mail rendered as
a wall of `&nbsp;`.

bem keeps Mutt's interaction model and throws out the plumbing. Mail lives in
Gmail; bem talks to the Gmail and Calendar APIs directly. HTML email is rendered
to readable text. And because the whole thing is a modern Python app, it can
lean on Claude for the parts of email that are pure drudgery — triaging,
filing, summarising, and drafting.

---

## Features

- **Mutt-style index + pager** — expandable threads, vim keys (`j`/`k`/`gg`/`G`),
  a `:` command line, and a `?` keybinding cheatsheet.
- **Native Gmail** — threads, labels-as-folders, stars, read/unread, archive,
  trash, and full Gmail [search syntax](https://support.google.com/mail/answer/7190)
  (`from:`, `label:`, `newer_than:`, …).
- **Compose in your editor** — reply / reply-all / forward / new, drafted with
  proper quoting and headers, then opened in `$EDITOR` (vim by default).
- **AI triage** — `:triage` classifies the inbox into *action / waiting / FYI /
  archive* and colour-codes the list.
- **Reply drafting in your voice** — `:reply-draft` writes a reply that learns
  from your Sent mail, your signature, and voice notes you configure.
- **Summarise & explain** — `:summarise` and `:explain` for long or unfamiliar
  threads.
- **Agentic inbox automation** — `:sort` files your inbox into folders, `:zero`
  drives toward inbox-zero (file, archive, and draft replies), `:agent` runs a
  free-form goal. Every mutation is queued for your approval before it touches
  the mailbox.
- **Mutt, the live copilot** — a background agent that watches for new mail,
  triages each arrival, and posts a numbered feed you can act on
  conversationally (*"archive 3"*, *"reply to the invoice"*). It can even drive
  the TUI on request.
- **Google Calendar invites** — invites are marked *pending / accepted / maybe /
  declined / cancelled / out-of-sync* inline; RSVP with `A` / `M` / `X`, and
  `:cal-clean` clears handled invite mail.
- **Standing rules & folder memory** — teach bem filing rules with `:rule`, and
  let `:tips` learn what lives in each folder so future sorts are faster and
  sharper.
- **Safe mode** — on by default: AI replies are saved as Gmail drafts, never
  sent automatically.

---

## Built with

| Layer            | Technology |
|------------------|------------|
| Terminal UI      | [Textual](https://textual.textualize.io/) |
| Mail & calendar  | [google-api-python-client](https://github.com/googleapis/google-api-python-client), `google-auth-oauthlib` |
| AI               | [Anthropic Claude](https://www.anthropic.com/) (`anthropic` SDK) |
| CLI              | [Click](https://click.palletsprojects.com/) |
| Rendering        | [Rich](https://github.com/Textualize/rich), [html2text](https://github.com/Alir3z4/html2text) |
| Tests            | [pytest](https://pytest.org/) + `pytest-asyncio` |

Requires **Python 3.11+** (the config loader uses the stdlib `tomllib`).

---

## Installation

```bash
git clone https://github.com/benmoir-bilue/ben-mutt.git
cd ben-mutt

# Recommended: a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install bem and its dependencies
pip install .          # or:  pip install -e ".[dev]"  for development
```

This installs the `bem` command (defined in `pyproject.toml` as
`bem = "bem.main:main"`).

---

## Setup

bem talks to Google's APIs on your behalf, so the one-time setup is providing
your own Google OAuth credentials. These are **per-user** and cannot be shipped
with the app. Run `bem setup` at any time to print these instructions.

### 1. Create Google OAuth credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Enable the **Gmail API** and the **Google Calendar API**
   (*APIs & Services → Enable APIs*).
4. Create credentials: *APIs & Services → Credentials → Create Credentials →
   OAuth client ID*. Choose **Desktop app** as the application type.
5. Download the JSON file and save it as:

   ```
   ~/.config/bem/credentials.json
   ```

bem requests these OAuth scopes on first run:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/calendar.events`

> Calendar's write scope is requested up front so that detecting an invite's
> status now and accepting/declining it later share a single consent.

### 2. (Optional) Enable AI features

Set your Anthropic API key. AI features stay disabled gracefully if this is
unset — the rest of bem works fine without it.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

The key is read from the environment by default and is **never written to
disk** unless you explicitly put it in `config.toml`.

### 3. First run

```bash
bem
```

On first launch, bem opens your browser to complete the Google OAuth consent
flow, caches a refresh token at `~/.config/bem/token.json`, and drops you into
your inbox.

---

## Usage

```bash
bem            # open the inbox (default)
bem run        # same as above
bem auth       # clear the cached token and re-run the OAuth flow
bem setup      # print setup instructions
bem version    # print the version
```

Press `?` at any time for the in-app keybinding cheatsheet.

### Keybindings

#### Index (thread list)

| Key      | Action |
|----------|--------|
| `j` / `k`| Next / previous thread |
| `gg`     | First thread |
| `G`      | Last thread |
| `v`      | Expand / collapse thread |
| `V`      | Expand / collapse all threads |
| `P`      | Jump to parent message |
| `Enter`  | Open thread + focus preview |
| `r`      | Reply to selected message |
| `R`      | Reply all |
| `f`      | Forward |
| `m`      | Compose new |
| `e`      | Archive |
| `s`      | Move to folder (AI suggests a label) |
| `d`      | Delete (trash) |
| `u`      | Toggle read / unread |
| `!`      | Toggle star |
| `c`      | Change folder |
| `/`      | Search |
| `Ctrl+R` | Refresh |
| `:`      | Command mode |
| `q`      | Quit |

#### Pager (message preview)

| Key      | Action |
|----------|--------|
| `Space`  | Page down |
| `b`      | Page up |
| `j` / `k`| Scroll line down / up |
| `J`      | Next thread |
| `K`      | Previous thread |

#### Calendar (on an invite)

| Key | Action |
|-----|--------|
| `A` | Accept invite |
| `M` | Maybe / tentative |
| `X` | Decline invite |

#### Copilot

| Key | Action |
|-----|--------|
| `t` | Talk to Mutt (focus the chat input) |

### Command reference (`:` commands)

#### AI (operate on the selected thread)

| Command | Effect |
|---------|--------|
| `:summarise` (`:summarize`, `:summary`) | Summarise the thread |
| `:triage` | Classify the inbox into action / waiting / FYI / archive |
| `:reply-draft [tone]` (`:rd`, `:draft-reply`) | Draft a reply in the given tone |
| `:explain` | Explain the thread |
| `:ai <prompt>` | Run a free-form prompt against the thread |

#### Agent (autonomous, Opus-backed; mutations need your approval)

| Command | Effect |
|---------|--------|
| `:tips` | Scan every folder and cache what lives in each (people, companies, topics) |
| `:sort [hint]` | File the inbox into folders (respects your rules and folder tips) |
| `:sort!` | Sort even when the folder tips are stale (>30 days) |
| `:zero [hint]` | Drive toward inbox-zero: file, archive, and draft replies |
| `:zero!` | Zero even when tips are stale |
| `:agent <goal>` | Pursue a free-form goal with the full tool set |
| `:rule <text>` | Save a standing filing rule (e.g. `invoices from Xero -> Finance`) |

#### Copilot

| Command | Effect |
|---------|--------|
| `:copilot` / `:mutt` | Toggle Mutt, the live inbox copilot, on/off |
| `:mutt <message>` | Send Mutt a one-liner (wakes it if off) |

#### Calendar & general

| Command | Effect |
|---------|--------|
| `:cal-clean` | Count calendar emails that are safe to delete |
| `:cal-clean!` | Trash all handled invite mail (accepted / maybe / declined / cancelled) |
| `:move <label>` | Move the thread to a label (creates it if missing) |
| `:folder <name>` (`:cd`, `:go`) | Switch to a folder by name (Mutt can drive this too) |
| `:search <query>` | Search Gmail with native query syntax |
| `:refresh` | Refresh the current folder |
| `:archive` / `:delete` | Archive / trash the current thread |
| `:quit` (`:q`) | Quit |

---

## AI in depth

bem uses three Claude tiers, each tuned to the job and individually
configurable (see [Configuration](#configuration)):

| Tier  | Default model | Used for |
|-------|---------------|----------|
| fast  | `claude-haiku-4-5-20251001` | triage, summarise, explain, label suggestions, Mutt's live triage |
| smart | `claude-sonnet-4-6` | reply drafting, free-form `:ai`, Mutt's chat reasoning |
| agent | `claude-opus-4-8` | the agentic loop behind `:sort`, `:zero`, `:tips`, `:agent` |

### Triage

`:triage` sends the loaded threads to the fast model and sorts them into four
buckets — **action needed** (red), **waiting on reply** (yellow), **FYI** (dim),
and **can archive** (dim italic) — then colour-codes the index so the shape of
your inbox is visible at a glance.

### Reply drafting in your voice

`:reply-draft` (alias `:rd`) drafts a reply with the smart model. It grounds the
draft in:

- a few of your recent **Sent** messages (so it matches how you actually write),
- your configured `signature` and `voice_notes`,
- and any standing rules from `rules.md`.

With **safe mode** on (the default) the draft is saved to Gmail Drafts for you
to review — nothing is sent automatically.

### The agent

`:sort`, `:zero`, `:tips`, and `:agent` run a tool-use loop on the agent model.
The agent can call:

| Tool | What it does |
|------|--------------|
| `list_labels` | List your labels with message counts |
| `search_threads` | Search with Gmail query syntax |
| `get_thread` | Read a full thread |
| `file_thread` | *(queued)* move a thread to a label and archive it |
| `archive_thread` | *(queued)* archive without filing |
| `draft_reply` | *(queued)* draft a reply in your voice |
| `save_folder_tips` | Write the folder-knowledge cache (local only) |

Read-only tools run immediately; **every mutation is queued and presented as a
plan you approve** before it touches the mailbox.

### Mutt, the live copilot

Toggle Mutt with `:copilot` (or `:mutt`). Mutt is a persistent background agent,
not a one-shot command. It:

- **watches** the inbox for new mail (polling more often during the day,
  timezone-aware),
- **triages** each arrival with the fast model — urgency, a one-line summary,
  and a suggested action,
- **posts a numbered feed** you can act on conversationally: *"archive 3"*,
  *"reply to the invoice"*,
- **chats** when you press `t`, escalating to the smart model for real
  reasoning,
- and can even **drive the full TUI** — switch folders, move the selection,
  open/expand threads, and scroll the preview. Ask it *"show me how you can
  control the TUI"* for an autopilot demo.

Mutt suggests; in safe mode it never acts without your say-so.

### Teaching bem: rules and tips

- **`rules.md`** — standing filing instructions you author with `:rule`. They
  take precedence over the model's own judgment and are fed to the agent, reply
  drafting, and Mutt.
- **`folder_tips.md`** — a cache, generated by `:tips`, recording the people,
  companies, and topics that live in each folder. Future `:sort`/`:zero` runs
  read the cache instead of re-scanning every label, which saves most of the
  turn budget. Tips carry a timestamp and are considered stale after 30 days
  (override with `:sort!` / `:zero!`).

---

## Calendar invites

bem parses iCalendar (RFC 5545) attachments itself — no extra dependencies — and
cross-references them against your Google Calendar. Invite threads are marked
inline:

| Mark | Meaning | Safe to delete? |
|------|---------|-----------------|
| `◷ pending` | Awaiting your RSVP | No |
| `✓ accepted` | You accepted | Yes |
| `~ maybe` | Tentatively accepted | Yes |
| `⊘ declined` | You declined | Yes |
| `✗ cancelled` | The event was cancelled | Yes |
| `⚠ out-of-sync` | Invite present but the event is gone | No |

RSVP straight from the inbox with `A` / `M` / `X` (the organiser is notified and
the mark updates), and use `:cal-clean` / `:cal-clean!` to clear the invite mail
you've already handled.

---

## Configuration

bem reads `~/.config/bem/config.toml` on startup. Every field is optional;
defaults are shown below.

```toml
editor = "nvim"                 # falls back to $EDITOR, then "vim"
threads_per_page = 50           # pagination size
theme = "dark"                  # "dark" or "light"
safe_mode = true                # true: save AI replies as drafts; false: send
signature = "Best,\nBen"        # appended to AI reply drafts
voice_notes = "Warm and concise. Match the sender's energy."

# AI models (override per tier if you like)
ai_model_fast  = "claude-haiku-4-5-20251001"
ai_model_smart = "claude-sonnet-4-6"
ai_model_agent = "claude-opus-4-8"

# Usually supplied via the environment instead (see below)
# anthropic_api_key = "sk-ant-..."
```

> When you change a setting from within bem, the config is rewritten and any
> field that merely mirrors an environment variable (the API key, the editor)
> is **not** persisted — so your key never lands on disk unless you put it there
> yourself.

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | Claude API key; enables AI features | unset (AI disabled) |
| `EDITOR` | Editor used for composing | `vim` |
| `BEM_CONFIG_DIR` | Override the config directory | `~/.config/bem` |

### Files in `~/.config/bem/`

| File | Source | Notes |
|------|--------|-------|
| `credentials.json` | **You** download it from Google Cloud | OAuth client; required |
| `token.json` | Auto-created after first auth | Cached refresh token; `chmod 600` |
| `config.toml` | You edit, or bem rewrites | Settings above; `chmod 600` |
| `rules.md` | Auto-created; grown via `:rule` | Standing filing rules |
| `folder_tips.md` | Auto-created by `:tips` | Folder-knowledge cache |

---

## Privacy & security

- **Your secrets stay yours.** `credentials.json`, `token.json`, `config.toml`,
  and `.env` are all git-ignored and are never committed. The Anthropic API key
  is read from the environment and is not written to disk unless you explicitly
  add it to `config.toml`.
- **OAuth lives on your machine.** bem uses Google's installed-app OAuth flow;
  the refresh token is cached locally with `0600` permissions and is only ever
  sent to Google.
- **Mail content goes to two places only:** Google (your own account) and, when
  AI features are enabled, the Anthropic API. Nothing else.
- **Safe mode is the default.** AI-written replies are saved as drafts; the agent
  queues every mutation for your approval. bem does not send or delete behind
  your back.

---

## Development

```bash
pip install -e ".[dev]"
pytest                  # run the full suite
pytest tests/test_calendar.py -q   # a single module
```

Tests are pure-Python and hit no network — the Gmail, Calendar, and Anthropic
clients are stubbed throughout. The suite covers the models, MIME parsing, the
compose/editor path, the AI commands, the agent loop and tools, the copilot,
calendar/ICS handling, and the TUI widgets.

### Project layout

```
bem/
├── main.py            # CLI entry point (run / auth / setup / version)
├── config.py          # config + file paths
├── gmail/             # Gmail API client, OAuth, and models
├── calendar/          # Calendar client + RFC 5545 iCalendar parser
├── ai/                # AIAssistant, EmailAgent, copilot (Mutt), tools, tips
└── tui/               # Textual app, screens, and widgets
    ├── screens/       # InboxScreen + domain mixins (compose, calendar,
    │                  #   copilot, agent) and the help screen
    └── widgets/       # message list, preview, folder list, command bar,
                       #   AI / agent / copilot panels
```

The `InboxScreen` is composed from focused mixins — `ComposeMixin`,
`CalendarMixin`, `CopilotMixin`, `AgentMixin` — each owning one domain of
behaviour.

---

## License

bem is free software, licensed under the **GNU General Public License v3.0**.
You may use, study, share, and modify it under the terms of the GPL; if you
distribute it or a derivative, you must pass on the same freedoms and make the
source available. See the [`LICENSE`](LICENSE) file for the full text.

```
Copyright (C) 2026 Ben Moir

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
```

---

## Acknowledgements

bem is a love letter to [**Mutt**](http://www.mutt.org/) and its author
**Michael Elkins**, whose 1995 disclaimer still rings true:

> *"All mail clients suck. This one just sucks less."*

Standing on the shoulders of [Textual](https://textual.textualize.io/),
[Rich](https://github.com/Textualize/rich), Google's API client libraries, and
[Claude](https://www.anthropic.com/).
