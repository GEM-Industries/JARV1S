# Jarvis AI Assistant Backend

A personal AI assistant backend built with FastAPI, featuring voice processing capabilities and smart home integration.

## Features (MVP)

- Voice Interface with wake word detection
- Conversation Engine with context memory
- Modular Function System
- Smart Home Integration
- Real-time WebSocket Communication
- Remote presence handshake (`connection_id` / `owner_id` / `node_id` / optional location refs)

## Setup

1. Ensure Python 3.11+ is installed

2. Install dependencies using `uv`:
```bash
uv sync
```

3. For development, copy `.env.example` to `.env` and configure optional infrastructure keys (SearXNG, smart home, reasoning tier, callback hosts). Main assistant LLM setup uses the first-run wizard; Exa, Composio, and Cartesia keys are managed in the app under **Apps → Credentials**.

```bash
cp ../.env.example ../.env
```

For contributor development, the repo-root browser Host uses disposable Docker data:

```bash
task start
```

It starts MongoDB and serves the built frontend. This data plane is isolated from the installed desktop app and must not be treated as personal app data.

4. Run the split development server:
```bash
task be:dev          # localhost only
task be:dev:lan      # LAN-reachable for satellites (0.0.0.0:8000)
```

See [docs/SATELLITE.md](../docs/SATELLITE.md) for how the Pi voice client works, and [docs/deployment/MULTI_DEVICE_REACHABILITY.md](../docs/deployment/MULTI_DEVICE_REACHABILITY.md) for private access (Tailscale Serve via Host Availability), in-app room-speaker mint (`POST /api/v1/device-auth/satellites`), phone pairing, CLI recovery (`task devices:*`), and turn-origin delivery across multiple nodes.

## Project Structure

```
├── api/                  # FastAPI application
│   ├── routes/          # API endpoints
│   └── websockets/      # WebSocket handlers
├── core/                # Core business logic
│   ├── voice/          # Voice processing
│   ├── setup/          # Jarvis Host readiness and runtime initialization
│   ├── credentials/    # Host-only provider credential storage
│   ├── agent/         # LLM integration & structured capability loop
│   ├── integrations/  # External API management (Integration Gate)
│   └── plugins/       # Plugin system infrastructure
├── services/           # Shared services
│   └── database/      # MongoDB interface
└── plugins/           # Plugin modules
```

## Development

This project uses `uv` for dependency management. Key features:

- **Plugin System**: Add tools by creating `plugins/{name}.py` or `plugins/{name}/` for complex integrations
- **Integration Gate**: External APIs are injected via `@tool(inject=["service"])` decorator
- **Environment Config**: `.env` holds infrastructure settings (SearXNG URL, smart home, reasoning tier, callback hosts). Product credentials (Exa, Composio, Cartesia) and main LLM setup use **Apps → Credentials** and the setup wizard (`system_config` + `CredentialStore`).

## Smart Home (Home Assistant)

**Product:** Open Smart Home in the app → Connect with Home Assistant URL + long-lived access token (`POST /api/v1/smart-home/connect`). Credentials persist to `system_config` + `CredentialStore`.

**Contributor CLI:**

```bash
task setup:home              # interactive: connect existing, onboard fresh, or HA Green guide
task setup:home:bootstrap    # Docker required: provision HA Container + connect
```

Contributor CLI writes only to disposable dev stores; it does not configure the installed app. Repo-root `.env` is an optional dev fallback. Fixture capture for the pinned bootstrap image:

```bash
task setup:home:capture-fixtures
task setup:home:fixture-drift   # compare live shapes against committed fixtures
```

See `docs/CORE_TOOLS.md` (smart_home tools) and `docs/proposals/partial/HA_FIRST_DEVICE_PAIRING.md` (Grid Connect / Tuya setup flow).

The frontend Smart Home panel reads `GET /api/v1/smart-home/status` for connection state, controllable devices by HA area, and a link to open Home Assistant. First-time connect uses the in-panel Connect form. Room speakers are managed from **Rooms & devices** (not a Smart Home Endpoints sub-mode).

**Rooms & devices** (`PresencePanel`) reads `GET /api/v1/presence/` for live and provisioned devices (This Mac, phones, room speakers; online/offline/revoked), assigns rooms via presence APIs, mints room-speaker credentials via `POST /api/v1/device-auth/satellites`, pairs phones/browsers via pairing codes, and revokes via `POST /api/v1/presence/devices/{device_id}/revoke`.

## Latency Toolkit

Unit and contract tests follow [`.cursor/rules/test-strategy.mdc`](../.cursor/rules/test-strategy.mdc): add them when they protect a real risk, not by default for every change. Use `docs/research/VOICE_EVALS.md` as the active voice eval workflow. Start with the narrowest layer that can fail: wakeword, then STT, then full voice-turn latency.

For full end-to-end checks, run these from the repo root with the backend already running:

```bash
task be:latency -- --text "How are you?" --runs 5
task be:latency -- --audio logs/fixtures/stt/how_are_you.wav --activate-audio --chunk-ms 96 --runs 5
task be:latency -- --suite voice-core --label baseline --activate-audio --runs 3
```

`be:latency` sends repeatable realtime-paced WebSocket turns and records first partial, committed transcript, commit after estimated speech end, first response, final response, and first audio timings. With `--suite` or `--label`, results are written to `logs/evals/<timestamp>_<label>/` with `manifest.json`, compact `results.jsonl`, `summary.json`, and a readable `summary.md`. Read `summary.md` first; open `results.jsonl` only when debugging a flagged run.

For raw STT quality checks, run:

```bash
task be:eval-stt -- --fixtures logs/fixtures/stt
task be:eval-stt -- --suite raw-stt --label mlx-medium
```

For tool-routing changes, run the production voice eval:

```bash
task be:eval-routing
```

This evaluates `voice_default` against `backend/evals/tool_routing.yaml` with category output.

For agent/LLM behavior (tool choice, consent, NO_REPLY, false completion claims):

```bash
task be:eval-agent
```

Cases live in `backend/evals/agent_behavior.yaml`. The runner reuses `_execute_turn` + `TurnResult` and scores trajectories via `backend/evals/agent_scorers.py`. Plugin manifests for offline/live runs load through `backend/evals/bootstrap.py` (no plugin startup side effects).

Three tiers:

| Tier | Flag | Purpose |
| :--- | :--- | :--- |
| `mock` | default | Scripted `AgentEvent`s through the harness; validates plumbing (1 run each). |
| `live` | `--live` | Real LLM canaries; production routing by default; executor stubbed so tools do not mutate external state. Live P0 requires strict `3/3` pass. |
| `probe` | `--probe --live` | Opt-in prompt-tuning probes. Reports pass rate but does not fail CI — use for before/after comparison only. |

```bash
cd backend
uv run python tools/evaluate_agent_behavior.py --live --priority P0 --label agent-live-p0
uv run python tools/evaluate_agent_behavior.py --probe --live --label agent-probes
uv run python tools/evaluate_agent_behavior.py --live --case live_scheduler_reminder_not_todo
```

Cases with `pin_manifest: true` test agent behavior against a fixed manifest instead of production routing.

After live canaries pass, prove the suite catches regressions:

1. Deliberately damage the relevant prompt, tool docstring, or routing surface for one live case.
2. Re-run that case with `--live --case <case_id>`.
3. Confirm it fails, then revert the damage immediately.

For tool-routing evals, keep plugin `metadata.utterances` intent-shaped (categories like "remind me later", not exact eval phrasing). Eval cases should use different wording so passes measure routing/behavior, not seed-phrase overlap.

Results are written to `logs/evals/<timestamp>_<label>/`.

