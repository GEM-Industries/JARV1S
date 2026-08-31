# JARV1S Frontend Architecture

Product experience: [`PRODUCT_BRIEF.md`](./PRODUCT_BRIEF.md). Holographic direction: [`VISUAL_LANGUAGE.md`](./VISUAL_LANGUAGE.md). Foundations: [`FOUNDATIONS.md`](./FOUNDATIONS.md). Visual tokens: [`STYLE_GUIDE.md`](./STYLE_GUIDE.md).

## Stack Overview
| Component | Choice | Justification |
| :--- | :--- | :--- |
| **Framework** | **React 19 (Vite)** | Best-in-class for dynamic component lifecycles (Widgets) and highly responsive via Concurrent Renderer. |
| **State** | **Zustand** | Lightweight, transient updates support (no full re-renders for streaming), accessible outside React components. |
| **Language** | **TypeScript** | Types shared with Python/Pydantic ensure robust data contracts. |
| **Styling** | **Tailwind CSS** | Zero-runtime overhead, rapid development. Uses **OKLCH** color space for perceptual uniformity in FUI glows. |

---

## Core Architecture Patterns

### 1. The Headless Core (Implemented)
**Pattern:** Logic resides outside the View layer.
- **`JarvisClient` (Singleton):** A vanilla TypeScript class managing the raw `WebSocket` connection, `AudioContext` (16kHz), and `AudioWorklet` buffering.
- **`RealtimeConnection` (Transport Boundary):** Owns the WebSocket instance, heartbeat, reconnect policy, close-code handling, and HMR-safe global singleton. `JarvisClient` delegates transport here and keeps audio/message orchestration.
- **State Flow:** `WebSocket` -> `RealtimeConnection` -> `JarvisClient` -> `Zustand Store` (`useJarvisStore`) -> `React Component`.
- **Reason:** Decouples networking/audio from React render cycles. Prevents stuttering and connection churn.

### 2. Visual State System (System Status)
**Pattern:** Derived State Configuration.
- **Logic:** `getSystemStatus(connection, agent, attempts)` -> recovery chrome for the control dock. `resolveDashboardStatus(...)` composes that with local playback, mic readiness, hard/soft mute, and attention mode into one trust-pill answer: recovery → Speaking/active phase → privacy/attention → Ready / Voice active.
- **Reason:** Centralizes the complex matrix of states (Connecting, Error, Thinking, Speaking) into a single readable label. Color and glow reinforce the label; they never replace it. Clock time stays as secondary metadata in the trust pill and never replaces status text.
- **Live stage:** `deriveLiveStagePresentation(...)` in `features/live-stage/` is a separate presentation selector for the optical centre. It maps the same runtime axes (host, connection, agent, local playback, mute/attention, transcript, widgets) into one focal subject: recovery, voice projection, a settled response, onboarding, or a single foreground widget. A finalized response remains as quiet reading content after playback ends for an adaptive 5–15 second reading dwell, then fades to the ambient indicator; a newer turn replaces it immediately. When the transcript drawer is open, it owns conversation text and the live stage keeps only phase feedback, avoiding duplicate reading surfaces. This is presentation continuity, not a second backend state machine.

### 3. The Typed Registry (Extensibility)
**Pattern:** Server-Driven UI (SDUI) with Dual-Mode Support.
- **Concept:** The Backend pushes *intent* (`component` name + `data`). The Frontend maps this to the right widget.
- **Dual-Mode:** Every widget implements `WidgetDefinition<T>` — a `Hero` component (full detail) and `getCompressedConfig()` (glanceable label + icon).
- **Dual-map:** `definitionMap` (synchronous, for compressed config) and `heroMap` (lazy-loaded via `React.lazy`, for the full view). Adding a widget requires an entry in both.
- **Surfaces:** `PrimaryCanvas` keeps one **dynamic live stage** in the centre, a right **receipt rail** overlaid on the stage (does not shift centre layout), an optional left **transcript drawer**, and a quiet **pinned-support shelf**. Receipts (`ContentWidget` with `data.display="receipt"`) never steal the centre. Foreground consent (`PendingInputWidget`) outranks ordinary selected content when it blocks the current turn; background approval receipts stay in the rail until selected. `WidgetWrapper` supports `layoutMode="stage"` for the single focused hero.
- **Lifecycle:** Live data is backend-pushed via `ui.update`; reconnects receive `ui.snapshot` from domain-owned snapshot providers. `WidgetWrapper` handles local expiry and chrome only — it does not poll the backend.
- **Reason:** Decouples layout from content. Tool authors choose no push, receipt, or content once at the tool layer; backend producers own update cadence; the frontend applies the visual hierarchy. Code-splitting keeps initial bundle lean.

