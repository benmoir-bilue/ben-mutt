# Mutt → "Chief of Staff" — high-level design (v0.1, proposed)

> Status: **proposed for sign-off**. No code until the build plan is approved.
> This supersedes the per-email "triage feed" copilot.

## 1. Vision in one line
A presence-aware **Chief of Staff** that continuously answers: *"What is the single
most important thing for Ben to do right now — and can I pre-stage it?"* — then
shows **one hero item + 3 on-deck**, weighted by **Ben's declared focus**, and
helps drive to inbox-zero when nothing is urgent.

## 2. Decisions (locked)
| Area | Decision |
|---|---|
| Priorities | Ben **declares a focus** (`:focus …`); agent ranks against it, asks when unsure |
| Surface | **One hero + 3 on-deck** (single decision at a time) |
| Autonomy when **away** | **Read-only**: triage + briefing only. Drafting/filing happen **only when attended + approved** (safe-mode unchanged) |
| Memory | **Markdown only** (focus/VIPs/rules, human-editable), injected into prompts. graphify dropped — the model resolves relationships from email text + injected focus/VIPs well enough; revisit a graph only if recall disappoints. |
| Ranking models | **Haiku scores/filters the whole inbox**; **Sonnet writes the hero rationale + next action** for the top few only (cost/quality balance) |
| Attended nudge | New hero ⇒ **quiet panel update + heartbeat blip**; never steals focus or yanks the view |

## 3. Hierarchy of needs (= build order)
1. **Liveness** — actually runs, refreshes the inbox, *visibly* works (heartbeat + animations). *Currently broken; fix first.*
2. **Awareness** — trustworthy current read of the whole inbox (new / unread / aging / VIP / threads I'm in).
3. **Prioritisation** — rank everything vs my focus → the one thing + on-deck.
4. **Action** — draft / RSVP / file / batch. Save real minutes.
5. **Autonomy & foresight** — presence-aware initiative, inbox-zero drives when calm, follow-up tracking, learning.

## 4. Architecture

### 4.1 Presence awareness (new)
- macOS idle time via `ioreg -c IOHIDSystem` → `HIDIdleTime`. Idle < threshold (default **90 s**) ⇒ **attended**; else **away**.
- **Attended:** live hero + on-deck, animations, offers to act; drafting/filing available via the existing safe-mode approval flow.
- **Away:** read-only. Curator keeps ranking in the background and accumulates a **"while you were out" briefing**. No mutations, no drafts.
- **Transition away→attended:** lead with the briefing — "While you were away: N new · here's the hero · what changed."

### 4.2 Agents (Strands)
- **Curator (ranking) agent** — periodic, presence-aware cadence. Input: inbox snapshot + focus + VIPs + memory. Output (structured): ranked items → **1 hero + 3 on-deck**, each with *why it matters*, *suggested next action*, *bem keystroke*, *freshness*. Cheap model for first-pass scoring, smart model for the hero rationale. **Replaces** the per-email Haiku triage.
- **Chat/Action agent** — the Strands chat agent already built (`CopilotBrain.chat` + `build_copilot_tools`). Unchanged in spirit; gains memory injection.
- (Future) orchestrator via agents-as-tools; not in v1.

### 4.3 Ranking model (the core value)
Composite score per thread:
- **Focus match** (entity tie to a declared focus, resolved via graph) — high weight
- **VIP sender** (board / investor / key customer / family) — high weight
- **Direct ask / waiting-on-Ben** (addressed, question posed) — high weight
- **Recency / decay** — newer wins; old unactioned mail decays (half-life default **5 days**; old ≈ stale)
- **Deadline / time-sensitivity** (explicit dates, calendar conflicts)
- **Thread participation** (Ben already in/replied)
- **Negative** — newsletters / automated / notifications down-weighted
→ Hero = top composite (+ confidence); on-deck = next 3; model emits a crisp rationale + the single next action.

### 4.4 Memory (plain markdown, injected into prompts)
Per-profile in the config dir (`~/.config/bem[-mutt]/`):
- `focus.md` — current focus, timestamped (re-ask when stale, default **7 days**)
- `vips.md` — VIP senders/domains + why
- `rules.md` — filing/handling rules (existing)

`memory.memory_context()` folds these into the Curator and chat prompts. The model
resolves "is this sender part of Globex?" from the email text + injected focus/VIPs —
no graph needed. **graphify dropped**; revisit a relationship graph only if ranking
recall proves insufficient. No `MemoryManager`/`FileSessionManager` either — the
markdown files *are* the durable state, and they're human-editable.

### 4.5 Liveness & feel (layer 1 — fix first)
- **Inbox refresh bug:** current poll only repaints when `_viewing_inbox() and _is_idle()`, so an active user on the inbox never sees updates. Fix so the inbox reliably refreshes and monitoring is visible.
- **Heartbeat:** persistent panel status — `🐕 sniffing… (last sniff 12s · 3 new today)` while a pass runs; idle shows `on watch · next sniff 45s`. Rotating status words + spinner (Claude-Code style).
- **Presence indicator:** `👀 here` vs `💤 away — I'll brief you when you're back`.

### 4.6 UI (CopilotPanel rework)
- **HERO card** (top): the one thing — why · next action · keystroke, prominent.
- **On-deck (3):** compact list.
- **Heartbeat/status line.**
- **Chat line** (existing) to talk to Mutt.
- On return from away: briefing prepended.

### 4.7 Commands
- `:focus <text>` — set current focus · `:focus` — show/clear
- `:brief` — show the while-you-were-out briefing on demand
- existing `:mutt`, `t` (talk), Esc to leave

### 4.8 Grouping & inbox-zero
- When attended and nothing urgent: offer a **tidy batch** — group similar low-value items ("12 newsletters — archive all?") into one approve action. Drives toward inbox-zero without per-item clicks.

## 5. Secondary defaults (tunable, not blocking)
- Away idle threshold: **90 s** · Focus staleness re-ask: **7 days** · Mail decay half-life: **5 days**
- OS notifications when away: **off in v1** (panel + briefing only; client is open most of the time)

## 6. Touch points in current code
- `bem/ai/copilot.py` — add `curate()` ranking pass; keep `chat()`; memory injection
- `bem/ai/presence.py` *(new)* — macOS idle detection
- `bem/ai/memory.py` — plain markdown stores (focus/VIPs/rules) + prompt-injection context
- `bem/tui/widgets/copilot_panel.py` — hero card + heartbeat + briefing
- `bem/tui/screens/inbox_copilot.py` — presence-aware cadence, briefing on return, `:focus`/`:brief`, **inbox-refresh fix**
- `bem/config.py` — thresholds, memory paths
