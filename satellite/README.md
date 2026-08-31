# JARV1S Satellite

Thin Raspberry Pi voice endpoint for JARV1S.

**Architecture and protocol reference:** [docs/SATELLITE.md](../docs/SATELLITE.md). Deployment URLs and device auth: [docs/deployment/MULTI_DEVICE_REACHABILITY.md](../docs/deployment/MULTI_DEVICE_REACHABILITY.md).

The satellite captures room audio, streams 16 kHz mono `s16le` PCM to the central backend over `/api/v1/ws`, plays backend `jarvis_audio`, and reports `audio.playback_end` after local playback drains and backend `audio.tts_end` confirms no more TTS chunks are coming. If playback drains but the marker is lost, a per-turn timeout sends a recovery `audio.playback_end` so the backend does not stay in `ACTIVE_AI_TURN`. By default the Host owns wakeword; with `edge_wakeword = true` the satellite runs local PASSIVE openWakeWord, keeps a short pre-roll, and only streams after wake or while the Host session is active (listening/speaking). STT, TTS, VAD, barge-in, agent logic, Home Assistant Assist, and room routing stay on the Host.

It also plays local notification sounds on backend `notification.sound` (`chime`, `timer`, `alarm`); `alarm` loops until the backend sends `system.stop`. The shared frontend sound assets are played separately from TTS so speech can duck active notification audio.

## Raspberry Pi Prerequisites

```bash
sudo apt update
sudo apt install -y alsa-utils
```

The default Pi deployment uses `arecord`/`aplay` so it does not need Python audio build headers. The optional PyAudio backend needs `python3-dev`, `portaudio19-dev`, and `libasound2-dev`.

## Config

Create `~/.jarvis-satellite/config.toml`:

```toml
backend_url = "ws://MacBook-Pro.local:8000/api/v1/ws"
timezone = "Australia/Sydney"
node_id = "jarvis-satellite-1"
node_label = "Bedroom Satellite"
capabilities = ["mic", "speaker"]

# Optional room refs.
room_id = "bedroom"
room_name = "Bedroom"

# ALSA device names from `arecord -L` / `aplay -L`.
audio_backend = "alsa"
input_device = "plughw:Array,0"
output_device = "plughw:Array,0"

# XVF3800 USB firmware exposes processed stereo at 16 kHz:
# channel 0 = conference, channel 1 = ASR-optimized.
input_channels = 2
input_channel_index = 1
# XVF3800 AEC uses playback channel 0 as the far-end reference.
playback_channels = 2

# Safety net only: normal turns use audio.tts_end and pay no fixed tail.
tts_end_timeout_s = 2.0

# Local safety override only. The normal Tool cues toggle is owner-wide on the
# Jarvis Host and is delivered through system.connect / preferences.update.
tool_cues_enabled = true

# Optional ReSpeaker XVF3800 status LED sync (see README).
# led_enabled = true
# xvf_host_path = "~/.jarvis-satellite/xvf_host"
# led_brightness = 80
```

**Dogfood (recommended):** on the Mac Host, finish **Settings → Availability → Enable private access**, deploy the speaker (`task sat:deploy`), then **Rooms & devices → Connect speaker**. The Host pairs over the LAN. If that fails, Rooms shows a paste-able command for the Pi, or from this Mac:

```bash
task sat:pair -- CODE
```

**Contributor LAN:** run `task be:dev:lan` so the backend listens on `0.0.0.0:8000`, then point `backend_url` at `ws://<brain-hostname>.local:8000/api/v1/ws`. Prefer the `.local` hostname over a raw LAN IP so DHCP lease changes do not break the satellite. Tailnet / public: [docs/deployment/MULTI_DEVICE_REACHABILITY.md](../docs/deployment/MULTI_DEVICE_REACHABILITY.md).

## Run

```bash
uv sync
uv run python -m jarvis_satellite --config ~/.jarvis-satellite/config.toml
```

List audio devices:

```bash
uv run python -m jarvis_satellite --list-devices
```

Pair with a Rooms setup code (fallback if Host LAN pair is unavailable):

```bash
uv run python -m jarvis_satellite pair CODE --url wss://<host>.ts.net:8443/api/v1/ws
```

Dry-run microphone capture:

```bash
uv run python -m jarvis_satellite --dry-run-audio
```

`--list-devices` and `--dry-run-audio` do not open a WebSocket; `backend_url` is validated only when the live client starts. The dry run prints captured chunk count plus peak/RMS levels; if it hangs or stays at `max_peak=0`, fix ALSA/USB audio before debugging Jarvis wakeword.

For XVF3800 echo cancellation, keep TTS playback as a 2-channel stream. The chip uses playback channel 0 as the far-end AEC reference; mono host playback can leave the reference path ambiguous even when audio is audible from the connected speaker.

Prefer pairing from the Host (**Rooms & devices → Connect speaker**). CLI recovery on the brain host:

