# The Profitable & Enjoyable Product Design Guide
**Refined Edition: Visual Systems + Accessibility + UI Craft**
*(Part 2: Companion to UX Laws)*

## Part 1: The Core Visual Systems (The "How")

### 1. The 4pt Grid and 8pt Rhythm
**Principle:** Spacing is the invisible skeleton of UI. Inconsistent relationships create cognitive friction ("something feels off").
*   **The Rule:** Use a **4px base grid** and an **8px primary rhythm**. Prefer 8px increments for layout and 4px increments within components.
*   **Relationship First:** Space within an element < within a group < between groups < between sections. Proximity communicates structure more strongly than uniform spacing.
*   **Composition:** Let parent layouts own sibling spacing with `gap`; preserve relationship ordering as layouts respond.
*   **Exceptions:** Typography, intrinsic dimensions, and deliberate optical corrections may fall off-grid.
*   **Optical Type:** Where supported, `text-box` can remove font leading from single-line headings and controls. Do not trim multiline body copy.
*   **The Internal ≤ External Law:** Internal spacing must be less than or equal to external spacing. Grouping relies on this relationship.

### 2. The Semantic Type Scale
**Principle:** Typography should express purpose and hierarchy, not expose arbitrary combinations of font properties.
*   **Role Tokens:** Define each role as a complete style: family, size, line height, weight, and tracking.
*   **Restraint:** Use the fewest typefaces, weights, and roles needed for clear hierarchy. Body faces prioritize sustained readability; display faces provide identity in short text.
*   **Rhythm:** Use tighter line height for large headings and more generous line height for body and small text. Keep long-form lines near 50–75 characters.
*   **Responsive Rule:** Preserve relative hierarchy as text resizes. Use fluid type only where the layout benefits, and never allow zoom or text-spacing overrides to clip content or controls.

### 3. Semantic Color Architecture
**Principle:** Color is a function, not a decoration.
*   **60-30-10 Rule:** 60% Neutral (Surface), 30% Secondary/Brand (UI elements), 10% Accent (Call to Action).
*   **Semantic Roles:** Define colors by *intent*, not hex code. `color-error` is better than `color-red`.
*   **Dark Mode Law:** Never use pure black (`#000000`). It causes eye strain (smearing). Use Dark Gray (`#121212`) and **desaturate** accent colors to prevent vibration.

### 4. Design Token Tiering
**Principle:** Hard-coded values are technical debt.
*   **Tier 1 (Reference):** `blue-500` (The raw value).
*   **Tier 2 (Semantic):** `primary-action` (The intent).
*   **Tier 3 (Component):** `button-bg-default` (The usage).
*   **Action:** Engineers and Designers should only ever speak in Tier 2 or 3 tokens.

***

## Part 2: The 10 Golden Rules of Visual Interface

### Rule 1: Visual Hierarchy (The 3-Size Limit)
**Source:** Gestalt Psychology
Every screen needs a clear "First, Second, Third" read order.
*   **Constraint:** Use max **3 font sizes** per view to enforce hierarchy.
*   **The 50ms Rule:** Users judge a site’s credibility in 0.05 seconds based purely on visual coherence.
*   **Action:** If everything is bold, nothing is bold.

### Rule 2: Proximity Over Borders (Gestalt)
**Source:** Gestalt Principles
Proximity is a stronger grouping signal than lines or boxes.
*   **Remove:** 50% of your borders.
*   **Add:** Whitespace. Elements close together are perceived as a group.
*   **Test:** If you remove the container border, can you still tell the elements belong together? If no, your spacing is wrong.

### Rule 3: Contrast Ratios (Accessiblity)
**Source:** WCAG 2.1 AA
Low contrast forces the brain to "decode" text, increasing load.
*   **Text:** 4.5:1 minimum (Normal text), 3:1 (Large text/Bold).
*   **UI Elements:** 3:1 minimum for input borders and icons.
*   **Disabled State:** The only exception.

