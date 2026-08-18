# Jarvis Host Startup Contract

This contract defines what any Jarvis Host launch surface must do before the user can reach first-run setup or chat. It applies to `task start`, contributor desktop mode, and the packaged desktop shell.

The goal is not to create another setup doctor. The goal is one shared startup path with clear status, actionable failure states, and no duplicate readiness logic.

## Principles

- Start with the smallest path to first text value: open Jarvis, configure an LLM provider if needed, send a turn.
- Show status for any step that takes more than a moment. Users should never stare at a silent terminal or blank app window.
- Use existing readiness sources. Launchers should call the same health and setup APIs the app uses.
- Keep optional capability setup out of the blocking path. Voice, smart home, search, weather, background agents, integrations, and satellites are post-core lanes.
- Make errors recoverable. Every user-facing failure should say what failed, why it matters, and the next action.

## Non-Goals

- This is not a native app packaging spec.
- This is not a setup dashboard design.
- This is not a provider onboarding expansion.
- This is not a separate doctor/checker abstraction.
- This does not add new readiness APIs or duplicate `require_llm_ready()`.

## Startup Phases

| Phase | User-facing copy | Required for first text turn | Existing source of truth |
| :--- | :--- | :--- | :--- |
| `check_prerequisites` | Checking your local setup (desktop: Checking JARV1S) | Yes | Runtime policy in `docs/deployment/VERSIONING_AND_DEPENDENCIES.md`; commands in `Taskfile.yml` |
| `prepare_dependencies` | Preparing Jarvis (desktop: Preparing the app) | Yes | Lockfile installs/builds for the selected launch surface; satellite install is separate unless launching a satellite node |
| `start_services` | Starting local services (desktop: Starting private data services) | Yes | Packaged: bundled `mongod` under `apps/desktop/resources/host/services/`; dev: `task db` / `docker-compose.yml` |
| `start_backend` | Starting JARV1S (desktop: Starting your assistant) | Yes | `task be:app`; app-mode serving in `backend/main.py` |
| `wait_for_health` | Checking JARV1S is reachable (desktop: Making sure JARV1S is ready) | Yes | `GET /api/v1/health` — HTTP 200 when the database is up (`needs_setup` / LLM `degraded` still 200); HTTP 503 when it is down |
| `resolve_setup_state` | Opening setup | Yes, if setup is incomplete | `GET /api/v1/setup/state`; `SetupStateResponse`; `ReadinessPhase` |
| `ready` | Jarvis is ready | Yes | `ReadinessPhase.READY` and `chat_enabled=true` |

Launch surfaces may skip phases that are already satisfied, but they should keep the same phase names and semantics in logs, UI state, and support output. Build-time work such as compiling the frontend or packaging the desktop runtime happens before launch, not as a runtime phase.

Dependency preparation must install from lockfiles. It must not update dependency versions as part of normal startup.

## User-Facing States

| State | Meaning | UI/CLI treatment |
| :--- | :--- | :--- |
| `checking` | The launcher is verifying prerequisites or existing state | Short status line; no noisy logs |
| `running` | A bounded startup step is actively doing work | Step label plus progress when measurable |
| `waiting` | The backend or services are booting and may take a few seconds | Spinner or stepper with current phase |
| `needs_setup` | Jarvis is reachable but LLM setup is incomplete | Open the setup UI; do not present this as an error |
| `ready` | Core text runtime is usable | Open the app |
| `degraded` | Jarvis is reachable but a non-core lane is unavailable | Open the app and surface capability lane status later |
| `failed` | A required startup step cannot continue | Stop and show actionable recovery copy |

## Failure Copy

Use plain language. Save raw logs for debug output, not the primary user path.

### Docker Unavailable (dev / `JARVIS_SERVICE_PROVIDER=docker` only)

```text
Docker Desktop is not running.

Jarvis uses Docker for its contributor database. Open Docker Desktop, wait until it says it is running, then start Jarvis again.
```

### Bundled Services Failed (packaged default)

```text
JARV1S could not start its local database.

Try starting JARV1S again. If this keeps happening, open the debug logs from the app.
```

### Local Services Failed (Docker Compose)

```text
Jarvis could not start its local services.

MongoDB is required before the app can open. Try starting Jarvis again. If this keeps happening, open the debug logs and check Docker Desktop.
```

### Dependency Install Failed

```text
Jarvis could not prepare its dependencies.

The install uses locked dependency files so every machine runs the same versions. Check your network connection, then start Jarvis again.
```

### Backend Unreachable

```text
Jarvis Host started, but the app could not reach it.

Wait a moment and try again. If this continues, another app may be using the Jarvis port.
```

### Setup Required

```text
Jarvis is ready for setup.

Choose a local runtime or cloud language model provider, then initialize the assistant to send your first message.
```

## Existing Sources Of Truth

- `Taskfile.yml`: current orchestration, lockfile installs, app-mode backend start, and stale frontend build behavior.
- `docker-compose.yml`: local MongoDB version, digest pin, and localhost binding.
- `backend/api/routes/base.py`: `/health` for startup reachability and `/version` for support metadata.
- `backend/core/setup/models.py`: setup response shape, readiness phases, service status, and capability lane status.
- `backend/core/setup/readiness.py`: authoritative setup state and readiness gate.
- `docs/deployment/VERSIONING_AND_DEPENDENCIES.md`: runtime version, lockfile, Docker image, and MongoDB upgrade policy.
- `docs/ARCHITECTURE.md`: Jarvis Host architecture and fail-closed core readiness philosophy.

## Future Consumers

- `task start`: contributor and early-tester entry point.
- Packaged Jarvis Host app (`apps/desktop/`): **Phase 1b implemented** — Tauri supervisor, startup UI, bundled Python runtime, bundled native `mongod` (no Docker for packaged default), signing/release CI. Override with `JARVIS_SERVICE_PROVIDER=docker` for technical beta fallback.
- `./jarvis start`: optional thin bootstrap launcher if needed for clean-room validation (not built; `task start` remains the contributor path).

All consumers should converge on the same startup phases and existing readiness APIs. If a future launcher needs more information, prefer extending the existing `/health`, `/version`, or `/setup/state` surfaces rather than adding a parallel checker.
