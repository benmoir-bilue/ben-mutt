"""Mutt-style thread tree: derive the reply structure of a thread from its
In-Reply-To / References headers and flatten it to drawable rows.

Gmail's API returns a thread as a flat, date-ordered message list — the tree
has to be reconstructed the same way Mutt does it, from the RFC 2822
threading headers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bem.gmail.models import Message, Thread


@dataclass
class TreeRow:
    message: Message
    prefix: str     # ASCII tree drawing for the index, e.g. "│ └─>"
    parent_id: str  # Gmail id of the parent message, "" for roots


_MSG_ID_RE = re.compile(r"<[^<>\s]+>")


def thread_tree(thread: Thread) -> list[TreeRow]:
    """Flatten a thread into depth-first tree rows with Mutt-style prefixes.

    A message's parent is the thread message named by its In-Reply-To header,
    falling back to the nearest References entry found in the thread (replies
    that arrived via a mailing list often only carry References). Messages
    with no resolvable parent become roots; siblings sort by date.
    """
    msgs = thread.messages
    by_header = {m.message_id_header: m.id for m in msgs if m.message_id_header}

    parent: dict[str, str] = {}
    for m in msgs:
        candidates = _MSG_ID_RE.findall(m.in_reply_to)
        candidates += reversed(_MSG_ID_RE.findall(m.references))
        parent[m.id] = next(
            (by_header[ref] for ref in candidates
             if by_header.get(ref) not in (None, m.id)),
            "",
        )

    # Malformed headers can create reference cycles; cut one link per cycle
    # so the walk below always terminates.
    for m in msgs:
        seen = {m.id}
        cur = parent[m.id]
        while cur:
            if cur in seen:
                parent[m.id] = ""
                break
            seen.add(cur)
            cur = parent[cur]

    roots: list[Message] = []
    children: dict[str, list[Message]] = {}
    for m in msgs:
        if parent[m.id]:
            children.setdefault(parent[m.id], []).append(m)
        else:
            roots.append(m)
    by_date = lambda m: (m.date, m.id)
    roots.sort(key=by_date)
    for kids in children.values():
        kids.sort(key=by_date)

    rows: list[TreeRow] = []

    def visit(m: Message, cont: str, branch: str) -> None:
        rows.append(TreeRow(message=m, prefix=cont + branch, parent_id=parent[m.id]))
        kids = children.get(m.id, [])
        # Roots contribute nothing to their children's continuation; a last
        # sibling ("└") leaves a gap below it, others a "│" rail.
        child_cont = cont if not branch else cont + ("  " if branch.startswith("└") else "│ ")
        for i, kid in enumerate(kids):
            visit(kid, child_cont, "└─>" if i == len(kids) - 1 else "├─>")

    for root in roots:
        visit(root, "", "")
    return rows