### Rule 4: The Light Source Assumption (Elevation)
**Source:** Material Design
Humans assume light comes from the top-left (90 degrees).
*   **Shadows:** `y-offset` should always be positive (shadow falls down).
*   **Layers:** Use shadow **blur** and **spread** to indicate depth (z-index), not just darkness.
*   **Rule:** Higher elevation = Larger, softer shadow. Lower elevation = Smaller, crisper shadow.

### Rule 5: Motion Timing (200ms Sweet Spot)
**Source:** Nielsen / Fluent Design
Animation is feedback, not cinema.
*   **Micro-interactions:** 100ms (Instant feel).
*   **Transitions:** 200ms–300ms (The limit of human perception of "instant").
*   **Easing:** Always use **Ease-Out** for UI entry (fast start, slow stop). Linear motion feels robotic.

### Rule 6: Input Field Anatomy
**Source:** Baymard Institute
Forms are the barrier to conversion.
*   **Labels:** Always visible (never use placeholder text as a label; it disappears when typing).
*   **Click Area:** Inputs need a minimum height of **40-48px**.
*   **Validation:** Inline and real-time. Don't wait for "Submit" to show an error.

### Rule 7: Nested Border Radius
**Source:** Visual Mathematics
Concentric shapes need different radii to look parallel.
*   **The Math:** `Inner Radius = Outer Radius - Padding`.
*   **Why:** If inner and outer radii are equal, the gap looks uneven (thick at corners, thin at edges).

### Rule 8: Icon Clarity
**Source:** Semiotics
Icons save space but cost recognition time.
*   **Labels:** An icon without a label is a decorative ambiguity. Always label navigation icons.
*   **Touch:** Optical alignment often differs from mathematical centering. Visually center icons based on their "mass," not their bounding box.

### Rule 9: The "Squint Test" (Blur Check)
**Source:** Visual Art
*   **Technique:** Squint your eyes (or blur the screen 5px) until text is unreadable.
*   **Pass:** You can still identify the primary CTA and the main content grouping.
*   **Fail:** The page looks like a gray blob. Increase contrast and spacing.

### Rule 10: State Visibility
**Source:** Interaction Design
Every interactive element must have clear visual states.
*   **The 5 States:** Default, Hover, Focus, Active (Pressed), Disabled.
*   **Focus Ring:** Never remove the default CSS outline without replacing it with a custom focus style. It breaks keyboard navigation.

***

## Part 3: The "Scalable" UI (System Specifics)

### 1. The Component "API"
Treat UI components like code functions.
*   **Props:** Define rigid variants (e.g., `Button: Primary | Secondary | Ghost`).
*   **Forbidden:** "Custom" overrides. If a designer needs a "slightly darker blue" button, they must update the system, not the instance.

### 2. Loading Skeletions > Spinners
Perceived performance matters more than actual performance.
*   **Skeleton Screens:** Show gray layout placeholders immediately. It creates the illusion that the *layout* has loaded and only data is fetching.
*   **Rule:** Spinners draw attention to the wait; skeletons draw attention to the progress.

### 3. Optical Alignment
Mathematics is often visually "wrong."
*   **Overshoot:** Curved letters (O, C) and icons must be slightly larger (~2-4%) than flat letters (T, H) to appear the same height.
*   **Centering:** A play button (triangle) mathematically centered in a circle looks off-center. Nudge it right to balance the visual weight.

***

## Part 4: The Checklist (Before Ship)

1.  **Is it accessible?** Can I navigate the entire UI with only `Tab` and `Enter`?
2.  **Is it readable?** Does all text meet WCAG 4.5:1 contrast?
3.  **Is it aligned?** Does spacing follow the 4px grid, 8px rhythm, and intended grouping?
4.  **Is it dark-mode ready?** Did I avoid pure black and desaturate accents?
5.  **Is it fluid?** Does the layout break or adapt when I resize the browser width?
6.  **Is it grouped?** Did I use whitespace instead of borders to group related content?
7.  **Is the hierarchy obvious?** Does the Squint Test pass?

***

**One-Line Philosophy:**
*"Design for the 'Squint Test,' space with math, color with intent, and build systems that make inconsistency impossible."*
