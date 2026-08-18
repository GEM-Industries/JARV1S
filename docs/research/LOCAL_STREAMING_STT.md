# On-device Streaming STT — Apple Speech Helper

JARV1S local voice input on macOS uses a supervised **Apple Speech** helper that speaks a small WebSocket contract to the Python Host.

## Components

| Piece | Location | Role |
| --- | --- | --- |
| Host adapter | `backend/core/voice/stt_service.py` (`AppleSpeechSTTService`) | Connects per utterance, feeds 16 kHz PCM, waits for terminal `done` |
| Provider switch | `backend/core/voice/runtime.py` (`SwitchableSTTBackend`) | Selects `apple_speech` or `cartesia` |
| Swift helper | `apps/desktop/apple-stt-helper/` | SpeechAnalyzer / SpeechTranscriber sidecar (macOS 26+) |
| Protocol stub | `backend/tools/apple_speech_helper.py` | Dev/smoke helper when the Swift app cannot be built |
| Supervisor | `apps/desktop/src-tauri/src/supervisor.rs` | Allocates port/token, launches helper, injects `VOICE__apple_speech_*` |

## WebSocket contract

- URL: runtime-only `VOICE__apple_speech_url` (default `ws://127.0.0.1:9091/asr`)
- Auth: `VOICE__apple_speech_token` included on every JSON control message when set
- Binary frames: 16 kHz mono PCM s16le
- Control messages:
  - `{"type":"status"}` → `{"type":"status","ready":bool,"state":...,"detail":...}`
  - `{"type":"prepare"}` → same status shape after permission/asset work
  - `{"type":"start","encoding":"pcm_s16le","sample_rate":16000,"channels":1}` → `{"type":"started"}`
  - `{"type":"finalize"}` → optional final text, then `{"type":"done"}`
  - `{"type":"cancel"}`
- The token field is omitted from these examples but is required on every control message.
- Transcripts are **cumulative snapshots**:
  - `{"type":"partial","text":"..."}`
  - `{"type":"final","text":"..."}`
- Only `done` is terminal. Host owns endpointing (`provider_turn_events=False`).
- Each accepted WebSocket has a server-retained `ClientSession` until the connection
  closes. NWConnection callbacks remain weak to avoid cycles.
- `prepare` validates permission/assets and asks SpeechAnalyzer to prepare once. Each
  utterance still creates its own analyzer; `.lingering` model retention lets macOS
  reuse model resources between sessions.

## Readiness states

`GET /api/v1/voice/input/status` and `POST /api/v1/voice/input/prepare` project helper status into:

- `ready`
- `needs_permission`
- `needs_assets`
- `unavailable`
- `unsupported`
- `missing_key` (Cartesia)

Voice-stream start is rejected when the selected provider is not ready.

## Packaging notes

- Bundle ID: `dev.jarv1s.host.speech`
- Packaged path: `Contents/Helpers/JARV1SSpeechHelper.app` (`LSUIElement`)
- Speech Recognition usage string lives on the helper `Info.plist`
- The supervisor waits briefly (~2s) for the helper to listen, then fails soft: if the helper never binds it is killed and the Host starts with empty `VOICE__apple_speech_*` env. Helper failure cannot block Host/text startup. A watchdog restarts a crashed helper on the same port/token.
- `build-host-runtime.sh` launches the built helper and performs an authenticated
  WebSocket `status` exchange. Release construction fails if the helper merely binds
  TCP but cannot process protocol messages.
- All builds require an Xcode 26+ SDK with SpeechAnalyzer and fail loudly without it. The helper still gates `AppleSpeechEngine` behind `#available(macOS 26.0, *)`, so on macOS < 26 it reports `unsupported` at runtime.

## Provider IDs

- `apple_speech` — on-device (migrated from legacy `local_streaming`)
- `cartesia` — cloud STT

Persisted config stores `stt_provider` plus TTS fields (`tts_provider`, `cartesia_voice_id`, `local_voice_id`). Helper URL/token are never written to MongoDB. See [LOCAL_TTS.md](./LOCAL_TTS.md) for spoken-reply providers.
