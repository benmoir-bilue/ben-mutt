"""
Quick smoke test for the Gmail integration.
Run with: .venv/bin/python test_gmail.py
"""
import sys
import traceback

def step(msg):
    print(f"\n{'─'*60}\n▶ {msg}")

def ok(msg=""):
    print(f"  ✓ {msg}" if msg else "  ✓ OK")

def fail(e):
    print(f"  ✗ FAILED: {e}")
    traceback.print_exc()
    sys.exit(1)


step("1. Loading config")
try:
    from bem.config import Config, CREDENTIALS_FILE, TOKEN_FILE
    cfg = Config.load()
    print(f"  credentials.json: {'EXISTS' if CREDENTIALS_FILE.exists() else 'MISSING ← run bem setup'}")
    print(f"  token.json:       {'EXISTS' if TOKEN_FILE.exists() else 'not yet (will be created)'}")
    print(f"  editor:           {cfg.editor}")
    print(f"  anthropic key:    {'set' if cfg.anthropic_api_key else 'not set (AI features disabled)'}")
    ok()
except Exception as e:
    fail(e)


step("2. OAuth2 authentication")
try:
    from bem.gmail.auth import authenticate
    creds = authenticate()
    ok(f"token valid: {creds.valid}, expired: {creds.expired}")
except SystemExit:
    print("  ✗ credentials.json not found — see bem setup")
    sys.exit(1)
except Exception as e:
    fail(e)


step("3. Gmail API — get profile")
try:
    from bem.gmail.client import GmailClient
    gmail = GmailClient(creds)
    profile = gmail.get_profile()
    ok(f"logged in as: {profile.get('emailAddress')}")
    print(f"  messages total: {profile.get('messagesTotal', '?')}")
except Exception as e:
    fail(e)


step("4. List labels")
try:
    labels = gmail.list_labels()
    ok(f"{len(labels)} labels found")
    for l in labels[:8]:
        unread = f" ({l.messages_unread} unread)" if l.messages_unread else ""
        print(f"  [{l.type:6}] {l.display_name}{unread}")
    if len(labels) > 8:
        print(f"  ... and {len(labels) - 8} more")
except Exception as e:
    fail(e)


step("5. List inbox threads (first 5)")
try:
    threads, next_token = gmail.list_threads(label_id="INBOX", max_results=5)
    ok(f"{len(threads)} threads fetched")
    for t in threads:
        unread = "●" if t.is_unread else " "
        date = t.date.strftime("%d %b %H:%M") if t.date else "?"
        print(f"  {unread} [{date}] {t.sender[:20]:<20}  {t.subject[:50]}")
except Exception as e:
    fail(e)


if not threads:
    print("\n  Inbox is empty — skipping thread detail test")
    sys.exit(0)


step("6. Fetch full thread (first result)")
try:
    first = threads[0]
    full = gmail.get_thread(first.id)
    ok(f"thread '{full.subject}' has {full.message_count} message(s)")
    for i, msg in enumerate(full.messages):
        body_preview = msg.body[:80].replace("\n", " ")
        print(f"  msg {i+1}: from={msg.display_from}, body={body_preview!r}")
        if msg.attachments:
            print(f"         attachments: {[a.filename for a in msg.attachments]}")
except Exception as e:
    fail(e)


step("7. All checks passed ✓")
print(f"\n  Gmail integration looks good. Run `bem` to open the TUI.\n")
