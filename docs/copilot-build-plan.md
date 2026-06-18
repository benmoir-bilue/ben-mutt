# Mutt "Chief of Staff" — build plan (proposed, for check)

> Status: **awaiting Ben's approval**. Sibling of `copilot-design.md`.
> Built in hierarchy-of-needs order; **each phase ships and is tested on its own**.
> All work lands on a feature branch; tested against the `bem-mutt` profile.

## Guiding rules
- Every phase is independently shippable + has tests; we stop and look after each.
- Reuse what exists: the Strands chat agent (`CopilotBrain.chat`, `build_copilot_tools`) stays; we add a ranking pass and a memory/presence layer around it.
- Safe-mode contract unchanged: no autonomous sends; away = strictly read-only.

## Phase 0 — Foundations (plumbing) · ~small
**Goal:** config + memory + the `:focus` command, so later phases have inputs.
- `bem/config.py`: add thresholds (away idle 90s, focus-stale 7d, decay half-life 5d) + memory dir.
- `bem/ai/memory.py` *(new)*: read/write markdown stores `focus.md`, `vips.md`, `rules.md` (exists), `voice.md`; a `Memory` accessor with timestamps + staleness.
  - **Plan note (memory abstraction):** v1 reads md + injects into prompts directly (simple, transparent). We adopt Strands `MemoryManager` (search/add tools + auto-injection) in Phase 5 *if* it earns its keep — flagged so we don't over-build early.
- `:focus <text>` set · `:focus` show/clear → writes `focus.md`.
**Tests:** memory read/write/staleness; `:focus` round-trip.
**Ship check:** `:focus closing Globex` persists and shows back.

## Phase 1 — Liveness (the "it's alive" fix) · **first, highest pain**
**Goal:** it visibly works and the inbox updates.
- **Inbox-refresh fix:** rework `_on_copilot_fetch` so new mail repaints the list without requiring `_is_idle` — preserve cursor/selection, never yank mid-navigation, but always reflect new arrivals + counts.
- `bem/ai/presence.py` *(new)*: macOS `HIDIdleTime` → attended/away; cheap, on a timer; graceful non-mac fallback (assume attended).
- **Heartbeat** in `CopilotPanel`: persistent status line — `🐕 sniffing… · 3 new today` during a pass, `on watch · next sniff 45s` idle; spinner + rotating words; presence indicator (`👀 here` / `💤 away`).
- Presence-aware poll cadence (extend `poll_interval`).
**Tests:** refresh updates list w/ cursor kept; presence parse (mock `ioreg`); heartbeat formatting; cadence.
**Ship check:** open `bem-mutt`, watch it sniff on a timer, see new seeded mail appear.

## Phase 2 — Awareness + Prioritisation (the Curator) · **core value**
**Goal:** rank the whole inbox → 1 hero + 3 on-deck.
- `CopilotBrain.curate(threads, focus, vips, …)`:
  - **Haiku pass:** score candidates (new + aging unread + threads I'm in) on focus-match, VIP, waiting-on-Ben, recency/decay, deadline, participation; − newsletters/automated.
  - **Sonnet pass:** for the top ~4, write hero rationale + single next action + bem keystroke; pick the hero (+ confidence).
  - Structured output (Strands schema) → typed `Ranking{hero, on_deck[3]}`.
- Entity match v1 = lightweight (sender/domain/name vs focus text + `vips.md`); graph-backed match deferred to Phase 5.
- Replace the per-email triage batch with a `curate` pass.
**Tests:** deterministic ranking with a fake model; focus weighting; decay ordering; hero selection; structured-output validation.
**Ship check:** with `:focus Globex`, a Globex thread becomes the hero over a newer newsletter.

## Phase 3 — Surface (panel rework)
**Goal:** the one-thing UI.
- `CopilotPanel`: **HERO card** (why · next action · keystroke) + **on-deck (3)** + heartbeat + chat line.
- Attended nudge = quiet hero refresh + heartbeat blip; **no focus steal** (assert in tests).
**Tests:** renders hero/on-deck from a `Ranking`; new hero updates card without moving focus.
**Ship check:** hero card reads cleanly; typing in the list is never interrupted.

## Phase 4 — Away mode + briefing
**Goal:** quiet while away, brief on return.
- While away: curator keeps ranking, **accumulates** a briefing (read-only; no drafts/files).
- away→attended transition: prepend **"while you were out"** (N new · hero · what changed).
- `:brief` to show on demand.
**Tests:** briefing accumulation; transition presents it; **no mutations occur while away** (guard test).
**Ship check:** lock screen ~2 min, return → briefing greets you.

## Phase 5 — Action, inbox-zero & learning (polish) · **markdown-native, no graphify**
**Goal:** save minutes + get smarter, all on plain markdown.
- **Tidy batch:** when attended + nothing urgent, Mutt offers to clear obvious noise
  (newsletters/automated/notifications, or curate items flagged archive/delete) in one
  go via the existing copilot archive path (with undo) — "12 newsletters, archive them?".
- **Learning (markdown):** `:vip <matcher>` to add a VIP (writes `vips.md`); existing
  `:rule` for filing rules; Mutt can add a VIP/rule when asked in chat. Explicit and
  inspectable — no magic auto-learning.
**Tests:** tidy selection picks only low-value mail; `:vip` round-trip; VIP feeds ranking.
**Ship check:** after catch-up, "tidy up" clears the newsletter pile in one undo-able step.

> **graphify dropped.** The Curator resolves relationships from email text + injected
> focus/VIPs (proven in the Phase-2 smoke test). Revisit a relationship graph only if
> ranking recall disappoints. No `MemoryManager`/`FileSessionManager` — markdown files
> are the durable, human-editable state.

## Sequencing & risk
- **Order:** 0 → 1 → 2 → 3 → 4 → 5. Phase 1 first (kills the "feels dead" pain immediately).
- **Models:** Haiku (score) + Sonnet (hero) per the locked decision; one Sonnet call per pass over ≤4 items keeps cost low.
- **Each phase = its own commit/PR-sized chunk**, tests green, quick look before the next.
