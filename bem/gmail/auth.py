from __future__ import annotations

import sys

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from bem.config import CREDENTIALS_FILE, TOKEN_FILE, GOOGLE_SCOPES, CONFIG_DIR


def authenticate() -> Credentials:
    """Return valid credentials, refreshing or prompting as needed."""
    creds = _load_token()

    # A token granted before a scope was added stays "valid", but calls needing
    # the new scope would 403. Force a fresh consent when scopes are missing.
    if creds and not _has_required_scopes():
        print("bem: additional Google permissions needed (Calendar/Chat), re-authenticating")
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except TransportError:
            # Offline — return stale creds and let the TUI handle it gracefully
            print("bem: network unavailable, starting with cached credentials")
            return creds
        except RefreshError:
            # Token revoked or otherwise unusable — discard it and re-run the flow
            print("bem: saved token is no longer valid, re-authenticating")
            TOKEN_FILE.unlink(missing_ok=True)
            creds = None

    if not CREDENTIALS_FILE.exists():
        _print_setup_instructions()
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    return creds


def _load_token() -> Credentials | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    except Exception:
        return None


def _has_required_scopes() -> bool:
    """True when the saved token was granted every scope in GOOGLE_SCOPES.
    Reads the granted scopes straight from the token file rather than trusting
    the requested-scopes list passed when loading the credential."""
    try:
        import json
        data = json.loads(TOKEN_FILE.read_text())
    except Exception:
        return False
    granted = set(data.get("scopes") or [])
    return set(GOOGLE_SCOPES).issubset(granted)


def _save_token(creds: Credentials) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def _print_setup_instructions() -> None:
    print("""
bem: Google credentials not found at:
  {path}

To set up bem:

  1. Go to https://console.cloud.google.com/
  2. Create a new project (or select an existing one)
  3. Enable the Gmail API: APIs & Services → Enable APIs → Gmail API
  4. Create OAuth 2.0 credentials:
       APIs & Services → Credentials → Create Credentials → OAuth client ID
       Application type: Desktop app
  5. Download the JSON file and save it to:
       {path}
  6. Run `bem` again to complete authentication

""".format(path=CREDENTIALS_FILE))
