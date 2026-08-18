# Release Key Custody

Private beta distribution depends on two long-lived secrets.

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

## Release checklist

1. Bump `backend/pyproject.toml`
2. `node apps/desktop/scripts/sync-app-version.mjs`
3. Tag `vX.Y.Z` matching that version
4. Confirm `JARVIS_UPDATE_BASE_URL` points at the channel release download URL
5. Confirm CI published both the versioned GitHub Release and the rolling channel tag (`internal` / `beta`)
6. Dogfood `N-1` → `N` with `JARVIS_ENABLE_AUTO_UPDATE=1` before inviting more testers

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
