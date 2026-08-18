"""Offline STT eval — replay verified clips through raw STT backends.

Run from backend/:
    uv run python tools/eval_stt.py --fixtures logs/fixtures/stt
    uv run python tools/eval_stt.py --backend mlx --model mlx-community/whisper-small.en-mlx-4bit
    uv run python tools/eval_stt.py --backend cartesia
    uv run python tools/eval_stt.py --failures
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import settings
from core.voice.stt_service import CartesiaSTTService, MLXSTTService, STTBackend

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
DEFAULT_FIXTURES = BACKEND_DIR / "logs/fixtures/stt"
DEFAULT_EVALS_DIR = BACKEND_DIR / "logs/evals"
BackendName = Literal["mlx", "cartesia"]


@dataclass(frozen=True)
class Fixture:
    audio_path: Path
    reference_path: Path | None
    reference_text: str
    audio_bytes: bytes
    audio_ms: float


@dataclass
class TextMetrics:
    reference_words: int
    hypothesis_words: int
    wer: float | None
    length_ratio: float | None
    repeated_sequence: str | None
    repeated_count: int
    empty_on_speech: bool
    large_deletion: bool
    tail_missing: bool
    prefix_only: bool

    @property
    def flagged(self) -> bool:
        return bool(
            self.empty_on_speech
            or self.large_deletion
            or self.tail_missing
            or self.prefix_only
            or (self.repeated_sequence and self.repeated_count >= 3)
        )


@dataclass
class EvalResult:
    fixture: str
    backend: BackendName
    model: str | None
    ok: bool
    audio_ms: float
    transcribe_ms: float | None = None
    rtf: float | None = None
    transcript: str = ""
    reference: str = ""
    metrics: TextMetrics | None = None
    error: str | None = None

    @property
    def flagged(self) -> bool:
        return bool(self.metrics and self.metrics.flagged)


def _load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != CHANNELS or wf.getsampwidth() != SAMPLE_WIDTH or wf.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"{path.name}: expected 16 kHz mono 16-bit PCM, got "
                f"{wf.getframerate()} Hz {wf.getnchannels()}ch {wf.getsampwidth() * 8}-bit"
            )
        return wf.readframes(wf.getnframes())


def _audio_duration_ms(audio_bytes: bytes) -> float:
    return round((len(audio_bytes) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)) * 1000, 1)


def _collect_fixtures(directory: Path) -> list[Fixture]:
    if not directory.is_dir():
        return []

    fixtures: list[Fixture] = []
    for audio_path in sorted(directory.glob("*.wav")):
        reference_path = audio_path.with_suffix(".txt")
        reference_text = reference_path.read_text(encoding="utf-8").strip() if reference_path.is_file() else ""
        audio_bytes = _load_wav_pcm(audio_path)
        fixtures.append(
            Fixture(
                audio_path=audio_path,
                reference_path=reference_path if reference_path.is_file() else None,
                reference_text=reference_text,
                audio_bytes=audio_bytes,
                audio_ms=_audio_duration_ms(audio_bytes),
            )
        )
    return fixtures


def _normalize_text(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9']+", " ", text.lower())
    return [word.strip("'") for word in cleaned.split() if word.strip("'")]


def _word_error_rate(reference: list[str], hypothesis: list[str]) -> float | None:
    if not reference:
        return None
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return round(previous[-1] / len(reference), 4)


def _repeated_sequence(words: list[str], *, max_ngram: int = 3, min_repeats: int = 3) -> tuple[str | None, int]:
    best_sequence: tuple[str, ...] | None = None
    best_count = 0
    for ngram_size in range(1, max_ngram + 1):
        previous: tuple[str, ...] | None = None
        count = 0
        for index in range(0, len(words) - ngram_size + 1, ngram_size):
            sequence = tuple(words[index : index + ngram_size])
            if sequence == previous:
                count += 1
            else:
                previous = sequence
                count = 1
            if count >= min_repeats and count > best_count:
                best_sequence = sequence
                best_count = count
    if not best_sequence:
        return None, 0
    return " ".join(best_sequence), best_count


def _tail_missing(reference_words: list[str], hypothesis_words: list[str], *, window: int = 5) -> bool:
    if not reference_words or not hypothesis_words:
        return False
    return reference_words[-1] not in hypothesis_words[-window:]


def _prefix_only(reference_words: list[str], hypothesis_words: list[str]) -> bool:
    return bool(
        reference_words
        and hypothesis_words
        and len(hypothesis_words) < len(reference_words)
        and reference_words[: len(hypothesis_words)] == hypothesis_words
    )


def _score_text(reference_text: str, transcript: str, *, audio_ms: float) -> TextMetrics:
    reference_words = _normalize_text(reference_text)
    hypothesis_words = _normalize_text(transcript)
    repeated, repeated_count = _repeated_sequence(hypothesis_words)
    length_ratio = (
        round(len(hypothesis_words) / len(reference_words), 4)
        if reference_words
        else None
    )
    wer = _word_error_rate(reference_words, hypothesis_words)
    return TextMetrics(
        reference_words=len(reference_words),
        hypothesis_words=len(hypothesis_words),
        wer=wer,
        length_ratio=length_ratio,
        repeated_sequence=repeated,
        repeated_count=repeated_count,
        empty_on_speech=bool(reference_words) and audio_ms >= 500 and not hypothesis_words,
        large_deletion=bool(reference_words and len(hypothesis_words) < len(reference_words) * 0.7),
        tail_missing=_tail_missing(reference_words, hypothesis_words),
        prefix_only=_prefix_only(reference_words, hypothesis_words),
    )


def _backend_names(selected: str) -> list[BackendName]:
    if selected == "both":
        return ["mlx", "cartesia"]
    return [selected]  # type: ignore[list-item]


async def _make_backend(name: BackendName, *, model: str | None) -> tuple[STTBackend, str | None]:
    if name == "mlx":
        selected_model = model or settings.VOICE.stt_model
        backend = MLXSTTService(model_size=selected_model)
        await backend.initialize()
        return backend, selected_model

    from core.credentials.store import credential_store

    if not credential_store.get_stored_secret("CARTESIA_API_KEY"):
        raise RuntimeError("CARTESIA_API_KEY must be stored in CredentialStore for --backend cartesia")
    backend = CartesiaSTTService()
    await backend.initialize()
    return backend, "ink-whisper"


async def _eval_fixture(
    fixture: Fixture,
    *,
    backend_name: BackendName,
    backend: STTBackend,
    model: str | None,
) -> EvalResult:
    started = time.perf_counter()
    try:
        transcript = await backend.transcribe_batched(fixture.audio_bytes)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        audio_seconds = fixture.audio_ms / 1000
        return EvalResult(
            fixture=fixture.audio_path.name,
            backend=backend_name,
            model=model,
            ok=True,
            audio_ms=fixture.audio_ms,
            transcribe_ms=elapsed_ms,
            rtf=round((elapsed_ms / 1000) / audio_seconds, 4) if audio_seconds else None,
            transcript=transcript,
            reference=fixture.reference_text,
            metrics=_score_text(fixture.reference_text, transcript, audio_ms=fixture.audio_ms),
        )
    except Exception as exc:
        return EvalResult(
            fixture=fixture.audio_path.name,
            backend=backend_name,
            model=model,
            ok=False,
            audio_ms=fixture.audio_ms,
            error=str(exc),
        )


def _print_result(result: EvalResult) -> None:
    status = "FAIL" if result.flagged or not result.ok else "ok"
    parts = [
        f"{status}",
        result.backend,
        result.fixture,
        f"audio={result.audio_ms:.0f}ms",
    ]
    if result.transcribe_ms is not None:
        parts.append(f"stt={result.transcribe_ms:.0f}ms")
    if result.rtf is not None:
        parts.append(f"rtf={result.rtf:.2f}")
    if result.metrics:
        if result.metrics.length_ratio is not None:
            parts.append(f"len={result.metrics.length_ratio:.2f}")
        if result.metrics.wer is not None:
            parts.append(f"wer={result.metrics.wer:.2f}")
        if result.metrics.repeated_sequence:
            parts.append(f"repeat={result.metrics.repeated_sequence!r}x{result.metrics.repeated_count}")
        if result.metrics.empty_on_speech:
            parts.append("empty")
        if result.metrics.large_deletion:
            parts.append("large_deletion")
        if result.metrics.tail_missing:
            parts.append("tail_missing")
        if result.metrics.prefix_only:
            parts.append("prefix_only")
    if result.error:
        parts.append(f"error={result.error}")
    print(" ".join(parts))


def _compact_result(result: EvalResult) -> dict:
    payload = asdict(result)
    payload["flagged"] = result.flagged
    return payload


def _write_jsonl(path: Path, results: list[EvalResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(_compact_result(result)) + "\n")


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label.strip()).strip("-")
    return cleaned or "eval"


def _create_run_dir(label: str, *, root: Path = DEFAULT_EVALS_DIR) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / f"{timestamp}_{_safe_label(label)}"


def _percent(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return round(ordered[index], 1)


def _summarize_results(results: list[EvalResult]) -> dict:
    total = len(results)
    ok = sum(1 for result in results if result.ok)
    flagged = sum(1 for result in results if result.flagged or not result.ok)
    transcribe_ms = [result.transcribe_ms for result in results if result.transcribe_ms is not None]
    rtf = [result.rtf for result in results if result.rtf is not None]
    exact = sum(
        1
        for result in results
        if result.metrics
        and result.metrics.wer == 0
        and result.metrics.length_ratio == 1
    )
    return {
        "runs": total,
        "ok_runs": ok,
        "failed_runs": total - ok,
        "flagged_runs": flagged,
        "success_rate": _percent(ok, total),
        "flagged_rate": _percent(flagged, total),
        "full_reference_match_rate": _percent(exact, total),
        "transcribe_ms_p50": _percentile(transcribe_ms, 0.5),
        "transcribe_ms_p90": _percentile(transcribe_ms, 0.9),
        "rtf_p50": _percentile(rtf, 0.5),
        "rtf_p90": _percentile(rtf, 0.9),
        "tail_missing_runs": sum(1 for result in results if result.metrics and result.metrics.tail_missing),
        "prefix_only_runs": sum(1 for result in results if result.metrics and result.metrics.prefix_only),
    }


def _write_markdown_summary(path: Path, *, title: str, summary: dict, results: list[EvalResult]) -> None:
    flagged = [result for result in results if result.flagged or not result.ok]
    lines = [
        f"# {title}",
        "",
        f"- Runs: {summary['runs']}",
        f"- Success rate: {summary['success_rate']:.0%}",
        f"- Flagged rate: {summary['flagged_rate']:.0%}",
        f"- Full reference match rate: {summary['full_reference_match_rate']:.0%}",
        f"- Transcribe p50/p90: {summary['transcribe_ms_p50']}ms / {summary['transcribe_ms_p90']}ms",
        f"- RTF p50/p90: {summary['rtf_p50']} / {summary['rtf_p90']}",
        "",
    ]
    if flagged:
        lines.append("## Flagged")
        lines.append("")
        for result in flagged:
            reason = result.error or "reference mismatch"
            lines.append(f"- `{result.fixture}`: {reason}")
    else:
        lines.append("No flagged raw STT results.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_run_artifacts(run_dir: Path, *, args: argparse.Namespace, results: list[EvalResult]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = _summarize_results(results)
    manifest = {
        "tool": "eval_stt",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "suite": args.suite,
        "label": args.label,
        "fixtures": str(args.fixtures),
        "backend": args.backend,
        "model": args.model,
        "results": len(results),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(_compact_result(result)) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _write_markdown_summary(run_dir / "summary.md", title=f"Raw STT Eval: {args.label}", summary=summary, results=results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline STT fixture eval via raw STT backends")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="directory of 16kHz mono WAV fixtures")
    parser.add_argument("--suite", choices=("raw-stt",), default=None, help="named fixture suite; raw-stt uses --fixtures")
    parser.add_argument("--backend", choices=("mlx", "cartesia", "both"), default="mlx")
    parser.add_argument("--model", default=None, help="MLX model repo/path; defaults to VOICE__stt_model")
    parser.add_argument("--failures", action="store_true", help="only print flagged or failed clips")
    parser.add_argument("--jsonl", type=Path, default=None, help="append raw results as JSONL")
    parser.add_argument("--label", default=None, help="write a timestamped run under logs/evals with this label")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    fixtures = _collect_fixtures(args.fixtures)
    if not fixtures:
        print(f"No WAV fixtures found in {args.fixtures}", file=sys.stderr)
        return 1

    results: list[EvalResult] = []
    for backend_name in _backend_names(args.backend):
        try:
            backend, model = await _make_backend(backend_name, model=args.model)
        except Exception as exc:
            print(f"SKIP {backend_name}: {exc}", file=sys.stderr)
            continue
        for fixture in fixtures:
            results.append(
                await _eval_fixture(
                    fixture,
                    backend_name=backend_name,
                    backend=backend,
                    model=model,
                )
            )

    if args.jsonl:
        _write_jsonl(args.jsonl, results)
    if args.label:
        run_dir = _create_run_dir(args.label)
        _write_run_artifacts(run_dir, args=args, results=results)
        print(f"wrote_eval={run_dir}")

    printed = 0
    for result in results:
        if args.failures and result.ok and not result.flagged:
            continue
        _print_result(result)
        printed += 1

    flagged = sum(1 for result in results if result.flagged or not result.ok)
    print(f"\nfixtures={len(fixtures)} results={len(results)} flagged={flagged} printed={printed}")
    return 0 if results else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
