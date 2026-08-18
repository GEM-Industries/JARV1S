import asyncio

import pytest
from fastapi import HTTPException

from api.routes import voice as voice_routes
from core.voice import service as voice_service
from core.voice.config import (
    ResolvedVoiceConfig,
    VoiceConfigSource,
    UpdateVoiceRuntimeConfigRequest,
    resolve_voice_config_sync,
    voice_config_store,
)
from core.voice.service import ensure_voice_config_available, update_voice_config
from core.voice.runtime import SwitchableSTTBackend, SwitchableTTSBackend


def test_resolve_voice_config_uses_persisted_provider():
    original = voice_config_store._cache
    voice_config_store._cache = {
        "stt_provider": "cartesia",
        "tts_provider": "off",
        "cartesia_voice_id": None,
        "local_voice_id": "af_heart",
    }
    try:
        config = resolve_voice_config_sync()
    finally:
        voice_config_store._cache = original

    assert config.stt_provider == "cartesia"
    assert config.tts_provider == "off"
    assert config.source == VoiceConfigSource.PERSISTED


def test_migrate_local_streaming_to_apple_speech():
    original = voice_config_store._cache
    voice_config_store._cache = {
        "stt_provider": "local_streaming",
        "tts_provider": "off",
        "cartesia_voice_id": None,
        "local_voice_id": "af_heart",
    }
    try:
        config = resolve_voice_config_sync()
    finally:
        voice_config_store._cache = original

    assert config.stt_provider == "apple_speech"


def test_migrate_legacy_tts_voice_id_to_cartesia():
    original = voice_config_store._cache
    voice_config_store._cache = {
        "stt_provider": "apple_speech",
        "tts_voice_id": "legacy-voice",
    }
    # Simulate load path normalization.
    from core.voice.config import _persisted_from_doc

    normalized = _persisted_from_doc(
        {"stt_provider": "apple_speech", "tts_voice_id": "legacy-voice"}
    )
    voice_config_store._cache = normalized
    try:
        config = resolve_voice_config_sync()
    finally:
        voice_config_store._cache = original

    assert config.tts_provider == "cartesia"
    assert config.cartesia_voice_id == "legacy-voice"


