# JARV1S Documentation

## Start here

| Doc | Purpose |
| :--- | :--- |
| [VISION.md](./VISION.md) | Product direction and tenets |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Codebase map and turn pipeline (normative for implementation) |
| [PLUGIN_ARCHITECTURE.md](./PLUGIN_ARCHITECTURE.md) | Canonical plugin, capability, permission, evidence, and runtime contract |
| [PLUGIN_CONFORMANCE.md](./PLUGIN_CONFORMANCE.md) | Current first-party plugin scorecard and prioritized contract gaps |
| [proposals/built/DYNAMIC_TOOL_ROUTING.md](./proposals/built/DYNAMIC_TOOL_ROUTING.md) | Canonical tool-manifest routing, modality policies, focus, and proactive handoff boundaries |
| [SYSTEM_STATES.md](./SYSTEM_STATES.md) | Voice modes, sessions, attention, and trigger delivery states |
| [TRIGGER_AUTHORING.md](./TRIGGER_AUTHORING.md) | Canonical trigger authoring contract: action, delivery, attention, freshness, offers, and evaluate paths |
| [ROADMAP.md](./ROADMAP.md) | What's done and what's next |
| [SATELLITE.md](./SATELLITE.md) | Room voice endpoint (`satellite/`) — protocol, audio path, auth, deployment |
| [deployment/MULTI_DEVICE_REACHABILITY.md](./deployment/MULTI_DEVICE_REACHABILITY.md) | Private access, LAN/tailnet/public WebSocket reachability, Rooms & devices auth, turn-origin delivery |
| [deployment/JARVIS_HOST_STARTUP_CONTRACT.md](./deployment/JARVIS_HOST_STARTUP_CONTRACT.md) | Shared startup phases, user-facing status, and failure copy for JARV1S launch surfaces |
| [deployment/VERSIONING_AND_DEPENDENCIES.md](./deployment/VERSIONING_AND_DEPENDENCIES.md) | App versioning, lockfiles, Docker image pins, and MongoDB upgrade policy |
| [deployment/PRIVATE_BETA_GATE.md](./deployment/PRIVATE_BETA_GATE.md) | Required clean-install, reliability, remote-access, update, and eval evidence before beta release |
| [CORE_TOOLS.md](./CORE_TOOLS.md) | Plugin and tool inventory |
| [BACKGROUND_AGENTS.md](./BACKGROUND_AGENTS.md) | Background agent runtimes (`mode="code"` vs `mode="jarvis"`) |

The installed desktop app is the personal runtime and owns the supported data under `~/Library/Application Support/JARV1S`. `task start` and Docker are isolated contributor infrastructure.

Desktop details: see [`apps/desktop/README.md`](../apps/desktop/README.md) and [`proposals/JARVIS_HOST_APP.md`](./proposals/JARVIS_HOST_APP.md). Use `task desktop:dogfood` for the installed app, `task desktop:dev` for disposable contributor mode, and `task desktop:doctor` for smoke checks.

## UI

| Doc | Purpose |
| :--- | :--- |
| [UI/PRODUCT_BRIEF.md](./UI/PRODUCT_BRIEF.md) | Cross-surface product experience and decision principles |
| [UI/FOUNDATIONS.md](./UI/FOUNDATIONS.md) | Token inventory (code is source of truth) |
| [UI/VISUAL_LANGUAGE.md](./UI/VISUAL_LANGUAGE.md) | Holographic identity and focused UI refactor rules |
| [`.cursor/skills/jarvis-ui/`](../.cursor/skills/jarvis-ui/SKILL.md) | Agent skill for frontend UI/UX work |
| [UI/FRONTEND_ARCHITECTURE.md](./UI/FRONTEND_ARCHITECTURE.md) | React client, WebSocket, SDUI |
| [UI/WIDGET_SYSTEM.md](./UI/WIDGET_SYSTEM.md) | Widget contracts and lifecycle |
| [UI/STYLE_GUIDE.md](./UI/STYLE_GUIDE.md) | Visual implementation |

## Proposals

| Location | Meaning |
| :--- | :--- |
| [`proposals/partial/`](./proposals/partial/) | Code exists; **same doc** still specs remaining work |
| [`proposals/built/`](./proposals/built/) | Shipped slice (often historical). Check **Superseded by** and [ROADMAP](./ROADMAP.md) — later phases in-file may be unbuilt |
| [`proposals/`](./proposals/) | Not started, deferred, or partial without a `partial/` doc (e.g. [UI_ACTION_BUS](./proposals/UI_ACTION_BUS.md)) |

**Partial (active spec):** [turn lifecycle](./proposals/partial/TURN_LIFECYCLE_CLEANUP.md) · [habits](./proposals/partial/HABITS_AND_GOALS.md) · [background trust](./proposals/partial/BACKGROUND_TASK_TRUST_HARDENING.md) · [HA first-device pairing](./proposals/partial/HA_FIRST_DEVICE_PAIRING.md) · [voice satellite edge](./proposals/partial/VOICE_SATELLITE_EDGE.md)

**Built:** [barge-in](./proposals/built/BARGE_IN_RELIABILITY.md) · [keyless onboarding lanes](./proposals/built/KEYLESS_ONBOARDING_LANES.md)

**Proposed / deferred:** [named work](./proposals/NAMED_WORK.md) · [conductor](./proposals/CONDUCTOR_ORCHESTRATION.md) · [morning briefing](./proposals/MORNING_BRIEFING.md) · [automation primitive](./proposals/AUTOMATION_PRIMITIVE.md) · [content widgets](./proposals/CONTENT_WIDGET_EXPANSION.md) · [delivery attempts](./proposals/NOTIFICATION_ATTEMPTS_FUTURE.md)

**Superseded:** [action runtime migration](./proposals/ACTION_RUNTIME_MIGRATION.md) — structured `tools=` loop and CodeAct deletion landed; remaining follow-ons are evidence contracts and local constrained decoding.

**In progress (not partial/built until complete):** [JARV1S desktop app](./proposals/JARVIS_HOST_APP.md) — Phase 0–1b shipped under `apps/desktop/` (bundled `mongod`, signing/release CI); clean-machine validation and Phase 1a exit (voice, `N-1`→`N` dogfood) remain

Built docs superseded by [BACKGROUND_AGENTS](./BACKGROUND_AGENTS.md) or [ARCHITECTURE](./ARCHITECTURE.md) when the status line says so.

## Research

[research/](./research/) — Non-normative notes.

| Doc | Purpose |
| :--- | :--- |
| [LOCAL_STREAMING_STT.md](./research/LOCAL_STREAMING_STT.md) | **On-device STT path** (Apple Speech helper) and its Host WebSocket contract |
| [VOICE_EVALS.md](./research/VOICE_EVALS.md) | Voice eval ladder: wakeword, STT, latency, tool routing, agent behavior |
| [HARNESS_ENGINEERING_LESSONS.md](./research/HARNESS_ENGINEERING_LESSONS.md) | Lilian Weng harness/RSI research mapped to JARV1S architecture decisions |
| [CLAUDE_CODE_ARCHITECTURE_LESSONS.md](./research/CLAUDE_CODE_ARCHITECTURE_LESSONS.md) | Coding-agent loop and context patterns for JARV1S |
| [OPENCLAW_ARCHITECTURE_LESSONS.md](./research/OPENCLAW_ARCHITECTURE_LESSONS.md) | Gateway/OS and memory patterns for JARV1S |

## Other

- Contributor test posture: [`.cursor/rules/test-strategy.mdc`](../.cursor/rules/test-strategy.mdc) — when to add or keep tests; prefer behavior over implementation coupling.
