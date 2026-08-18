# JARV1S Visual Language

The small set of rules that gives JARV1S an ambient holographic identity without making ordinary work harder to read or slower to render.

Product behavior comes from [`PRODUCT_BRIEF.md`](./PRODUCT_BRIEF.md). Tokens and component details remain in [`FOUNDATIONS.md`](./FOUNDATIONS.md) and [`STYLE_GUIDE.md`](./STYLE_GUIDE.md).

## Direction

JARV1S is a calm environment that comes alive around the current task. It should feel like projected intelligence, not a dashboard wearing neon decoration.

The Iron Man HUD references are useful for their depth, dark negative space, asymmetric composition, fine geometry, and concentrated points of light. Do not copy their constant density, tiny essential text, or cinematic motion.

**Core rule:** readability establishes the interface; holographic effects communicate focus, state, and change.

## Three optical layers

Every element belongs to one layer:

1. **Ambient** — canvas, wallpaper, faint geometry, and passive system presence. It stays peripheral and never competes with content.
2. **Interface** — navigation, transcripts, forms, settings, and readable results. It uses stable dark surfaces and familiar controls.
3. **Live projection** — listening, current work, active results, warnings, and direct focus. It may use brighter strokes, glow, and purposeful motion.

Depth is created through overlap, contrast, and restrained translucency. Keep primary reading content on one stable visual plane; do not simulate depth by blurring or moving essential content.

## Shape grammar

Use shape to communicate purpose:

| Shape | Role |
| :--- | :--- |
| Panel or backplate | Reading, forms, lists, and sustained interaction |
| Full holographic frame | Major workspace or hero widget boundary |
| Corner brackets | Temporary focus, targeting, or active inspection |
| Arc or ring | Listening, sensing, progress, or spatial context |
| Rail or fine line | Timeline, relationship, direction, or data flow |
| Reticle | One current focal subject only |
| Pill | Compact status or state |

Use familiar rectangular controls for actions and input. Radial controls and reticles are display language, not a replacement for standard navigation.

## Light and emphasis

Treat luminosity as a limited resource:

- One dominant luminous subject per view.
- At most one secondary live indicator.
- Idle surfaces do not glow.
- Passive structure uses a low-emphasis stroke or no border.
- Success and error emphasis resolves after acknowledgement; it does not remain theatrical.
- Position, spacing, type, and opacity establish hierarchy before glow.

Do not place a full holographic frame around every nested section. One strong shell with quiet content is more convincing and easier to scan.

## State and motion

Motion explains what JARV1S is doing:

| State | Visual behavior |
| :--- | :--- |
| Idle | Nearly still; ambient detail remains peripheral |
| Waking / detected | Brief materialization or convergence |
| Listening | Input-responsive breathing, ring, or waveform |
| Thinking | Contained cyclic motion indicating ongoing work |
| Executing | Directional progress or discrete advancing steps |
| Speaking | Output color with restrained outward movement |
| Success | Short resolve, then return to rest |
| Warning / error | Immediate contrast and recovery action; no agitated loop |

Use existing `instant`, `feedback`, and `transition` timings. Prefer `transform` and `opacity`; do not animate layout, large shadows, or blur. Loop only while the represented process is genuinely active. Reduced motion keeps text, progress, and state changes while removing ornamental movement.

Transitions preserve spatial continuity: content should materialize in place, update in place, and recede without moving unrelated controls.

## Ambient behavior

Ambient means informative without demanding attention:

- Keep most of the idle canvas dark and quiet.
- Let persistent status live at the edge of attention.
- Bring a task to the center only when it needs reading, action, or recovery.
- Return completed work to a receipt, pinned widget, or history instead of leaving the stage busy.
- Use gentle static gradients, vignettes, or sparse geometry instead of permanent full-screen animation.
- Never flash, pulse, or play sound merely to make the interface feel alive.

The interface should move easily from the periphery to the center of attention and back.

## Materials and legibility

- Text-heavy and interactive content uses a dark, sufficiently opaque backplate.
- Translucency belongs to shell and peripheral chrome, not behind long-form text.
- Avoid stacking translucent surfaces; it weakens hierarchy and increases rendering cost.
- Bright white and saturated cyan are highlights, not large-area fills.
- Thin lines may be ornamental only. Controls, meaningful graphics, and state indicators need sufficient contrast.
- Glow reinforces a crisp source edge; it cannot be the only visible boundary.
- Blur may be static and local. Never animate blur or apply large full-screen blur layers.

## Functional detail

HUD detail must either communicate real information or remain clearly decorative.

Good detail includes real time, source, progress, location, confidence, latency, relationship, and system state. Decorative ticks, gaps, and arcs may add texture without pretending to be data.

Do not invent coordinates, IDs, telemetry, or numbers for atmosphere. Keep ornamental FUI text nonessential and subordinate; never use it for status, actions, or decisions.

## Interaction states

Each state has one job:

- **Hover:** this can be used.
- **Focus:** keyboard location; visible under `html[data-keyboard-nav] :focus-visible`.
- **Pressed:** immediate physical feedback.
- **Selected:** persistent choice.
- **Live:** changing now.
- **Attention:** review is needed.
- **Outcome:** success, warning, or failure.

Do not express all of these with the same cyan glow. State must remain understandable from text, shape, or position without color or motion.

## Surface adaptation

Preserve the same hierarchy on every display:

- Desktop may show stage, receipts, and list/detail together.
- Narrow screens show one primary task with explicit Back navigation.
- Ambient displays show glanceable status and results, not dense controls.
- Voice-only nodes use the same state model through speech and earcons.

Reduce ornament before reducing text size, target size, or essential context.

## Refactor check

For each surface:

1. Identify its primary task and one focal subject.
2. Assign elements to ambient, interface, or live projection.
3. Remove borders and glow that do not communicate structure or state.
4. Check idle, loading, active, success, error, and reduced-motion states.
5. Confirm text and controls remain clear with effects removed.
6. Promote a new token or primitive only after the same need appears twice.

## Research basis

- [Microsoft: Color, light, and materials](https://learn.microsoft.com/en-us/windows/mixed-reality/design/color-light-and-materials) — dark backplates, restrained brightness, and avoiding fragile thin geometry.
- [Microsoft: Designing content for holographic display](https://learn.microsoft.com/en-us/windows/mixed-reality/design/designing-content-for-holographic-display) — opaque reading surfaces and limited transparency improve legibility and interaction confidence.
- [Apple: Materials](https://developer.apple.com/design/human-interface-guidelines/materials) — translucent material works as a distinct functional layer when it preserves foreground contrast.
- [Weiser and Brown: The Coming Age of Calm Technology](https://veryinteractive.net/pdfs/weiserbrown-thecomingageofcalmtechnology.pdf) — ambient information should move between peripheral and central attention.
- [web.dev: High-performance CSS animations](https://web.dev/articles/animations-guide) — prefer compositor-friendly transform and opacity; avoid layout and expensive paint animation.
- [W3C: Understanding non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html) — meaningful controls, graphics, and state indicators require perceivable contrast.
