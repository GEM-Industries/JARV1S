# Release Key Custody

Private beta distribution depends on Apple signing, updater signing, and the official Google OAuth identity.

## Apple Developer ID

- Certificate: Developer ID Application
- Notarization: App Store Connect API key preferred (`APPLE_API_KEY*`)
- Store the `.p12` and API key outside the repo (1Password / org secrets)
- Rotation requires re-signing all nested Mach-O binaries and republishing

## Tauri updater signing key

- Public key is embedded in [`apps/desktop/src-tauri/tauri.conf.json`](../../apps/desktop/src-tauri/tauri.conf.json) and [`apps/desktop/updater.pub`](../../apps/desktop/updater.pub)
- Private key lives only in `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PATH`
- Losing the private key strands installed clients; back it up offline before the first private-beta invite
- Do not commit the private key

## Official Google OAuth identity

- Source: gitignored [`apps/desktop/resources/product_oauth.json`](../../apps/desktop/resources/product_oauth.json) (see `.example`)
- The Host passes its path as `JARVIS_PRODUCT_OAUTH`; do not put client id/secret in `.env`
- Forks without that file use Apps → Advanced

## Release checklist

Private origin (`GTS-html77/JARV1S`) is the beta/control plane: invited testers, versioned DMG, rolling updater channel (`internal` / `beta`). `GEM-Industries/JARV1S` is public GA: sanitized source plus only versions you explicitly promote. Same signed bytes; no second notarization.

1. Freeze [`CHANGELOG.md`](../../CHANGELOG.md) for this version (move `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`). Publish fails without that section.
2. Bump `backend/pyproject.toml`
3. `node apps/desktop/scripts/sync-app-version.mjs`
4. `task desktop:release:local` (sign, notarize, staple)
5. Confirm `JARVIS_UPDATE_BASE_URL` points at the **private** channel download URL
6. `task desktop:release:publish` — private prerelease DMG + updater channel. Does not touch the public repo.
7. Dogfood `N-1` → `N` with `JARVIS_ENABLE_AUTO_UPDATE=1` before inviting more testers
8. When that version should be public: `task desktop:release:promote` (source snapshot, then the same DMG on GEM-Industries). No updater channel on public, so beta auto-updates cannot leak.

Pushing `vX.Y.Z` still starts release CI. If the private GitHub Release already has the DMG, CI skips the Mac rebuild. Use workflow_dispatch with **force** only to replace a broken artifact. Apple secrets stay on the private repo.

## GitHub release configuration

Repository secrets:

- `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`
- `APPLE_SIGNING_IDENTITY`
- either `APPLE_API_ISSUER`, `APPLE_API_KEY_ID`, and `APPLE_API_KEY`, or the
  documented Apple ID notarization fallback
- `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`

Repository variables:

- `JARVIS_UPDATE_BASE_URL` — public HTTPS base for unauthenticated updater downloads
- `JARVIS_RELEASE_CHANNEL` — `internal` or `beta`

The tag workflow fails before signing when the update base URL is missing or not HTTPS.
Run `task desktop:validate-artifact BUNDLE=...` against the exact workflow bundle before
publishing it to testers.
