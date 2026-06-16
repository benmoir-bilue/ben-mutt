"""Helpers shared between inbox.py and its screen mixins, kept in a neutral
module so the mixins need not import the screen (which would cycle)."""
from __future__ import annotations

from typing import Optional

from bem.gmail.models import Label as GmailLabel


def _match_label(labels: list[GmailLabel], name: str) -> Optional[GmailLabel]:
    """Resolve a user-typed (or model-suggested) label name, case-insensitively."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    return next((l for l in labels if l.name.lower() == wanted), None)


def _describe_error(e: Exception) -> str:
    """Turn a worker exception into a message that says what to do about it."""
    from googleapiclient.errors import HttpError

    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", None)
        if status in (401, 403):
            return "Gmail authorisation failed — run `bem auth` to re-authenticate"
        return f"Gmail API error ({status}): {e.reason if hasattr(e, 'reason') else e}"
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return f"Network error: {e}"
    return f"{type(e).__name__}: {e}"
