# bem — cleanup & improvement round

**Status (10 Jun 2026):** P0 (items 1–6) and P1 (items 7–12) fixed; test suite in
place (`tests/`, 38 passing). P2 done: 13, 14, 16, 18, 20, and the MIME-helper half
of 15. `test_gmail.py` moved to `scripts/smoke_gmail.py`; empty `bem/config/` dir
removed; config/token/credentials files chmod 600.

**Round 2 (10 Jun 2026):** item 15 done (single `_load_full_thread` worker);
all of P3 done — `/` prefills `search `, refresh re-runs the active search and
folder changes clear it, pagination (next page auto-loads when the cursor hits
the last row; status bar shows `n/50+` while more pages exist), `:reply-draft`
offers "Open as reply in editor?" (AI text lands above the quoted original),
and folder unread counts refresh after mutations and mark-read (FolderList
keeps its cursor). New: HTML emails render via `html2text` (links, lists,
tables, blockquotes; images/styles dropped), with the regex stripper as
fallback. Suite: 81 passing.

**Still open:** item 19's remaining gap (no InboxScreen-level integration
tests — widget and pure-function coverage only).

**Item 17 done:** `:triage` is now one structured call (with per-thread notes in
the JSON) that drives both the panel text and the y=apply colours — they can no
longer disagree. The 20-thread cap is gone; all loaded threads are classified.
The old streaming `triage()` method was removed.

**Post-review finding (fixed):** the AIPanel footer was invisible — CSS gave it
`height: 1` with `padding-bottom: 1`, leaving 0 rows for content, so the triage
`y=apply` confirm prompt (and the scroll hints) never rendered. Now `height: 2`,
with Textual pilot tests in `tests/test_ai_panel.py` covering footer visibility
and the y/n confirm flow.

**Needs manual verification in the running app:** AI panel early-close, archive/
delete cursor behaviour, unread/star toggles, reply-all Cc, debounced previews.

Findings from a full code review (June 2026). Ordered by priority; each item lists
the file(s) and the concrete fix. Evidence from `~/.config/bem/bem.log` confirms the
network-storm issues (timeouts, SSL record-layer failures from overlapping requests).

## P0 — crashes and broken behaviour

1. **Closing the AI panel mid-stream can crash the app** — `tui/widgets/ai_panel.py`, `tui/screens/inbox.py:480`
   `_run_ai_worker` keeps streaming after the panel is dismissed. `panel.append_text` →
   `query_one("#ai-body")` on an unmounted screen raises, and the worker has
   `exit_on_error=True` (default), which takes the whole app down.
   Fix: cancel the `"ai"` worker group in `AIPanel.on_unmount` (or have the panel own the
   worker), guard `append_text`/`mark_done` with `self.is_mounted`, and add
   `exit_on_error=False` to all `@work` decorators that lack it (`_mark_read_background`,
   `_mutate_thread`, `_save_draft_worker`, `_send_worker`, `_run_ai_worker`,
   `_apply_triage_labels`).

2. **Enter on an empty `:` command bar leaves the UI wedged** — `tui/widgets/command_bar.py:55`
   Empty submit calls `hide()` but posts no message, so `InboxScreen._restore_status_bar`
   never runs: the status bar stays hidden and focus is lost.
   Fix: post `Dismissed()` when the buffer is empty on Enter.

3. **Reply-all drops Cc recipients and can address yourself** — `tui/screens/compose.py:21`
   `build_reply_draft` only uses `last.to`; `last.cc` is ignored entirely, and when the
   last message is your own (replying in a thread you just answered), `to` becomes your
   address. Fix: To = sender (or original To if sender is me), Cc = original To+Cc minus
   my address; emit a `Cc:` header line in the template and honour it in `parse_draft`
   and `GmailClient.send`.

4. **Mutations never update local state — toggles are stuck** — `tui/screens/inbox.py:320`
   `_after_mutation` doesn't touch `thread.label_ids`, so:
   - `u` (toggle unread) always performs the same direction; the ●/bold row never changes.
   - `!` (toggle star) same problem.
   - Opening a thread marks it read server-side but the row stays bold and the unread
     count is stale.
   Fix: mutate `label_ids` on the local `Thread` after a successful op and refresh that
   row. `MessageList.update_thread` exists but is dead code and buggy (remove+re-add
   appends the row at the bottom, losing order) — fix it to rebuild in place (or rebuild
   the table preserving cursor) and actually call it.

5. **Cursor jumps to the top after archive/delete** — `tui/screens/inbox.py:320`, `tui/widgets/message_list.py:63`
   `_after_mutation` repopulates the whole table and `populate()` resets the cursor to
   row 0. In Mutt the cursor stays put. Fix: remember the removed row index and restore
   the cursor to the same position (clamped); same for `ctrl+r` refresh.

6. **Revoked/invalid token crashes startup** — `gmail/auth.py:20`
   Only `TransportError` is caught around `creds.refresh()`. A `RefreshError` (revoked or
   expired-beyond-refresh token) propagates as a raw traceback. Fix: catch `RefreshError`,
   delete the stale token, and fall through to the OAuth flow. Also: the `TransportError`
   path returns *expired* creds — every later API call then fails; better to surface
   "offline" state explicitly.

## P1 — robustness (the "feels buggy" causes)

