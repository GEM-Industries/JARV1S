# JARV1S UI Foundations

Source of truth is **code**, not this doc. This is a short inventory of what is already defined so we do not invent a parallel system.

## Where tokens live

| Layer | Location |
| :--- | :--- |
| Palette primitives + semantic CSS aliases | `frontend/src/index.css` |
| Tailwind semantic utilities (colors, type, radius, motion, glow) | `frontend/tailwind.config.js` |
| Holographic direction and usage rules | [`VISUAL_LANGUAGE.md`](./VISUAL_LANGUAGE.md) |
| Human-readable visual guide | [`STYLE_GUIDE.md`](./STYLE_GUIDE.md) |
| Shared primitives | `frontend/src/components/ui/` |

Prefer **semantic** Tailwind tokens (`bg-canvas`, `text-foreground-muted`, `border-outline`, `rounded-control`, `duration-feedback`) over raw hex, palette names, or one-off `text-[Npx]`.

When adding a `fontSize` token, register its suffix in `frontend/src/utils/cn.ts` (`extendTailwindMerge` → `theme.text`). Custom colors need no merge config; unregistered custom sizes collide with `text-*` colors.

## Defined today

**Color** — Figma palette stays as immutable primitives (`rich-black`, `indigo-dye`, `blue-green`, `celadon`, `persian-red`, `carrot`, white / white-sky / disabled-grey). Components use semantic aliases only:

| Role | Utility examples | Maps to |
| :--- | :--- | :--- |
| Canvas | `bg-canvas`, `bg-canvas-sunken` | Rich black medium / deep |
| Surface | `bg-surface`, `bg-surface-raised`, `bg-surface-sunken`, `bg-surface-highlight` | Indigo dye family |
| Outline | `border-outline`, `border-outline-strong`, `border-outline-subtle` | Indigo dye medium / light |
| Foreground | `text-foreground`, `text-foreground-muted`, `text-foreground-subtle`, `text-foreground-disabled` | White family |
| Brand | `bg-brand`, `text-brand`, `text-brand-fg`, `bg-brand-output` | Blue green (+ accessible fg tint); celadon for speaking/output |
| Status | `text-status-success`, `text-status-warning`, `text-status-danger`, `text-status-danger-fg` | Celadon / carrot / persian red (+ accessible danger fg) |
| Hologram chrome | `border-hologram-error`, `border-hologram-inactive` | Shell-only treatments |

`text-brand-fg` and `text-status-danger-fg` are lighter text-only tints for WCAG on indigo surfaces; fills and glows keep the original Figma colors. Do not use `text-foreground-disabled` for enabled metadata — use `text-foreground-subtle`.

**Typography** — `font-display` → Space Grotesk, `font-body` → Camber, `font-mono` → JetBrains Mono. Semantic size tokens and complete `.type-*` role utilities:

| Role | Token / utility | Size / line height |
| :--- | :--- | :--- |
| Display | `text-display` / `.type-display` | 32 / 40 |
| Title | `text-title` / `.type-title` | 24 / 32 |
| Section | `text-section` / `.type-section` | 20 / 28 |
| Heading | `text-heading` / `.type-heading` | 16 / 24 |
| Body | `text-body` / `.type-body` | 14 / 20 |
| Body reading | `text-body-reading` / `.type-body-reading` | 16 / 24 |
| Label | `text-label` / `.type-label` | 14 / 20 Medium |
| Small label | `text-label-small` / `.type-label-small` | 12 / 16 Medium |
| Meta | `text-meta` / `.type-meta` | 12 / 16 Regular |
| FUI | `text-fui` / `.type-fui` | 10 / 12 ornamental |

