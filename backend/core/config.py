"""
Configuration settings for Jarvis AI Assistant.

This module provides configuration settings for the application, loaded from
environment variables with sensible defaults.
"""

import os
from enum import Enum
from typing import List, Literal, Optional, Dict
from pathlib import Path
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvironmentType(str, Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TESTING = "testing"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class VoiceConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    # Slightly more lenient than 0.8
    vad_threshold: float = 0.8
    # Low enough to catch short responses like "Yes" / "No thanks"
    min_speech_frames: int = 2
    # ~1s at 256ms chunks; browser AEC handles echo, this catches residual bleed
    barge_in_min_frames: int = 4
    # Candidate window: VAD starts a reversible barge-in candidate; handlers commit or suppress.
    barge_in_candidate_min_delay_s: float = 0.15
    # Duplex STT + speaker scoring need ~1.5s; 0.8s caused matched+empty and
    # premature mismatch suppresses while Apple STT was still catching up.
    barge_in_candidate_max_wait_s: float = 1.5
    barge_in_candidate_min_text_chars: int = 4
    # Earliest VAD endpoint candidate; TurnDetector decides whether to commit.
    silence_threshold: float = 0.12
    # Seconds after endpoint within which a new utterance merges with the previous turn
    fast_recovery_window: float = 2.0
    # Silence in ACTIVE_IDLE before returning to PASSIVE (e.g. after playback_end)
    active_timeout: float = 4.0
    # Rolling pre-roll audio retained before wake-word detection, seeded into the turn.
    wake_preroll_seconds: float = 3.0
    # While soft-muted, retain only this much pre-roll (enough for the wake word, not ambient speech).
    soft_mute_preroll_seconds: float = 1.0
    # Local hard-negative finetune operating point: 88% positives_real recall and
    # 1.07 enrolled free-speech FA/hr on the current evaluation set.
    wakeword_sensitivity: float = 0.70
    # Consecutive 80ms frames that must clear the threshold before firing. The adapter
    # saturates to ~1.0 for both real and false triggers, so peak score doesn't discriminate
    # — sustain does: a real "Jarvis" holds a ~7-frame plateau, false triggers are 1-2 frame
    # bursts. N=3 is the measured recall/FAR balance for the current finetuned model.
    wakeword_patience: int = 3
    # Silero VAD gate (openWakeWord built-in). Zeroes scores without speech energy
    # (taps, clicks, TTS bleed), roughly halving false triggers before sustain filtering. 0 disables.
    wakeword_vad_threshold: float = 0.5
    # Minimum gap between committed passive wakes (duplicate/self-trigger guard).
    wakeword_refractory_seconds: float = 2.0
    # Passive wake suppression after assistant TTS ends (room echo / self-wake guard).
    wakeword_post_tts_suppression_seconds: float = 0.8
    # Save user-confirmed / auto-dismissed detections to training/wakeword/data/feedback/positives/
    wakeword_save_positive_feedback: bool = False
    # Stage 2b speaker embedding verifier (Sherpa-ONNX).
    # Runtime uses owner profiles under DATA_DIR when present; otherwise AcceptAll.
    wakeword_speaker_verifier_enabled: bool = True
    wakeword_speaker_model_path: str | None = (
        "resources/models/speaker/nemo_en_titanet_small.onnx"
    )
    # Explicit developer/eval override only. Product runtime loads owner profiles from DATA_DIR.
    wakeword_speaker_profile_path: str | None = None
    # Provisional TitaNet operating point; keep model-specific.
    wakeword_speaker_threshold: float = 0.21
    # Cosine threshold for barge-in owner gating. Calibrated separately from wake
    # because free-speech candidates are not wake-phrase windows.
    # Five-of-eight duplex cross-validation: ~95% owner recall and ~5% FA.
    barge_in_speaker_threshold: float = 0.21
    wakeword_speaker_num_threads: int = 1
    # Stream STT while the user speaks.
    stt_streaming_enabled: bool = True
    stt_stream_finalize_timeout: float = 0.5
    stt_partial_emit_interval_s: float = 0.12

    # Apple Speech helper WebSocket URL. Injected at runtime by the desktop supervisor.
    apple_speech_url: str = "ws://127.0.0.1:9091/asr"
    # Per-launch auth token for the Apple Speech helper (runtime-only).
    apple_speech_token: str = ""
    apple_speech_connect_timeout: float = 2.0
    # Local Kokoro TTS helper WebSocket URL. Injected at runtime by the desktop supervisor.
    local_tts_url: str = "ws://127.0.0.1:9092/tts"
    # Per-launch auth token for the local TTS helper (runtime-only).
    local_tts_token: str = ""
    local_tts_connect_timeout: float = 2.0
    # Apple Speech partials can look semantically complete while still moving.
    # Baseline runs stacked ~200ms after late short-phrase STT; 100ms is enough to
    # reject one-frame flicker without dominating submit latency.
    apple_speech_commit_stability_delay: float = 0.1
    # Cap in-candidate wait when audio EOU says continue. LiveKit pairs v1-mini
    # with max_delay=2.5s; this path only runs on continue, so EOU-done commits
    # stay on turn_detector_min_delay. 0.25s was cutting mid-thought fillers.
    apple_speech_endpoint_max_delay: float = 2.5
    apple_speech_stream_finalize_timeout: float = 0.6
    # Stdout voice trace (batch segments, continuation merge, fast recovery). Also logs
    # transcript regressions to perf. Noisy — enable only while debugging STT/merge.
    trace_voice_events: bool = False
    # Save exact PCM sent to streaming STT as 16 kHz mono WAVs for replay/debugging.
    stt_debug_dump_audio: bool = False
    stt_debug_dump_dir: str = "logs/fixtures/stt_debug"
    # Minimum delay from the last VAD-positive speech frame. The 120ms VAD
    # silence window normally satisfies this floor before TURN_COMPLETE fires.
    turn_detector_min_delay: float = 0.05
    turn_detector_max_delay: float = 3.0
    # After VAD endpoint, poll for the first streaming transcript in-candidate
    # before continue_turn (which resets silence and forces another VAD cycle).
    turn_detector_awaiting_stt_timeout: float = 0.5
    # stt_model is used by offline/raw MLX STT evals.
    # tiny.en-mlx-4bit: ~22MB, ~0.1-0.2s  |  small.en-mlx-4bit: ~130MB, ~0.5s
    # medium.en-mlx-4bit: ~400MB, ~0.8-1.2s  |  large-v3-mlx-4bit: ~800MB, ~1.5-2s
    stt_model: str = "mlx-community/whisper-medium.en-mlx-4bit"


class Settings(BaseSettings):
    # Core Path Settings
    # Path(__file__) is jarv1s/backend/core/config.py
    # .parent.parent is jarv1s/backend/
    BASE_DIR: Path = Path(__file__).parent.parent
    PLUGINS_DIR: Path = BASE_DIR / "plugins"
    DATA_DIR: Path = (
        Path(os.environ["JARVIS_DATA_DIR"])
        if os.environ.get("JARVIS_DATA_DIR")
        else BASE_DIR.parent / ".data"
    )
    # Generated runtime state — never write under BASE_DIR in packaged apps.
    LOGS_DIR: Path = DATA_DIR / "logs"
    CACHE_DIR: Path = DATA_DIR / "cache"

    # Environment Settings
    ENVIRONMENT: EnvironmentType = EnvironmentType.DEVELOPMENT
    DEBUG: bool = True
    LOG_LEVEL: LogLevel = LogLevel.INFO

    # API Settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "Jarvis AI Assistant"
    SYSTEM_NAME: str = "JARV1S"
    VERSION: str = "0.1.0"
    DESCRIPTION: str = (
        "Personal AI assistant with voice interface and smart home integration"
    )
    DEFAULT_USER_ID: str = "local"

    # Per-device WebSocket auth (Phase 10.7)
    DEVICE_AUTH_REQUIRED: bool = True
    DEVICE_AUTH_DEV_BYPASS: bool = True
    DEVICE_AUTH_WS_TICKET_TTL_S: int = 60
    DEVICE_AUTH_PAIRING_CODE_TTL_S: int = 300
    DEVICE_AUTH_PAIRING_MAX_ATTEMPTS: int = 5
    DEVICE_AUTH_PAIRING_ATTEMPT_WINDOW_S: int = 300
    DEVICE_AUTH_COOKIE_MAX_AGE_S: int = 30 * 86400

    # CORS Settings
    BACKEND_CORS_ORIGINS: List[str] = [
        "*"
    ]  # In production, replace with specific origins
    CORS_ORIGINS: List[str] = ["*"]  # Alias for BACKEND_CORS_ORIGINS for compatibility

    # MongoDB Settings
    MONGODB_PORT: int = 27018
    MONGODB_URL: str = ""  # Derived from MONGODB_PORT if not explicitly set
    DATABASE_NAME: str = "jarvis"
    MONGODB_DB_NAME: str = "jarvis"  # Alias for DATABASE_NAME for compatibility

    @model_validator(mode="after")
    def _derive_settings(self) -> "Settings":
        if not self.MONGODB_URL:
            self.MONGODB_URL = f"mongodb://localhost:{self.MONGODB_PORT}"
        if self.ENVIRONMENT == EnvironmentType.PRODUCTION:
            self.DEVICE_AUTH_REQUIRED = True
            self.DEVICE_AUTH_DEV_BYPASS = False
            if (
                not self.BACKEND_CORS_ORIGINS
                or not self.CORS_ORIGINS
                or "*" in self.BACKEND_CORS_ORIGINS
                or "*" in self.CORS_ORIGINS
            ):
                origin = self.FRONTEND_ORIGIN.rstrip("/")
                self.BACKEND_CORS_ORIGINS = [origin]
                self.CORS_ORIGINS = [origin]
        return self

    # Voice Settings (override with VOICE__VAD_THRESHOLD etc.)
    VOICE: VoiceConfig = VoiceConfig()

    # Main assistant LLM setup is stored in MongoDB system_config and CredentialStore.
    LLM_TEXT_REASONING_EFFORT: Literal["low", "medium", "high"] | None = "low"
    LLM_HEADLESS_REASONING_EFFORT: Literal["low", "medium", "high"] | None = "medium"
    LLM_MAX_TOKENS: int = 16384
    LLM_HTTP_TIMEOUT_S: float = 120.0
    LLM_STREAM_FIRST_TOKEN_TIMEOUT_S: float = 7.0
    LLM_STREAM_FIRST_TOKEN_RETRIES: int = 1
    LLM_STREAM_IDLE_TIMEOUT_S: float = 30.0
    # Local/on-device models: cold MLX load + prompt prefill often exceeds
    # the cloud first-token budget (see managed Ollama on :11435).
    LLM_LOCAL_STREAM_FIRST_TOKEN_TIMEOUT_S: float = 90.0
    LLM_LOCAL_STREAM_IDLE_TIMEOUT_S: float = 90.0

    # Background agents (explicit dispatch only; Anthropic-backed today).
    BACKGROUND_AGENT_MODEL: str = "claude-opus-4-8"
    BACKGROUND_AGENT_HTTP_TIMEOUT_S: float = 130.0
    BACKGROUND_AGENT_STREAM_FIRST_TOKEN_TIMEOUT_S: float = 45.0
    BACKGROUND_AGENT_STREAM_FIRST_TOKEN_RETRIES: int = 1
    BACKGROUND_AGENT_STREAM_IDLE_TIMEOUT_S: float = 60.0
    AGENT_MAX_CONCURRENT: int = 2
    AGENT_MAX_DEPTH: int = 1
    AGENT_MAX_PER_SOURCE: int = 3
    AGENT_INPROCESS_MAX_TURNS: int = 30
    # Cap on concurrent headless (non-session) turns — silent automations,
    # prefetch, and SystemPulse escalations share this semaphore.
    AGENT_HEADLESS_CONCURRENCY: int = 5

    # Optional periodic evaluator that escalates unhealthy runtime state.
    SYSTEM_PULSE_ENABLED: bool = False
    SYSTEM_PULSE_INTERVAL_MIN: int = 30

    # Prefetch (Phase 9c) — pre-renders protocol-linked triggers a few minutes
    # early via a headless silent turn so announce delivery is sub-second.
    PREFETCH_ENABLED: bool = True
    PREFETCH_WINDOW_MIN: int = 5
    PREFETCH_POLL_INTERVAL_S: int = 60
    PREFETCH_FALLBACK_TIMEZONE: str = "UTC"

    # Context Management
    # Gemma 4 26B-A4B context: 256K tokens. 100K input leaves headroom for output + loop.
    CONTEXT_MAX_INPUT_TOKENS: int = 100_000
    CONTEXT_OFFLOAD_THRESHOLD: int = 4000
    # Summarize oldest messages when total history tokens exceed this fraction of the budget.
    CONTEXT_SUMMARIZE_THRESHOLD: float = 0.7
    # Short-term prompt history is already node-scoped; this gap starts a fresh prompt window.
    CONVERSATION_SESSION_INACTIVITY_MINUTES: int = 120

    # Performance diagnostics
    PERF_ENABLED: bool = True

    # Prompt Dump — writes full LLM context to logs/prompt_dumps/ on every call.
    # Enable temporarily to audit token usage and identify bloat.
    PROMPT_DUMP_ENABLED: bool = False

    # LiteLLM library debug logging (request payloads, per-token stream deltas, cost lookups).
    LITELLM_VERBOSE_LOGGING: bool = False

    # Integration API Keys (Optional - tools gracefully degrade if missing)
    EXA_API_KEY: Optional[str] = (
        None  # Deprecated in .env — manage via Settings → Credentials.
    )
    SEARXNG_URL: Optional[str] = (
        None  # Self-hosted SearXNG base URL, e.g. http://127.0.0.1:8080
    )

    # MCP Auto-Bridge — path to mcp_servers.yaml (relative to BASE_DIR)
    MCP_SERVERS_CONFIG: Optional[Path] = BASE_DIR / "mcp_servers.yaml"

    # Smart Home (Home Assistant) — direct REST/WebSocket client
    HA_URL: Optional[str] = None
    HA_TOKEN: Optional[str] = None
    # Legacy MCP bridge settings (deprecated — use HA_URL + HA_TOKEN)
    HA_MCP_SERVER_PATH: Optional[str] = None
    HA_MCP_TOKEN: Optional[str] = None

    # Composio — declared here so Pydantic doesn't reject it as an extra field.
    # Product path stores COMPOSIO_API_KEY via Settings → Credentials UI (CredentialStore).
    # First-party OAuth app metadata (product path — no per-user Cloud Console setup).
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = (
        None  # Public desktop-app metadata when required by Google.
    )
    MICROSOFT_OAUTH_CLIENT_ID: Optional[str] = None

    COMPOSIO_API_KEY: Optional[str] = (
        None  # Deprecated in .env — manage via Settings → Credentials.
    )
    # Background agents / contributor CLI may still export this; product LLM
    # credentials live in CredentialStore. Declared so pydantic does not reject it.
    ANTHROPIC_API_KEY: Optional[str] = None
    # OAuth callback URL routes through the frontend proxy so postMessage works.
    # In production set this to your public domain.
    FRONTEND_ORIGIN: str = "http://localhost:5173"
    # Public HTTPS origin for inbound triggers (no trailing path). Packaged Host
    # persists this in Mongo via Availability → External triggers; contributors
    # may set EXTERNAL_INGRESS_BASE_URL explicitly (e.g. a tunnel URL).
    EXTERNAL_INGRESS_BASE_URL: Optional[str] = None
    # Composio webhook HMAC secret — emergency/contributor override. Product
    # runtime persists the secret in CredentialStore after subscription create/rotate.
    COMPOSIO_WEBHOOK_SECRET: Optional[str] = None

    # Multi-Provider Calendar — default label -> provider mapping used only when
    # no runtime mapping exists in MongoDB yet (first boot / before any connect).
    # The runtime mapping is owned by backend/core/auth/account_labels.py.
    ACCOUNT_PROVIDERS: Dict[str, str] = {
        "personal": "google",
        "work": "microsoft",
    }

    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file="../.env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        # Required so VOICE__VAD_THRESHOLD overrides one field and keeps others at default.
        nested_model_default_partial_update=True,
    )

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == EnvironmentType.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == EnvironmentType.PRODUCTION

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT == EnvironmentType.TESTING


# Create settings instance
settings = Settings()

# Export settings
__all__ = ["settings"]
