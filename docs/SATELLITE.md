# JARV1S Voice Satellite

Normative reference for the room voice endpoint in `satellite/`. For deployment URLs, TLS, and device auth, see [deployment/MULTI_DEVICE_REACHABILITY.md](./deployment/MULTI_DEVICE_REACHABILITY.md). For remaining product work (on-device wakeword, binary framing, eval gates), see [proposals/partial/VOICE_SATELLITE_EDGE.md](./proposals/partial/VOICE_SATELLITE_EDGE.md) and [ROADMAP.md](./ROADMAP.md).

## What it is

A **thin Raspberry Pi voice node** that:

- Captures room microphone audio and streams it to the central Jarvis Host over the same WebSocket contract as the browser client.
- Plays assistant TTS (`jarvis_audio`) and local notification sounds on a room speaker.
- Announces stable **presence identity** (`node_id`, capabilities, optional room refs) at connect time.
- Reports **`audio.playback_end`** when local playback has actually drained so the backend can leave `ACTIVE_AI_TURN`.

It does **not** run wake word, VAD, STT, TTS, the agent, Home Assistant Assist, or local room routing. The brain stays centralized; the Pi is a mic/speaker/display transport.

```
┌─────────────────────┐         WebSocket /api/v1/ws          ┌──────────────────────────┐
│  satellite/         │  user_audio (16 kHz PCM, base64)  ──► │  backend/api/websockets  │
│  jarvis_satellite   │  ◄── jarvis_audio, audio.tts_end      │  + core/voice/*          │
│                     │  ◄── status.update, speech.start      │  + core/turns/*          │
│  ALSA / PyAudio I/O │  audio.playback_end ────────────────► │  (wakeword, STT, agent)  │
└─────────────────────┘                                       └──────────────────────────┘
```

V1 intentionally reuses the browser protocol (JSON envelopes, base64 audio) so one backend path serves both clients. Binary WebSocket frames are planned before multiple active satellites share one host.

## Code layout

| Path | Role |
| :--- | :--- |
| `satellite/src/jarvis_satellite/client.py` | `SatelliteClient` — connect, reconnect, protocol dispatch, playback-end coordination |
| `satellite/src/jarvis_satellite/protocol.py` | Message types and outbound helpers |
| `satellite/src/jarvis_satellite/audio.py` | ALSA (`arecord`/`aplay`) and optional PyAudio capture/playback |
| `satellite/src/jarvis_satellite/identity.py` | Stable `node_id`, WebSocket URL + presence query params |
| `satellite/src/jarvis_satellite/ticket.py` | `POST /api/v1/device-auth/ws-ticket` exchange |
| `satellite/src/jarvis_satellite/backend_url.py` | URL guardrails (`ws://` vs `wss://`) |
| `satellite/src/jarvis_satellite/notification_audio.py` | Local `chime` / `timer` / `alarm` assets plus tool cue WAV decoding |
| `satellite/src/jarvis_satellite/led.py` | Optional ReSpeaker XVF3800 ring LED sync |
| `satellite/deploy/` | `config.example.toml`, systemd unit, `deploy.sh` |

Operational Pi setup (ALSA devices, XVF3800 channel map, LED table) lives in [`satellite/README.md`](../satellite/README.md).

## Lifecycle

1. **Load config** — `~/.jarvis-satellite/config.toml`, overridden by `JARVIS_SATELLITE_*` env vars and CLI flags (`--config`, `--backend-url`, etc.).
2. **Resolve `node_id`** — from config, or `identity.json` under `state_dir`, or `hostname-<uuid>`.
3. **Start audio** — mic chunks enqueued; playback callbacks signal drain events.
4. **Mint ticket** — if `device_token` is set, `POST /api/v1/device-auth/ws-ticket` on the HTTP origin derived from `backend_url`.
5. **Connect** — `websockets.connect` to `backend_url` with presence query params (`timezone`, `node_id`, `capabilities`, optional location refs, `ticket`).
6. **Run four tasks** until one exits: receive loop, mic loop (`user_audio`), heartbeat (`system.ping` / `system.pong`), playback-drained handler.
7. **Reconnect** — exponential backoff (`reconnect_base_delay_s` → `reconnect_max_delay_s`). Close code **4001** (`NODE_REPLACED`) means another client claimed the same `node_id`; the process stops instead of reconnecting.

Optional **`--activate`** sends `voice.activate` once after connect (testing only). Daily use relies on backend wake word over continuous PCM.

## WebSocket protocol

The satellite speaks the same message vocabulary as `frontend/src/client/`. Envelope shape: `{ "id", "type", "data" }`.