Prefer `.type-*` when applying a full role. Space Grotesk and JetBrains Mono are self-hosted WOFF2 under `frontend/src/assets/fonts/`. Camber remains local OTF (trial-named; verify licensing before distribution). Roles and usage rules: [`STYLE_GUIDE.md`](./STYLE_GUIDE.md#2-typography).

**Radius** — `rounded-control` 12px, `rounded-panel` 16px, `rounded-shell` 32px. Values live in CSS `--radius-*` (`index.css`); Tailwind references those variables. These semantic radii progressively enhance with `corner-shape: squircle` (Apple-style continuous corners) and fall back to the circular radius where unsupported. Use `corner-squircle` only when a radius must be derived from a semantic token, such as concentric segmented controls. `rounded-full` and `corner-round` remain geometrically round for capsules, circles, rings, and status dots. Grouped list tiles use `.ui-surface-group` (`overflow-hidden` + `rounded-panel`) so row fills clip to the curve. Prefer these tokens over equivalent `rounded-xl` / `rounded-2xl` or one-off radius utilities.

**Motion** — `instant` 100ms, `feedback` 200ms, `transition` 300ms; `hologram` / snappy easings. `StatusBarSurfaceHost` uses fade-through content and an isolated top-right shell morph: incoming content lays out once at its final size while the shell clips and reveals it, preventing text reflow during geometry changes. In-panel drill-ins (e.g. Smart Home → HA rooms) reuse the same fade-through via `useFadeThrough`. Honor `prefers-reduced-motion`. Prefer semantic duration utilities (`duration-feedback`, `duration-transition`) in shared primitives.

**Status vocabulary** — Shared primitives use one tone set: `success`, `active`, `warning`, `error`, `neutral`, `off` (`StatusDot`, `StatusPill`). Widget chrome may alias `StatusDot` but must not invent a parallel enum.

**Layout chrome** — `--safe-area-top` / `--safe-area-bottom` reserve stage clearance. `--status-bar-inset`, `--status-nav-height`, and `--shell-overlay-gap` define the shared StatusBar geometry; `--shell-overlay-top` anchors every `StatusBarSurfaceHost` destination at the same gap below the nav. The host owns semantic width presets from compact menus through narrow and full workspaces; feature content selects a preset instead of supplying dimensions. Focus: `outline: none !important` on `:focus` (beats UA), zero Tailwind ring vars (protects hologram inset), custom outline only under `html[data-keyboard-nav]` via `installKeyboardNavFocus`. Because `:focus` zeroes `--tw-ring-*`, **selected/active chrome must use `border` (`.ui-surface-selected`), not `ring`** — rings vanish while the control stays focused after click.

**Spacing** — Use Tailwind’s 4px base grid with an 8px primary rhythm:

- `2px` — rare optical nudges, never structural spacing
- `4px` — tightly coupled elements
- `8px` — related content
- `12px` — compact control padding
- `16px` — normal component and group spacing
- `24px` — separation between groups
- `32px` — sections and panel padding
- `48px` — major regions
- `64px` — ambient-stage whitespace

Choose by relationship, not decoration: coupled (`4px`) < related (`8px`) < grouped (`16px`) < separated (`24–32px`) < regional (`48–64px`). A heading sits closer to the content it introduces than to the content before it; a typical title/subtitle/body stack uses `4px`, then `8–12px`, while a new section starts after `24–32px`.

Let the parent layout own sibling spacing with `gap`; do not attach reusable margins to individual text elements. Preserve the relationship order when responsive layouts contract. Typography, intrinsic dimensions, and deliberate optical corrections may fall off-grid. For single-line headings, labels, buttons, and badges, `text-box: trim-both cap alphabetic` may correct font leading when supported; do not apply it to multiline body copy.

JARV1S uses context-appropriate density rather than a separate density theme.

## Open foundation work

- Formal spacing semantic aliases (`section-gap`, etc.)
- Production-licensed Camber WOFF2 (current files are trial-named OTF)
- Multi-brand or light theme
- Elevation / shadow catalog beyond hologram glows
- Contribution / governance process for a design system

Add new token categories only when the same need shows up twice. Camber licensing must be resolved before production distribution.

## Product constraints

The intended cross-surface experience and product principles: [`PRODUCT_BRIEF.md`](./PRODUCT_BRIEF.md).