```bash
task devices:satellite-token -- --node-id jarvis-satellite-1 --node-label "Bedroom Satellite"
```

Add `device_token` to `~/.jarvis-satellite/config.toml` (or `JARVIS_SATELLITE_DEVICE_TOKEN`). The satellite exchanges it for a short-lived WS ticket on each reconnect.

For first backend testing, use `--activate` to send `voice.activate` after connect. Daily use: continuous PCM with Host-owned wakeword, or enable on-device PASSIVE wake:

```toml
edge_wakeword = true
wakeword_model_path = "~/.jarvis-satellite/models/Jarvis.onnx"
wake_preroll_seconds = 3.0
```

Install deps with `uv sync --extra wakeword` (or `SATELLITE_EDGE_WAKEWORD=1 task sat:deploy`). Deploy copies `Jarvis.onnx` into `~/.jarvis-satellite/models/` and enables the config. Idle rooms stop streaming until local wake or an active Host stage needs audio; post-wake VAD/STT/TTS stay on the Host. If edge wake is enabled but its model or dependencies cannot load, the service exits instead of silently reverting to continuous room audio.

## Status LED Sync (ReSpeaker XVF3800)

The XVF3800 ships with DOA mode enabled by default: the ring lights up blue and points green toward any detected speech. That is independent of JARV1S and will react to ambient noise.

The satellite can drive the ring from backend `status.update` and `speech.start` events so it matches the frontend status model in `docs/SYSTEM_STATES.md`:

| Stage | Ring behavior |
| :--- | :--- |
| `idle` / connected / disconnected | Off (bedroom-safe default) |
| `idle` + soft mute | Steady orange |
| `idle` + paused / powered down | Steady red |
| `idle` + quiet attention | Off |
| `waking` | Green fade |
| `listening` | Steady blue ring |
| `transcribing` / `thinking` / `running_tool` | Blue fade |
| `speaking` | Green fade |
| disconnected / reconnecting | Off |

### Setup

Enable LED sync in `~/.jarvis-satellite/config.toml`:

```toml
led_enabled = true
led_brightness = 80
```

The satellite uses direct USB control through `pyusb`, so state changes apply immediately enough for wake-word feedback. On connect the ring stays off until wake/listen/active work begins, which also replaces factory DOA mode.

The XVF3800 can return to its factory rainbow/DOA LED behavior after a USB or device reset. The satellite periodically reasserts the current JARV1S LED state, so an idle bedroom satellite should return to off without needing a physical unplug/replug.

`xvf_host_path` is optional fallback/debug tooling only. If you have the ReSpeaker helper installed, you can manually disable DOA with:

```bash
~/.jarvis-satellite/xvf_host LED_EFFECT --values 0
```

LED sync is optional and fail-soft: if USB control fails, audio streaming continues normally.

## Deploy

```bash
task sat:deploy
```

Deployment installs a user systemd unit on the Pi. For a new config, deploy writes `backend_url` as `ws://<brain-hostname>.local:8000/api/v1/ws`; override with `SATELLITE_BACKEND_URL` for tailnet or public deployments. Logs are written to `~/.jarvis-satellite/satellite.log`; `task sat:logs` tails that file and falls back to the user service journal if the file is missing.

## Guided Setup Lessons

The first successful hardware bring-up exposed a few high-leverage places where Jarvis should carry the setup burden for the user:

- **One command should validate the whole path.** A future `task sat:doctor` / Jarvis-guided setup should check backend reachability, device-token auth, service status, microphone chunks, wakeword detection, STT, TTS playback, and `audio.playback_end` in order. Each step should report a clear pass/fail and the next concrete fix.
- **Detect known audio hardware and choose defaults.** For `reSpeaker XVF3800`, Jarvis should infer `input_channels = 2` and `input_channel_index = 1` from ALSA/USB identity because channel 1 is the ASR-optimized stream. The user should not need to know the firmware channel layout.
- **Fail fast on ALSA before debugging wakeword.** If `arecord` hangs, returns `pcm_read: Input/output error`, or dry-run reports `max_peak=0`, Jarvis should stop and say the USB audio path is broken. Do not let the user keep repeating "Jarvis" when no usable PCM is reaching the backend.
- **Encode Pi Zero OTG fixes as a readiness check.** Pi Zero / Zero 2 OTG on recent Raspberry Pi OS can fail USB isochronous audio until boot params such as `dwc_otg.fiq_enable=0 dwc_otg.fiq_fsm_enable=0` are applied. Jarvis should detect this class of failure, offer to apply the workaround with sudo, reboot, and retest.
- **Make the success moment obvious.** The final setup step should prompt: "Say: Jarvis, what time is it?" and then confirm the observed chain: wakeword detected, transcript captured, response generated, audio played. This is the user's first-room "it works" moment and should be treated as the end of setup, not buried in logs.
