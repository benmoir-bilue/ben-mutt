from __future__ import annotations

import sys
import click

from bem.config import Config, CREDENTIALS_FILE, CONFIG_DIR
from bem.gmail.auth import authenticate
from bem.gmail.client import GmailClient
from bem.tui.app import BemApp


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """bem — a modern Mutt-inspired terminal email client."""
    if ctx.invoked_subcommand is None:
        run()


@main.command()
def run() -> None:
    """Open the email client (default)."""
    config = Config.load()
    config.ensure_config_dir()

    from bem import log as bemlog
    bemlog.setup()

    try:
        creds = authenticate()
    except SystemExit:
        sys.exit(1)

    gmail = GmailClient(creds)
    app = BemApp(gmail=gmail, config=config)
    app.run()


@main.command()
def auth() -> None:
    """Re-run the Google OAuth2 authentication flow."""
    from bem.config import TOKEN_FILE
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        click.echo("Cleared existing token.")
    authenticate()
    click.echo("Authentication successful.")


@main.command(name="chat-spaces")
def chat_spaces() -> None:
    """List your Google Chat spaces and ids (for google_chat_space)."""
    from bem.gchat import ChatClient
    try:
        creds = authenticate()
    except SystemExit:
        sys.exit(1)
    try:
        spaces = ChatClient(creds).list_spaces()
    except Exception as e:
        click.echo(f"Couldn't list spaces: {e}")
        click.echo("Make sure the Google Chat API is enabled in your Cloud project.")
        sys.exit(1)
    if not spaces:
        click.echo("No Chat spaces found. Create one in Google Chat, add yourself, then retry.")
        return
    for s in spaces:
        click.echo(f"  {s.name}\t{s.type}\t{s.display or '(direct message)'}")
    click.echo('\nPut one in config.toml:  google_chat_space = "spaces/…"')


@main.command(name="chat-test")
def chat_test() -> None:
    """Send a test message — via the webhook if set (notifies you), else the API."""
    config = Config.load()
    from bem.gchat import ChatClient
    msg = "🐕 Mutt test — Chat pings are wired up."
    if config.google_chat_webhook:
        try:
            ChatClient(None).send_webhook(config.google_chat_webhook, msg)
        except Exception as e:
            click.echo(f"Webhook failed: {e}")
            sys.exit(1)
        click.echo("Sent via webhook. You should get a phone notification.")
        return
    if not config.google_chat_space:
        click.echo("Set google_chat_webhook (recommended) or google_chat_space first.")
        click.echo("See: bem chat-spaces, and the setup notes in: bem setup.")
        sys.exit(1)
    try:
        creds = authenticate()
    except SystemExit:
        sys.exit(1)
    try:
        ChatClient(creds).send(config.google_chat_space, msg)
    except Exception as e:
        click.echo(f"Failed: {e}")
        sys.exit(1)
    click.echo("Sent via API. NOTE: API messages are authored by you, so your "
               "phone won't notify — set google_chat_webhook for alerts.")


@main.command()
def setup() -> None:
    """Show setup instructions."""
    click.echo(f"""
bem setup
─────────
Config directory: {CONFIG_DIR}

1. Create a Google Cloud project and enable the Gmail, Calendar, and
   (optional, for Mutt's away pings) Google Chat APIs.
2. Create OAuth 2.0 Desktop credentials and download credentials.json.
3. Place credentials.json at:
     {CREDENTIALS_FILE}
4. (Optional) Set ANTHROPIC_API_KEY for AI features.
5. Run `bem` to authenticate and open your inbox.

Google Chat (optional — Mutt messages you when away, and acts on your replies):
  • Create a space (just you), then add an incoming webhook to it:
      space → Apps & integrations → Webhooks → Add webhook → name it "Mutt".
    Put its URL in config as google_chat_webhook — messages then come from the
    webhook (not you), so your phone actually NOTIFIES you.
  • For two-way (Mutt reading your replies): also run `bem chat-spaces`, and set
    google_chat_space to that space's id.
  • `bem chat-test` sends a test message; reply in the space with instructions
    ("archive the invoice") and Mutt picks them up on his next poll.

Config file: {CONFIG_DIR / "config.toml"}
Example config:

  editor = "nvim"
  threads_per_page = 50
  theme = "green"   # dark | light | green (1980s phosphor CRT)
  google_chat_webhook = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=…"
  google_chat_space = "spaces/AAAA…"   # bem chat-spaces to find yours
""")


@main.command()
def version() -> None:
    """Print version."""
    from bem import __version__
    click.echo(f"bem {__version__}")


if __name__ == "__main__":
    main()
