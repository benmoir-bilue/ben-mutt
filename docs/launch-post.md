A while back, the author of Mutt described his own email client like this:

> *"All mail clients suck. This one just sucks less."*

I've been a Mutt-and-vim person for years, and that line has always felt about right. So I rebuilt the idea for how I actually work now.

It's called **bem** (Ben-Mutt — yes, my initials). A terminal email client for Gmail and Google Workspace that keeps everything Mutt got right — keyboard-driven index and pager, vim muscle memory, a `:` command line, nothing more than a keystroke away — and throws out the plumbing nobody misses: mbox files, procmail, IMAP gymnastics, HTML mail rendered as a wall of `&nbsp;`.

The part I'm actually enjoying is wiring it end-to-end with Claude. Not as a gimmick bolted on the side — as the bit that does the drudgery. It triages the inbox, drafts replies in my own voice, handles calendar invites, and clears to inbox-zero on command.

And there's a copilot called Mutt (a dog, naturally) that sits in a side pane, watches new mail, and will drive the UI for you — "archive the invoice", "open the one from Anna", "what's urgent?" — and just does it.

Does it still suck? Probably. But it sucks less, and now it sucks *less in ways I chose*.

Built for me, but it's open source — have a look, steal whatever's useful: github.com/benmoir-bilue/ben-mutt