### 4. Attachment Pipeline (Multimodal)
**Pattern:** Immediate Buffer, Consume on Turn.
- **Upload:** `ControlBar` reads a file/paste, stores it as `pendingAttachment` in Zustand, and immediately sends `user_attachment` over WS so the backend buffers it on the session.
- **Consume:** When a turn is created (text submit or voice transcript arrival), `consumePendingAttachment()` atomically returns the data URL and clears the store. The transcript item is created with `attachments: [{ type, url }]`.
- **Why store, not local state:** Both `ControlBar` (text turns) and `JarvisClient` (voice turns) need to consume the same attachment at transcript-creation time. Zustand makes it accessible to both without prop drilling or backend round-trips.

### 5. Voice User Transcript (Partials vs Final)
**Pattern:** Interim blue text, then in-place commit to final gray.

- **Apple Speech / Cartesia (selected in Settings → Voice & Audio):** `conversation.partial` updates user text in blue (`isPartial: true`) while speaking; `conversation.transcript` commits the same `turn_id` as final gray text.
- **On-device speech:** First-run setup and Start voice prepare Apple Speech automatically. Settings should not be required just to download the model.
- **Late continuation:** `turn_id` keeps one transcript row across merged segments.

### 6. Direct Action Bus (Latency)
**Pattern:** Optimistic UI / RPC.
- **Concept:** User interactions bypass the LLM reasoning loop.
- **Flow:** Click Button -> `client.send('ui.action', { plugin, tool, args })` -> Backend Router -> Python Function -> optional `ui.update`.
- **Reason:** Instant feedback (<50ms) vs LLM round-trip (>2s).
- **Shipped examples:** `PendingInputWidget` (approve/deny), `TodoWidget` (toggle), `InboxWidget` (archive / mark read with optimistic row updates).

### 7. History Hydration (Transcript Persistence)
**Pattern:** REST for initial load, WebSocket for live updates.
- **On connect:** `JarvisClient.loadHistory()` fetches `GET /api/v1/history/` and populates the Zustand transcript — only when empty (prevents duplicate on reconnect).
- **Types:** User/assistant turns, capability-call receipts parsed from the existing preview string (`plugin.tool({…})`, collapsed by default), and provider reasoning (`type: "reasoning"`, correlated by `response_id`, collapsed by default via `isCollapsed`). Consecutive assistant text, receipts, and reasoning share a Jarvis turn only while they share `turn_id`. Unsolicited system speech is a separate Jarvis turn labeled Notice. Receipts are a local holographic panel, not a chat-kit tool card; rich outcomes still belong to widgets.
- **Live:** All subsequent turns arrive via WebSocket as normal (`conversation.transcript`, `conversation.response`, `conversation.reasoning` on text turns).
- **Why REST, not WebSocket:** Bulk history load over WebSocket would block the connection handshake. REST is fire-and-forget, keeps WS clean for real-time events.

### 8. Widget Snapshot Hydration
**Pattern:** Domain snapshots over WebSocket.
- **On connect:** Backend sends `ui.snapshot` after `system.connect`.
- **Backend ownership:** Snapshot providers rebuild active widgets from durable domain stores, currently pinned widgets, pending inputs, and running background tasks (as progress receipts).
- **Frontend behavior:** `JarvisClient` converts the snapshot list into a widget map and calls `setWidgets(...)`; the snapshot replaces the active widget map.
- **Why not localStorage:** Active widgets are restored from backend-owned state. One-off receipts remain ephemeral; progress receipts can be rebuilt when they represent durable domain state such as a running background task.

