# Vision

To build a **highly responsive, locally-grounded AI Home Assistant** (JARV1S) that feels alive, personal, and powerful.

## Key Tenets
1.  **Fast Voice Interaction:** Latency is the enemy. It should feel conversational, not transactional.
2.  **Privacy/Local First:** Where possible, run things locally (wake word, STT, TTS, private servers). Room satellites still depend on the central Host for synthesis and playback routing.
3.  **Proactive Assistance:** JARV1S should warn of upcoming events or issues before they happen.
4.  **Environmental Integration:** JARV1S isn't just a speaker; it uses screens (widgets), multiple rooms (distributed audio), and home hardware.
5.  **Intelligent Triage:** JARV1S chooses the best way to respond—voice for urgency, UI widgets for detail, or silence for background tasks.
6.  **Speaker & Intent Awareness:** JARV1S knows who is speaking and whether a comment is directed at it or someone else in the room.

## Core System Architecture
*   **Voice-First Pipeline:** VAD endpointing, pluggable streaming STT (Cartesia cloud or on-device Apple Speech), and pluggable streaming TTS (Cartesia cloud or on-device Kokoro, or text-only).
*   **Structured Capability Calls:** An agent that interacts with the world by emitting named JSON tool calls. For heavy tasks, JARV1S dispatches to a local coding worker (Cursor or Claude Code) or an in-process Jarvis agent, and stays fast on the voice loop.
*   **Modular Plugin System:** Self-discovering plugins. A semantic Tool Router offers only the relevant plugin schemas per turn via `tools=`. User-owned Agent Skills live in Home and are procedures, not plugins.
*   **Identity & Presence Management:** Distinguishing between users and maintaining awareness of who is in which room.
*   **Dynamic Context:** Real-time awareness of time, location, and user preferences for personalized turns.
*   **Contextual UI:** The ability for the agent to push data (widgets, images, status) to the frontend for visual context.
*   **Centralized Brain, Distributed Presence:** A single intelligent core that coordinates multiple room instances (Lobby, Bedroom) and handles cross-room alerts. V1 room satellites are JARV1S WebSocket nodes with stable presence metadata, not Home Assistant Assist satellites.
*   **Omni-Channel Input:** Voice, text, and image input through a unified pipeline. Voice stays primary; text and multimodal extend reach to when you can't speak or need to share visual context.
*   **Starvation-Free Concurrency:** Lane-based priority scheduling (Voice > System > Background) to ensure responsiveness never degrades due to background tasks.
*   **Two-Tier Memory:** Core profile facts injected every turn (Layer 1) plus timestamped archival events with semantic recall on demand (Layer 2). Personality lives in user-owned Agent Home `PROMPT.md`; product reliability lives in packaged `SYSTEM.md`.
*   **System Pulse:** A background heartbeat service that allows the agent to be proactive independent of user triggering.

## Skills & Capabilities (Plugins)
*   **Advanced Scheduling:** ✅ Recurring alarms, series control, snooze, DST-safe recurrence, offline buffering.
*   **Protocols & Routines:** ✅ User-defined multi-step routines with execution history, alarm-linked or on-demand. The primary differentiator — routines that remember, adapt, and compose.
*   **Memory & Personalization:** ✅ Core facts shape every response; archival recall answers "when did I mention…?" This is what commodity assistants lack.
*   **System Control & Diagnostics:** ✅ Volume, app control, consent-gated shell, machine health, file access.
*   **Subagent Dispatch:** Spawn a local coding worker (Cursor or Claude Code) or an in-process Jarvis agent as background work. Named work (`work_id` + title) survives the Home conversation window.
*   **Information & Research:** ✅ Web search, unified calendar (EventKit on this Mac plus Google + Outlook OAuth).
*   **Home Control:** ✅ Home Assistant via direct REST + WebSocket. Product setup: Smart Home panel (URL + long-lived token → `system_config` + CredentialStore). Contributor CLI: `task setup:home` (connect existing, onboard fresh, or Docker bootstrap). Eight curated tools cover search, control, setup validation, Tuya refresh/reconcile (`refresh_home_assistant`, `organize_device`), and room binding. Vendor apps commission devices; JARV1S handles post-HA naming, areas, and control — see [HA_FIRST_DEVICE_PAIRING.md](./proposals/partial/HA_FIRST_DEVICE_PAIRING.md).
*   **Media & Music:** Streaming music to specific speakers or syncing audio across the house.

## Future Inspiration (The "Jarvis" Benchmark)
*   **Proactive Protocols:** Complex multi-device routines (e.g., "House Party Protocol").
*   **Environmental Awareness:** Knowing which room a user is in and adjusting behavior accordingly.
