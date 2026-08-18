#!/usr/bin/env python
"""Run repeatable WebSocket latency probes against JARV1S."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import wave
from array import array
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import websockets  # type: ignore[import-not-found]

from tools.eval_stt import _create_run_dir, _percent, _percentile, _score_text


DEFAULT_URL = "ws://localhost:8000/api/v1/ws"


def _api_base_from_ws_url(ws_url: str) -> str:
    parts = urlsplit(ws_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def mint_ws_ticket(ws_url: str, device_token: str, *, timeout_s: float = 10.0) -> str:
    api_base = _api_base_from_ws_url(ws_url)
    payload = json.dumps({"device_token": device_token}).encode("utf-8")
    request = Request(
        f"{api_base}/api/v1/device-auth/ws-ticket",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_s) as response:
        body = json.loads(response.read().decode("utf-8"))
    ticket = str(body.get("ticket") or "").strip()
    if not ticket:
        raise RuntimeError("ws-ticket response missing ticket")
    return ticket


def resolve_ws_url(ws_url: str, device_token: str | None) -> str:
    token = device_token or os.environ.get("JARVIS_DEVICE_TOKEN")
    if not token:
        return ws_url
    ticket = mint_ws_ticket(ws_url, token)
    parts = urlsplit(ws_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=False))
    query["ticket"] = ticket
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = BACKEND_DIR / "logs/fixtures/stt"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1

VOICE_SUITES = {
    "voice-core": ("normal_request.wav", "planning_comments.wav", "natural_pause.wav", "technical_terms.wav"),
    "streaming-smoke": ("normal_request.wav", "planning_comments.wav"),
}


@dataclass
class ProbeResult:
    run: int
    mode: str
    ok: bool
    fixture: str | None = None
    suite: str | None = None
    error: str | None = None
    transcript: str | None = None
    reference: str | None = None
    reference_metrics: dict[str, Any] | None = None
    response_preview: str | None = None
    audio_ms: float | None = None
    speech_end_ms: float | None = None
    gap_ms: int | None = None
    chunk_ms: int | None = None
    realtime_pacing: bool = True
    first_partial_ms: float | None = None
    latest_partial_ms: float | None = None
    latest_partial_after_speech_ms: float | None = None
    first_transcript_ms: float | None = None
    last_transcript_ms: float | None = None
    first_transcript_after_speech_ms: float | None = None
    last_transcript_after_speech_ms: float | None = None
    commit_ms: float | None = None
    commit_after_speech_ms: float | None = None
    first_response_ms: float | None = None
    response_ms: float | None = None
    first_final_response_ms: float | None = None
    final_response_ms: float | None = None
    first_audio_ms: float | None = None
    latest_partial: str | None = None
    partials_count: int = 0
    partials: list[str] = field(default_factory=list)
    partial_events: list[dict[str, Any]] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    messages: dict[str, int] = field(default_factory=dict)
    turn_run: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProbeCase:
    fixture: str
    audio_path: Path | None = None
    second_audio_path: Path | None = None
    reference_path: Path | None = None
    text: str | None = None
    gap_sweep_ms: str | None = None


def now_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def encode_audio(audio: bytes) -> str:
    return base64.b64encode(audio).decode("utf-8")


def bytes_per_ms() -> int:
    return SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 1000


def iter_audio_chunks(audio: bytes, *, chunk_ms: int) -> Iterator[tuple[bytes, float]]:
    chunk_size = max(bytes_per_ms() * chunk_ms, SAMPLE_WIDTH)
    for offset in range(0, len(audio), chunk_size):
        chunk = audio[offset: offset + chunk_size]
        yield chunk, audio_duration_ms(chunk)


def load_audio(path: Path) -> bytes:
    if path.suffix.lower() != ".wav":
        return path.read_bytes()

    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path} must be {SAMPLE_RATE}Hz, got {wav.getframerate()}Hz")
        if wav.getnchannels() != CHANNELS:
            raise ValueError(f"{path} must be mono, got {wav.getnchannels()} channels")
        if wav.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError(f"{path} must be 16-bit PCM, got sample width {wav.getsampwidth()}")
        return wav.readframes(wav.getnframes())


def audio_duration_ms(audio: bytes) -> float:
    return round((len(audio) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)) * 1000, 1)


def estimate_speech_end_ms(audio: bytes, *, frame_ms: int = 20, amplitude_threshold: int = 500) -> float | None:
    frame_size = max(bytes_per_ms() * frame_ms, SAMPLE_WIDTH)
    last_speech_byte: int | None = None
    for offset in range(0, len(audio), frame_size):
        frame = audio[offset: offset + frame_size]
        usable = len(frame) - (len(frame) % SAMPLE_WIDTH)
        if usable <= 0:
            continue
        samples = array("h")
        samples.frombytes(frame[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        if samples and max(abs(sample) for sample in samples) >= amplitude_threshold:
            last_speech_byte = offset + usable
    if last_speech_byte is None:
        return None
    return audio_duration_ms(audio[:last_speech_byte])


def _combined_reference_path(audio_path: Path, second_audio_path: Path) -> Path | None:
    if audio_path.parent != second_audio_path.parent:
        return None
    suffix_pairs = (("_part1", "_part2"), ("-part1", "-part2"))
    for first_suffix, second_suffix in suffix_pairs:
        if not audio_path.stem.endswith(first_suffix):
            continue
        base = audio_path.stem[: -len(first_suffix)]
        if second_audio_path.stem != f"{base}{second_suffix}":
            continue
        candidate = audio_path.with_name(f"{base}.txt")
        return candidate if candidate.is_file() else None
    return None


def load_reference(
    path: Path | None,
    audio_path: Path | None = None,
    second_audio_path: Path | None = None,
) -> str | None:
    reference_path = path
    if reference_path is None and audio_path is not None and second_audio_path is not None:
        reference_path = _combined_reference_path(audio_path, second_audio_path)
        if reference_path is None:
            first = audio_path.with_suffix(".txt")
            second = second_audio_path.with_suffix(".txt")
            if first.is_file() and second.is_file():
                return " ".join(
                    part
                    for part in (
                        first.read_text(encoding="utf-8").strip(),
                        second.read_text(encoding="utf-8").strip(),
                    )
                    if part
                )
    if reference_path is None and audio_path is not None:
        candidate = audio_path.with_suffix(".txt")
        reference_path = candidate if candidate.is_file() else None
    if reference_path is None:
        return None
    return reference_path.read_text(encoding="utf-8").strip()


def apply_reference_metrics(result: ProbeResult, reference: str | None) -> None:
    if not reference:
        return
    result.reference = reference
    metrics = _score_text(reference, result.transcript or "", audio_ms=result.audio_ms or 0)
    result.reference_metrics = asdict(metrics) | {"flagged": metrics.flagged}


def finalize_result_contract(result: ProbeResult) -> None:
    result.commit_ms = result.first_transcript_ms
    result.commit_after_speech_ms = result.first_transcript_after_speech_ms
    result.response_ms = result.first_response_ms
    result.final_response_ms = result.first_final_response_ms
    result.partials_count = len(result.partials)
    result.latest_partial = result.partials[-1] if result.partials else None
    if result.partial_events:
        result.latest_partial_ms = result.partial_events[-1]["ms"]


def apply_speech_relative_metrics(result: ProbeResult) -> None:
    if result.speech_end_ms is None:
        return
    if result.latest_partial_ms is None and result.partial_events:
        result.latest_partial_ms = result.partial_events[-1]["ms"]
    if result.latest_partial_ms is not None:
        result.latest_partial_after_speech_ms = round(result.latest_partial_ms - result.speech_end_ms, 1)
    if result.first_transcript_ms is not None:
        result.first_transcript_after_speech_ms = round(result.first_transcript_ms - result.speech_end_ms, 1)
    if result.last_transcript_ms is not None:
        result.last_transcript_after_speech_ms = round(result.last_transcript_ms - result.speech_end_ms, 1)


def extract_turn_run_summary(document: dict[str, Any]) -> dict[str, Any]:
    stages = {
        stage.get("key"): stage
        for stage in document.get("stages", [])
        if isinstance(stage, dict) and stage.get("key")
    }
    return {
        "status": document.get("status"),
        "turn_id": document.get("turn_id"),
        "stages": stages,
        "stt": document.get("stt") or {},
        "turn_detection": document.get("turn_detection") or {},
        "voice": document.get("voice") or {},
    }


def load_turn_run_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not payload:
            return None
        payload = payload[0]
    if isinstance(payload, dict) and "turn_runs" in payload and isinstance(payload["turn_runs"], list):
        if not payload["turn_runs"]:
            return None
        payload = payload["turn_runs"][0]
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a turn_runs document or list")
    return extract_turn_run_summary(payload)


def resolve_suite_cases(suite: str, fixtures_dir: Path) -> list[ProbeCase]:
    if suite in VOICE_SUITES:
        return [
            ProbeCase(fixture=Path(name).stem, audio_path=fixtures_dir / name)
            for name in VOICE_SUITES[suite]
        ]
    if suite == "short-commands":
        return [
            ProbeCase(fixture=path.stem, audio_path=path)
            for path in sorted(fixtures_dir.glob("short_*.wav"))
        ]
    if suite == "fast-recovery":
        return [
            ProbeCase(
                fixture="recovery",
                audio_path=fixtures_dir / "recovery_part1.wav",
                second_audio_path=fixtures_dir / "recovery_part2.wav",
                gap_sweep_ms="200,350,500,650,800,1200",
            )
        ]
    raise ValueError(f"Unknown suite: {suite}")


def resolve_cases(args: argparse.Namespace) -> list[ProbeCase]:
    if args.suite:
        return resolve_suite_cases(args.suite, args.fixtures)
    if args.text:
        return [ProbeCase(fixture="text", text=args.text)]
    if args.audio:
        return [
            ProbeCase(
                fixture=args.audio.stem,
                audio_path=args.audio,
                second_audio_path=args.then_audio,
                reference_path=args.reference,
                gap_sweep_ms=args.gap_sweep_ms,
            )
        ]
    raise ValueError("--suite, --text, or --audio is required")


async def send_json(ws: Any, message_type: str, data: dict[str, Any], message_id: str) -> None:
    await ws.send(json.dumps({
        "id": message_id,
        "type": message_type,
        "data": data,
    }))


async def send_audio_chunks(
    ws: Any,
    audio: bytes,
    *,
    chunk_ms: int,
    run: int,
    prefix: str,
    realtime: bool,
) -> None:
    for index, (chunk, chunk_duration_ms) in enumerate(iter_audio_chunks(audio, chunk_ms=chunk_ms)):
        await send_json(
            ws,
            "user_audio",
            {"audio": encode_audio(chunk), "encoding": "base64"},
            f"probe-{run}-{prefix}-{index}",
        )
        if realtime:
            await asyncio.sleep(chunk_duration_ms / 1000)


async def send_silence(
    ws: Any,
    *,
    duration_ms: int,
    chunk_ms: int,
    run: int,
    prefix: str,
    realtime: bool,
) -> None:
    if duration_ms <= 0:
        return
    await send_audio_chunks(
        ws,
        bytes(bytes_per_ms() * duration_ms),
        chunk_ms=chunk_ms,
        run=run,
        prefix=prefix,
        realtime=realtime,
    )


async def send_audio(
    ws: Any,
    audio: bytes,
    *,
    chunk_ms: int,
    silence_ms: int,
    run: int,
    realtime: bool,
) -> None:
    await send_audio_chunks(ws, audio, chunk_ms=chunk_ms, run=run, prefix="audio", realtime=realtime)
    await send_silence(ws, duration_ms=silence_ms, chunk_ms=chunk_ms, run=run, prefix="silence", realtime=realtime)


async def send_audio_sequence(
    ws: Any,
    first_audio: bytes,
    second_audio: bytes,
    *,
    chunk_ms: int,
    gap_ms: int,
    silence_ms: int,
    run: int,
    realtime: bool,
) -> None:
    await send_audio_chunks(ws, first_audio, chunk_ms=chunk_ms, run=run, prefix="part1", realtime=realtime)
    await send_silence(ws, duration_ms=gap_ms, chunk_ms=chunk_ms, run=run, prefix="gap", realtime=realtime)
    await send_audio_chunks(ws, second_audio, chunk_ms=chunk_ms, run=run, prefix="part2", realtime=realtime)
    await send_silence(ws, duration_ms=silence_ms, chunk_ms=chunk_ms, run=run, prefix="silence", realtime=realtime)


async def activate_voice(ws: Any, *, run: int, timeout_s: float = 5.0) -> None:
    await send_json(ws, "voice.activate", {}, f"probe-{run}-voice-activate")
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.perf_counter()))
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"voice.activate timed out after {timeout_s:.1f}s") from exc

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if message.get("type") == "system.error":
            raise RuntimeError(message.get("error") or str(message.get("data") or "system.error"))

        data = message.get("data") or {}
        if message.get("type") == "status.update" and data.get("stage") == "listening":
            return

    raise TimeoutError(f"voice.activate timed out after {timeout_s:.1f}s")


async def collect_result(
    ws: Any,
    *,
    start: float,
    run: int,
    mode: str,
    timeout_s: float,
    audio_settle_s: float,
) -> ProbeResult:
    result = ProbeResult(run=run, mode=mode, ok=False)
    deadline = start + timeout_s
    last_message_at = time.perf_counter()
    playback_end_sent = False
    playback_end_at: float | None = None

    while time.perf_counter() < deadline:
        remaining = max(0.1, deadline - time.perf_counter())
        if (
            mode == "audio"
            and result.first_audio_ms is not None
            and result.first_final_response_ms is not None
            and not playback_end_sent
        ):
            remaining = min(remaining, max(0.1, audio_settle_s))
        elif playback_end_sent:
            remaining = min(remaining, 0.5)

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            if (
                mode == "audio"
                and result.first_audio_ms is not None
                and result.first_final_response_ms is not None
                and not playback_end_sent
                and (time.perf_counter() - last_message_at) >= audio_settle_s
            ):
                await send_json(ws, "audio.playback_end", {}, f"probe-{run}-playback-end")
                playback_end_sent = True
                playback_end_at = time.perf_counter()
                continue
            if playback_end_sent:
                result.ok = True
                return result
            result.error = f"timed out after {timeout_s:.1f}s"
            return result

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = message.get("type", "")
        result.messages[msg_type] = result.messages.get(msg_type, 0) + 1
        last_message_at = time.perf_counter()

        if msg_type == "system.error":
            result.error = message.get("error") or str(message.get("data") or "system.error")
            return result

        data = message.get("data") or {}
        if msg_type == "conversation.partial":
            text = str(data.get("text") or "")
            if text:
                partial_ms = now_ms(start)
                if result.first_partial_ms is None:
                    result.first_partial_ms = partial_ms
                result.latest_partial_ms = partial_ms
                result.partials.append(text)
                result.partial_events.append({"ms": partial_ms, "text": text})

        elif msg_type == "conversation.transcript":
            text = str(data.get("text") or "")
            if result.first_transcript_ms is None:
                result.first_transcript_ms = now_ms(start)
            result.last_transcript_ms = now_ms(start)
            result.transcript = text
            result.transcripts.append(text)

        elif msg_type == "conversation.response":
            text = str(data.get("text") or "")
            if text and result.first_response_ms is None:
                result.first_response_ms = now_ms(start)
                result.response_preview = text[:180]
            if data.get("is_partial") is False and result.first_final_response_ms is None:
                result.first_final_response_ms = now_ms(start)

        elif msg_type == "jarvis_audio" and result.first_audio_ms is None:
            result.first_audio_ms = now_ms(start)

        elif (
            playback_end_sent
            and msg_type == "status.update"
            and data.get("stage") in {"idle", "listening"}
        ):
            result.ok = True
            return result
        elif mode == "audio" and msg_type == "status.update" and data.get("stage") in {"idle", "listening"}:
            if result.first_transcript_ms is not None:
                result.ok = True
                return result

        if mode == "text" and result.first_response_ms is not None and result.first_final_response_ms is not None:
            result.ok = True
            return result

        if playback_end_sent and playback_end_at is not None and (time.perf_counter() - playback_end_at) >= 0.5:
            result.ok = True
            return result

    result.error = f"timed out after {timeout_s:.1f}s"
    return result


async def run_once(
    args: argparse.Namespace,
    run: int,
    audio: bytes | None,
    *,
    second_audio: bytes | None,
    gap_ms: int | None,
    reference: str | None,
    turn_run_summary: dict[str, Any] | None,
    fixture: str | None = None,
    suite: str | None = None,
    text: str | None = None,
) -> ProbeResult:
    connect_url = resolve_ws_url(args.url, args.device_token)
    async with websockets.connect(connect_url) as ws:
        start = time.perf_counter()
        if text:
            await send_json(ws, "user_text", {"text": text}, f"probe-{run}-text")
            result = await collect_result(
                ws,
                start=start,
                run=run,
                mode="text",
                timeout_s=args.timeout,
                audio_settle_s=args.audio_settle,
            )
            result.turn_run = turn_run_summary
            result.fixture = fixture
            result.suite = suite
            finalize_result_contract(result)
            return result

        if audio is None:
            raise ValueError("--audio is required when --text is not provided")
        if args.activate_audio:
            await activate_voice(ws, run=run)
        start = time.perf_counter()
        if second_audio is None:
            send_task = asyncio.create_task(
                send_audio(
                    ws,
                    audio,
                    chunk_ms=args.chunk_ms,
                    silence_ms=args.silence_ms,
                    run=run,
                    realtime=not args.fast,
                )
            )
            audio_ms = audio_duration_ms(audio)
            speech_end_ms = estimate_speech_end_ms(audio)
        else:
            selected_gap_ms = gap_ms if gap_ms is not None else args.gap_ms
            send_task = asyncio.create_task(
                send_audio_sequence(
                    ws,
                    audio,
                    second_audio,
                    chunk_ms=args.chunk_ms,
                    gap_ms=selected_gap_ms,
                    silence_ms=args.silence_ms,
                    run=run,
                    realtime=not args.fast,
                )
            )
            first_audio_ms = audio_duration_ms(audio)
            second_speech_end_ms = estimate_speech_end_ms(second_audio)
            first_speech_end_ms = estimate_speech_end_ms(audio)
            audio_ms = first_audio_ms + audio_duration_ms(second_audio)
            speech_end_ms = (
                first_audio_ms + selected_gap_ms + second_speech_end_ms
                if second_speech_end_ms is not None
                else first_speech_end_ms
            )
        try:
            result = await collect_result(
                ws,
                start=start,
                run=run,
                mode="audio",
                timeout_s=args.timeout,
                audio_settle_s=args.audio_settle,
            )
            result.audio_ms = audio_ms
            result.speech_end_ms = round(speech_end_ms, 1) if speech_end_ms is not None else None
            result.gap_ms = selected_gap_ms if second_audio is not None else None
            result.chunk_ms = args.chunk_ms
            result.realtime_pacing = not args.fast
            result.turn_run = turn_run_summary
            result.fixture = fixture
            result.suite = suite
            apply_speech_relative_metrics(result)
            apply_reference_metrics(result, reference)
            finalize_result_contract(result)
            return result
        finally:
            if not send_task.done():
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass


def print_result(result: ProbeResult) -> None:
    status = "ok" if result.ok else "fail"
    parts = [
        f"run={result.run}",
        f"fixture={result.fixture}" if result.fixture else "",
        f"status={status}",
        f"gap={result.gap_ms}" if result.gap_ms is not None else "",
        f"commit={result.commit_ms}",
        f"commit_after_speech={result.commit_after_speech_ms}"
        if result.commit_after_speech_ms is not None
        else "",
        f"partial={result.first_partial_ms}",
        f"latest_partial_after_speech={result.latest_partial_after_speech_ms}"
        if result.latest_partial_after_speech_ms is not None
        else "",
        f"response={result.response_ms}",
        f"final={result.final_response_ms}",
        f"audio={result.first_audio_ms}",
    ]
    parts = [part for part in parts if part]
    if result.reference_metrics and result.reference_metrics.get("flagged"):
        parts.append("ref=flagged")
    if result.error:
        parts.append(f"error={result.error}")
    print(" ".join(parts))


def _compact_probe_result(result: ProbeResult, *, include_evidence: bool = False) -> dict[str, Any]:
    payload = {
        "run": result.run,
        "suite": result.suite,
        "fixture": result.fixture,
        "mode": result.mode,
        "ok": result.ok,
        "error": result.error,
        "transcript": result.transcript,
        "reference": result.reference,
        "reference_metrics": result.reference_metrics,
        "response_preview": result.response_preview,
        "audio_ms": result.audio_ms,
        "speech_end_ms": result.speech_end_ms,
        "gap_ms": result.gap_ms,
        "chunk_ms": result.chunk_ms,
        "realtime_pacing": result.realtime_pacing,
        "first_partial_ms": result.first_partial_ms,
        "latest_partial_ms": result.latest_partial_ms,
        "latest_partial_after_speech_ms": result.latest_partial_after_speech_ms,
        "commit_ms": result.commit_ms,
        "commit_after_speech_ms": result.commit_after_speech_ms,
        "response_ms": result.response_ms,
        "final_response_ms": result.final_response_ms,
        "first_audio_ms": result.first_audio_ms,
        "partials_count": result.partials_count,
        "latest_partial": result.latest_partial,
        "messages": result.messages,
        "turn_run": result.turn_run,
    }
    if include_evidence:
        payload["partials"] = result.partials
        payload["partial_events"] = result.partial_events
        payload["transcripts"] = result.transcripts
    elif not result.ok or (result.reference_metrics and result.reference_metrics.get("flagged")):
        payload["last_partials"] = result.partials[-3:]
        payload["transcripts"] = result.transcripts
    return payload


def summarize_results(results: list[ProbeResult]) -> dict[str, Any]:
    total = len(results)
    ok = sum(1 for result in results if result.ok)
    timeouts = sum(1 for result in results if result.error and "timed out" in result.error)
    committed = sum(1 for result in results if result.commit_ms is not None)
    flagged = sum(1 for result in results if result.reference_metrics and result.reference_metrics.get("flagged"))
    full_matches = sum(
        1
        for result in results
        if result.reference_metrics
        and result.reference_metrics.get("wer") == 0
        and result.reference_metrics.get("length_ratio") == 1
    )
    by_fixture: dict[str, dict[str, Any]] = {}
    for fixture in sorted({result.fixture or "single" for result in results}):
        subset = [result for result in results if (result.fixture or "single") == fixture]
        by_fixture[fixture] = summarize_results_flat(subset)
    summary = summarize_results_flat(results)
    summary.update(
        {
            "runs": total,
            "ok_runs": ok,
            "timeout_runs": timeouts,
            "committed_runs": committed,
            "flagged_runs": flagged,
            "success_rate": _percent(ok, total),
            "timeout_rate": _percent(timeouts, total),
            "commit_rate": _percent(committed, total),
            "flagged_rate": _percent(flagged, total),
            "full_reference_match_rate": _percent(full_matches, total),
            "tail_missing_runs": sum(
                1
                for result in results
                if result.reference_metrics and result.reference_metrics.get("tail_missing")
            ),
            "prefix_only_runs": sum(
                1
                for result in results
                if result.reference_metrics and result.reference_metrics.get("prefix_only")
            ),
            "by_fixture": by_fixture,
        }
    )
    return summary


def summarize_results_flat(results: list[ProbeResult]) -> dict[str, Any]:
    return {
        "first_partial_ms_p50": _percentile([r.first_partial_ms for r in results if r.first_partial_ms is not None], 0.5),
        "latest_partial_after_speech_ms_p50": _percentile(
            [r.latest_partial_after_speech_ms for r in results if r.latest_partial_after_speech_ms is not None],
            0.5,
        ),
        "latest_partial_after_speech_ms_p90": _percentile(
            [r.latest_partial_after_speech_ms for r in results if r.latest_partial_after_speech_ms is not None],
            0.9,
        ),
        "commit_after_speech_ms_p50": _percentile(
            [r.commit_after_speech_ms for r in results if r.commit_after_speech_ms is not None],
            0.5,
        ),
        "commit_after_speech_ms_p90": _percentile(
            [r.commit_after_speech_ms for r in results if r.commit_after_speech_ms is not None],
            0.9,
        ),
        "first_response_ms_p50": _percentile([r.response_ms for r in results if r.response_ms is not None], 0.5),
        "first_response_ms_p90": _percentile([r.response_ms for r in results if r.response_ms is not None], 0.9),
        "first_audio_ms_p50": _percentile([r.first_audio_ms for r in results if r.first_audio_ms is not None], 0.5),
        "latest_partial_text": next((r.latest_partial for r in reversed(results) if not r.ok and r.latest_partial), None),
    }


def summarize(results: list[ProbeResult]) -> None:
    summary = summarize_results(results)
    print()
    print("Summary")
    print("-------")
    print(
        f"runs={summary['runs']} ok={summary['ok_runs']} "
        f"committed={summary['committed_runs']} timeouts={summary['timeout_runs']} "
        f"flagged={summary['flagged_runs']}"
    )
    print(
        f"success={summary['success_rate']:.0%} commit={summary['commit_rate']:.0%} "
        f"flagged={summary['flagged_rate']:.0%} full_match={summary['full_reference_match_rate']:.0%}"
    )
    for label, key in (
        ("first_partial", "first_partial_ms_p50"),
        ("latest_partial_after_speech_p50", "latest_partial_after_speech_ms_p50"),
        ("latest_partial_after_speech_p90", "latest_partial_after_speech_ms_p90"),
        ("commit_after_speech_p50", "commit_after_speech_ms_p50"),
        ("commit_after_speech_p90", "commit_after_speech_ms_p90"),
        ("first_response_p50", "first_response_ms_p50"),
        ("first_response_p90", "first_response_ms_p90"),
        ("first_audio", "first_audio_ms_p50"),
    ):
        value = summary.get(key)
        if value is not None:
            print(f"{label}={value:.1f}ms")


def write_markdown_summary(path: Path, *, title: str, summary: dict[str, Any], results: list[ProbeResult]) -> None:
    lines = [
        f"# {title}",
        "",
        f"- Runs: {summary['runs']}",
        f"- Success rate: {summary['success_rate']:.0%}",
        f"- Commit rate: {summary['commit_rate']:.0%}",
        f"- Timeout rate: {summary['timeout_rate']:.0%}",
        f"- Flagged rate: {summary['flagged_rate']:.0%}",
        f"- Full reference match rate: {summary['full_reference_match_rate']:.0%}",
        f"- First partial p50: {summary['first_partial_ms_p50']}ms",
        f"- Latest partial after speech p50/p90: {summary['latest_partial_after_speech_ms_p50']}ms / {summary['latest_partial_after_speech_ms_p90']}ms",
        f"- Commit after speech p50/p90: {summary['commit_after_speech_ms_p50']}ms / {summary['commit_after_speech_ms_p90']}ms",
        f"- First response p50/p90: {summary['first_response_ms_p50']}ms / {summary['first_response_ms_p90']}ms",
        "",
        "## Fixtures",
        "",
    ]
    for fixture, item in summary.get("by_fixture", {}).items():
        subset = [result for result in results if (result.fixture or "single") == fixture]
        ok = sum(1 for result in subset if result.ok)
        committed = sum(1 for result in subset if result.commit_ms is not None)
        lines.append(
            f"- `{fixture}`: ok={ok}/{len(subset)} committed={committed}/{len(subset)} "
            f"latest_partial_after_speech_p50={item.get('latest_partial_after_speech_ms_p50')}ms "
            f"commit_after_speech_p50={item.get('commit_after_speech_ms_p50')}ms"
        )
    flagged = [
        result
        for result in results
        if not result.ok or (result.reference_metrics and result.reference_metrics.get("flagged"))
    ]
    if flagged:
        lines.extend(["", "## Flagged Or Failed", ""])
        for result in flagged:
            reason = result.error or "reference mismatch"
            if result.latest_partial and not result.transcript:
                reason = f"{reason}; latest_partial={result.latest_partial!r}"
            lines.append(f"- `{result.fixture or result.run}` run {result.run}: {reason}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_artifacts(
    run_dir: Path,
    *,
    args: argparse.Namespace,
    results: list[ProbeResult],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_results(results)
    manifest = {
        "tool": "latency_probe",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "suite": args.suite,
        "label": args.label,
        "url": args.url,
        "fixtures": str(args.fixtures),
        "runs": args.runs,
        "gap_runs": args.gap_runs,
        "chunk_ms": args.chunk_ms,
        "silence_ms": args.silence_ms,
        "activate_audio": args.activate_audio,
        "voice_env": {key: value for key, value in sorted(os.environ.items()) if key.startswith("VOICE__")},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(_compact_probe_result(result, include_evidence=args.include_evidence)) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_markdown_summary(run_dir / "summary.md", title=f"Voice Latency Eval: {args.label or args.suite}", summary=summary, results=results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe JARV1S WebSocket turn latency")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--text", help="text turn to send")
    mode.add_argument("--audio", type=Path, help="16kHz mono 16-bit PCM WAV or raw PCM")
    mode.add_argument(
        "--suite",
        choices=("voice-core", "short-commands", "fast-recovery", "streaming-smoke"),
        help="named fixture suite",
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="directory for named suites")
    parser.add_argument("--then-audio", type=Path, default=None, help="second audio file for fast-recovery replay")
    parser.add_argument("--gap-ms", type=int, default=450, help="silence gap before --then-audio")
    parser.add_argument("--gap-sweep-ms", default=None, help="comma-separated gaps for fast-recovery sweeps")
    parser.add_argument("--gap-runs", type=int, default=3, help="runs per gap when --gap-sweep-ms is set")
    parser.add_argument("--url", default=DEFAULT_URL, help="WebSocket URL")
    parser.add_argument(
        "--device-token",
        default=None,
        help="durable device token for ws-ticket exchange (or set JARVIS_DEVICE_TOKEN)",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per run")
    parser.add_argument("--chunk-ms", type=int, default=96, help="audio chunk size; 96ms matches frontend worklet cadence")
    parser.add_argument("--silence-ms", type=int, default=1200, help="silence after audio")
    parser.add_argument("--audio-settle", type=float, default=0.75, help="quiet period before playback_end")
    parser.add_argument("--activate-audio", action="store_true", help="open active listening before audio")
    parser.add_argument("--fast", action="store_true", help="send audio without realtime sleeps; invalid for timing-sensitive evals")
    parser.add_argument("--reference", type=Path, default=None, help="reference transcript; defaults to audio .txt when present")
    parser.add_argument("--turn-run-json", type=Path, default=None, help="optional dumped turn_runs document to attach/extract")
    parser.add_argument("--out", type=Path, default=None, help="write JSONL results")
    parser.add_argument("--label", default=None, help="write a timestamped run under logs/evals with this label")
    parser.add_argument("--include-evidence", action="store_true", help="include full partial/transcript arrays in eval run results")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    turn_run_summary = load_turn_run_summary(args.turn_run_json)
    results: list[ProbeResult] = []

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)

    cases = resolve_cases(args)
    if args.gap_sweep_ms and not (args.audio or args.suite == "fast-recovery"):
        raise ValueError("--gap-sweep-ms can only be used with --audio or --suite fast-recovery")
    if args.gap_sweep_ms and args.audio and not args.then_audio:
        raise ValueError("--gap-sweep-ms requires --then-audio")
    if args.then_audio and args.text:
        raise ValueError("--then-audio can only be used with --audio")
    if args.fast and args.audio:
        raise ValueError("--fast is disabled for audio probes because it invalidates endpointing and fast-recovery timing")
    if args.fast and args.suite:
        raise ValueError("--fast is disabled for audio suites because it invalidates endpointing and fast-recovery timing")

    for case in cases:
        audio = load_audio(case.audio_path) if case.audio_path else None
        second_audio = load_audio(case.second_audio_path) if case.second_audio_path else None
        reference = load_reference(case.reference_path, case.audio_path, case.second_audio_path)
        selected_gap_sweep = args.gap_sweep_ms or case.gap_sweep_ms
        if selected_gap_sweep and second_audio is None:
            raise ValueError(f"{case.fixture}: gap sweep requires second audio")

        run_specs: list[int | None]
        if selected_gap_sweep:
            gaps = [int(raw.strip()) for raw in selected_gap_sweep.split(",") if raw.strip()]
            run_specs = [gap for gap in gaps for _ in range(args.gap_runs)]
        else:
            run_specs = [args.gap_ms if second_audio is not None else None for _ in range(args.runs)]

        for run, gap_ms in enumerate(run_specs, start=1):
            try:
                result = await run_once(
                    args,
                    run,
                    audio,
                    second_audio=second_audio,
                    gap_ms=gap_ms,
                    reference=reference,
                    turn_run_summary=turn_run_summary,
                    fixture=case.fixture,
                    suite=args.suite,
                    text=case.text,
                )
            except Exception as exc:
                result = ProbeResult(
                    run=run,
                    mode="text" if case.text else "audio",
                    ok=False,
                    fixture=case.fixture,
                    suite=args.suite,
                    error=str(exc),
                )
                finalize_result_contract(result)
            results.append(result)
            print_result(result)
            if args.out:
                with args.out.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(asdict(result)) + "\n")

    summarize(results)
    if args.label or args.suite:
        label = args.label or args.suite or "latency"
        run_dir = _create_run_dir(label)
        write_run_artifacts(run_dir, args=args, results=results)
        print(f"wrote_eval={run_dir}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
