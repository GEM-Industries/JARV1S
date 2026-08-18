# Home Assistant first-device pairing

After Docker bootstrap V1, the Home Assistant setup CLI (`task setup:home`), or in-app Smart Home Connect, JARV1S can install and connect to Home Assistant. Device commissioning remains in vendor/HA apps; JARV1S handles post-HA reconciliation.

## Goal

Walk the user from "HA connected, zero devices" to voice-controllable lights without opening the HA UI for naming or areas.

## V1 scope (Grid Connect / Tuya first)

Primary path for current hardware:

1. **Grid Connect E27 (Tuya cloud)** — commission bulbs in Smart Life/Tuya Smart, link the Tuya account in HA once via QR, reload integration when new bulbs appear
2. **Tapo/Kasa (second slice)** — per-device local config flow with human handoff for app-only steps (client methods exist; no setup tools yet)

Defer Zigbee, Thread, Z-Wave, and Matter-over-Thread until radio hardware and HAOS add-on story is defined.

## User flow (Grid Connect / Tuya)

1. Bootstrap or connect completes → readiness shows `safe_controllable_count: 0`
2. User commissions bulbs in **Smart Life** or **Tuya Smart** (not Grid Connect app)
3. If Tuya is not linked in HA, JARV1S returns exact handoff: HA → Add Integration → Tuya → scan QR in Smart Life
4. User: "I added the bulbs in Smart Life"
5. JARV1S runs `refresh_home_assistant(reload_domain="tuya")` and reports one of the reload outcomes below
6. JARV1S asks only for missing facts: room name, friendly name
7. `organize_device` writes name/area to HA registries and refreshes inventory
8. User controls lights normally via voice (`control_device`) — first real command is the proof
9. If a physical voice satellite is present and unbound, JARV1S may ask once to `bind_node_area` for "in here" commands

## `refresh_home_assistant` outcomes

The tool owns the distinction — do not infer success from a generic "refreshed" message:

| Outcome | Meaning |
|---------|---------|
| `integration_missing` | No Tuya config entry in HA; return Smart Life + QR handoff |
| `reload_failed` | Reload did not succeed or config entry never returned to `loaded` |
| `reload_ok_no_entities` | Reload succeeded but no controllable Tuya entities visible yet (cloud sync may lag) |
| `reload_ok_with_entities` | Reload succeeded with safe controllable candidates |

Tuya lights are identified by HA config-entry membership (`config_entry_ids`), not by comparing cached inventory snapshots. All matching Tuya config entries are reloaded.

## Tool usage notes

- **`organize_device`** — pass an `entity_id` from `refresh_home_assistant` candidates or `search_devices`. Resolve "this one" / "the second light" internally; never ask the user to speak an entity ID aloud.
- **`control_device`** — normal voice control after setup; lights and switches do not require approval.
- **`bind_node_area`** — optional, confirmation-based only for room-relative voice ("in here"). Explicit room names work without satellite binding.

## Validation loop

After pairing the device in Smart Life and linking Tuya in HA:

1. Run `jarvis.smart_home.refresh_home_assistant`.
2. If outcome is `reload_ok_with_entities`, run `jarvis.smart_home.organize_device`.
3. If outcome is `reload_ok_no_entities`, wait briefly and refresh again — Tuya cloud sync can lag.
4. If outcome is `reload_failed`, inspect HA integration state before retrying.
5. Control a light with a normal voice command (e.g. "turn on the bedroom light").

## What JARV1S owns vs HA / vendor apps

| Step | Owner |
|------|--------|
| Install HA | JARV1S bootstrap (Docker) or dedicated hardware |
| Onboard HA | JARV1S API (bootstrap) or HA UI |
| Commission bulb onto Wi-Fi | Smart Life / Tuya Smart app (human) |
| Link Tuya account in HA | HA UI QR scan (human) |
| Reload integration / read registry | JARV1S `refresh_home_assistant` |
| Name and room in HA | JARV1S `organize_device` |
| Control lights | JARV1S `control_device` |
| Room-relative voice ("in here") | JARV1S `bind_node_area` (optional, confirmation-based) |

Satellite nodes do not need HA device pairing. Pair controllable devices in HA, then optionally bind the JARV1S satellite node to the HA area for room context.

## Not in this milestone

- Generic config-flow automator across all HA integrations (Tapo/Kasa driver is the second slice)
- JARV1S-side LAN scanner
- Persistent setup receipt / history
- Zigbee/Thread radio setup
- Consumer HA Green flashing/provisioning by JARV1S
