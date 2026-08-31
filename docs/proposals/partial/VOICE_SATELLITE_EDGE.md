# Voice satellite edge node

**How the shipped client works today:** [SATELLITE.md](../../SATELLITE.md) (normative). This doc tracks hardware targets, bring-up checklists, and **remaining** work.

## Goal

Turn the first Raspberry Pi + ReSpeaker room unit into a reliable JARV1S endpoint: wake/capture audio in the room, play assistant audio back, identify itself with stable presence metadata, and let the central backend do the reasoning.

V1 is a thin JARV1S node, not a Home Assistant Assist satellite and not an edge LLM box.

## Hardware target

- Raspberry Pi Zero 2 W or replacement low-power Linux node
- ReSpeaker/XVF3800 USB audio path
- 3.5mm powered speaker or small passive driver from the ReSpeaker output path
- Same LAN as the central JARV1S backend

## Architecture decision

Keep the brain centralized:

1. Satellite captures 16 kHz mono PCM and sends it over the existing JARV1S WebSocket audio contract.
2. Backend handles wakeword state, STT, turn detection, agent execution, TTS, widgets, and traces.
3. Satellite plays backend audio and reconnects after Wi-Fi or backend restarts.

This matches the existing `Dumb Terminal / Smart Backend` multi-device strategy and avoids splitting agent state across rooms.

V1 implementation lives in `satellite/` as a lightweight Python service. It is intentionally separate from `backend/` so the Pi does not install Mongo, STT, MLX, agent, or integration dependencies.

V1's backend-owned wakeword is a bring-up choice, not the final multi-room privacy model. Before adding independent rooms, PASSIVE wake detection should move to the satellite so idle rooms do not stream raw microphone audio. After wake, and while the assistant is listening or speaking, the satellite should continue streaming to the backend so the existing VAD, STT, endpointing, fast recovery, and barge-in behavior remains centralized.

## V1 responsibilities

- Persist a stable `node_id`, for example `satellite-kitchen-1`.
- Announce capabilities: `mic`, `speaker`, and optionally `display`.
- Send optional location refs: `room_id`, `room_name`, `ha_area_id`, `ha_device_id`, or `ha_entity_id`.
- Capture audio as 16 kHz mono signed 16-bit PCM.
- Continuously stream microphone PCM for first-room V1 bring-up, including while assistant audio is playing, so backend wakeword, barge-in, exact stop/dismiss commands, and echo validation keep working.
- Play streamed assistant audio with low startup latency and send one `audio.playback_end` after local playback drains and backend `audio.tts_end` confirms the delivery stream is complete. A per-turn timeout backs this up if the marker is lost.
- Start under user systemd and reconnect with backoff. Full boot persistence may require enabling lingering for the Pi user.
- Expose enough local logs to debug audio device, network, and service failures over SSH.

## Home Assistant relationship

Home Assistant owns device pairing and area inventory. JARV1S owns voice turns and smart-home control.

Flow after HA is connected:

1. Commission devices in Smart Life/Tuya (or pair in HA for other integrations).
2. Run `jarvis.smart_home.refresh_home_assistant` to reload Tuya and list controllable candidates.
3. Run `jarvis.smart_home.organize_device` to name the device and assign an HA area.
4. Control a light with a normal voice command (e.g. "turn on the bedroom light").
5. Optionally bind the physical satellite node to the HA area with `jarvis.smart_home.bind_node_area` when the user confirms they want "in here" commands from that node.

Do not route voice through HA Assist in V1. If HA's Linux Voice Assistant or ESPHome protocol becomes useful later, treat it as a separate adapter decision, not the first implementation path.

## Bring-up checklist

1. SSH into the Pi and confirm stable hostname/network access.
2. Confirm USB audio enumeration with ALSA.
3. Record a five-second sample from the ReSpeaker and inspect playback on the Mac or Pi.
4. Play a known WAV through the selected speaker path.
5. Measure whether TTS playback leaks into the mic strongly enough to trigger barge-in.
6. Install the satellite client as a user systemd service.
7. Confirm reconnect after backend restart, Wi-Fi drop, and Pi reboot.
8. Run the wakeword/STT/latency eval ladder against real captured audio before using it daily.

## Eval gates

Use the existing voice eval order:

1. Wakeword: `backend/tools/eval_wakeword.py`
2. STT quality: `backend/tools/eval_stt.py` with real room captures
3. Full turn latency: `task be:latency -- --activate-audio`

