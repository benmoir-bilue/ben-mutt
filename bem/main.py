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


@main.command()
def setup() -> None:
    """Show setup instructions."""
    click.echo(f"""
bem setup
─────────
Config directory: {CONFIG_DIR}

1. Create a Google Cloud project and enable the Gmail API.
2. Create OAuth 2.0 Desktop credentials and download credentials.json.
3. Place credentials.json at:
     {CREDENTIALS_FILE}
4. (Optional) Set ANTHROPIC_API_KEY for AI features.
5. Run `bem` to authenticate and open your inbox.

Config file: {CONFIG_DIR / "config.toml"}
Example config:

  editor = "nvim"
  threads_per_page = 50
  theme = "dark"
""")


@main.command()
def version() -> None:
    """Print version."""
    from bem import __version__
    click.echo(f"bem {__version__}")


if __name__ == "__main__":
    main()