@pytest.mark.asyncio
async def test_voice_route_maps_bad_update_to_400(monkeypatch):
    async def fail_update(_request):
        raise ValueError("Store a Cartesia API key before selecting Cartesia voice input.")

    monkeypatch.setattr(voice_routes.voice_service, "update_voice_config", fail_update)

    with pytest.raises(HTTPException) as exc:
        await voice_routes.update_voice_config(
            UpdateVoiceRuntimeConfigRequest(stt_provider="cartesia")
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_selecting_cartesia_requires_stored_key(monkeypatch):
    monkeypatch.setattr("core.voice.service.credential_store.get_stored_secret", lambda _name: None)

    with pytest.raises(ValueError, match="Cartesia API key"):
        await update_voice_config(UpdateVoiceRuntimeConfigRequest(stt_provider="cartesia"))


@pytest.mark.asyncio
async def test_text_only_replies_set_tts_off(monkeypatch):
    current = ResolvedVoiceConfig(
        stt_provider="apple_speech",
        tts_provider="cartesia",
        cartesia_voice_id="voice-1",
        local_voice_id="af_heart",
        source=VoiceConfigSource.PERSISTED,
    )
    saved: dict[str, object] = {}

    async def fake_resolve():
        return current

    async def fake_save(**kwargs):
        saved.update(kwargs)

    async def fake_promote(*_args, **_kwargs):
        return None

    monkeypatch.setattr(voice_service, "resolve_voice_config", fake_resolve)
    monkeypatch.setattr(voice_service.voice_config_store, "save", fake_save)
    monkeypatch.setattr(voice_service, "get_voice_config", fake_resolve)
    monkeypatch.setattr(voice_service.switchable_tts, "promote", fake_promote)

    result = await update_voice_config(
        UpdateVoiceRuntimeConfigRequest(tts_provider="off")
    )

    assert result == current
    assert saved["tts_provider"] == "off"
    assert saved["cartesia_voice_id"] == "voice-1"


@pytest.mark.asyncio
async def test_missing_cartesia_key_falls_back_to_local(monkeypatch):
    voice_config_store._cache = {
        "stt_provider": "cartesia",
        "tts_provider": "off",
        "cartesia_voice_id": None,
        "local_voice_id": "af_heart",
    }
    saved: dict[str, str | None] = {}

    async def fake_save(**kwargs):
        saved.update(kwargs)
        voice_config_store._cache = dict(kwargs)

    async def fake_refresh():
        return None

    monkeypatch.setattr("core.voice.service.credential_store.get_stored_secret", lambda _name: None)
    monkeypatch.setattr("core.voice.service.voice_config_store.save", fake_save)
    monkeypatch.setattr("core.voice.service.switchable_stt.refresh", fake_refresh)
    monkeypatch.setattr("core.voice.service.macos_supports_apple_speech", lambda: True)

    await ensure_voice_config_available()

    assert saved["stt_provider"] == "apple_speech"
    voice_config_store.clear_cache()


@pytest.mark.asyncio
async def test_clone_cartesia_voice_selects_cartesia(monkeypatch):
    clone_args: dict[str, object] = {}
    selected_provider: str | None = None
    selected_voice_id: str | None = None

    class FakeVoices:
        async def clone(self, **kwargs):
            clone_args.update(kwargs)
            return type("Voice", (), {"id": "cloned-voice-id"})()

    class FakeCartesia:
        def __init__(self, *, api_key: str):
            assert api_key == "secret"
            self.voices = FakeVoices()

        async def close(self):
            return None

    async def fake_update(request):
        nonlocal selected_provider, selected_voice_id
        selected_provider = request.tts_provider
        selected_voice_id = request.cartesia_voice_id
        return "updated"

    monkeypatch.setattr("cartesia.AsyncCartesia", FakeCartesia)
    monkeypatch.setattr(voice_service.credential_store, "get_stored_secret", lambda _name: "secret")
    monkeypatch.setattr(voice_service, "update_voice_config", fake_update)

    result = await voice_service.clone_cartesia_voice(
        clip=b"audio",
        filename="voice.wav",
        content_type="audio/wav",
        name="JARV1S voice",
        language="en",
    )

    assert result == "updated"
    assert selected_provider == "cartesia"
    assert selected_voice_id == "cloned-voice-id"
    assert clone_args["clip"] == ("voice.wav", b"audio", "audio/wav")
    assert clone_args["extra_headers"] == {"Cartesia-Version": "2026-03-01"}


@pytest.mark.asyncio
async def test_apple_speech_status_unavailable_without_url(monkeypatch):
    config = ResolvedVoiceConfig(
        stt_provider="apple_speech",
        tts_provider="off",
        cartesia_voice_id=None,
        local_voice_id="af_heart",
        source=VoiceConfigSource.PERSISTED,
    )

    async def fake_resolve():
        return config

    monkeypatch.setattr(voice_service, "resolve_voice_config", fake_resolve)
    monkeypatch.setattr(voice_service, "macos_supports_apple_speech", lambda: True)
    monkeypatch.setattr(
        type(config),
        "apple_speech_url",
        property(lambda self: ""),
    )
    voice_service._invalidate_voice_input_status_cache()

    status = await voice_service.get_voice_input_status(
        provider="apple_speech", force=True
    )
    assert status.ready is False
    assert status.state == "unavailable"
    assert "not running" in (status.detail or "").lower()


@pytest.mark.asyncio
async def test_apple_speech_probe_is_bounded(monkeypatch):
    config = ResolvedVoiceConfig(
        stt_provider="apple_speech",
        tts_provider="off",
        cartesia_voice_id=None,
        local_voice_id="af_heart",
        source=VoiceConfigSource.PERSISTED,
    )

    class HangingClient:
        def __init__(self, **_kwargs):
            pass

        async def status(self):
            await asyncio.Event().wait()

    async def fake_resolve():
        return config

    monkeypatch.setattr(voice_service, "resolve_voice_config", fake_resolve)
    monkeypatch.setattr(voice_service, "AppleSpeechHelperClient", HangingClient)
    monkeypatch.setattr(voice_service, "_APPLE_SPEECH_PROBE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(voice_service, "macos_supports_apple_speech", lambda: True)
    voice_service._invalidate_voice_input_status_cache()

    status = await asyncio.wait_for(
        voice_service.get_voice_input_status(provider="apple_speech", force=True),
        timeout=0.2,
    )
    assert status.ready is False
    assert status.state == "unavailable"
    assert status.detail == "Apple Speech helper unreachable: TimeoutError"


@pytest.mark.asyncio
async def test_switchable_stt_rebuilds_when_config_changes(monkeypatch):
    configs = [
        ResolvedVoiceConfig(
            stt_provider="apple_speech",
            tts_provider="cartesia",
            cartesia_voice_id="voice-1",
            local_voice_id="af_heart",
            source=VoiceConfigSource.PERSISTED,
        ),
        ResolvedVoiceConfig(
            stt_provider="cartesia",
            tts_provider="cartesia",
            cartesia_voice_id="voice-1",
            local_voice_id="af_heart",
            source=VoiceConfigSource.PERSISTED,
        ),
    ]
    built: list[str] = []

    class FakeBackend:
        capabilities = object()

        def __init__(self, name: str):
            self.name = name

        async def initialize(self):
            return None

        async def transcribe_batched(self, _audio_bytes: bytes) -> str:
            return self.name

        async def start_streaming(self, on_transcript=None, on_turn_end=None):
            return None

    async def fake_resolve():
        return configs[0]

    def fake_build(config):
        built.append(config.stt_provider)
        return FakeBackend(config.stt_provider)

    monkeypatch.setattr("core.voice.runtime.resolve_voice_config", fake_resolve)
    monkeypatch.setattr("core.voice.runtime.build_stt_backend", fake_build)

    switcher = SwitchableSTTBackend()
    assert await switcher.transcribe_batched(b"audio") == "apple_speech"
    configs[0] = configs[1]
    assert await switcher.transcribe_batched(b"audio") == "cartesia"
    assert built == ["apple_speech", "cartesia"]


@pytest.mark.asyncio
async def test_switchable_tts_defers_close_until_stream_ends(monkeypatch):
    class FakeBackend:
        sample_rate = 24000

        def __init__(self, name: str):
            self.name = name
            self.closed = False

        @property
        def ready(self) -> bool:
            return True

        async def initialize(self) -> bool:
            return True

        def prepare_for_turn(self) -> None:
            return

        async def generate_audio_stream(self, text, context_id=None, add_silence_ms=0):
            del text, context_id, add_silence_ms
            yield f"{self.name}-audio".encode()
            await asyncio.sleep(0.05)
            yield b"more"

        async def close(self) -> None:
            self.closed = True

    first = FakeBackend("first")
    second = FakeBackend("second")
    config = ResolvedVoiceConfig(
        stt_provider="apple_speech",
        tts_provider="cartesia",
        cartesia_voice_id="v1",
        local_voice_id="af_heart",
        source=VoiceConfigSource.PERSISTED,
    )

    async def fake_resolve():
        return config

    monkeypatch.setattr("core.voice.runtime.resolve_voice_config", fake_resolve)
    switcher = SwitchableTTSBackend()
    await switcher.promote(first, config.tts_signature)

    chunks: list[bytes] = []

    async def consume():
        async for chunk in switcher.generate_audio_stream("hi"):
            chunks.append(chunk)
            if len(chunks) == 1:
                await switcher.promote(second, ("local", "af_heart", "ws://x"))

    await consume()
    assert chunks[0] == b"first-audio"
    assert first.closed is True
    assert second.closed is False