### Client → server

| Type | When |
| :--- | :--- |
| `user_audio` | Continuous mic chunks: 16 kHz mono `s16le`, base64 in `data.audio` |
| `audio.playback_end` | Local speaker drained for the current TTS turn; echoes `turn_id` when known (see below) |
| `system.ping` | Heartbeat every `heartbeat_interval_s`; socket closed if no `system.pong` within `heartbeat_timeout_s` |
| `voice.activate` | Only with `--activate` / `auto_activate` |

### Server → client

| Type | Satellite behavior |
| :--- | :--- |
| `system.connect` | Log accepted identity; seed LED context from `session` / `attention`; receive owner `preferences` |
| `system.pong` | Refresh heartbeat timestamp |
| `system.error` | Log backend error |
| `status.update` | Log stage; drive LED from `stage` + attention / soft-mute context |
| `preferences.update` | Refresh owner runtime preferences such as `audio.tool_cues_enabled`; local config can still disable cues as a safety override |
| `speech.start` | LED feedback (`waking` on wake word, `listening` otherwise). `barge_candidate: true` is reversible and does not stop audio. Wake-word and committed speech stop local alert audio so alarms/timers behave like the browser client |
| `jarvis_audio` | Decode base64 PCM, remember `turn_id`, duck notification audio, enqueue playback (`sample_rate` default 24 kHz) |
| `audio.tts_end` | Mark TTS producer complete for this `turn_id`; finish playback stream |
| `system.stop` | Stop alarm loop and flush playback; send `audio.playback_end` if audio was playing |
| `notification.sound` | Play local asset: `chime`, `timer`, or looping `alarm` until `system.stop` |
| `audio.cue` | Play a short local tool lifecycle cue (`start` / `done`) when backend preferences allow it |

Inbound audio uses **16 kHz** (`protocol.INPUT_SAMPLE_RATE`). Outbound TTS is typically **24 kHz** from the backend Cartesia path.

## Playback completion (`audio.playback_end`)

The backend uses two boundaries (see [SYSTEM_STATES.md](./SYSTEM_STATES.md)):

- **`audio.tts_end`** — backend finished producing the delivery stream for this turn.
- **`audio.playback_end`** — the playback client’s speaker queue has drained.

`SatelliteClient` tracks a per-turn **generation** counter and echoes the backend `turn_id` on `audio.playback_end` so stale completion events cannot be mistaken for the current turn. Happy path:

1. Each `jarvis_audio` chunk records `turn_id`, increments generation, and enqueues audio.
2. `audio.tts_end` records `turn_id`, sets `_tts_end_generation`, and calls `finish_playback_stream()`.
3. When the audio backend reports drain, if generation matches `_tts_end_generation`, send `audio.playback_end` with the same `turn_id` immediately.

**Safety net:** if playback drains before `audio.tts_end` arrives, arm `tts_end_timeout_s` (default 2s). If the marker still never arrives, send a recovery `audio.playback_end` with the last audio `turn_id` so `ACTIVE_AI_TURN` cannot hang.

`system.stop` increments generation, suppresses a spurious drain callback, and sends `audio.playback_end` with the active `turn_id` when stopping mid-playback.

## Presence and auth

At connect, the satellite appends query parameters built by `build_presence_params()`:

- **Required:** `timezone`, `node_id`, `capabilities` (comma-separated, default `mic,speaker`)
- **Optional:** `node_label`, `location_provider`, `room_id`, `room_name`, `ha_area_id`, `ha_device_id`, `ha_entity_id`
- **Auth:** short-lived `ticket` from the device credential

The backend resolves `owner_id` from the device token server-side (`backend/api/websockets/presence.py`). Never send `owner_id` from the client.

**Owner voice profile:** enroll once on the Host in **Settings → Voice & Audio**. The normalized embedding gallery lives under Host `DATA_DIR` and is applied to every live session for that owner — browser and satellite alike — via Stage 2b reload (no satellite restart, no edge enrollment). Until enrollment, Stage 1 wake still runs with accept-all Stage 2b. Satellites never record enrollment audio and never store a voiceprint locally.

Provision a room-speaker credential from the Host UI (**Rooms & devices → Add room speaker**). That mints once via `POST /api/v1/device-auth/satellites` and returns `device_token` plus a canonical `backend_ws_url` (`wss://…/api/v1/ws` when private access is ready). Paste both into the Pi config, restart the service, and wait until Rooms & devices shows the speaker online.

CLI recovery on the brain host:

