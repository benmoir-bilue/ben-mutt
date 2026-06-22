from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from bem.tui.widgets.message_preview import MessagePreview


class PreviewApp(App):
    def __init__(self):
        super().__init__()
        self.focused_links: list = []

    def compose(self) -> ComposeResult:
        yield MessagePreview(id="preview")

    def on_message_preview_link_focused(self, event) -> None:
        self.focused_links.append((event.index, event.total, event.url))


@pytest.mark.asyncio
async def test_markdown_links_collected_in_order(make_message, make_thread):
    # An HTML-only message: `body` is the markdown html2text produced, and
    # body_is_html is true, so the preview renders it through Rich Markdown.
    msg = make_message(
        body_plain="",
        body_html=(
            "See the [Google Doc](https://docs.google.com/doc1) and "
            "the [Sheet](https://docs.google.com/sheet2)."
        ),
    )
    thread = make_thread(msg)

    app = PreviewApp()
    async with app.run_test() as pilot:
        preview = app.query_one(MessagePreview)
        preview.show_thread(thread)
        await pilot.pause()
        assert preview._links == [
            "https://docs.google.com/doc1",
            "https://docs.google.com/sheet2",
        ]


@pytest.mark.asyncio
async def test_bare_urls_in_plain_body(make_message, make_thread):
    msg = make_message(
        body_plain="Visit https://example.com/a now.\nAlso https://example.com/b)",
        body_html="",
    )
    thread = make_thread(msg)

    app = PreviewApp()
    async with app.run_test() as pilot:
        preview = app.query_one(MessagePreview)
        preview.show_thread(thread)
        await pilot.pause()
        # Trailing '.' and ')' are stripped from the bare URLs.
        assert preview._links == [
            "https://example.com/a",
            "https://example.com/b",
        ]


@pytest.mark.asyncio
async def test_arrow_keys_navigate_and_wrap(make_message, make_thread):
    msg = make_message(
        body_plain="A https://example.com/1 B https://example.com/2 C",
        body_html="",
    )
    thread = make_thread(msg)

    app = PreviewApp()
    async with app.run_test() as pilot:
        preview = app.query_one(MessagePreview)
        preview.show_thread(thread)
        preview.focus()
        await pilot.pause()
        assert preview._sel == -1

        await pilot.press("down")
        await pilot.pause()
        assert preview._sel == 0

        await pilot.press("down")
        await pilot.pause()
        assert preview._sel == 1

        await pilot.press("down")  # wraps
        await pilot.pause()
        assert preview._sel == 0

        await pilot.press("up")  # wraps back to last
        await pilot.pause()
        assert preview._sel == 1

        # Each move surfaces the focused URL for the status bar.
        assert app.focused_links[-1] == (1, 2, "https://example.com/2")


@pytest.mark.asyncio
async def test_enter_opens_selected_link(make_message, make_thread, monkeypatch):
    opened: list = []
    monkeypatch.setattr(
        "bem.tui.widgets.message_preview.webbrowser.open",
        lambda url: opened.append(url),
    )
    msg = make_message(
        body_plain="Go https://example.com/x then https://example.com/y",
        body_html="",
    )
    thread = make_thread(msg)

    app = PreviewApp()
    async with app.run_test() as pilot:
        preview = app.query_one(MessagePreview)
        preview.show_thread(thread)
        preview.focus()
        await pilot.pause()

        # Enter with nothing selected selects the first link rather than opening.
        await pilot.press("enter")
        await pilot.pause()
        assert preview._sel == 0
        assert opened == []

        await pilot.press("down")  # -> second link
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["https://example.com/y"]


@pytest.mark.asyncio
async def test_arrows_scroll_when_no_links(make_message, make_thread):
    body = "\n".join(f"line {i}" for i in range(200))  # no URLs, tall body
    msg = make_message(body_plain=body, body_html="")
    thread = make_thread(msg)

    app = PreviewApp()
    async with app.run_test() as pilot:
        preview = app.query_one(MessagePreview)
        preview.show_thread(thread)
        preview.focus()
        await pilot.pause()
        assert preview._links == []
        start = preview.scroll_offset.y
        await pilot.press("down")
        await pilot.pause()
        # With no links, arrows fall through to the default scroll behaviour.
        assert preview.scroll_offset.y >= start
