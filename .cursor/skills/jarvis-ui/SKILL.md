---
name: jarvis-ui
description: >-
  Design and implement JARV1S frontend UI/UX with product taste, layout stability,
  and visual craft. Use when building or refining React UI, settings, chrome
  (status/control/chat bars), widgets, empty/loading/error states, layout shift,
  holographic styling, Tailwind tokens, or when the user mentions Jarvis feel,
  polish, consolidate settings, or design foundations.
paths:
  - "frontend/src/**/*.{tsx,ts,css}"
  - "docs/UI/**/*.md"
---

# JARV1S UI

Use this skill for product UI work in the React client. Do not start a redesign program — fix the surface in front of you and leave reusable residue when the issue will recur.

## Canonical docs (read these)

1. `docs/UI/PRODUCT_BRIEF.md` — product experience + decision principles
2. `docs/UI/FOUNDATIONS.md` — what tokens exist in code
3. `docs/UI/VISUAL_LANGUAGE.md` — holographic identity + refactor rules
4. `docs/UI/STYLE_GUIDE.md` — visual implementation
5. `.cursor/rules/frontend-component-library.mdc` — shared primitives vs feature styling

Principle references (load when needed):

- `references/ux-principles.md` — Laws of UX + HIG + retention psychology
- `references/ui-principles.md` — visual systems + accessibility + UI craft

## Product direction

- Lead with the useful outcome; reveal supporting detail on demand.
- Adapt content, density, modality, and suggested actions to context while keeping interaction patterns familiar.
- Treat voice as primary and use screens for detail, confirmation, correction, and control.
- Present JARV1S as one integrated assistant, not a chatbot or collection of providers and plugins.
- Earn autonomy and attention; follow the product's existing approval, attention, and delivery policies.

## Implementation guardrails

- Preserve spatial continuity through loading, progress, success, and error states. Avoid unexpected layout shifts.
- Keep system status understandable without relying on color or glow alone.
- Prefer proximity and hierarchy over unnecessary containers.
- Follow the semantic typography roles in `STYLE_GUIDE.md` (prefer `.type-*`); mono uppercase and `10px` / `text-fui` are ornament, never essential UI.
- Use `frontend/src/components/ui` primitives and semantic Tailwind tokens (`bg-canvas`, `text-foreground-muted`, `border-outline`, `rounded-control`, `duration-feedback`, …) over one-off styling or palette names.

## Workflow

1. Read `PRODUCT_BRIEF.md` and the relevant section of `VISUAL_LANGUAGE.md`, `STYLE_GUIDE.md`, or `FOUNDATIONS.md`.
2. Match nearby existing UI before inventing a new pattern.
3. Implement the smallest change that fixes the issue.
4. If the failure class will recur, leave one reusable token, primitive, or a one-line note in `docs/UI/`.
5. Before finishing, run the checklist below.

## Ship checklist

- State changes preserve spatial continuity
- Squint test: primary action and grouping are obvious
- Status is written in text (not color-only)
- Touch targets ≥ 40px desktop / 44px coarse; labels visible on inputs
- Spacing follows the 4/8 scale and clearly distinguishes related groups
- Typography uses the correct semantic role; essential text is ≥ 12px
- `:focus-visible` preserved; `prefers-reduced-motion` honored
- Shared primitive used instead of a one-off control, when one fits