### 9. Shell Navigation
**Pattern:** Voice-first visual fallback.
- **Rule:** Persistent chrome is reserved for work that benefits from a screen: setup, recovery, scanning recent events, comparing lists, and confirming risky state. If a task is natural by voice and does not need scanning, credentials, or recovery, it should not become a top-level button.
- **Primary destinations:** StatusBar exposes compact labeled destinations for **Activity**, **Home**, **Apps**, and **Settings**. Labels are always visible; hover labels are supplemental only. Audio/model/credential configuration belongs to Settings rather than competing with product destinations. Cartesia input, optional spoken replies, mic/speaker selection, and owner wake enrollment live together in **Settings → Voice & Audio**.
- **Trust pill:** Answers “What is JARV1S doing?” with a sentence-case status label (`Ready`, `Voice active`, `Listening`, `Mic muted`, …). Local time is quiet secondary metadata in the same pill. Its recovery popover stays small: state, latency, core name, and reconnect.
- **Developer mode:** **Diagnostics** (performance, snapshots, pipeline detail) is hidden unless `import.meta.env.DEV` or `localStorage.jarvis.developer_mode === '1'`. `Cmd/Ctrl+Shift+D` enables it at runtime and opens Diagnostics; the Diagnostics menu includes a disable action for packaged builds. The shortcut ignores editable fields so it does not steal input while the user is typing. Room speakers and phone pairing live in **Rooms & devices**, not Diagnostics.
- **Interaction rule:** Labeled controls are destinations. Icon-only controls are reserved for momentary actions and must be visually separated from destinations.

#### Shell surfaces (when to use what)

| Primitive | Implementation | Use when |
| :--- | :--- | :--- |
| **Stage + chrome** | `RootLayout`, `PrimaryCanvas`, `StatusBar`, `ControlBar` | Always; agent, widgets, and transcript live here |
| **StatusBarSurfaceHost** | Persistent top-right host for Activity (glance + workspace), Diagnostics, Apps, Settings, Smart Home, and Rooms & devices | Morphing StatusBar destinations; menu and workspace semantics remain distinct inside one shell |
| **StatusBarWorkspaceHeader** | Shared title chrome for workspace-kind host content | Activity, Apps, Settings, Smart Home, Rooms & devices; owns the explicit close button |
| **StatusBarMenuContent** / **MenuSectionHeader** | Glance-menu content chrome | Activity peek, Diagnostics, and other action menus; no close button |
| **HolographicMenu** | Independently anchored StatusBar popovers | Small action menus outside the shared right-side destination host, such as trust recovery |

- **Dismiss policy:** Host closes on outside click, Escape, and re-clicking the active destination. Workspace content adds one `StatusBarWorkspaceHeader` close. Glance menus rely on host dismiss only.
- **Never trap for config:** StatusBar workspaces stay non-modal so the user may leave them open while the stage continues.
- **One navigation surface at a time:** `activeOverlay` owns workspace destinations; local StatusBar state owns glance menus. `StatusBarSurfaceHost` resolves both into one shell, with **workspaces preferred over menus** so promoting a glance into its workspace (`openOverlay` while a menu is open) morphs the host in place. Do not call host dismiss before opening the overlay — that tears down the shell. Re-clicking an active destination closes it.
- **Layering:** Stage `z-10`, chrome and its navigation surfaces `z-[65]` (above modal backdrop), modal backdrop `z-60`, modal panels `z-70`, blocking banners (e.g. pairing) `z-100`. Select popups portal above the owning dialog (`z-[80]`).
- **Shell gutter:** `StatusBarSurfaceHost` and the receipt rail both use `top-shell-overlay` and `right-6`, keeping every right-side surface aligned to the navigation shell. Workspace mode and the receipt rail extend to `bottom-safe-bottom`.
- **Surface motion:** Opening uses a top-right opacity/scale reveal. Destination changes fade outgoing content, morph the isolated shell width and height, crossfade its bracket/full-frame chrome, then fade through incoming content laid out at its final anchored size. The shell clips the transition, so text neither scales nor reflows frame by frame.

