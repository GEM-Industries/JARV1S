# ContentWidget Expansion

**Status:** Proposed
**Date:** 2026-04-19
**Priority:** Deferred — not on the critical path. AEC, installability, and Phase 9/10 outrank this. Each step here is independently shippable; the plan can stop after any step that earns its keep.
**Depends on:** [`UI_ACTION_BUS.md`](./UI_ACTION_BUS.md) (Steps 2–4 are inert without it).

---

## Problem

`ContentWidget` already covers the majority of structured display: `markdown`, `table`, `list`, `code`, `kv`, `metric`. Looking at what plugins actually push today and what they'd plausibly push next, four real gaps remain:

1. **No interactive blocks.** Users can read a list but can't tap an item to act on it.
2. **No charts.** Forecasts, sleep rings, system load, stock tickers all want a sparkline or line chart and currently get a number with text.
3. **No images.** Album art, weather icons in forecasts, integration logos in `ConnectWidget`, document thumbnails — all currently absent or hand-built per widget.
4. **No partial-update lifecycle.** A widget either gets re-pushed wholesale (flicker, loses scroll position) or stays static. Append-as-you-go cases (a background task's growing tool log, a stock that ticks) don't have a primitive.

That's the actual delta. Not a 16-primitive grammar, not a rebrand — four surgical additions to a structure that already works.

This proposal is explicitly **not**:
- A rename to "Canvas" or "Block Kit." `ContentWidget` is fine.
- A 16-primitive vocabulary. The existing 6 + 4 new = 10 is the cap; if a real use case appears that needs an 11th, add it then.
- A Component Catalog dev page. Iterate on visuals by pushing them from a real plugin in a DEV session; don't build separate infrastructure to render fake examples.
- A unified layout grammar with `stack` / `row` / `card`. ContentWidget already lays out vertically with a header; if a future use case wants horizontal grouping, add a single `row` block then.

The grammar-unification question can be revisited in six months once there's actual usage data showing where ContentWidget is the constraint. Today that data doesn't exist.

---

## The four additions

### 1. Action-aware list items (depends on `UI_ACTION_BUS.md`)

Today's `ListSection`:

```ts
{ type: 'list', items: string[], ordered?: boolean }
```

Becomes:

```ts
type ListItem = string | {
  text: string;
  status?: 'good' | 'warn' | 'crit' | 'info';
  checked?: boolean;            // renders as a checkbox
  action?: { name: string; params?: Record<string, unknown> };  // ui_action key
};

{ type: 'list', items: ListItem[], ordered?: boolean }
```

Bare strings still work — pure additive change to existing widgets. A list item with an `action` becomes tappable and dispatches through the UI Action Bus on click. A `checked` field renders a checkbox; the action handler returns a `{patch}` to flip it.

This single change unlocks: todo complete, mark-as-read, dismiss alert, archive email row, "skip" / "snooze" inline.

### 2. `button` and `input` sections (depends on `UI_ACTION_BUS.md`)

```ts
{ type: 'button', label: string, action: { name: string; params?: ... },
  intent?: 'primary' | 'secondary' | 'danger', icon?: IconName }

{ type: 'input', name: string, placeholder?: string,
  kind?: 'text' | 'number',
  submit_action: { name: string; params?: ... } }
```

`button` covers: "Reconnect," "Open in browser," "Cancel task," "Show details," "Approve / Reject" for non-consent flows.
`input` covers: "Set the volume to ___," "Snooze for ___ minutes," "Reply with ___."

`Action.params` carries the input value on submit (`{value: "..."}` merged in).

Frontend renders both via existing `ui/Button` primitive — no new design work.

### 3. Lifecycle: `update_content()` and `append_section()` (no new primitives)

Two backend helpers in `core/plugins/ui.py`:

```python
def update_content(widget_id: str, *, patch: dict) -> None:
    """Shallow-merge `patch` into the widget's data. No flicker, no scroll loss."""

def append_section(widget_id: str, section: dict) -> None:
    """Append a section to the widget's existing sections list."""
```

Both ride the existing `UI_UPDATE` event with a small flag (`mode: "patch" | "append"` on the envelope). Frontend store reducer handles the merge.

This unlocks: live tool-call log in `BackgroundTaskWidget`, stock tick refresh, forecast hourly update, growing transcript, a timer's countdown text being patched (the timer-with-laps case becomes "append a list item to the existing list section" — no slot addressing needed).

### 4. `chart` and `image` sections

```ts
{ type: 'chart', kind: 'line' | 'bar' | 'area',
  series: Array<{ label: string; points: Array<[number, number]> }>,
  x?: string, y?: string }

{ type: 'image', src: string, alt?: string,
  aspect?: '1:1' | '16:9' | '4:3' }
```

**`chart`**: native SVG renderer first. Line, bar, and area are all 30–50 LOC each in raw SVG. Only add `recharts` (~100KB) if a use case needs stacked/pie/composed and SVG doesn't carry it. Default to drawing it ourselves; the JARV1S aesthetic wants thin lines and glow effects that off-the-shelf chart libs make hard anyway.

**`image`**: data URLs and `https://` only (no `file://` paths). Aspect ratio enforced via Tailwind's `aspect-*` classes to prevent layout jumps before load.

---

## What this does NOT add

Explicitly skipped from the prior over-spec'd version:

- **No `stack`, `row`, `card`** layout primitives. ContentWidget's existing vertical-with-header is sufficient. If a real "two columns side by side" use case appears, add a single `row` then — not all three.
- **No `text` / `heading` / `badge` separate from existing `markdown`.** Markdown handles all three (`# Heading`, `**bold**`, inline code).
- **No `kv` change.** Already exists.
- **No slot addressing (`into="laps"`).** Append goes to the section list root. The "timer with lap times" case is "the timer is `update_content`'d; the lap log is its own list section that gets `append_section`'d when the user says lap." No tree traversal.
- **No icon vocabulary.** Buttons get an optional `icon: IconName` constrained to a small set already used in the app (check, x, play, pause, plus, arrow-right, link, trash, star). Adding more is one-line additions to the icon map; not worth a primitive.

---

## Addressing the voice-model JSON composition concern

The fast voice tier (Groq Gemma) composes deeply nested JSON poorly. This is real. Two structural mitigations:

1. **CodeAct + Python builders.** The model writes Python, not JSON. Builder helpers in `core/plugins/ui.py` mean the model writes:
   ```python
   push_content("Forecast", [
       metric_section([...]),
       chart_section(kind="line", series=[...]),
   ])
   ```
   instead of hand-typing the dict tree. Function calls with named args are exactly what Gemma-class models do well. JSON composition error rate isn't the relevant variable; Python keyword-argument fluency is.

2. **Pydantic validation at the builder.** Each builder validates against a `Section` discriminated union before the envelope is constructed. Invalid sections raise `ValueError` inside the executor; the agent sees the error and self-corrects on the next CodeAct iteration. The model rarely sees raw JSON; when it does, it sees a typed error pointing at the exact field.

3. **Powerful tier handles complexity.** Phase 7.5b's `_route_model()` already routes complex requests to the powerful agent. Multi-section interactive widgets are a case where the routing pre-classifier should bias toward powerful. Worth a regex extension once Step 2 ships and we see real composition failures.

If the voice model still struggles after Steps 1–2 ship, that's a real signal — and the right response is to tighten the builder API further (e.g., one helper per common widget shape: `inbox_widget(emails)`, `forecast_widget(days)`) rather than reach for a more elaborate grammar.

---

## Phase plan

Each step is independently shippable. Stop after any step that earns its keep.

| Step | What | Effort | Earns its keep if... |
| :--- | :--- | :--- | :--- |
| **0 (prereq)** | [`UI_ACTION_BUS.md`](./UI_ACTION_BUS.md) ships first | 1.5 days | (separately justified) |
| **1** | Action-aware list items | 0.5 day | Inbox archive + todo complete feel right at <100ms |
| **2** | `button` + `input` sections | 0.5 day | At least three plugins reach for them within a month |
| **3** | `update_content()` + `append_section()` lifecycle | 0.5 day | `BackgroundTaskWidget` migrates to `append_section` and the bespoke event-stream code shrinks |
| **4** | `chart` + `image` sections | 1 day | A real plugin pushes a non-trivial chart (sleep, system load, weather hourly) |

Total ceiling: ~2.5 days on top of the action bus. Floor: 0 if Step 1 doesn't pan out.

No rebrand, no catalog, no migration aliases, no version-bump ceremony. ContentWidget quietly grows new section types. The Python docstring and one-paragraph mention in `WIDGET_SYSTEM.md` is the documentation surface.

---

## Research before committing past Step 2

Pulling forward two of the user-feedback research items because they actually matter for the back half of this plan:

- **Audit the logs.** Over the next month after Step 1 ships, count how many times a plugin would have wanted `chart` or `image` and currently doesn't push one. If the count is <5, defer Steps 3–4 indefinitely.
- **Eval the voice tier on Python builder composition** (not raw JSON). Give Gemma 10 realistic prompts that should produce a 3-section ContentWidget call. Measure: does it pick the right section types? Pass valid args? If pass rate is high, Steps 2–4 are safe. If low, the answer is tighter helpers (one-shot widget builders) before more primitives.

These are cheap (a half-day each) and either result is informative.

---

## What this leaves on the table

Things worth wanting but not addressed here:

- **Layout grouping** (side-by-side comparisons). Defer until a real use case lands; then add one `row` section, not a layout grammar.
- **Map** (calendar event locations, "where's the package"). Genuine high-value capability but pulls in a tile provider + library. Worth its own proposal when something concrete needs it.
- **Slot-scoped replacement** (Anthropic's pattern for in-line visuals that mutate as a conversation evolves). The model just reuses `widget_id` and uses `update_content`; this falls out for free with Step 3.
- **The grammar-unification question.** Whether ContentWidget should eventually become a more formal block-tree renderer is a real question, and the right time to answer it is six months after these steps land, with logs showing where the section model strains. Not now.
