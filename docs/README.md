# JARV1S Documentation

## Start here

| Doc | Purpose |
| :--- | :--- |
| [VISION.md](./VISION.md) | Product direction and tenets |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Codebase map and turn pipeline |
| [AGENT_HOME.md](./AGENT_HOME.md) | User-owned Home overlay (`PROMPT.md`, skills, extra MCP) |
| [PLUGIN_ARCHITECTURE.md](./PLUGIN_ARCHITECTURE.md) | Plugin / capability / dispatcher contract |
| [CORE_TOOLS.md](./CORE_TOOLS.md) | Plugin inventory and how `tools=` is offered |
| [BACKGROUND_AGENTS.md](./BACKGROUND_AGENTS.md) | Delegated work: `mode="code"` vs `mode="jarvis"`, named work |
| [SYSTEM_STATES.md](./SYSTEM_STATES.md) | Voice modes, sessions, attention, and trigger delivery |
| [TRIGGER_AUTHORING.md](./TRIGGER_AUTHORING.md) | Trigger authoring: action, delivery, attention, freshness |
| [ROADMAP.md](./ROADMAP.md) | Shipped history and what's next |
| [SATELLITE.md](./SATELLITE.md) | Room voice endpoint (`satellite/`) |

The installed desktop app is the personal runtime and owns data under `~/Library/Application Support/JARV1S`. `task start` and Docker are isolated contributor infrastructure.

Desktop details: [`apps/desktop/README.md`](../apps/desktop/README.md). Use `task desktop:dogfood` for the installed app, `task desktop:dev` for contributor mode, and `task desktop:doctor` for smoke checks.

## Deployment

| Doc | Purpose |
| :--- | :--- |
| [MULTI_DEVICE_REACHABILITY.md](./deployment/MULTI_DEVICE_REACHABILITY.md) | LAN / tailnet / public WebSocket reachability and device auth |
| [JARVIS_HOST_STARTUP_CONTRACT.md](./deployment/JARVIS_HOST_STARTUP_CONTRACT.md) | Shared launch phases and failure copy |
| [VERSIONING_AND_DEPENDENCIES.md](./deployment/VERSIONING_AND_DEPENDENCIES.md) | App versioning, lockfiles, Docker pins, MongoDB upgrades |
| [PRIVATE_BETA_GATE.md](./deployment/PRIVATE_BETA_GATE.md) | Evidence required before a beta release |

## UI

| Doc | Purpose |
| :--- | :--- |
| [UI/PRODUCT_BRIEF.md](./UI/PRODUCT_BRIEF.md) | Cross-surface product experience |
| [UI/FOUNDATIONS.md](./UI/FOUNDATIONS.md) | Token inventory (code is source of truth) |
| [UI/VISUAL_LANGUAGE.md](./UI/VISUAL_LANGUAGE.md) | Holographic identity |
| [`.cursor/skills/jarvis-ui/`](../.cursor/skills/jarvis-ui/SKILL.md) | Agent skill for frontend UI work |
| [UI/FRONTEND_ARCHITECTURE.md](./UI/FRONTEND_ARCHITECTURE.md) | React client, WebSocket, SDUI |
| [UI/WIDGET_SYSTEM.md](./UI/WIDGET_SYSTEM.md) | Widget contracts and lifecycle |
| [UI/STYLE_GUIDE.md](./UI/STYLE_GUIDE.md) | Visual implementation |

## Proposals

| Location | Meaning |
| :--- | :--- |
| [`proposals/partial/`](./proposals/partial/) | Code exists; the same doc still specs remaining work |
| [`proposals/built/`](./proposals/built/) | Shipped slice. Later in-file phases may still be unbuilt |
| [`proposals/`](./proposals/) | Not started, deferred, or leftover design notes |

**Partial:** [turn lifecycle](./proposals/partial/TURN_LIFECYCLE_CLEANUP.md) · [habits](./proposals/partial/HABITS_AND_GOALS.md) · [background trust](./proposals/partial/BACKGROUND_TASK_TRUST_HARDENING.md) · [HA first-device pairing](./proposals/partial/HA_FIRST_DEVICE_PAIRING.md) · [voice satellite edge](./proposals/partial/VOICE_SATELLITE_EDGE.md)

**Built (current contracts):** [tool routing](./proposals/built/DYNAMIC_TOOL_ROUTING.md) · [barge-in](./proposals/built/BARGE_IN_RELIABILITY.md) · [keyless onboarding](./proposals/built/KEYLESS_ONBOARDING_LANES.md) · [local-first connections](./proposals/LOCAL_FIRST_INTEGRATIONS.md)

**Shipped V1, leftover in-file:** [named work](./proposals/NAMED_WORK.md) — `work_id` + title over `background_tasks`; remaining notes are dogfood, not a new collection.

**Proposed / deferred:** [conductor](./proposals/CONDUCTOR_ORCHESTRATION.md) · [morning briefing](./proposals/MORNING_BRIEFING.md) · [automation primitive](./proposals/AUTOMATION_PRIMITIVE.md) · [content widgets](./proposals/CONTENT_WIDGET_EXPANSION.md) · [delivery attempts](./proposals/NOTIFICATION_ATTEMPTS_FUTURE.md)

**Superseded:** [action runtime migration](./proposals/ACTION_RUNTIME_MIGRATION.md) — structured `tools=` loop landed.

**In progress:** [JARV1S desktop app](./proposals/JARVIS_HOST_APP.md) — Phase 0–1b under `apps/desktop/`; clean-machine validation and Phase 1a dogfood remain.

Built docs that say **Superseded by** defer to [BACKGROUND_AGENTS](./BACKGROUND_AGENTS.md) or [ARCHITECTURE](./ARCHITECTURE.md).

## Research

[research/](./research/) — Non-normative notes.

| Doc | Purpose |
| :--- | :--- |
| [LOCAL_STREAMING_STT.md](./research/LOCAL_STREAMING_STT.md) | On-device Apple Speech STT path |
| [VOICE_EVALS.md](./research/VOICE_EVALS.md) | Voice eval ladder |
| [HARNESS_ENGINEERING_LESSONS.md](./research/HARNESS_ENGINEERING_LESSONS.md) | Harness / RSI research mapped to JARV1S |
| [CLAUDE_CODE_ARCHITECTURE_LESSONS.md](./research/CLAUDE_CODE_ARCHITECTURE_LESSONS.md) | Coding-agent loop patterns |
| [OPENCLAW_ARCHITECTURE_LESSONS.md](./research/OPENCLAW_ARCHITECTURE_LESSONS.md) | Gateway / memory patterns |

## Other

- Contributor test posture: [`.cursor/rules/test-strategy.mdc`](../.cursor/rules/test-strategy.mdc)