#### Operational UI primitives
Feature panels compose shared controls rather than recreating Tailwind recipes:
- `StatusBarWorkspaceHeader` for StatusBar workspace titles; do not invent per-panel close chrome on host surfaces.
- `SegmentedTabs` is the accessible workspace-tab primitive; `Chip` is reserved for filter/toggle groups.
- `FieldControl`, `Input`, `SearchField`, and `Select` own persistent labels, 44px targets, clear/focus states, and themed menus. `Select` and `Modal` wrap Base UI behavior; feature code imports the JARV1S wrappers only.
- `PanelSection` is the low-emphasis inner surface below a `Hologram`; `DataField`, `EmptyState`, and `Switch` standardize facts, guidance, and binary settings.
- Desktop inspection surfaces use list/detail grids. On narrow layouts, detail replaces the list and provides Back.

### 10. Activity And Configured Work
**Pattern:** Lightweight recent glance plus a scan-and-investigate workspace. Two distinct information types — "what happened" vs "what's configured" — have explicit tab identity.
- **Activity:** `OperationsPanelContent` loads `/api/v1/activity/page` with an opaque cursor inside `StatusBarSurfaceHost` (`activeOverlay: 'operations'`). Primary facets are `All`, `Reminders`, `Automations`, `System`, and `Conversations`. **All excludes conversations by default** (operational runs only); Conversations is the opt-in facet for cross-node dialogue audit. Outcome, time, source, node, and search are secondary filters with visible applied state. Results are grouped by day. Selecting a row lazy-loads canonical detail in a stable desktop pane; narrow layouts replace the list and provide Back. Background tasks open their task widget.
- **Quick view:** `ActivityMenu` uses the compatibility activity helper for a small glanceable **operational** list (no user turns). **View all activity** calls `openOverlay('operations')` only; because workspaces win over menus in the host, the glance morphs into the Activity workspace.
- **Configured:** `/api/v1/operations/setups` is the one read contract for surfaced schedules, event automations, reminders, timers, alarms, deferred instructions, and saved protocols. Rules support optimistic enable/pause with rollback through `PATCH /operations/setups/{id}`. Protocols are visibly read-only. “View activity” moves to the matching timeline filter rather than opening another surface.
- **Live refresh:** Canonical domain writes publish `activity.changed` or `operations.changed`; the WebSocket bridge bumps the appropriate Zustand version and only the open surface refetches.
- **Component boundary:** `features/operations/ActivityTimeline.tsx` owns compact rows only. `OperationRunDetail.tsx` owns lazy technical detail. The timeline never embeds full trace payloads.
- **Why not SDUI:** This is an inspection surface over existing stores, not a pushed domain widget. It should lazy-load from REST so the normal UI path does not copy large trace blobs.

### 11. Smart Home Visibility
**Pattern:** On-demand inspection overlay over Home Assistant readiness and guided connect.
- **Entry:** StatusBar **Home** opens `HomePanel` (`activeOverlay: 'smart_home'`). **Home Assistant** opens `HomeAssistantPanel` (`activeOverlay: 'home_assistant'`). Presence (`Rooms & devices`) stays in the same nav family.
- **Data:** `smartHomeApi.getStatus()` fetches `GET /api/v1/smart-home/status` when the panel opens (no background polling). Backend maps liveness/readiness into explicit UI states (`unconfigured`, `unreachable`, `auth_failed`, `empty_inventory`, `ready`, etc.) with `next_action` hints.
- **Setup (unconfigured / auth_failed / invalid_config):** `HomeAssistantSetup` auto-discovers fixed local candidates, then browser-authorizes. Install guidance is progressive disclosure (“Don’t have Home Assistant yet?”). Manual URL + long-lived token stays under **Connect manually**. Authorization reuses `beginOAuthAuthorization` / `watchOAuthCompletion` with `openExternalUrl` for local HA instances and completes through the standard `auth.oauth.changed` event.
- **Content (connected):** Compact connection status, controllable devices grouped by HA area (primary), then secondary links for **HA rooms** (in-panel) and **Rooms & devices** (morphs to Presence). Empty inventory prompts opening Home Assistant to add devices.
- **Actions:** **Open Home Assistant**, **Refresh**, **HA rooms → Manage** (`panelMode: 'rooms'` with header back), **Rooms & devices → Manage** (`openOverlay('presence')` — host morphs; Presence header back returns to Home Assistant / Home).
- **Why not SDUI:** Same rationale as History — a setup/inspection surface over existing HA helpers, not a pushed widget.

