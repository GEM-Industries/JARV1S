# Widget System Architecture

## Core Concept: Server-Driven UI (SDUI)
The backend defines *what* to render. The frontend defines *how* to render it and owns local presentation lifecycle.

The important boundary is ownership:

- **Authoritative state** lives in domain stores such as `pending_inputs`, `background_tasks`, calendars, inboxes, automations, or integration providers.
- **Widget envelopes** are serializable render snapshots of that state.
- **UI-only state** such as active widget, expanded rows, or receipt rail ordering stays local unless a domain explicitly needs it. Pinning is backend-owned because pinned widgets must survive reconnects.

---

## 1. The Contract: `UIEnvelope`
All plugins communicate with the UI via a strict JSON schema. This decouples Python logic from React rendering.

**Backend Model (Python):**
```python
class UIEnvelope(BaseModel):
    widget_id: str          # Unique instance ID (e.g., "weather-kitchen-01")
    component: str          # React Component Key (e.g., "WeatherCard")
    data: Dict[str, Any]    # Props for the component (e.g., { temp: 24 })

    # Lifecycle Metadata
    title: str = "Widget"
    created_at: int                       # Unix timestamp in ms
    expires_at: Optional[int] = None      # Unix timestamp in ms, local auto-removal
    layout: WidgetLayout = WidgetLayout()
    pinned: bool = False                  # Durable main-canvas state
```

`UIEnvelope` intentionally does **not** contain a refresh cadence. Backend producers own cadence and push fresh envelopes through `ui.update`; widgets do not poll by timer.

---

## 2. Data Flow Strategy

### A. Static / One-Shot
*   **Use Case:** "Show me this file summary", "Display this draft."
*   **Flow:** Tool result -> `UIEnvelope` -> render.
*   **Update:** None. The same artifact remains until removed, expired, replaced by the same `widget_id`, or pinned.

### B. Live / Backend-Pushed
*   **Use Case:** Background tasks, approval prompts, inbox state, weather, system metrics, and dashboard widgets.
*   **Mechanism:** Domain-owned producer pushes fresh `UIEnvelope`s over `ui.update`.
*   **Logic:**
    1.  Backend source changes via webhook, subscription, automation, scheduler, or provider poller.
    2.  The owning plugin/service re-emits a full widget envelope with the same `widget_id`.
    3.  Frontend upserts the envelope and renders the latest snapshot.
*   **Benefit:** One backend producer serves all displays, survives disconnects, can cache/dedup, and restores through `ui.snapshot`.

Widget-level polling is avoided. If a future data source truly needs polling, the poller belongs in the backend domain service, not `WidgetWrapper`.

### C. Reconnect / Snapshot Restore
*   **Use Case:** Reload the browser while a pinned widget, approval, or background task is still active.
*   **Mechanism:** `ui.snapshot`.
*   **Logic:**
    1.  On WebSocket connect, backend calls registered widget snapshot providers.
    2.  Providers rebuild active `UIEnvelope`s from their domain stores.
    3.  Frontend receives `ui.snapshot` and calls `setWidgets(...)`.
*   **Current providers:** pinned widgets, pending inputs, and running background tasks (restored as progress receipts, not hero widgets).

### D. Ephemeral / Time-Bound
*   **Use Case:** "Set a timer for 1 minute."
*   **Mechanism:** **Client-Side TTL.**
*   **Logic:**
    1.  Backend sends `expires_at: <timestamp>`.
    2.  Frontend `WidgetWrapper` schedules one local `setTimeout`.
    3.  Visible hero widgets update the progress bar at low cadence.
*   **Benefit:** Zero network traffic required for cleanup.

### E. Receipt / Content / Pinned Visualization
*   **Review Rail:** Compact cards overlaid on the right of the stage (absolute; centre projection stays viewport-centered) for glanceable confirmations and long-running work progress. Backend tools use `push_receipt(...)` / `receipt_envelope(...)` for one-shot confirmations, or `progress_receipt_envelope(...)` for stable upsertable progress receipts. Both emit a `ContentWidget` with `data.display = "receipt"`. Progress receipts may include `receipt_kind`, `status`, `attention`, and an `action` object that tells the frontend what click should do.
*   **Background task review:** `BackgroundTaskWidget` leads with the result, then changed files as the actionable list (open in the editor). The work log is supporting detail, grouped into replies and tool batches — not a raw stream.
*   **Dynamic live stage:** The centre shows exactly one foreground subject. Voice phases (`Detected`, `Listening`, `Transcribing`, `Thinking`, `Executing`, `Speaking`) own a stable projection slot. After playback ends, the latest finalized response settles without live motion for an adaptive 5–15 second reading dwell, then fades to the ambient indicator. A newer turn, content widget, or foreground `PendingInputWidget` replaces it immediately. Blocking consent outranks stale widget selection; background completions stay in the receipt rail and never auto-steal the centre.
*   **Pinned support:** Pinning keeps a widget available in a quiet supporting shelf beneath the stage. Pinning is durable main-canvas availability, not movement into the receipt rail.
*   **No Push:** Sensory or self-evident actions such as volume, playback, lights, or tiny list changes should not create widgets.