```bash
task devices:satellite-token -- --node-id jarvis-satellite-1 --node-label "Bedroom Satellite"
```

Add `device_token` to `~/.jarvis-satellite/config.toml` or `JARVIS_SATELLITE_DEVICE_TOKEN`. Prefer the UI-minted `backend_ws_url` over assembling a WebSocket URL by hand. Private access (Tailscale Serve) is enabled from **Settings → Availability** — see [MULTI_DEVICE_REACHABILITY.md](./deployment/MULTI_DEVICE_REACHABILITY.md).

**Turn-origin delivery:** user-initiated voice turns answer on the `connection_id` / `node_id` that asked. Kitchen speaker questions are not spoken on the browser because it connected last. Mid-turn disconnect does not reroute elsewhere.

**Proactive room-targeted alarms:** when a wake alarm is authored for a bound room (for example `deliver_to="bedroom"`), fire-time routing first looks for a live speaker in that room. If the bedroom satellite is offline, critical wake alarms may fall back to the last-active speaker elsewhere in the home. Normal room-targeted reminders stay strict and remain in `awaiting_delivery` until the bound room reconnects. Troubleshooting checklist:

1. Confirm the satellite credential is bound to the intended room (`jarvis.smart_home.bind_node_area`).
2. Confirm the satellite is online in presence with `speaker` capability.
3. Use `scheduler.get_alerts(status="awaiting_delivery")` to inspect backlog rows and `failure_reason` (for example `target_location_offline`).
4. Prefer `deliver_to="anywhere"` for follow-me wake alarms when bedroom hardware is unreliable.

## Audio hardware

### Backends

| `audio_backend` | Use |
| :--- | :--- |
| `alsa` (Pi default) | `arecord` / `aplay` subprocesses — no PortAudio build deps |
| `pyaudio` | In-process PortAudio when headers/wheels are available |
| `auto` | PyAudio if importable, else ALSA |

Diagnostics without a live backend connection:

```bash
task sat:list-devices
uv run python -m jarvis_satellite --dry-run-audio --config ~/.jarvis-satellite/config.toml
```

### ReSpeaker XVF3800 (first-room reference)

USB firmware exposes **stereo 16 kHz**:

- **Input channel 1** — ASR-optimized (set `input_channel_index = 1`)
- **Playback channel 0** — AEC far-end reference (set `playback_channels = 2`)

Keep TTS as **2-channel playback** so hardware AEC has a clean reference. Mono host playback can leave echo that triggers false barge-in / follow-on STT turns.

Example defaults are in `satellite/deploy/config.example.toml`. `task sat:deploy` derives a LAN `.local` backend URL for new configs unless `SATELLITE_BACKEND_URL` is set.

## Notification Sounds And Tool Cues

`notification.sound` plays packaged WAV assets (synced from `frontend/public/sounds` on deploy). Sounds are driven by a local notification producer, but share the same physical playback backend as TTS on the Pi:

- **`chime`**, **`timer`** — play once
- **`alarm`** — loops until `system.stop`

TTS **ducks** active notification audio (`NotificationSoundPlayer.duck()` on first `jarvis_audio`; `unduck()` on `audio.playback_end`). Because the satellite playback backend is shared, stopping a live alarm may flush local playback more aggressively than the browser's separate HTML audio elements; the protocol contract still matches the browser on the important boundary: barge candidates do not stop audio, committed speech does.

`audio.cue` is backend-controlled tool feedback. The owner-wide preference lives on the Jarvis Host (`/api/v1/preferences`, included in `system.connect`, broadcast as `preferences.update`); when `audio.tool_cues_enabled=false`, the backend does not emit cue messages to any client. Satellite `tool_cues_enabled` remains a local safety override: if it is `false`, the Pi ignores cue messages even when the shared preference is enabled.

Tool cue assets are also synced from `frontend/public/sounds`. Keep them short and satellite-friendly (`24 kHz`, mono, low peak) so the ALSA backend can play them without sample-rate churn or clipping on ReSpeaker output.

## Status LED (optional)

When `led_enabled = true`, `led.py` drives the XVF3800 ring over USB (`pyusb`) from `status.update` and `speech.start`, aligned with [SYSTEM_STATES.md](./SYSTEM_STATES.md) frontend stages. Fail-soft: LED errors do not stop audio.

## Home Assistant relationship

HA owns device inventory and areas; JARV1S owns voice turns. Satellites do not use HA Assist in V1.

Typical flow after HA is connected:

1. Commission devices in vendor apps / HA.
2. `jarvis.smart_home.refresh_home_assistant` + `organize_device` for naming and areas.
3. Optional: `jarvis.smart_home.bind_node_area` so "in here" resolves to the satellite’s room.

