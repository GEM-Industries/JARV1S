# JARV1S Desktop (Phase 1b)

Signed technical beta shell for the JARV1S Host. The desktop app supervises bundled `mongod` and the bundled FastAPI backend in packaged mode, shows startup progress, then opens the existing React UI from the backend origin.

Invite-only macOS builds: [latest private-repo release](https://github.com/GTS-html77/JARV1S/releases/latest). Public clones should use `task desktop:dogfood` or `task desktop:dev` instead.

**Spec:** [`docs/proposals/JARVIS_HOST_APP.md`](../../docs/proposals/JARVIS_HOST_APP.md)

## Runtime modes

| Mode | When | Backend launch |
| :--- | :--- | :--- |
| `dev_repo` | `task desktop:dev` | `uv run uvicorn` from repo checkout |
| `packaged_runtime` | Built `.app` / release | Bundled relocatable Python + locked deps from `backend/uv.lock` |

Set explicitly with `JARVIS_RUNTIME_MODE=dev_repo|packaged_runtime`.

## Prerequisites

### Contributor dev (`dev_repo`)
- Rust toolchain (`rustup`)
- Node.js 20+ (`nvm use`)
- `uv`, Docker Desktop, built frontend (`task desktop:prepare`)

### Packaged / release (`packaged_runtime`)
- macOS 14+ arm64 build host for release artifacts
- No Docker required for end users (bundled `mongod`)
- Set `JARVIS_SERVICE_PROVIDER=docker` to fall back to Docker Compose for dogfood

## Commands

From the repository root:

```bash
task desktop:dogfood              # build, install to /Applications, open packaged app
task desktop:doctor               # check desktop Rust + bundled services smoke
task desktop:release:local        # local signed/notarized release
task desktop:release              # arm64 signed/notarized release (requires credentials)
task desktop:data:backup          # save keys+config → JARV1S Backups/daily (quit app first)
task desktop:data:restore         # restore that slot
task desktop:data:reset           # first-run wipe so SetupWizard returns
task desktop:dev                  # contributor shell only — not dogfood data
```

### Dogfood data (packaged app)

Durable state is under `~/Library/Application Support/JARV1S`.  
**Quit JARV1S** before backup / restore / reset.

| Saves | Skips |
| :--- | :--- |
| `mongo/` (LLM, HA URL, OAuth, transcript) | `models/` (re-downloadable, large) |
| `credentials/` (API keys, HA_TOKEN) | `cache/`, `valkey/`, `run/` |
| `host-prefs.json`, `voice/` | Diagnostics JSON in `~/Library/Logs/JARV1S` |

```bash
osascript -e 'quit app "JARV1S"'
task desktop:data:backup      # once your dogfood setup feels right
task desktop:data:reset       # walk first-run
open /Applications/JARV1S.app
# …
osascript -e 'quit app "JARV1S"'
task desktop:data:restore     # back to dogfood
open /Applications/JARV1S.app
```

Optional: `NAME=pre-ftue` / `SOURCE=pre-ftue` for a second slot.  
CLI helpers (no Task wrappers): `uv run python tools/jarvis_data.py list|status` from `backend/`.

Use `task desktop:dogfood` when you want to test the packaged user path. It builds the unsigned packaged app, quits the currently installed `JARV1S.app`, replaces `/Applications/JARV1S.app`, and opens the fresh copy. Set `JARVIS_INSTALL_APP_PATH=/path/to/JARV1S.app` to push somewhere other than `/Applications`.

Before a change that mutates persisted app data, quit JARV1S and run `task desktop:data:backup`. Code-only changes do not require a backup.

`task desktop:dev` is for contributor-only shell work. Its Docker database and `repo/.data` are disposable and **never** share dogfood Application Support data.
Use `task desktop:release:local` for local releases. The first run asks for the Apple and updater key details, saves non-secret settings under `~/.config/jarv1s`, and stores the updater password in macOS Keychain. Later releases require only the same command. Run `task desktop:release:local -- --setup` to replace the saved settings. If only updater signing failed after notarization, run `task desktop:release:local -- --retry-updater` to replace that password and reuse the accepted app and DMG.

`task desktop:release` is the non-interactive CI entry point. It builds the runtime once, then signs, notarizes, staples, and emits updater metadata.

After a local build finishes, publish to GitHub with `task desktop:release:publish`.

Lower-level tasks (`desktop:bootstrap`, `desktop:build-runtime`, `desktop:build`, `desktop:push`) remain available for CI and debugging, but most day-to-day work should use the commands above.

CI: push a `v*` tag (for example `v0.2.0`) to run [`.github/workflows/desktop-release.yml`](../../.github/workflows/desktop-release.yml) (arm64 macOS).

## Release secrets

| Secret | Purpose |
| :--- | :--- |
| `APPLE_SIGNING_IDENTITY` | Developer ID Application identity |
| `APPLE_CERTIFICATE` / `APPLE_CERTIFICATE_PASSWORD` | CI keychain import |
| `APPLE_API_KEY` or `APPLE_API_KEY_PATH` + `APPLE_API_KEY_ID` + `APPLE_API_ISSUER` | Notarization API key |
| `APPLE_ID` / `APPLE_PASSWORD` / `APPLE_TEAM_ID` | Notarization Apple ID fallback |
| `TAURI_SIGNING_PRIVATE_KEY` or `TAURI_SIGNING_PRIVATE_KEY_PATH` | Updater artifact signing (never commit) |

`task desktop:release` fails fast if signing, notarization, or updater signing credentials are missing. Use `task desktop:build` for unsigned local packaged builds. CI smoke tests may set `JARVIS_SKIP_NOTARIZATION=1`, but distributable artifacts must not.

Public updater key: [`updater.pub`](updater.pub) (embedded in `tauri.conf.json`).

Generate/update keys:

```bash
npm run tauri signer generate -- --ci -w ~/.tauri/jarvis-updater.key -f
```

## Data paths (packaged mode)

| Path | Purpose |
| :--- | :--- |
| `~/Library/Application Support/JARV1S` | Host data root + encrypted credentials |
| `~/Library/Application Support/JARV1S/mongo` | Bundled MongoDB data (config, OAuth, transcript) |
| `~/Library/Application Support/JARV1S/credentials` | Encrypted API keys / HA_TOKEN |
| `~/Library/Application Support/JARV1S Backups` | Dogfood backups (`task desktop:data:backup` → `daily`) |
| `~/Library/Application Support/JARV1S/run` | Unix socket for bundled MongoDB |
| `~/Library/Logs/JARV1S` | Supervisor logs + diagnostics export JSON (not a backup) |

## Updates

Auto-update install is opt-in for the technical beta. Set `JARVIS_ENABLE_AUTO_UPDATE=1` only for internal dogfood builds that should check the configured static updater endpoint on launch. Without it, updater config and signed artifacts are still generated, but launch does not perform a network update check.

## Architecture

- **Shell**: Tauri 2 (`apps/desktop`)
- **Supervisor**: Rust module spawning bundled `mongod` (packaged) or Docker Compose (dev), plus backend with ordered shutdown
- **UI after startup**: existing React app served by backend at `http://127.0.0.1:<port>`

Startup phases follow [`docs/deployment/JARVIS_HOST_STARTUP_CONTRACT.md`](../../docs/deployment/JARVIS_HOST_STARTUP_CONTRACT.md). Setup readiness remains on `/api/v1/setup/state`.
Bundled MongoDB is a supervised child process group. Startup readiness is bounded to 30 seconds, reports child exits with retained service logs, and does not require an end-user MongoDB, Docker, or Python installation. `scripts/smoke-services.sh` exercises MongoDB through the bundled Python runtime using an empty data directory and paths containing spaces.

Tauri commands include Host lifecycle/status/preferences, `get_host_status` / `enable_host_serve` (private Tailscale access), `export_diagnostics_bundle`, and `open_logs_folder`. The backend-served desktop UI is explicitly scoped to the loopback origin in the Tauri capability; normal browsers do not receive IPC access.

Product surfaces for multi-device use: **Settings → Availability** (private access), **Rooms & devices** (speakers/phones), and **Home** (HA rooms with a link out to Rooms & devices). See [MULTI_DEVICE_REACHABILITY.md](../../docs/deployment/MULTI_DEVICE_REACHABILITY.md).

## Current Limits

- Contributor dev mode still uses Docker Compose so it stays close to the backend workflow.
- arm64 macOS first; universal/x64 after signing pipeline is stable
- Updater uses channel-specific signed manifests (`latest.json` for `internal`, `latest-beta.json` for `beta`); launch checks require `JARVIS_ENABLE_AUTO_UPDATE=1`
- Diagnostics are intentionally hidden from normal users. Press `Cmd+Shift+D` on macOS or `Ctrl+Shift+D` elsewhere to enable developer mode and open Diagnostics; use **Turn off developer mode** inside Diagnostics to hide it again.
- The fresh-user signed-DMG journey, including `getUserMedia`, Google/Cerebras connection, optional Cartesia STT/TTS, and owner enrollment, remains a manual release gate
- Runtime bundle is large because it ships the locked backend ML dependency stack verbatim