---

## 3. Implementation Patterns

### Backend (Python)
Plugins implement a standardized render/update pattern that returns or pushes a full `UIEnvelope`.
```python
def get_weather(self):
    return UIEnvelope(
        widget_id="weather-current",
        component="WeatherWidget",
        data={ "temp": 22 },
        pinned=True # Default to dashboard
    )
```

For tool-authored review surfaces:

```python
# Glanceable confirmation in the review rail.
return ToolResult(
    content="Recurring reminder set.",
    ui=[receipt_envelope("Reminder", "Pick up dry cleaning · 3pm tomorrow")],
)

# Long-running work with stable progress in the review rail.
envelope = progress_receipt_envelope(
    widget_id="scan-receipt-42",
    title="Scanning inbox",
    line="Checking unread messages…",
    sublabel="Running",
    kind="inbox_scan",
    ref_id="scan-42",
    status="running",
    action={"type": "activate_widget", "widget_id": "inbox-current"},
)
push_ui(envelope)

# Readable artifact in the main canvas.
return ToolResult(
    content="Draft saved.",
    ui=[content_envelope("Draft to Alice", [
        {"type": "kv", "pairs": {"To": "alice@example.com", "Subject": "Hello"}},
        {"type": "markdown", "content": body},
    ])],
)
```

For reconnect recovery, domain stores register snapshot providers instead of persisting a generic widget table:

```python
register_widget_snapshot_provider("background_tasks", background_task_snapshot_widgets)
```

Only backend-rebuildable widgets should appear in `ui.snapshot`. Pinning promotes a widget into durable backend-owned state; pending inputs and running background tasks are restored from their domain stores; transient one-off receipts do not need persistence.

### Frontend (React)
The frontend uses a **Typed Registry** pattern. All widgets register a `WidgetDefinition<T>` with a hero renderer and compressed configuration.

**Contract (`types.ts`):**
```typescript
export interface BaseWidgetProps {
  widgetId: string;
}
export interface WidgetDefinition<T = any> {
  Hero: React.FC<T & BaseWidgetProps>;
  getCompressedConfig: (data: T) => {
    icon?: React.ReactNode;
    label: string | number;
    labelVariant?: 'display' | 'mono';
    eyebrow?: string;
    subLabel?: string;
    variant?: 'default' | 'receipt';
    width?: 'square' | 'wide';
    indicator?: 'running' | 'warning' | 'success' | 'error';
  };
}
```

Hero receives envelope `data` plus `widgetId`. Layout `mode` (`hero` | `compressed`) stays on `WidgetWrapper` and is not injected into Hero — that key collides with domain fields such as background-task `mode`.

**Receipt rail helpers (`widgetRail.ts`, `PrimaryCanvas.tsx`):**
- `isReceiptRailWidget()` — filters compressed review-rail cards
- `receiptRailPriority()` — keeps urgent/running progress above static receipts
- `getReceiptAction()` — reads generic click routing from `data.action`; `PrimaryCanvas` dispatches the action to activate an existing widget or open a domain detail surface.

**Component Hierarchy:**
```text
<WidgetWrapper envelope={env}>  <-- Handles TTL, pinning, and mode logic
  <Hologram variant="base">     <-- Visual frame
    <WeatherWidget {...data} /> <-- Custom UI (Handles Hero/Compressed)
  </Hologram>
</WidgetWrapper>
```

---

## 4. Trigger Unification
Both **Agent Actions** (LLM tool calls) and **User Actions** (widget clicks) use the plugin layer.
1.  **User Click:** Sends `ui.action` -> calls plugin -> optional `ui.update`.
2.  **LLM Tool:** Calls plugin -> returns `ToolResult.ui` or pushes `ui.update`.
3.  **Reconnect:** Backend sends `ui.snapshot` -> frontend restores active widgets.

**Result:** UI updates are always server-authored envelopes, while frontend lifecycle stays small and local.
