# JARV1S UI Style Guide

This document defines the visual implementation details for the JARV1S frontend.
Product experience: [`PRODUCT_BRIEF.md`](./PRODUCT_BRIEF.md). Holographic direction and refactor rules: [`VISUAL_LANGUAGE.md`](./VISUAL_LANGUAGE.md). Token inventory: [`FOUNDATIONS.md`](./FOUNDATIONS.md).
For UX behavior, layout, shell overlays, and widget lifecycle, refer to [`FRONTEND_ARCHITECTURE.md`](./FRONTEND_ARCHITECTURE.md).

## 1. Color Palette

Dark-mode foundation (OLED friendly). Figma brand colors are the source of truth; code maps them through semantic roles.

| Color Name | Hex Codes | Usage Role |
| :--- | :--- | :--- |
| **Rich Black** | `#050A10` (Deep)<br>`#0B1824` (Medium) | Canvas backgrounds (`bg-canvas-sunken` / `bg-canvas`). |
| **Indigo Dye** | `#113040` (Base)<br>`#174157` (Medium)<br>`#226081` (Light) | Surfaces and outlines (`bg-surface`, `border-outline`, `bg-surface-highlight`). |
| **Blue Green** | `#2299BE` | Brand / trust (`brand`). Active, Listening, Thinking. Use `text-brand-fg` for readable brand text on indigo surfaces. |
| **Celadon** | `#A0E8AF` | Success outcomes (`status-success`) and speaking/output identity (`brand-output`). |
| **Persian Red** | `#CC2936` | Danger / critical (`status-danger`). Use `text-status-danger-fg` for readable danger text on indigo surfaces. |
| **Carrot** | `#FFB300` | Warning / attention (`status-warning`). |
| **White Family** | `#FFFFFF` (Primary)<br>`#DEE6EB` (White Sky / muted)<br>`#636B70` (Disabled Grey) | Foreground hierarchy. Code also exposes a mid-emphasis subtle step (`text-foreground-subtle`) between White Sky and Disabled Grey for enabled metadata. |

Foreground emphasis:

| Utility | Use |
| :--- | :--- |
| `text-foreground` | Primary content and titles |
| `text-foreground-muted` | Supporting body copy |
| `text-foreground-subtle` | Enabled metadata, hints, de-emphasized labels |
| `text-foreground-disabled` | Truly disabled / unavailable controls and placeholders only |

Brand vs status:

| Intent | Utility |
| :--- | :--- |
| Interactive brand chrome | `brand` (fills, borders, labeled brand buttons); `text-brand-fg` only for small brand text on indigo surfaces |
| Speaking / output identity | `brand-output` |
| Successful outcome | `status-success` |
| Warning | `status-warning` |
| Error / destructive | `status-danger` / `text-status-danger-fg` |

Type role × default foreground:

| Role | Default color |
| :--- | :--- |
| `.type-display` / `.type-title` / `.type-section` / `.type-heading` | `text-foreground` |
| `.type-body` / `.type-body-reading` | `text-foreground` or `text-foreground-muted` when de-emphasized |
| `.type-label` / `.type-label-small` | `text-foreground` or `text-foreground-muted` |
| `.type-meta` | `text-foreground-subtle` |
| `.type-fui` | `text-foreground-subtle` or brand accent |

## 2. Typography

| Font Family | Role | Usage |
| :--- | :--- | :--- |
| **Space Grotesk** | **Display / identity** | Short workspace, section, and component titles. Prefer Medium or Semibold; do not use for paragraphs or routine labels. |
| **Camber** | **Interface / reading** | Controls, transcripts, settings, descriptions, and functional labels. Use Regular for content and Medium for emphasis. |
| **JetBrains Mono** | **Technical data** | Code, identifiers, timestamps, and compact machine data. Do not use for normal navigation or body copy. |

Use semantic text roles rather than choosing size and weight independently. Prefer `.type-*` utilities (complete role) or the matching `text-*` size token paired with the correct `font-*` family:

| Role | Typeface | Size / line height | Weight | Use | Utility |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `display` | Space Grotesk | `32px / 40px` | Medium | Rare, high-emphasis identity moments | `.type-display` |
| `title` | Space Grotesk | `24px / 32px` | Medium | Workspace and major surface titles | `.type-title` |
| `section` | Space Grotesk | `20px / 28px` | Medium | Prominent sections and dialog titles | `.type-section` |
| `heading` | Space Grotesk | `16px / 24px` | Medium | Sections and component titles | `.type-heading` |
| `body` | Camber | `14px / 20px` | Regular | Operational UI and short responses | `.type-body` |
| `body-reading` | Camber | `16px / 24px` | Regular | Longer responses and sustained reading | `.type-body-reading` |
| `label` | Camber | `14px / 20px` | Medium | Actions, tabs, filters, and prominent labels | `.type-label` |
| `label-small` | Camber | `12px / 16px` | Medium | Field labels and compact controls | `.type-label-small` |
| `meta` | Camber | `12px / 16px` | Regular | Supporting metadata | `.type-meta` |
| `fui` | JetBrains Mono | `10px / 12px` | Medium | Non-essential ornament only | `.type-fui` |

Operational surfaces prioritize readability over ornamental density:
- Use sentence case for functional UI. Reserve tracked uppercase mono for short tags and ornament.
- Use `tabular-nums` for changing values instead of making an entire row monospaced.
- Keep paragraphs and detailed responses at approximately `50–75ch`.
- Essential state, actions, and decision-making content must be at least `12px`; body and row content must be at least `14px`.
- Layouts must tolerate 200% text resizing and user text-spacing overrides without clipping or losing functionality.
- Apply `text-box` trimming only to single-line headings and controls, never multiline body copy.
- Interactive controls use a `40px` desktop minimum and `44px` on coarse pointers.

