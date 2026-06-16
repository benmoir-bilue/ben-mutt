"""Folder tips: a cached map of what lives in each Gmail folder.

The :tips agent scans every folder once and records the people, companies
and topics found there. Later :sort runs feed this file to the agent instead
of re-sampling labels, which saves most of the turn budget. Tips carry their
generation time; after MAX_AGE_DAYS a :sort suggests refreshing them
(:tips) or overriding (:sort!).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bem.config import TIPS_FILE

MAX_AGE_DAYS = 30

_STAMP_RE = re.compile(r"<!--\s*generated:\s*(?P<stamp>[^\s]+)\s*-->")


@dataclass
class Tips:
    content: str
    generated_at: Optional[datetime]  # None if the stamp is missing/corrupt

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        if self.generated_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, (now - self.generated_at).days)

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        age = self.age_days(now)
        return age is None or age > MAX_AGE_DAYS


def save_tips(content: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    TIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    TIPS_FILE.write_text(
        f"<!-- generated: {stamp} -->\n{content.strip()}\n", encoding="utf-8"
    )


def load_tips() -> Optional[Tips]:
    """Read the tips file. Returns None when no tips have been saved yet."""
    try:
        raw = TIPS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    generated_at: Optional[datetime] = None
    match = _STAMP_RE.search(raw.splitlines()[0])
    if match:
        try:
            generated_at = datetime.fromisoformat(match.group("stamp"))
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
        except ValueError:
            generated_at = None
        raw = "\n".join(raw.splitlines()[1:]).strip()
    return Tips(content=raw, generated_at=generated_at)
