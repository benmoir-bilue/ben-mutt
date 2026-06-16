from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from bem.gmail.models import Thread, Message


COMPOSE_TEMPLATE = """\
To: {to}
Cc: {cc}
Subject: {subject}

{body_prefix}"""

REPLY_QUOTE_HEADER = "On {date}, {from_} wrote:"


def build_reply_draft(
    thread: Thread,
    reply_all: bool = False,
    my_address: str = "",
    body: str = "",
    message: Optional[Message] = None,
) -> str:
    """Build a reply draft. `body` pre-fills the reply text (e.g. an AI draft)
    above the quoted original. `message` targets a specific message in the
    thread (Mutt-style mid-thread reply); default is the newest message."""
    last = message or thread.last_message
    if not last:
        return COMPOSE_TEMPLATE.format(to="", cc="", subject="", body_prefix="")

    me = my_address.lower()
    if me and last.from_address.lower() == me:
        # Replying to my own message: keep addressing the original recipients
        to_addrs = [a for a in last.to if a.lower() != me] or [last.from_address]
    else:
        to_addrs = [last.from_address]

    cc_addrs: list[str] = []
    if reply_all:
        seen = {a.lower() for a in to_addrs} | {me}
        for addr in last.to + last.cc:
            if addr.lower() not in seen:
                cc_addrs.append(addr)
                seen.add(addr.lower())

    subj = last.subject
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"

    date_str = last.date.astimezone().strftime("%a %d %b %Y %H:%M") if last.date else ""
    from_str = f"{last.display_from} <{last.from_address}>" if last.from_name else last.from_address
    quote_header = REPLY_QUOTE_HEADER.format(date=date_str, from_=from_str)
    quoted_body = "\n".join(f"> {line}" for line in last.body.splitlines()[:100])

    reply_text = f"\n{body}\n" if body else "\n"
    return COMPOSE_TEMPLATE.format(
        to=", ".join(to_addrs),
        cc=", ".join(cc_addrs),
        subject=subj,
        body_prefix=f"{reply_text}\n{quote_header}\n{quoted_body}",
    )


def build_forward_draft(thread: Thread, message: Optional[Message] = None) -> str:
    last = message or thread.last_message
    if not last:
        return COMPOSE_TEMPLATE.format(to="", cc="", subject="", body_prefix="")

    subj = last.subject
    if not subj.lower().startswith("fwd:"):
        subj = f"Fwd: {subj}"

    date_str = last.date.astimezone().strftime("%a %d %b %Y %H:%M") if last.date else ""
    separator = "-" * 60
    header = (
        f"{separator}\n"
        f"Forwarded message\n"
        f"From: {last.display_from} <{last.from_address}>\n"
        f"Date: {date_str}\n"
        f"Subject: {last.subject}\n"
        f"{separator}\n"
    )

    return COMPOSE_TEMPLATE.format(
        to="",
        cc="",
        subject=subj,
        body_prefix=f"\n\n{header}{last.body[:3000]}",
    )


def build_new_draft() -> str:
    return COMPOSE_TEMPLATE.format(to="", cc="", subject="", body_prefix="")


def launch_editor(
    draft_text: str, editor: str, treat_unchanged_as_cancel: bool = True
) -> Optional[str]:
    """Open $editor with the draft. Returns edited content, or None if aborted.

    Abort detection: if the file is unchanged after the editor exits, the user
    quit without saving (e.g. :q! in vim), so we treat it as a cancellation.
    Pass treat_unchanged_as_cancel=False when the draft is already complete
    (e.g. reviewing an AI draft) and saving it unmodified is a valid outcome.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".eml", delete=False, encoding="utf-8"
    ) as f:
        f.write(draft_text)
        tmp_path = f.name

    try:
        # $EDITOR may include arguments, e.g. "code -w"
        result = subprocess.run(shlex.split(editor) + [tmp_path])
        if result.returncode != 0:
            return None
        content = Path(tmp_path).read_text(encoding="utf-8")
        # Treat unchanged file as cancellation (user did :q! or similar)
        if treat_unchanged_as_cancel and content == draft_text:
            return None
        return content
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Lines beginning with this marker are bem's own notes (e.g. the AI-draft
# disclaimer) — shown in the editor but stripped before the email is sent.
AI_DRAFT_MARKER = "[ai-draft]"


def parse_draft(content: str) -> tuple[str, str, str, str]:
    """Parse an edited draft file into (to, cc, subject, body)."""
    lines = content.splitlines()
    headers: dict[str, str] = {}
    body_start = 0

    for i, line in enumerate(lines):
        if line == "":
            body_start = i + 1
            break
        if ":" in line:
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()
        else:
            body_start = i
            break

    body_lines = [
        ln for ln in lines[body_start:]
        if not ln.lstrip().lower().startswith(AI_DRAFT_MARKER)
    ]
    body = "\n".join(body_lines).strip()
    return (
        headers.get("to", ""),
        headers.get("cc", ""),
        headers.get("subject", "(no subject)"),
        body,
    )
