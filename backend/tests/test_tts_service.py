import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.voice.tts_service import CartesiaTTSService, WEBSOCKET_MAX_IDLE_S


async def _audio_stream(audio: bytes = b"audio"):
    yield SimpleNamespace(audio=audio)


@pytest.mark.asyncio
async def test_replaces_websocket_before_cartesia_idle_timeout():
    service = CartesiaTTSService()
    old_ws = SimpleNamespace(close=AsyncMock())
    new_ws = SimpleNamespace()
    service._client = SimpleNamespace(
        tts=SimpleNamespace(websocket=AsyncMock(return_value=new_ws))
    )
    service._ws = old_ws
    service._ws_last_used_at = time.monotonic() - WEBSOCKET_MAX_IDLE_S

    result = await service._ensure_websocket()

    assert result is new_ws
    old_ws.close.assert_awaited_once()
    service._client.tts.websocket.assert_awaited_once()


@pytest.mark.asyncio
async def test_retries_once_when_stale_websocket_fails_before_audio():
    async def failed_stream():
        raise RuntimeError("connection idle timeout")
        yield

    async def successful_stream():
        yield SimpleNamespace(audio=b"audio")

    stale_ws = SimpleNamespace(
        send=AsyncMock(return_value=failed_stream()),
        close=AsyncMock(),
    )
    fresh_ws = SimpleNamespace(
        send=AsyncMock(return_value=successful_stream()),
        close=AsyncMock(),
    )
    service = CartesiaTTSService()
    service._client = SimpleNamespace(
        tts=SimpleNamespace(websocket=AsyncMock(return_value=fresh_ws))
    )
    service._ws = stale_ws
    service._ws_last_used_at = time.monotonic()

    with patch(
        "core.voice.tts_service.resolve_voice_config_sync",
        return_value=SimpleNamespace(cartesia_voice_id="voice"),
    ):
        chunks = [
            chunk
            async for chunk in service.generate_audio_stream("Wake up, sir.")
        ]

    assert chunks
    stale_ws.close.assert_awaited_once()
    service._client.tts.websocket.assert_awaited_once()
    fresh_ws.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_does_not_retry_after_audio_was_yielded():
    async def partial_stream():
        yield SimpleNamespace(audio=b"first")
        yield SimpleNamespace(audio=b"second")
        raise RuntimeError("connection lost")

    ws = SimpleNamespace(
        send=AsyncMock(return_value=partial_stream()),
        close=AsyncMock(),
    )
    service = CartesiaTTSService()
    service._client = SimpleNamespace(
        tts=SimpleNamespace(websocket=AsyncMock())
    )
    service._ws = ws
    service._ws_last_used_at = time.monotonic()

    with patch(
        "core.voice.tts_service.resolve_voice_config_sync",
        return_value=SimpleNamespace(cartesia_voice_id="voice"),
    ):
        chunks = []
        with pytest.raises(RuntimeError, match="connection lost"):
            async for chunk in service.generate_audio_stream("Partial response"):
                chunks.append(chunk)

    assert chunks == [b"first"]
    service._client.tts.websocket.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_reuses_fresh_websocket_without_warmup():
    service = CartesiaTTSService()
    ws = SimpleNamespace()
    service._client = SimpleNamespace(tts=SimpleNamespace(websocket=AsyncMock()))
    service._ws = ws
    service._ws_last_used_at = time.monotonic()

    with (
        patch.object(service, "warmup", AsyncMock()) as warmup,
        patch(
            "core.voice.tts_service.credential_store.get_stored_secret",
            return_value="key",
        ),
        patch(
            "core.voice.tts_service.resolve_voice_config",
            AsyncMock(return_value=SimpleNamespace(cartesia_voice_id="voice")),
        ),
    ):
        assert await service.initialize() is True

    service._client.tts.websocket.assert_not_awaited()
    warmup.assert_not_awaited()
    assert service.ready is True


@pytest.mark.asyncio
async def test_initialize_reconnects_and_warms_idle_websocket_once():
    service = CartesiaTTSService()
    old_ws = SimpleNamespace(close=AsyncMock())
    new_ws = SimpleNamespace()
    service._client = SimpleNamespace(
        tts=SimpleNamespace(websocket=AsyncMock(return_value=new_ws))
    )
    service._ws = old_ws
    service._ws_last_used_at = time.monotonic() - WEBSOCKET_MAX_IDLE_S

    with (
        patch.object(service, "warmup", AsyncMock()) as warmup,
        patch(
            "core.voice.tts_service.credential_store.get_stored_secret",
            return_value="key",
        ),
        patch(
            "core.voice.tts_service.resolve_voice_config",
            AsyncMock(return_value=SimpleNamespace(cartesia_voice_id="voice")),
        ),
    ):
        assert await service.initialize() is True

    old_ws.close.assert_awaited_once()
    service._client.tts.websocket.assert_awaited_once()
    warmup.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_for_turn_deduplicates_inflight_preparation():
    service = CartesiaTTSService()
    started = asyncio.Event()
    release = asyncio.Event()

    async def initialize():
        started.set()
        await release.wait()
        return True

    with patch.object(service, "initialize", side_effect=initialize) as initialize_mock:
        service.prepare_for_turn()
        first_task = service._prepare_task
        service.prepare_for_turn()

        await started.wait()
        assert service._prepare_task is first_task
        release.set()
        assert first_task is not None
        await first_task
        await asyncio.sleep(0)

    initialize_mock.assert_awaited_once()
    assert service._prepare_task is None


@pytest.mark.asyncio
async def test_generation_waits_for_inflight_turn_preparation():
    service = CartesiaTTSService()
    release = asyncio.Event()

    async def prepare():
        await release.wait()

    ws = SimpleNamespace(
        send=AsyncMock(return_value=_audio_stream()),
        close=AsyncMock(),
    )
    service._client = SimpleNamespace(tts=SimpleNamespace(websocket=AsyncMock()))
    service._ws = ws
    service._ws_last_used_at = time.monotonic()
    service._prepare_task = asyncio.create_task(prepare())

    async def consume():
        return [
            chunk
            async for chunk in service.generate_audio_stream("Prepared response")
        ]

    with patch(
        "core.voice.tts_service.resolve_voice_config_sync",
        return_value=SimpleNamespace(cartesia_voice_id="voice"),
    ):
        generation = asyncio.create_task(consume())
        await asyncio.sleep(0)
        ws.send.assert_not_awaited()
        release.set()
        assert await generation

    assert ws.send.await_args.kwargs["max_buffer_delay_ms"] == 0


@pytest.mark.asyncio
async def test_close_cancels_inflight_turn_preparation():
    service = CartesiaTTSService()
    started = asyncio.Event()

    async def prepare():
        started.set()
        await asyncio.Event().wait()

    service._prepare_task = asyncio.create_task(prepare())
    await started.wait()

    await service.close()

    assert service._prepare_task is None


@pytest.mark.asyncio
async def test_warmup_disables_server_side_buffering():
    service = CartesiaTTSService()
    ws = SimpleNamespace(send=AsyncMock(return_value=_audio_stream()))
    service._client = SimpleNamespace(tts=SimpleNamespace(websocket=AsyncMock()))
    service._ws = ws
    service._ws_last_used_at = time.monotonic()

    with patch(
        "core.voice.tts_service.resolve_voice_config_sync",
        return_value=SimpleNamespace(cartesia_voice_id="voice"),
    ):
        await service.warmup()

    assert ws.send.await_args.kwargs["max_buffer_delay_ms"] == 0
