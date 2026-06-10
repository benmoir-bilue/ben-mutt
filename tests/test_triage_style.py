from __future__ import annotations

import pytest

from bem.tui.screens.inbox import _triage_heading_style


@pytest.mark.parametrize(
    "line,style",
    [
        ("ACTION NEEDED", "bold red"),
        ("ACTION NEEDED:", "bold red"),
        ("WAITING FOR REPLY", "yellow"),
        ("FYI / LOW PRIORITY", "dim"),
        ("FYI/LOW PRIORITY:", "dim"),
        ("CAN ARCHIVE", "dim italic"),
        ("can archive", "dim italic"),
    ],
)
def test_headings_get_row_colours(line, style):
    assert _triage_heading_style(line) == style


@pytest.mark.parametrize(
    "line",
    [
        "",
        "1 — Navigation Notes deadline in 20 days",
        "20 — Sydney Airport parking confirmation (completed)",
        "Some other commentary line",
        "13 — Intro request from Ruth Neech (CTO visiting Friday)",
    ],
)
def test_non_headings_unstyled(line):
    assert _triage_heading_style(line) is None
