# Versioning And Dependencies

JARV1S keeps app versioning, package dependencies, and local service data upgrades as separate concerns. This keeps the Jarvis Host reproducible without turning every app release into a database migration.

## App Version

The backend package version in `backend/pyproject.toml` is the canonical Jarvis Host app version. Frontend and satellite packages may carry matching package versions, but release/support tooling should treat the backend app version as the host version.

The Jarvis Host exposes support metadata at:

```bash
GET /api/v1/version
```

Packaged desktop releases add shell-owned metadata during build (`apps/desktop/resources/host/runtime-bundle.json`, with Tauri app version synced from `backend/pyproject.toml`):

| Field | Source |
| :--- | :--- |
| `app_version` | Tauri app version (`apps/desktop/scripts/sync-app-version.mjs`) |
| `host_version` | `backend/pyproject.toml` / `GET /api/v1/version` |
| `frontend_build` | Git commit at release build |
| `runtime_bundle` | Git commit at Host runtime build |
| `release_channel` | `internal` or invited `beta`, selected at release time |
| `service_bundle` | Pinned versions + SHA256 from `apps/desktop/services/versions.json` |

The desktop release script builds the app-owned runtime once from lockfiles, signs/notarizes the macOS artifacts, and writes signed updater metadata (`latest.json` or `latest-<channel>.json`) for the configured channel. Version sync also pins the embedded updater endpoint to that channel. Launch-time update checks are opt-in with `JARVIS_ENABLE_AUTO_UPDATE=1`.

Release CI (`.github/workflows/desktop-release.yml`) is the backup builder when a `vX.Y.Z` tag has no DMG yet. Local publish is the usual path. CI requires:

1. Tag `vX.Y.Z` matching `backend/pyproject.toml` (skipped on `workflow_dispatch`)
2. Backend/frontend/desktop smoke gates before signing
3. Absolute `JARVIS_UPDATE_BASE_URL` (private updater origin)
4. Publishing a **private** versioned GitHub Release and rolling channel artifacts

Promote a baked version to the public repo with `task desktop:release:promote` — same DMG, no rebuild. See [`RELEASE_KEY_CUSTODY.md`](./RELEASE_KEY_CUSTODY.md).

See [`RELEASE_KEY_CUSTODY.md`](./RELEASE_KEY_CUSTODY.md) for Apple/Tauri signing key custody.

## Python And Node

Python dependencies are locked by `backend/uv.lock` and `satellite/uv.lock`. Normal installs use:

```bash
uv sync --locked
```

Node dependencies are locked by `frontend/package-lock.json`. Normal installs use:

```bash
npm ci
```

Use the explicit update tasks only when intentionally changing dependencies:

```bash
task be:update-deps
task sat:update-deps
task fe:update-deps
```

Runtime versions are declared at the repo root:

- `.python-version` for Python
- `.nvmrc` for Node

## Local Services

### Contributor / Docker path

Docker service images are pinned by human-readable tag plus immutable digest in `docker-compose.yml`. Do not use floating tags like `latest`.

Current local service versions:

- MongoDB `8.2`

Digest updates should be reviewed like dependency updates. They are not app migrations by themselves.

### Packaged desktop path

The packaged app ships MongoDB from `apps/desktop/services/versions.json` (currently `8.2.3`). `apps/desktop/scripts/build-service-binaries.sh` downloads, verifies SHA256, and stages the binary under `apps/desktop/resources/host/services/` for release builds.

- **Default provider:** `Bundled` — a Unix socket under `~/Library/Application Support/JARV1S/run/`, with data under `~/Library/Application Support/JARV1S/mongo`.
- **Override:** `JARVIS_SERVICE_PROVIDER=docker` falls back to Docker Compose (contributor dev and technical beta dogfood).

Bump the bundled MongoDB version by editing `versions.json`, rebuilding the runtime (`task desktop:build-runtime`), and running `task desktop:doctor` before release.

## MongoDB Upgrades

MongoDB is data-versioned. Major MongoDB upgrades require a deliberate process:

1. Back up the data volume or export the database.
2. Read the MongoDB release notes for every major version in the path.
3. Upgrade sequentially through supported major versions.
4. Check `featureCompatibilityVersion`.
5. Only set the new FCV after the upgraded binary has run successfully and rollback risk is acceptable.
6. Verify the Jarvis Host starts and passes health checks.

Patch or minor image updates do not require JARV1S data migrations unless the app's persisted schema changed.

## JARV1S Data Migrations

Create a JARV1S app migration only when JARV1S changes its own persisted data shape, indexes, or semantics. Do not couple app migrations to every package or Docker image update.

The installed app database under `~/Library/Application Support/JARV1S` is the only supported personal data plane. Contributor Docker data is disposable and is not migrated, merged, or synchronized with the app.

Prefer additive fields and current-shape readers. Before a destructive data change:

1. Quit JARV1S so bundled MongoDB has stopped all writes.
2. Run `task desktop:data:backup`.
3. Apply and verify the change against the installed app.
4. Reopen the app and verify `/api/v1/health` plus affected collection behavior.

The cold backup contains MongoDB, the encrypted credential vault, host preferences, and enrolled speaker profiles. It excludes runtime socket state. Its manifest verifies that copied files are complete and unchanged, but logical MongoDB recovery still requires a separate restore drill and collection-level validation.

The macOS Keychain key is intentionally not exported. Restoring on the same Mac retains access to Keychain-backed credentials; restoring on another machine may require integration reauthentication. Dev vault restores require the matching `JARVIS_CREDENTIAL_PASSPHRASE`.

Remove one-off migration code, startup repairs, and old-field fallbacks after the supported app database is verified clean. Add an ordered migration framework only when a concrete shipped migration requires sequencing; do not keep compatibility infrastructure speculatively.
