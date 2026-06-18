"""Copilot memory — plain, human-editable markdown the agent reads and injects.

Deliberately simple: no database, no framework. Each fact lives in a markdown
file under the profile's config dir, so Ben can open and edit them by hand and
see exactly what Mutt knows. (A Strands MemoryManager may wrap these later; it
isn't needed for the value here.)

- focus.md : what Ben is prioritising right now — set via ``:focus``, timestamped
             so a stale focus can prompt a re-ask.
- vips.md  : senders / domains / names that always jump the priority queue.
- rules.md : standing filing/handling rules (shared with the sorting agent).

The Curator and chat agents call :func:`memory_context` to fold these into their
prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bem.config import FOCUS_FILE, VIPS_FILE, RULES_FILE

# A focus older than this is treated as possibly stale — the UI can nudge Ben to
# refresh it rather than silently ranking against last week's priorities.
FOCUS_STALE_DAYS = 7

_STAMP_RE = re.compile(r"<!--\s*set:\s*(?P<stamp>[^\s]+)\s*-->")


@dataclass
class Focus:
    """Ben's current declared priority, with when it was set."""
    text: str
    set_at: Optional[datetime]  # None if the stamp is missing/corrupt

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        if self.set_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, (now - self.set_at).days)

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        age = self.age_days(now)
        return age is None or age > FOCUS_STALE_DAYS


def save_focus(text: str, now: Optional[datetime] = None) -> None:
    """Record Ben's current focus, stamped with the time it was set."""
    now = now or datetime.now(timezone.utc)
    FOCUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    FOCUS_FILE.write_text(f"<!-- set: {stamp} -->\n{text.strip()}\n", encoding="utf-8")


def load_focus() -> Optional[Focus]:
    """Read the current focus, or None when none is set."""
    try:
        raw = FOCUS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    set_at: Optional[datetime] = None
    first = raw.splitlines()[0]
    match = _STAMP_RE.search(first)
    if match:
        try:
            set_at = datetime.fromisoformat(match.group("stamp"))
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=timezone.utc)
        except ValueError:
            set_at = None
        raw = "\n".join(raw.splitlines()[1:]).strip()
    if not raw:
        return None
    return Focus(text=raw, set_at=set_at)


def clear_focus() -> None:
    try:
        FOCUS_FILE.unlink()
    except OSError:
        pass


def load_vips() -> list[str]:
    """VIP matchers — one per non-empty, non-heading, non-comment line of vips.md.

    Each line is a substring matched (case-insensitively) against a thread's
    sender, e.g. ``priya@northwind.example``, ``@globex.example``, or ``Marie``.
    Anything after a ``#`` or ``-`` bullet marker is treated as the matcher;
    trailing ``— note`` text is ignored.
    """
    try:
        raw = VIPS_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        # Keep just the matcher; drop a trailing "— why this is a VIP" note.
        matcher = re.split(r"\s[—–-]\s", line, maxsplit=1)[0].strip()
        if matcher:
            out.append(matcher)
    return out


def add_vip(matcher: str) -> bool:
    """Append a VIP matcher to vips.md (deduped). Returns True if it was added."""
    matcher = matcher.strip().lstrip("-*").strip()
    if not matcher:
        return False
    if matcher.lower() in {v.lower() for v in load_vips()}:
        return False
    VIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = VIPS_FILE.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(VIPS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{sep}- {matcher}\n")
    return True


def load_rules() -> str:
    """The standing filing/handling rules, or a placeholder when none exist."""
    try:
        text = RULES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "(none yet)"


def memory_context(now: Optional[datetime] = None) -> str:
    """A compact block of everything Mutt should keep in mind, for prompt
    injection into the Curator and chat agents."""
    parts: list[str] = []
    focus = load_focus()
    if focus and focus.text:
        age = focus.age_days(now)
        when = f" (set {age}d ago)" if age is not None else ""
        stale = " — possibly stale, confirm with Ben if it conflicts" if focus.is_stale(now) else ""
        parts.append(f"BEN'S CURRENT FOCUS{when}{stale}:\n{focus.text}")
    else:
        parts.append("BEN'S CURRENT FOCUS: (none set — rank on general importance)")
    vips = load_vips()
    if vips:
        parts.append("VIP SENDERS (always surface): " + ", ".join(vips))
    rules = load_rules()
    if rules and rules != "(none yet)":
        parts.append(f"STANDING RULES:\n{rules}")
    return "\n\n".join(parts)
