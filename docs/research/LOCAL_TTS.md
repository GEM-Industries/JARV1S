# On-device TTS — Kokoro Helper

JARV1S local spoken replies on the desktop Host use a supervised **Kokoro** helper that streams PCM to the Python Host over a small WebSocket contract.

## Components

| Piece | Location | Role |
| --- | --- | --- |
| Host adapter | `backend/core/voice/tts_service.py` (`LocalTTSService`) | One WebSocket per sentence; receives `pcm_f32le` @ 24 kHz |
| Provider switch | `backend/core/voice/runtime.py` (`SwitchableTTSBackend`) | Selects `off`, `cartesia`, or `local` |
| Helper | `backend/tools/kokoro_tts_server.py` | Persistent process; lazy model warm; CPU ONNX |
| Assets | `apps/desktop/local-tts/` (dev) / `host/local-tts/` (packaged) | Pinned `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin` |
| Supervisor | `apps/desktop/src-tauri/src/supervisor.rs` | Allocates port/token, launches helper, injects `VOICE__local_tts_*` |

## WebSocket contract

- URL: runtime-only `VOICE__local_tts_url` (default `ws://127.0.0.1:9092/tts`)
- Auth: `VOICE__local_tts_token` on every JSON control message when set
- Control messages:
  - `{"type":"status"}` → `{type, ready, state, detail}`
  - `{"type":"warm","voice":"af_heart"}` → loads/warms model
  - `{"type":"speak","utterance_id","text","voice","speed"}` → binary PCM frames, then `{type:"done"}`
- Closing the speak WebSocket cancels delivery; the helper may finish CPU inference offline
- Binary frames: mono `pcm_f32le` at 24000 Hz (~80 ms per frame)

## Latency characteristics

Kokoro int8 on CPU emits audio only after a whole segment is synthesized, so
time-to-first-audio equals synthesis time for the turn's first sentence
(measured on M-series, `af_heart`):

| Utterance | Synthesis | Audio | Real-time factor |
| --- | --- | --- | --- |
| "Sure." | ~450 ms | 0.7 s | 0.66 |
| ~40 chars | ~950 ms | 2.1 s | 0.46 |
| ~70 chars | ~1550 ms | 3.9 s | 0.40 |

Because the factor stays well under 1.0, the delivery worker synthesizes later
sentences while earlier audio is still playing, so only the first sentence's
latency is user-visible; `perf` records it as `tts_first_chunk` ("Voice start").
Two things were measured and rejected as not worth their cost: ONNX session
tuning (`intra_op_num_threads`, CoreML EP) landed within noise of the default CPU
session, and splitting a sentence into clauses only cuts first-audio by ~25%
because each inference call carries a ~415 ms floor, while adding prosody seams.
Revisit via `tts_first_chunk` if first-audio latency becomes a complaint.

## Persisted config

`system_config.voice_config` stores:

- `stt_provider`
- `tts_provider` (`off` | `cartesia` | `local`)
- `cartesia_voice_id`
- `local_voice_id`

Helper URL/token are never written to MongoDB. Legacy `tts_voice_id` is migrated to `cartesia_voice_id` + `tts_provider=cartesia` on load and unset on the next save.

## Packaging notes

- Assets are bundled by `apps/desktop/scripts/build-local-tts-assets.sh` (URL + SHA-256 pin in `apps/desktop/local-tts/manifest.json`)
- Packaged Host runs the helper with the same relocatable Python as the backend
- Soft-fail: if the helper never binds, Host starts with empty `VOICE__local_tts_*` and text/Cartesia remain available
- Engine: MIT (`kokoro-onnx`); model: Apache-2.0 (Kokoro-82M)

## Provider IDs

- `off` — text replies only
- `cartesia` — cloud Sonic TTS
- `local` — on-device Kokoro