The first satellite is not complete until wake reliability, echo behavior, and short-command latency are good enough in the room where it will live.

## Non-goals for V1

- Edge LLM inference
- Edge Parakeet/STT on the Pi
- Local TTS on the Pi
- HA Assist / Wyoming / ESPHome voice protocol integration
- Multi-room proactive routing
- Speaker diarization or enrolled voice identity
- Generic device pairing automation inside HA

## V1 decisions and remaining questions

- The minimum useful satellite client is a small Python service in `satellite/` using the existing `/api/v1/ws` JSON/base64 protocol.
- The first Pi deployment uses the ALSA `arecord`/`aplay` backend because it avoids Python audio build dependencies. The PyAudio backend remains optional for systems with PortAudio headers/wheels.
- LAN satellite testing uses `task be:dev:lan` (`0.0.0.0:8000`) and `ws://<brain-hostname>.local:8000/api/v1/ws`. See [MULTI_DEVICE_REACHABILITY.md](../../deployment/MULTI_DEVICE_REACHABILITY.md).
- Wakeword stays in the backend for V1. The satellite continuously streams PCM while the backend owns `PASSIVE -> ACTIVE_IDLE -> ACTIVE_AI_TURN`, VAD, STT, endpointing, fast recovery, and barge-in candidate policy. For V1.5/multi-room, the satellite should own PASSIVE wakeword and only stream after local wake or while the room is already active.
- `speech.start` with `barge_candidate: true` is feedback only; the satellite must not stop playback until the backend sends `system.stop`.
- The ReSpeaker path is exposed as a standard USB audio device on the first Pi image, so no vendor driver is required for V1 bring-up.
- Resolved for first-room V1: the chosen speaker path requires XVF3800 hardware AEC reference routing. Satellite playback now sends 2-channel TTS so playback channel 0 carries the far-end reference, and the clean echo repro no longer creates a follow-on false STT turn.

## Follow-ups before room two

- Reachable backend — implemented; see [MULTI_DEVICE_REACHABILITY.md](../../deployment/MULTI_DEVICE_REACHABILITY.md). Host **Availability** enables Tailscale Serve in-app.
- Device authentication — implemented via owner-bearing device credentials, Rooms **Connect speaker** (Host LAN pair; `jarvis-satellite pair` fallback), CLI recovery (`task devices:*` / `POST /device-auth/satellites`), and REST ws-ticket exchange before connect. Revocation is in **Rooms & devices** (`PresencePanel`).
- Turn-origin output — implemented; user turns answer on the originating `connection_id` / `node_id`. Mid-turn disconnect does not fan out to another node. Background `run_protocol` omits bogus `connection_id` and falls back to owner-default. Proactive delivery uses the presence endpoint router.
- Local wakeword in PASSIVE — implemented behind satellite `edge_wakeword`: idle rooms run openWakeWord on-device and send no `user_audio` until wake (or while the Host session is already active for barge-in). Active rooms continue streaming for backend VAD/STT/barge-in. Default remains continuous Host-owned wake for bring-up; the live edge wake → STT → TTS gate is still pending.
- Protocol framing — keep JSON/base64 for the first room because it reuses the browser contract, but move audio to binary WebSocket frames before adding multiple active satellites.
- Playback completion — backend `audio.tts_end` retires the normal drain debounce: `audio.playback_end` is sent immediately once local playback has drained and the marker has arrived. The remaining timer is a per-turn missing-marker safety net, not happy-path latency.
- Conversation session policy — keep long-term memory owner-global, but scope short-term LLM history to a structured conversation key (`owner_id`, node/window, optional future `speaker_id`) before independent rooms can speak concurrently.
- Presence resolver/UI — shipped: fire-time resolver + Rooms & devices overlay (online/offline, assign room, add room speaker, revoke). Remaining gaps are ops tooling (`sat:doctor`), not a second devices panel.
- Acoustic reference path — done for first-room V1. Keep `playback_channels = 2` for XVF3800/ReSpeaker deployments so channel 0 remains the AEC reference; only tune `AUDIO_MGR_SYS_DELAY` / `AUDIO_MGR_REF_GAIN` if a new room still shows measurable residual bleed.
- Ducking — consider reducing assistant playback volume during committed listening/barge-in windows once the basic audio path is stable.