Config `room_id` / `room_name` (or HA refs) seed `location_ref` at connect; durable binding is written to the device credential via `bind_node_area`.

## Tasks

| Task | Purpose |
| :--- | :--- |
| `task sat:install` | `uv sync` in `satellite/` |
| `task sat:test` | Pytest suite |
| `task sat:list-devices` | Enumerate local audio devices |
| `task sat:deploy` | Rsync to Pi, install systemd user unit, restart service |
| `task sat:logs` | Tail `~/.jarvis-satellite/satellite.log` or user journal |
| `task be:dev:lan` | Bind backend `0.0.0.0:8000` for LAN satellites (contrib) |
| `task devices:satellite-token` | CLI recovery: mint durable device credential (prefer Rooms & devices UI) |
| `task be:latency -- --url ws://<host>:8000/api/v1/ws --device-token …` | Smoke-test WS + turn path |

Deploy example (CLI recovery; dogfood prefers Host UI mint + Serve `wss://`):

```bash
task devices:satellite-token -- --node-id jarvis-satellite-1 --node-label "Bedroom Satellite"
# Add device_token (+ backend_url) to Pi config, then:
task sat:deploy
```

Env overrides: `SATELLITE_HOST`, `SATELLITE_USER`, `SATELLITE_BRAIN_HOST`, `SATELLITE_BACKEND_URL`, `SATELLITE_WRITE_CONFIG=1` to overwrite remote `config.toml`.

## V1 bring-up vs planned work

| Shipped (V0) | Open (see ROADMAP / VOICE_SATELLITE_EDGE) |
| :--- | :--- |
| Thin Python client, stable `node_id`, presence params | Binary WebSocket audio frames |
| Continuous PCM + backend-owned wake (default) **or** on-device PASSIVE `edge_wakeword` with pre-roll flush | Satellite E2E eval rung in unified `task be:eval` |
| `audio.tts_end` + `audio.playback_end` coordination | `task sat:doctor` guided setup |
| Device token + WS ticket auth; Host mint/pairing UI | Room onboarding checklist automation (Pi Tailscale install, etc.) |
| Turn-origin user replies + proactive presence resolver | |
| Rooms & devices UI (online/offline, assign room, revoke) | |
| XVF3800 AEC reference routing (2-ch playback) | |
| Notification sounds + optional LED sync | |

**Idle streaming:** default V1 still streams raw mic PCM for Host wakeword. With `edge_wakeword = true`, PASSIVE detection runs on the Pi; idle rooms stop `user_audio` until local wake (or an active Host session for barge-in). Post-wake streaming stays centralized for VAD, STT, endpointing, and barge-in.

## Operator trace access

Satellite turns stay on turn-origin delivery at runtime — the browser does not receive live transcript or tool events from another node. Debugging uses the **persisted read path only**; opening a trace never subscribes to or perturbs a live turn.

| Layer | Role |
| :--- | :--- |
| `conversations` | Full turn trace keyed by `turn_id` (heard text, tool calls, tool outputs/errors) |
| `turn_runs` | Compact telemetry keyed by `turn_id` (STT, endpointing, voice recovery, timings) |
| Operations UI | Existing Runs overlay — **User turns** facet (opt-in, not in default activity feed) |

**Keying:** `turn_id` is the correlation spine. `node_id` / `node_label` are facets for filtering, not separate conversation identities. Short-term LLM context remains scoped to the origin node; proactive follow-ups on another node are separate turns linked only by operator inspection, not automatic fan-out.

**How to inspect:**

1. **Presence → View turns** on a satellite row opens Operations pre-filtered to that `node_id`.
2. **Operations → Runs → User turns** lists recent user-initiated turns (`GET /api/v1/operations/turns`).
3. Expand a row for drill-down (`GET /api/v1/operations/turns/{turn_id}`): trace lines, STT/endpointing/voice telemetry, tool errors.

**Principal model (current):** operator equals owner for a single-owner home. Future relay/multi-user work must revisit cross-owner trace visibility.

**Future:** optional OpenTelemetry export can reference `turn_id` and store raw content in Mongo; external spans carry metadata and references only.

## Related docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — full turn pipeline (satellite is another WebSocket playback client)
- [SYSTEM_STATES.md](./SYSTEM_STATES.md) — voice modes and `playback_end` semantics
- [VISION.md](./VISION.md) — centralized brain, distributed presence
- [satellite/README.md](../satellite/README.md) — Pi prerequisites, config template, LED table, guided-setup lessons
