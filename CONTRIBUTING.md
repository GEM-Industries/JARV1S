# Contributing

JARV1S is developed in a private canonical repository. The public tree is a
periodic snapshot. Pull requests on the public repo are welcome; they are
reviewed there and landed through the private tree on the next publish.

## License

By opening a pull request you agree to the [Individual Contributor License
Agreement](CLA.md). Comment on the PR that you agree to the CLA. We cannot
merge without it.

The project is licensed under the [GNU Affero General Public License
v3.0 or later](LICENSE).

## Run locally

```bash
task start        # contributor browser Host (Docker MongoDB + backend)
task desktop:dev  # Tauri shell + repo backend + Docker
```

Split development: `task db`, `task be:dev`, `task fe:dev`.

See [README.md](README.md) and [docs/README.md](docs/README.md).

## Tests

```bash
cd backend && uv run pytest -q
```

Frontend: `cd frontend && npm test` (or the project's existing npm test script).
Desktop: `cd apps/desktop && npm run test:release-scripts`.