Font delivery must not depend on the network. Space Grotesk and JetBrains Mono are self-hosted WOFF2 assets. Self-host production-licensed Camber WOFF2 before distribution — current Camber files are trial-named OTF assets.

## 3. Operational surfaces

Use the optical layers, shape grammar, and luminescence budget in [`VISUAL_LANGUAGE.md`](./VISUAL_LANGUAGE.md). Holographic treatment communicates structure and live state; it does not replace readable surfaces or familiar controls.

Activity (glance + full workspace), Diagnostics, Apps, Settings, Smart Home, and Rooms & devices use the shared `StatusBarSurfaceHost`. Its shell morphs between glance-menu and workspace geometry while preserving the appropriate menu or non-modal dialog semantics. Promoting a glance (or nested workspace) into another workspace is just `openOverlay` — the host prefers workspace destinations over menus, so the surface grows in place.
1. Use one strong shell treatment; data rows inside it use restrained borders and no repeated glow.
2. Selection, live state, keyboard focus, and destructive confirmation are the only row-level emphasis states.
3. Desktop keeps list and detail visible together. Narrow layouts replace the list with detail and provide an explicit Back action.
4. Status is always written in text; color and luminescence are reinforcement, never the sole signal.
5. **Dismiss:** `StatusBarSurfaceHost` closes on outside click, Escape, and re-clicking the active nav item. Workspace surfaces also show one explicit close via `StatusBarWorkspaceHeader`. Glance menus do not render a close button.

### Operational component contract
Use the shared primitives under `frontend/src/components/ui` rather than styling controls inside feature panels:
- `Button` is for labeled transactional actions (forms, confirmations, settings). Default `brand` / `warning` own holographic ring chrome; secondary actions use `variant="ghost"` with `neutral` / `subtle` / `danger` so they stay quieter. Hover uses `duration-feedback` (200ms). `TacticalButton` is reserved for stage-chrome icon docks (StatusBar, widget chrome) with holographic ring affordances — do not merge them.
- `ActionMenu` is the overflow for operational list rows: one primary `Button` (or none), remaining actions in a quiet menu. Do not wrap a row in equally-weighted ghost buttons. `HolographicMenu` stays StatusBar chrome; do not reuse it inside workspace lists.
- `TextLink` is for inline text actions that flow with surrounding copy (open docs, jump to a related surface, edit a row fact). Underlined brand text, optional `external` affordance — not a pill and not a substitute for primary `Button` CTAs.
- `StatusBarWorkspaceHeader` owns workspace title chrome (title, optional subtitle/leading/trailing, close) with a shared `px-6` content gutter that body layouts should match. `MenuSectionHeader` / `StatusBarMenuContent` own glance-menu chrome (`px-2` inset, heading-role titles). Do not invent per-panel close buttons on StatusBar surfaces.
- `SegmentedTabs` owns workspace tabs and WAI-ARIA keyboard behavior. `Chip` is only for filters and toggles.
- `FieldControl`, `Input`, and `SearchField` provide persistent labels, 44px targets, clear actions, and consistent focus/error states. Idle inputs use a quiet `surface` fill and low-emphasis outline; brand stroke/ring is reserved for focus (aligned with `Select`).
- `Select` replaces native selects so closed and open states remain readable and on-brand. Trigger styling matches `Input` (quiet surface, brand on focus/open). The listbox matches trigger width via `--anchor-width`, uses the same `rounded-control` / outline recipe (not a full `Hologram` shell), and owns plain option rows with a selected rail. Pointer hover uses CSS; Base UI highlighting is reserved for keyboard navigation so pointer movement cannot activate keyboard focus chrome. Do not reuse `holographic-menu/MenuItem` inside Select — that component is for action menus with icons.
- `PanelSection` is the low-emphasis inner surface. Do not nest full holographic borders inside a `Hologram` shell. Grouped rows that share one tile use `.ui-surface-group` so fills clip to `rounded-panel` / squircle corners.
- `DataField` standardizes compact label/value facts; `EmptyState` teaches the next action; `Switch` owns binary preferences.
- `Placeholder` is a compact inline loading/empty/error line inside an already-framed panel. Prefer `EmptyState` when the surface needs a title, description, and recovery action. Do not merge them — density and semantics differ.
- Operational surfaces follow the typography minimums in §2. Widget chrome may use denser FUI ornament (`10px` and below) only when the text is nonessential decoration — never for status, actions, or decisions.

### Accessibility minimums
- Interactive controls: `40px` desktop / `44px` coarse-pointer minimum. Compact `Button` sizes must still meet this.
- Keyboard focus uses a custom `:focus-visible` outline gated by `html[data-keyboard-nav]`; never remove the indicator for reduced motion without a replacement.
- Honor `prefers-reduced-motion`: keep essential loading/state feedback, suppress ornamental transitions.
- Status indicators pair color with text (`StatusPill`) or an adjacent label; `StatusDot` alone is never the sole signal.

Operational copy uses **Activity** for the cross-domain timeline of what ran (reminders, automations, tasks, system). **Conversations** is an opt-in Activity facet for cross-node dialogue audit — not part of the default All feed. “History” is reserved for the live transcript / context history, and internal terms such as trigger instances stay out of navigation labels.
