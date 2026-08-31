# JARV1S

A locally-grounded AI home assistant: low-latency voice, plugins, and a
packaged macOS Host. Source is AGPL-3.0-or-later.

macOS Apple Silicon: [download the latest release](https://github.com/GEM-Industries/JARV1S/releases/latest). What changed: [changelog](CHANGELOG.md).

Copyright GEM Industries. Licensed under the [GNU Affero General Public
License v3.0 or later](LICENSE). Contributions require the [CLA](CLA.md).

## Getting started

Prerequisites: [Task](https://taskfile.dev), Docker, [uv](https://docs.astral.sh/uv/),
Node.js 20+ (see `.nvmrc`), Python 3.12 (see `.python-version`).

```bash
git clone https://github.com/GEM-Industries/JARV1S.git
cd JARV1S
task start        # contributor browser Host
```

```bash
task desktop:dev  # Tauri shell + repo backend + Docker
task desktop:dogfood   # build, install to /Applications, open packaged app
task desktop:doctor    # Rust check + bundled service smoke test
```

Docker MongoDB and `repo/.data` are disposable development infrastructure; they
do not share personal app data. The packaged macOS app owns the personal
database under `~/Library/Application Support/JARV1S`.

See [`apps/desktop/README.md`](apps/desktop/README.md) for the desktop shell.

## Documentation

See [docs/README.md](docs/README.md) for architecture, roadmap, and proposals.
How to contribute: [CONTRIBUTING.md](CONTRIBUTING.md).
Security reports: [SECURITY.md](SECURITY.md).

## Structure

- `backend/`: Python backend (FastAPI, WebSockets, core logic).
- `frontend/`: React 19 / TypeScript / Vite UI.
- `apps/desktop/`: Tauri desktop shell for packaged macOS builds.
- `satellite/`: Thin Raspberry Pi voice endpoint. See [docs/SATELLITE.md](docs/SATELLITE.md).

Home Assistant: connect in the app (**Smart Home** panel) or contributor CLI
`task setup:home` (see [docs/CORE_TOOLS.md](docs/CORE_TOOLS.md#what-exists-today)
and [backend/README.md](backend/README.md)).

Multi-device reachability: [docs/deployment/MULTI_DEVICE_REACHABILITY.md](docs/deployment/MULTI_DEVICE_REACHABILITY.md).