### 12. Rooms & Devices
**Pattern:** StatusBar workspace for things that hear/speak (This Mac, phones, room speakers).
- **Entry:** Smart Home overview **Rooms & devices → Manage** opens `PresencePanelContent` (`activeOverlay: 'presence'`) in `StatusBarSurfaceHost` (`workspace-narrow`). Title copy is **Rooms & devices**. Header `leading` back opens `smart_home` so the host morphs in place.
- **Data:** `presenceApi.getPresence()` fetches `GET /api/v1/presence/` when the panel opens, on manual refresh, and when `presence.changed` bumps Zustand. Backend merges in-memory WebSocket sessions with Mongo `ws_device_credentials`. Optional HA room list powers **Assign room**.
- **Content:** Devices grouped Online / Offline with plain kind labels (`Room speaker`, `This Mac`, `Phone`), room, capabilities, last seen, and status. Setup cards: **Add a device** collapsible rows for **Phone** (QR/code, gated on private access when remote) and **Room speaker** (**Connect speaker** — Host LAN-pairs first; fallback is a paste-able `jarvis-satellite pair` on the speaker). Offline room speakers expose one primary **Reconnect** action; remaining actions live in `ActionMenu`.
- **Actions:** **Assign room** is the room name (or “Assign room”) as a `TextLink` (`presenceApi.assignNodeRoom`). **Reconnect** tries Host LAN pair against that speaker’s `node_id` (keeps its room), then the same fallback command. Overflow holds check-again, copy address, Activity, and Remove access (confirm-gated revoke; disabled for this device and live-only nodes without a credential). Empty/offline copy points at next steps in plain language (finish private access, add room speaker) — not CLI tasks.
- **Availability link:** Private Tailscale access lives in **Settings → Availability** (`HostSettings` + `enableHostServe`); Host LAN pair (and the fallback command) include the Serve origin. Opening Availability from Rooms & devices uses `openOverlay('settings', …)` so the host morphs.
- **Why not SDUI:** Operational surface for multi-device trust — same StatusBar workspace pattern as Smart Home and Activity.

### 13. Apps
**Pattern:** StatusBar workspace for connection trust — one Apps flow regardless of OS permission, direct OAuth, or cloud connector.
- **Entry:** StatusBar **Apps** opens `IntegrationsPanel` (`activeOverlay: 'integrations'`).
- **Data:** `integrationsApi` + `oauthApi` when the panel opens. Connection labels: **On this Mac**, **Direct** / **Advanced**, **Cloud connector — powered by Composio**.
- **Connect:** Gmail/Google uses bundled `product_oauth.json` when present (provider sign-in); otherwise Advanced. Calendar can use EventKit without OAuth. Composio apps use Connect Link and mount only an explicit `tools` allowlist.
- **Why not SDUI:** Inspection/setup over `IntegrationView` and provider grants, not a pushed widget.

---

## Multi-Device Strategy

**Pattern:** Dumb Terminal / Smart Backend.
- **Identity:** Each client announces endpoint context on the WebSocket handshake. The backend returns a `PresenceIdentity` over `system.connect`:
  ```json
  {
    "connection_id": "conn-ab12cd34ef56",
    "owner_id": "default",
    "node_id": "browser-9f3a...",
    "node_label": "Kitchen Display",
    "capabilities": ["display", "mic", "speaker"],
    "location": {
      "provider": "manual",
      "room_id": "kitchen",
      "room_name": "Kitchen",
      "ha_area_id": null,
      "ha_device_id": null,
      "ha_entity_id": null
    }
  }
  ```