7. **Network storm on j/k scrolling** — `tui/screens/inbox.py:171`
   Every cursor highlight fires `_load_full_thread` (a full `threads().get`). The
   exclusive worker group cancels the *callback*, not the in-flight HTTP request, so
   rapid scrolling stacks up requests — matching the `TimeoutError` / `SSL:
   RECORD_LAYER_FAILURE` entries in the log. Fix: debounce highlight → load by
   ~200 ms (`set_timer`), and cache fetched threads by id (invalidate on refresh/mutation).

8. **Naive/aware datetime mix** — `gmail/models.py:151`
   `parsedate_to_datetime` returns tz-aware; the `internalDate` fallback returns a *naive
   local* datetime that display code then assumes is UTC. Fix: normalise to aware UTC at
   parse time (`datetime.fromtimestamp(..., tz=timezone.utc)`), drop the scattered
   `tzinfo` patch-ups in `message_list.py` / `message_preview.py`. Also `_format_date`'s
   `delta.days == 0` means "last 24 h", not "today" — compare calendar dates.

9. **Small inline attachments become the message body** — `gmail/models.py:216`
   A `text/plain` part with a filename but inline data (no `attachmentId`) falls through
   to the body branch — an attached `.txt`/`.log` replaces the real body. Fix: check
   `filename` first regardless of `attachmentId`; record inline attachments too.

10. **`Config.save()` writes invalid TOML** — `config.py:44`
    `repr(True)` → `True` (TOML needs `true`); next `Config.load()` would crash at
    startup. It would also flatten comments and persist the API key read from the env.
    Currently unused, but a landmine. Fix: proper TOML serialisation (or drop `save()`),
    never write `anthropic_api_key` when it came from the environment, and `chmod 600`
    config/token files on write.

11. **`$EDITOR` with arguments breaks compose** — `tui/screens/compose.py:96`
    `subprocess.run([editor, tmp_path])` treats `"code -w"` as one executable.
    Fix: `shlex.split(editor) + [tmp_path]`.

12. **All load failures report "offline"** — `tui/screens/inbox.py:157`
    Auth errors, quota errors, and bugs all show `⚠ offline`. Distinguish at least
    auth-vs-network and include a hint (`bem auth`).

## P2 — dead code, duplication, hygiene

13. **Delete `bem/ai.py`** — stale older copy of `bem/ai/commands.py`; the `bem/ai/`
    package shadows it, so it's unreachable dead code (with a different default model).
14. **Remove or fix dead `MessageList.current_thread()`** — `tui/widgets/message_list.py:119`
    confused logic (`get_row_at` result discarded); nothing calls it.
15. **Dedupe near-identical code:**
    - `_load_full_thread` / `_load_full_thread_and_preview` → one worker with a flag
      (`tui/screens/inbox.py:199`).
    - MIME building in `GmailClient.send` vs `create_draft` → shared helper; note
      `create_draft` omits the `References` header that `send` sets (`gmail/client.py`).
    - `triage` vs `triage_structured` prompt-building (`ai/commands.py`).
16. **Rich markup escaping** — `tui/widgets/message_preview.py:99`, `ai_panel.py:81`
    Hand-rolled `_e()` also escapes `]`, which Rich renders as a literal backslash —
    subjects like `[EXTERNAL]` display as `\[EXTERNAL\]`. Use `rich.markup.escape`.
    `AIPanel.set_error` writes the error unescaped (markup injection).
17. **Triage runs the model twice and may disagree with itself** — `tui/screens/inbox.py:509`
    The streamed `:triage` output and the later `triage_structured` call are independent
    requests; the applied colours can contradict the displayed analysis. Fix: one
    structured call, render text from it, reuse for labels. Also: triage silently caps at
    20 threads while the list shows 50 (`ai/commands.py:40`) — process all or say so; and
    `max_tokens=256` risks truncated JSON.
18. **Dependency pins** — `pyproject.toml` declares `textual>=0.85` but the venv runs
    Textual 8.2.7; pin realistic floors (`textual>=8,<9`, `anthropic>=0.6x`). Remove
    unused config fields (`realname`, `from_address`, `sort_threads`, `theme`) or wire
    them up.
19. **Tests** — `tests/` is empty; `test_gmail.py` is a manual smoke script (move to
    `scripts/smoke_gmail.py` so pytest doesn't collect it). Add pytest + unit tests for
    the pure functions, which is where the bugs above live: `parse_draft`,
    `build_reply_draft` (Cc cases), `_extract_body` (multipart/attachment fixtures),
    `_strip_html`, `_format_date`, `Label.sort_key`, `Config.load`, triage JSON parsing.
20. **Logging** — always-on DEBUG to an unbounded `bem.log`; make level configurable,
    add rotation (`RotatingFileHandler`).

## P3 — UX improvements (post-cleanup)

21. `/` should enter command mode pre-filled with `search ` (currently opens an empty `:` bar).
22. Search state: after `:search`, `ctrl+r` reloads the previous folder while the status
    bar still says `search: …` — track the active query.
23. Pagination: `list_threads` returns `next_page_token` but the UI ignores it — no way
    to see past the first page.
24. `:reply-draft` output is read-only; on close, offer "y = open in editor as reply"
    (reuse the AIPanel confirm mechanism).
25. Folder unread counts never refresh after mark-read/archive — reload labels after
    mutations (cheap batch) or adjust counts locally.

## Suggested order of work

1. P0 items 1–6 (each small and independently verifiable).
2. Test scaffolding (item 19) — write regression tests alongside the P0/P1 fixes.
3. P1 items 7–12.
4. P2 sweep in one pass (13–18, 20).
5. P3 as a separate UX round.
