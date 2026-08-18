# Home Assistant API fixtures

Captured shapes for unit tests against the pinned bootstrap image
(`ghcr.io/home-assistant/home-assistant:2025.4.4`). See `manifest.json`.

Refresh after bumping `HA_CONTAINER_IMAGE` in `plugins/smart_home/bootstrap_config.py`:

```bash
# Fresh HA during onboarding (bootstrap URL, loopback client_id)
task setup:home:capture-fixtures

# Check for pinned-image response-shape drift without rewriting committed fixtures
task setup:home:fixture-drift

# Or capture with an existing token
cd backend && uv run python tools/capture_ha_fixtures.py --url http://127.0.0.1:8123 --token YOUR_TOKEN
```

Token fixture fields are redacted before writing. Fixture drift is guarded by
`task setup:home:fixture-drift` and the committed fixture smoke tests.

Tuya/Grid Connect setup fixtures (config entries, device/entity registries, states):
`config_entries_tuya.json`, `device_registry_tuya.json`, `entity_registry_tuya.json`, `states_tuya.json`.