- **Frontend defaults:** `JarvisClient` persists `node_id` in `localStorage`, sends `capabilities=mic,speaker,display`, and passes optional location refs from URL query params (`room_id`, `room_name`, `ha_area_id`, `ha_device_id`, `ha_entity_id`). It does not send `owner_id`; the backend derives that from trusted configuration/auth.
- **Ephemeral GPS:** On connect and foreground, the client sends `context.update` with `{source=gps, …}`. Desktop uses the Host Core Location bridge; phone/browser use the Geolocation API. No IP fallback. Denied/unavailable clears session location (`location: null`).
- **Device credentials:** Browser pairing (Rooms & devices / Availability) sets a same-origin `HttpOnly`, `SameSite=Strict` cookie; durable device tokens are not stored in JavaScript-accessible storage. Room speakers consume a pairing code via Host LAN pair (`POST` to the speaker `:8742/pair`) or fallback `jarvis-satellite pair` (`POST /device-auth/pair`, `client_surface=satellite`); `POST /device-auth/satellites` remains CLI recovery. Pairing links contain a short-lived one-time code and QR images are generated locally.
- **Preferences:** User-facing runtime preferences are backend-owned. `system.connect` includes the current `preferences` snapshot, `preferences.update` broadcasts changes, and the frontend writes changes through REST (`PATCH /api/v1/preferences/`) rather than `localStorage` so browser and satellite behavior stays aligned.
- **Targeting:**
  - **User Turn:** Response routed only to the requesting socket.
  - **Owner Default:** Legacy owner-targeted messages resolve to the currently active/default connection for that owner.
  - **State Update:** Broadcast to all sockets when the state is genuinely global (e.g., "Music Playing" updates everywhere).
- **Room Semantics:** Location refs are advisory metadata, not a first-class room graph. Home Assistant can become the source of truth later without migrating frontend identity.
- **Follow-Me:** Backend manages session portability; Frontend simply renders what it is told.

### Satellite Client

A Raspberry Pi/ReSpeaker room node follows the same identity model as the browser client:

- It sends a stable `node_id`, human `node_label`, capabilities (`mic`, `speaker`, optional `display`), and optional HA area refs.
- The implemented audio-only client lives in `satellite/`; it omits React/UI concerns and ignores widget/transcript messages while the backend still owns turns, widgets, traces, and routing decisions.
- It streams microphone PCM to the backend and plays assistant audio returned over the JARV1S protocol, reporting `audio.playback_end` after local playback drains and backend `audio.tts_end` confirms the stream is complete. A per-turn timeout covers a missing marker without adding happy-path tail latency.
- It is not a Home Assistant Assist or Wyoming satellite in V1. HA provides device/area inventory; JARV1S owns voice transport and agent execution.

---

## Directory Structure
```text
frontend/
├── src/
│   ├── client/              # The Headless Core
│   │   ├── JarvisClient.ts  # Logic: WS, Audio, DeviceID
│   │   ├── integrationsApi.ts # REST client for /api/v1/integrations
│   │   ├── oauthApi.ts        # REST client for /api/v1/auth (Google/Microsoft)
│   │   ├── operationsApi.ts   # REST client for activity + operations drill-down
│   │   ├── smartHomeApi.ts    # REST client for /api/v1/smart-home/status
│   │   └── presenceApi.ts     # REST client for /api/v1/presence
│   ├── store/               # Global State
│   │   └── useJarvisStore.ts # Zustand Store
│   ├── components/          
│   │   ├── layout/          # 3-Zone Layouts (StatusBar, ControlBar, PrimaryCanvas)
│   │   ├── features/        # Business logic components (Transcript, Widgets, Operations, LiveStage)
│   │   │   ├── live-stage/  # Derived focal-stage presentation + projection UI
│   │   │   └── widgets/     # SDUI registry, receipt rail helpers, receipt activation
│   │   └── ui/              # Atom Primitives (Hologram, Button, Modal, StatusDot, Divider, SectionHeader)
│   ├── types/               # Shared Protocol Types (UIEnvelope)
│   ├── config/              # Logic Configurations
│   │   └── systemStatus.ts  # State Matrix Definitions
│   └── App.tsx              # Application Root
```
