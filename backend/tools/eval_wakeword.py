"""Offline wakeword eval — replay verified clips through WakeWordService.

Run from backend/:
    uv run python tools/eval_wakeword.py
    uv run python tools/eval_wakeword.py --grid
    uv run python tools/eval_wakeword.py --model ../training/wakeword/models/jarvis_au_adapter.onnx
    uv run python tools/eval_wakeword.py --failures
    uv run python tools/eval_wakeword.py --ambient ../training/wakeword/data/ambient/eval
    uv run python tools/eval_wakeword.py --ambient-manifest ../training/wakeword/manifests/public_fa_eval.jsonl
    uv run python tools/eval_wakeword.py --ambient room.wav --max-fa-per-hour 1.0
    uv run python tools/eval_wakeword.py --diagnose-feedback
    uv run python tools/eval_wakeword.py --ambient-manifest ../training/wakeword/manifests/enrolled_free_speech.jsonl
    uv run python tools/eval_wakeword.py --speaker-verifier --speaker-model resources/models/speaker/wespeaker.onnx \\
        --speaker-enrollment-manifest ../training/wakeword/manifests/speaker_enrollment.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import wave
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Protocol

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.config import settings  # noqa: E402
from core.voice.wakeword_service import WakeWordService  # noqa: E402

# ~256 ms at 16 kHz mono 16-bit — matches typical frontend chunk size
CHUNK_BYTES = WakeWordService.INFERENCE_WINDOW_BYTES * 2
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono 16-bit

DEFAULT_POSITIVES = REPO_ROOT / "training/wakeword/data/positives_real"
DEFAULT_FEEDBACK_POS = REPO_ROOT / "training/wakeword/data/feedback/positives"
DEFAULT_NEGATIVES = REPO_ROOT / "training/wakeword/data/feedback/negatives"
DEFAULT_ENROLLED_FREE_SPEECH = REPO_ROOT / "training/wakeword/data/enrolled_eval/free_speech"
DEFAULT_MODEL = "resources/models/wakeword/Jarvis.onnx"
DEFAULT_SPEAKER_MODEL = "resources/models/speaker/nemo_en_titanet_small.onnx"
DEFAULT_SPEAKER_ENROLLMENT = REPO_ROOT / "training/wakeword/manifests/speaker_enrollment.jsonl"

GRID_PATIENCE = (3, 4)
GRID_SENSITIVITY = (0.90, 0.93)
GRID_VAD = (0.4, 0.5)
SPEAKER_THRESHOLD_GRID = (0.15, 0.18, 0.21, 0.24, 0.27, 0.30)


@dataclass(frozen=True)
class WakewordConfig:
    sensitivity: float
    patience: int
    vad: float

    def label(self) -> str:
        return f"thr={self.sensitivity:.2f} N={self.patience} vad={self.vad:.1f}"


@dataclass
class EvalResult:
    config: WakewordConfig
    pos_recall: float
    pos_fired: int
    pos_total: int
    neg_false_rate: float
    neg_fired: int
    neg_total: int
    vad_effective: float

    @property
    def score(self) -> float:
        return self.pos_recall - self.neg_false_rate


@dataclass(frozen=True)
class AmbientFire:
    """One false accept during continuous ambient replay."""

    timestamp_s: float
    source: str


@dataclass(frozen=True)
class AmbientClip:
    path: Path
    source: str


@dataclass(frozen=True)
class SpeakerEvalConfig:
    enabled: bool = False
    model_path: str | None = None
    profile_path: Path | None = None
    enrollment_manifest: Path | None = None
    threshold: float | None = None
    speaker_id: str = "enrolled_user"


@dataclass
class PipelineAttribution:
    stage1_candidates: int = 0
    verifier_rejects: int = 0
    final_commits: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class AmbientEvalResult:
    config: WakewordConfig
    duration_s: float
    false_accepts: int
    false_accepts_per_hour: float
    vad_effective: float
    speaker_threshold: float | None = None
    fires: list[AmbientFire] = field(default_factory=list)
    attribution: PipelineAttribution = field(default_factory=PipelineAttribution)


@dataclass(frozen=True)
class AmbientCandidate:
    timestamp_s: float
    source: str
    speaker_score: float | None = None


class WakeDetector(Protocol):
    def process(self, chunk: bytes) -> bool: ...

    def reset(self) -> None: ...


def pcm_duration_seconds(pcm: bytes) -> float:
    """Duration of 16 kHz mono 16-bit PCM."""
    return len(pcm) / BYTES_PER_SECOND


def false_accepts_per_hour(false_accepts: int, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    return false_accepts * 3600.0 / duration_s


def _parse_float_grid(raw: str) -> list[float]:
    values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("grid cannot be empty")
    return values


def _parse_int_grid(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("grid cannot be empty")
    return values


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes:02d}:{secs:06.3f}"


def collect_ambient_paths(path: Path) -> list[Path]:
    """Resolve a single WAV file or a directory of *.wav files."""
    if not path.exists():
        raise FileNotFoundError(f"Ambient path not found: {path}")
    if path.is_file():
        if path.suffix.lower() != ".wav":
            raise ValueError(f"Ambient path must be a .wav file: {path}")
        return [path]
    if path.is_dir():
        wavs = sorted(path.glob("*.wav"))
        if not wavs:
            raise ValueError(f"No .wav files in ambient directory: {path}")
        return wavs
    raise ValueError(f"Ambient path must be a file or directory: {path}")


def collect_ambient_clips(path: Path) -> list[AmbientClip]:
    return [AmbientClip(path=p, source=p.name) for p in collect_ambient_paths(path)]


def load_ambient_manifest(path: Path) -> list[AmbientClip]:
    """Load a JSONL manifest of public negative WAVs for FA/hr evaluation."""
    if not path.exists():
        raise FileNotFoundError(f"Ambient manifest not found: {path}")

    clips: list[AmbientClip] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: manifest row must be an object")

            if row.get("contains_wakeword") is True:
                raise ValueError(f"{path}:{line_no}: FA/hr manifest cannot include wakeword-positive audio")

            raw_clip_path = row.get("path")
            if not isinstance(raw_clip_path, str) or not raw_clip_path:
                raise ValueError(f"{path}:{line_no}: missing string field 'path'")

            clip_path = Path(raw_clip_path)
            if not clip_path.is_absolute():
                clip_path = path.parent / clip_path
            clip_path = clip_path.resolve()
            if clip_path.suffix.lower() != ".wav":
                raise ValueError(f"{path}:{line_no}: manifest path must point to a .wav file")
            if not clip_path.exists():
                raise FileNotFoundError(f"{path}:{line_no}: manifest audio not found: {clip_path}")

            source = str(row.get("source") or "").strip()
            category = str(row.get("category") or "").strip()
            label_parts = [part for part in (source, category, clip_path.name) if part]
            clips.append(AmbientClip(path=clip_path, source="/".join(label_parts) or clip_path.name))

    if not clips:
        raise ValueError(f"No ambient clips in manifest: {path}")
    return clips


def _load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            raise ValueError(
                f"{path.name}: expected 16 kHz mono 16-bit PCM, got "
                f"{wf.getframerate()} Hz {wf.getnchannels()}ch {wf.getsampwidth() * 8}-bit"
            )
        return wf.readframes(wf.getnframes())


def _collect_wavs(directory: Path | None) -> list[Path]:
    if directory is None or not directory.is_dir():
        return []
    return sorted(directory.glob("*.wav"))


def stream_false_accepts(
    detector: WakeDetector,
    pcm: bytes,
    *,
    time_offset_s: float = 0.0,
    source: str = "",
    chunk_bytes: int = CHUNK_BYTES,
) -> list[AmbientFire]:
    """Replay continuous PCM; reset detector after each fire (matches runtime)."""
    fires: list[AmbientFire] = []
    for offset in range(0, len(pcm), chunk_bytes):
        if detector.process(pcm[offset : offset + chunk_bytes]):
            timestamp_s = time_offset_s + offset / BYTES_PER_SECOND
            fires.append(AmbientFire(timestamp_s=timestamp_s, source=source))
            detector.reset()
    return fires


def stream_false_accepts_with_attribution(
    ww: WakeWordService,
    pcm: bytes,
    *,
    time_offset_s: float = 0.0,
    source: str = "",
    chunk_bytes: int = CHUNK_BYTES,
) -> tuple[list[AmbientFire], dict[str, int]]:
    """Replay continuous PCM and count verifier reject reasons."""
    fires: list[AmbientFire] = []
    reject_reasons: dict[str, int] = {}
    for offset in range(0, len(pcm), chunk_bytes):
        if ww.process(pcm[offset : offset + chunk_bytes]):
            timestamp_s = time_offset_s + offset / BYTES_PER_SECOND
            fires.append(AmbientFire(timestamp_s=timestamp_s, source=source))
            ww.reset()
        elif ww.last_had_candidate:
            decision = ww.last_decision
            if decision is not None and not decision.accept:
                reject_reasons[decision.reason] = reject_reasons.get(decision.reason, 0) + 1
    return fires, reject_reasons


def stream_ambient_candidates(
    ww: WakeWordService,
    pcm: bytes,
    *,
    time_offset_s: float = 0.0,
    source: str = "",
    chunk_bytes: int = CHUNK_BYTES,
) -> list[AmbientCandidate]:
    """Replay PCM once and record every Stage-1 candidate plus its speaker score."""
    candidates: list[AmbientCandidate] = []
    for offset in range(0, len(pcm), chunk_bytes):
        committed = ww.process(pcm[offset : offset + chunk_bytes])
        if committed or ww.last_had_candidate:
            decision = ww.last_decision
            score = None
            if decision is not None:
                score = decision.scores.get("speaker_cosine")
            timestamp_s = time_offset_s + offset / BYTES_PER_SECOND
            candidates.append(
                AmbientCandidate(
                    timestamp_s=timestamp_s,
                    source=source,
                    speaker_score=score,
                )
            )
            if committed:
                ww.reset()
    return candidates


def _clip_fires(ww: WakeWordService, pcm: bytes) -> bool:
    ww.reset()
    for offset in range(0, len(pcm), CHUNK_BYTES):
        if ww.process(pcm[offset : offset + CHUNK_BYTES]):
            return True
    return False


def _clip_fires_with_preroll(ww: WakeWordService, pcm: bytes, *, preroll_s: float = 2.0) -> bool:
    """Replay with leading silence to simulate streaming mel context."""
    ww.reset()
    preroll = b"\x00\x00" * int(SAMPLE_RATE * preroll_s)
    for offset in range(0, len(preroll), CHUNK_BYTES):
        ww.process(preroll[offset : offset + CHUNK_BYTES])
    for offset in range(0, len(pcm), CHUNK_BYTES):
        if ww.process(pcm[offset : offset + CHUNK_BYTES]):
            return True
    return False


def _wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def _duration_stats(paths: list[Path]) -> dict[str, float | int]:
    if not paths:
        return {"count": 0}
    durations = [_wav_duration_seconds(p) for p in paths]
    return {
        "count": len(durations),
        "min_s": min(durations),
        "max_s": max(durations),
        "mean_s": sum(durations) / len(durations),
    }


def diagnose_feedback_positives(
    model_path: str,
    *,
    feedback_dir: Path,
    positives_dir: Path,
    config: WakewordConfig | None = None,
    sample_limit: int | None = None,
) -> int:
    """Explain low feedback-positive clip recall vs positives_real."""
    config = config or _default_config()
    feedback_paths = _collect_wavs(feedback_dir)
    positive_paths = _collect_wavs(positives_dir)
    if not feedback_paths:
        print(f"No feedback positives in {feedback_dir}", file=sys.stderr)
        return 1

    if sample_limit is not None:
        feedback_paths = feedback_paths[:sample_limit]

    print(f"Model: {model_path}")
    print(f"feedback_positives: {len(feedback_paths)} (sampled)" if sample_limit else f"feedback_positives: {len(feedback_paths)}")
    print(f"positives_real: {len(positive_paths)}")
    print()

    fb_stats = _duration_stats(feedback_paths)
    real_stats = _duration_stats(positive_paths)
    print("Duration stats:")
    print(f"  feedback_positives: {fb_stats}")
    print(f"  positives_real:     {real_stats}")
    print()

    cold_pass: list[Path] = []
    preroll_pass: list[Path] = []
    cold_ww = _make_service(model_path, config)
    preroll_ww = _make_service(model_path, config)
    for path in feedback_paths:
        pcm = _load_wav_pcm(path)
        if _clip_fires(cold_ww, pcm):
            cold_pass.append(path)
        if _clip_fires_with_preroll(preroll_ww, pcm):
            preroll_pass.append(path)

    cold_recall = len(cold_pass) / len(feedback_paths)
    preroll_recall = len(preroll_pass) / len(feedback_paths)
    print(f"Cold clip replay recall:        {len(cold_pass)}/{len(feedback_paths)} ({cold_recall:.0%})")
    print(f"With 2s silence preroll recall: {len(preroll_pass)}/{len(feedback_paths)} ({preroll_recall:.0%})")

    if positive_paths:
        real_ww = _make_service(model_path, config)
        real_pass = sum(
            1 for p in positive_paths if _clip_fires(real_ww, _load_wav_pcm(p))
        )
        print(f"positives_real recall:          {real_pass}/{len(positive_paths)} ({real_pass / len(positive_paths):.0%})")
    print()

    capture_window_s = (16 * 0.08) + 0.04  # 16 OWW windows + 40ms tail
    print("Interpretation:")
    print(
        f"  - Feedback clips cluster near {capture_window_s:.2f}s — the runtime detection capture window, "
        "not independent wake utterances."
    )
    print(
        "  - Cold replay from reset() lacks the streaming mel context present at live fire time; "
        "low replay recall does not by itself prove deployment miss rate."
    )
    if preroll_recall <= cold_recall + 0.05:
        print("  - 2s silence preroll did not materially recover recall → treat feedback positives as verifier/training data, not the recall gate.")
    else:
        print("  - Preroll improved recall → streaming context is a contributing factor.")
    print("  - Keep `positives_real` as the clip recall gate until a enrolled-user live recall corpus exists.")
    return 0


def _evaluate_clips(
    ww: WakeWordService,
    positives: list[Path],
    negatives: list[Path],
) -> tuple[int, int, int, int]:
    pos_fired = sum(1 for p in positives if _clip_fires(ww, _load_wav_pcm(p)))
    neg_fired = sum(1 for p in negatives if _clip_fires(ww, _load_wav_pcm(p)))
    return pos_fired, len(positives), neg_fired, len(negatives)


def _make_service(
    model_path: str,
    config: WakewordConfig,
    speaker: SpeakerEvalConfig | None = None,
) -> WakeWordService:
    logging.getLogger("core.voice.wakeword_service").setLevel(logging.WARNING)
    verifiers = None
    if speaker is not None and speaker.enabled:
        from core.voice.wakeword.speaker_verifier import SpeakerEmbeddingWakeVerifier

        if not speaker.model_path:
            raise ValueError("--speaker-model is required with --speaker-verifier")
        if speaker.profile_path is None and speaker.enrollment_manifest is None:
            raise ValueError("--speaker-profile or --speaker-enrollment-manifest is required with --speaker-verifier")

        model = Path(speaker.model_path)
        if not model.is_absolute():
            model = (BACKEND_DIR / model).resolve()
        profile = speaker.profile_path
        if profile is not None and not profile.is_absolute():
            profile = (BACKEND_DIR / profile).resolve()
        manifest = speaker.enrollment_manifest.resolve() if speaker.enrollment_manifest is not None else None
        threshold = (
            speaker.threshold
            if speaker.threshold is not None
            else settings.VOICE.wakeword_speaker_threshold
        )
        verifiers = [
            SpeakerEmbeddingWakeVerifier(
                model_path=model,
                profile_path=profile,
                enrollment_manifest=manifest,
                threshold=threshold,
                speaker_id=speaker.speaker_id,
                num_threads=settings.VOICE.wakeword_speaker_num_threads,
            )
        ]

    ww = WakeWordService(
        model_path,
        sensitivity=config.sensitivity,
        consecutive_required=config.patience,
        vad_threshold=config.vad,
        verifiers=verifiers,
    )
    return ww


def _eval_config(
    model_path: str,
    config: WakewordConfig,
    positives: list[Path],
    negatives: list[Path],
    speaker: SpeakerEvalConfig | None = None,
) -> EvalResult:
    ww = _make_service(model_path, config, speaker)
    if not ww._model_loaded:
        raise RuntimeError(f"Failed to load model at {model_path}")
    pos_fired, pos_total, neg_fired, neg_total = _evaluate_clips(ww, positives, negatives)
    pos_recall = pos_fired / pos_total if pos_total else 0.0
    neg_rate = neg_fired / neg_total if neg_total else 0.0
    return EvalResult(
        config=config,
        pos_recall=pos_recall,
        pos_fired=pos_fired,
        pos_total=pos_total,
        neg_false_rate=neg_rate,
        neg_fired=neg_fired,
        neg_total=neg_total,
        vad_effective=ww.vad_threshold,
    )


def _eval_ambient(
    model_path: str,
    config: WakewordConfig,
    ambient_clips: list[AmbientClip],
    speaker: SpeakerEvalConfig | None = None,
) -> AmbientEvalResult:
    ww = _make_service(model_path, config, speaker)
    if not ww._model_loaded:
        raise RuntimeError(f"Failed to load model at {model_path}")

    ww.reset()
    ww.reset_stats()
    all_fires: list[AmbientFire] = []
    merged_reject_reasons: dict[str, int] = {}
    total_duration_s = 0.0

    for clip in ambient_clips:
        pcm = _load_wav_pcm(clip.path)
        file_duration = pcm_duration_seconds(pcm)
        fires, reject_reasons = stream_false_accepts_with_attribution(
            ww,
            pcm,
            time_offset_s=total_duration_s,
            source=clip.source,
        )
        all_fires.extend(fires)
        for reason, count in reject_reasons.items():
            merged_reject_reasons[reason] = merged_reject_reasons.get(reason, 0) + count
        total_duration_s += file_duration

    false_count = len(all_fires)
    stats = ww.pipeline_stats
    return AmbientEvalResult(
        config=config,
        duration_s=total_duration_s,
        false_accepts=false_count,
        false_accepts_per_hour=false_accepts_per_hour(false_count, total_duration_s),
        vad_effective=ww.vad_threshold,
        fires=all_fires,
        attribution=PipelineAttribution(
            stage1_candidates=stats.get("candidates", 0),
            verifier_rejects=stats.get("verifier_rejects", 0),
            final_commits=stats.get("commits", 0),
            reject_reasons=merged_reject_reasons,
        ),
    )


def _limit_ambient_clips_by_duration(
    clips: list[AmbientClip],
    *,
    max_hours: float | None,
) -> list[AmbientClip]:
    if max_hours is None:
        return clips
    if max_hours <= 0:
        raise ValueError("--ambient-max-hours must be > 0")

    max_seconds = max_hours * 3600.0
    selected: list[AmbientClip] = []
    total = 0.0
    for clip in clips:
        selected.append(clip)
        total += _wav_duration_seconds(clip.path)
        if total >= max_seconds:
            break
    return selected


def _filter_ambient_clips_by_source(
    clips: list[AmbientClip],
    *,
    source_regex: str | None,
) -> list[AmbientClip]:
    if not source_regex:
        return clips
    pattern = re.compile(source_regex)
    selected = [clip for clip in clips if pattern.search(clip.source)]
    if not selected:
        raise ValueError(f"--ambient-source-regex matched no clips: {source_regex}")
    return selected


def _eval_ambient_candidates(
    model_path: str,
    config: WakewordConfig,
    ambient_clips: list[AmbientClip],
    speaker: SpeakerEvalConfig | None = None,
) -> tuple[float, float, list[AmbientCandidate]]:
    ww = _make_service(model_path, config, speaker)
    if not ww._model_loaded:
        raise RuntimeError(f"Failed to load model at {model_path}")

    ww.reset()
    total_duration_s = 0.0
    candidates: list[AmbientCandidate] = []
    for clip in ambient_clips:
        pcm = _load_wav_pcm(clip.path)
        candidates.extend(
            stream_ambient_candidates(
                ww,
                pcm,
                time_offset_s=total_duration_s,
                source=clip.source,
            )
        )
        total_duration_s += pcm_duration_seconds(pcm)
    return total_duration_s, ww.vad_threshold, candidates


def _ambient_result_from_candidates(
    config: WakewordConfig,
    *,
    duration_s: float,
    vad_effective: float,
    candidates: list[AmbientCandidate],
    speaker_threshold: float | None,
) -> AmbientEvalResult:
    if speaker_threshold is None:
        accepted = candidates
        rejects = 0
    else:
        accepted = [
            candidate
            for candidate in candidates
            if candidate.speaker_score is not None and candidate.speaker_score >= speaker_threshold
        ]
        rejects = len(candidates) - len(accepted)

    fires = [
        AmbientFire(timestamp_s=candidate.timestamp_s, source=candidate.source)
        for candidate in accepted
    ]
    return AmbientEvalResult(
        config=config,
        duration_s=duration_s,
        false_accepts=len(fires),
        false_accepts_per_hour=false_accepts_per_hour(len(fires), duration_s),
        vad_effective=vad_effective,
        speaker_threshold=speaker_threshold,
        fires=fires,
        attribution=PipelineAttribution(
            stage1_candidates=len(candidates),
            verifier_rejects=rejects,
            final_commits=len(fires),
            reject_reasons={"speaker_mismatch": rejects} if rejects else {},
        ),
    )


def _default_config() -> WakewordConfig:
    return WakewordConfig(
        sensitivity=settings.VOICE.wakeword_sensitivity,
        patience=settings.VOICE.wakeword_patience,
        vad=settings.VOICE.wakeword_vad_threshold,
    )


def _print_result(result: EvalResult, *, label: str = "") -> None:
    prefix = f"{label}: " if label else ""
    print(
        f"{prefix}{result.config.label()} (vad_eff={result.vad_effective:.1f}) | "
        f"recall {result.pos_fired}/{result.pos_total} ({result.pos_recall:.0%}) | "
        f"false {result.neg_fired}/{result.neg_total} ({result.neg_false_rate:.0%}) | "
        f"score {result.score:+.2f}"
    )


def _print_ambient_result(result: AmbientEvalResult) -> None:
    hours = result.duration_s / 3600.0
    speaker_label = (
        f" speaker_thr={result.speaker_threshold:.2f}"
        if result.speaker_threshold is not None
        else ""
    )
    print(
        f"ambient {result.config.label()}{speaker_label} (vad_eff={result.vad_effective:.1f}) | "
        f"duration {result.duration_s:.1f}s ({hours:.3f}h) | "
        f"false_accepts {result.false_accepts} | "
        f"FA/hr {result.false_accepts_per_hour:.3f}"
    )
    attr = result.attribution
    if attr.stage1_candidates or attr.verifier_rejects:
        print(
            f"pipeline | stage1_candidates {attr.stage1_candidates} | "
            f"verifier_rejects {attr.verifier_rejects} | "
            f"final_commits {attr.final_commits}"
        )
        if attr.reject_reasons:
            reasons = ", ".join(f"{k}={v}" for k, v in sorted(attr.reject_reasons.items()))
            print(f"reject_reasons | {reasons}")
    if result.fires:
        by_source: dict[str, int] = {}
        for fire in result.fires:
            group = fire.source.split("/", maxsplit=1)[0] if fire.source else "unknown"
            by_source[group] = by_source.get(group, 0) + 1
        summary = ", ".join(f"{source}={count}" for source, count in sorted(by_source.items()))
        print(f"false_accept_sources | {summary}")
        print("\nFalse accepts (timestamp, source):")
        for fire in result.fires:
            print(f"  {format_timestamp(fire.timestamp_s)}  {fire.source}")


def _print_ambient_grid(results: list[AmbientEvalResult]) -> None:
    print("\nAmbient grid (ranked by FA/hr, then verifier rejects):")
    for result in sorted(
        results,
        key=lambda r: (
            r.false_accepts_per_hour,
            -r.attribution.verifier_rejects,
            -r.attribution.final_commits,
        ),
    ):
        speaker_label = (
            f" speaker_thr={result.speaker_threshold:.2f}"
            if result.speaker_threshold is not None
            else ""
        )
        print(
            f"{result.config.label()}{speaker_label} (vad_eff={result.vad_effective:.1f}) | "
            f"FA/hr {result.false_accepts_per_hour:.3f} | "
            f"false {result.false_accepts} | "
            f"stage1 {result.attribution.stage1_candidates} | "
            f"speaker_rejects {result.attribution.verifier_rejects}"
        )


def _list_failures(
    model_path: str,
    config: WakewordConfig,
    negatives: list[Path],
    speaker: SpeakerEvalConfig | None = None,
) -> list[Path]:
    ww = _make_service(model_path, config, speaker)
    fired: list[Path] = []
    for path in negatives:
        if _clip_fires(ww, _load_wav_pcm(path)):
            fired.append(path)
    return fired


def _speaker_config_from_args(args: argparse.Namespace) -> SpeakerEvalConfig | None:
    if not args.speaker_verifier:
        return None
    profile = getattr(args, "speaker_profile", None)
    manifest = args.speaker_enrollment_manifest
    if profile is None and manifest is None:
        raise ValueError("--speaker-profile or --speaker-enrollment-manifest is required with --speaker-verifier")
    return SpeakerEvalConfig(
        enabled=True,
        model_path=args.speaker_model,
        profile_path=profile,
        enrollment_manifest=manifest,
        threshold=args.speaker_threshold,
        speaker_id=args.speaker_id,
    )


def _parse_threshold_grid(raw: str | None) -> list[float]:
    if raw is None or not raw.strip():
        return list(SPEAKER_THRESHOLD_GRID)
    thresholds: list[float] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        threshold = float(value)
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(f"speaker threshold must be in [0, 1], got {threshold}")
        thresholds.append(threshold)
    if not thresholds:
        raise ValueError("speaker threshold grid cannot be empty")
    return thresholds


def _build_enrolled_verifier_from_args(args: argparse.Namespace):
    from core.voice.speaker_verifier import EnrolledSpeakerVerifier

    if not args.speaker_model:
        raise ValueError("--speaker-model is required for --speaker-only")
    if args.speaker_profile is None and args.speaker_enrollment_manifest is None:
        raise ValueError(
            "--speaker-profile or --speaker-enrollment-manifest is required for --speaker-only"
        )

    model = Path(args.speaker_model)
    if not model.is_absolute():
        model = (BACKEND_DIR / model).resolve()
    profile = args.speaker_profile
    if profile is not None and not profile.is_absolute():
        profile = (BACKEND_DIR / profile).resolve()
    manifest = (
        args.speaker_enrollment_manifest.resolve()
        if args.speaker_enrollment_manifest is not None
        else None
    )
    return EnrolledSpeakerVerifier(
        owner_id=args.speaker_id,
        model_path=model,
        profile_path=profile,
        enrollment_manifest=manifest,
        speaker_id=args.speaker_id,
        num_threads=settings.VOICE.wakeword_speaker_num_threads,
        enabled=True,
    )


def _score_clips_speaker_only(
    verifier,
    clips: list[Path],
) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    for path in clips:
        pcm = _load_wav_pcm(path)
        evidence = verifier.verify_pcm(pcm, threshold=0.0)
        cosine = float(evidence.cosine or 0.0)
        scores.append((path.name, cosine))
    return scores


def _run_speaker_only(args: argparse.Namespace) -> int:
    """Bypass Stage 1 and score free-speech PCM against the enrolled verifier."""
    owner_clips = _collect_wavs(args.positives)
    non_owner_clips = _collect_wavs(args.negatives)
    tts_echo_clips = _collect_wavs(getattr(args, "tts_echo", None))
    if not owner_clips:
        print("Owner clips are required for --speaker-only calibration.", file=sys.stderr)
        return 1
    if not tts_echo_clips:
        print("Jarvis TTS echo clips are required for --speaker-only calibration.", file=sys.stderr)
        return 1

    try:
        verifier = _build_enrolled_verifier_from_args(args)
        thresholds = (
            _parse_threshold_grid(args.speaker_threshold_grid)
            if args.speaker_threshold_grid is not None
            else [
                args.speaker_threshold
                if args.speaker_threshold is not None
                else settings.VOICE.barge_in_speaker_threshold
            ]
        )
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"\nSpeaker-only barge calibration | owner={len(owner_clips)} "
        f"non_owner={len(non_owner_clips)} tts_echo={len(tts_echo_clips)}"
    )
    owner_scores = _score_clips_speaker_only(verifier, owner_clips)
    non_owner_scores = _score_clips_speaker_only(verifier, non_owner_clips)
    tts_echo_scores = _score_clips_speaker_only(verifier, tts_echo_clips)
    best: tuple[float, float, float, int, int] | None = None
    for threshold in thresholds:
        owner_total = len(owner_scores)
        other_total = len(non_owner_scores)
        tts_total = len(tts_echo_scores)
        owner_matched = sum(score >= threshold for _, score in owner_scores)
        other_matched = sum(score >= threshold for _, score in non_owner_scores)
        tts_matched = sum(score >= threshold for _, score in tts_echo_scores)
        owner_recall = (owner_matched / owner_total) if owner_total else 1.0
        other_accept = (other_matched / other_total) if other_total else 0.0
        tts_accept = (tts_matched / tts_total) if tts_total else 0.0
        print(
            f"  thr={threshold:.2f} owner_recall={owner_matched}/{owner_total} ({owner_recall:.0%}) "
            f"non_owner_accept={other_matched}/{other_total} ({other_accept:.0%}) "
            f"tts_echo_accept={tts_matched}/{tts_total} ({tts_accept:.0%})"
        )
        if args.failures:
            for name, score in owner_scores:
                marker = "HIT" if score >= threshold else "MISS"
                print(f"    owner[{marker}] {name}: {score:.3f}")
            for name, score in non_owner_scores:
                marker = "FA" if score >= threshold else "ok"
                print(f"    other[{marker}] {name}: {score:.3f}")
            for name, score in tts_echo_scores:
                marker = "FA" if score >= threshold else "ok"
                print(f"    tts_echo[{marker}] {name}: {score:.3f}")
        if (
            owner_recall >= 0.90
            and tts_matched == 0
            and (best is None or other_accept < best[1])
        ):
            best = (threshold, other_accept, owner_recall, other_matched, tts_matched)

    if best is not None:
        print(
            f"\nSuggested barge_in_speaker_threshold={best[0]:.2f} "
            f"(owner_recall={best[2]:.0%}, non_owner_accept={best[1]:.0%}, "
            f"n_fa={int(best[3])}, tts_echo_fa={int(best[4])})"
        )
    else:
        print(
            "\nNo threshold met >=90% owner recall with zero Jarvis-TTS echo accepts "
            "on this corpus."
        )
    return 0


def _run_speaker_threshold_grid(args: argparse.Namespace, config: WakewordConfig) -> int:
    if not args.speaker_verifier:
        print("--speaker-threshold-grid requires --speaker-verifier", file=sys.stderr)
        return 1

    try:
        thresholds = _parse_threshold_grid(args.speaker_threshold_grid)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    positives = _collect_wavs(args.positives)
    negatives = _collect_wavs(args.negatives)
    if not positives:
        print("No positives_real clips found for threshold grid.", file=sys.stderr)
        return 1
    if not negatives:
        print("No negative clips found for threshold grid.", file=sys.stderr)
        return 1

    print(
        f"\nSpeaker threshold grid | positives_real={len(positives)} "
        f"negatives={len(negatives)}"
    )
    results: list[tuple[float, EvalResult]] = []
    for threshold in thresholds:
        speaker = SpeakerEvalConfig(
            enabled=True,
            model_path=args.speaker_model,
            profile_path=args.speaker_profile,
            enrollment_manifest=args.speaker_enrollment_manifest,
            threshold=threshold,
            speaker_id=args.speaker_id,
        )
        try:
            result = _eval_config(args.model, config, positives, negatives, speaker)
        except (RuntimeError, ValueError) as exc:
            print(f"SKIP speaker_threshold={threshold:.2f}: {exc}")
            continue
        results.append((threshold, result))

    if not results:
        return 1

    for threshold, result in sorted(results, key=lambda item: item[1].score, reverse=True):
        _print_result(result, label=f"speaker_threshold={threshold:.2f}")
    return 0


def _run_ambient(
    model_path: str,
    *,
    ambient: Path | None,
    ambient_manifest: Path | None,
    max_fa_per_hour: float | None,
    ambient_grid: bool = False,
    ambient_source_regex: str | None = None,
    ambient_max_hours: float | None = None,
    sensitivities: list[float] | None = None,
    patiences: list[int] | None = None,
    vad_thresholds: list[float] | None = None,
    speaker_thresholds: list[float] | None = None,
    speaker: SpeakerEvalConfig | None = None,
) -> int:
    try:
        if ambient_manifest is not None:
            ambient_clips = load_ambient_manifest(ambient_manifest)
            ambient_label = f"manifest {ambient_manifest}"
        elif ambient is not None:
            ambient_clips = collect_ambient_clips(ambient)
            ambient_label = str(ambient)
        else:
            print("--ambient or --ambient-manifest is required", file=sys.stderr)
            return 1
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        ambient_clips = _filter_ambient_clips_by_source(
            ambient_clips,
            source_regex=ambient_source_regex,
        )
        ambient_clips = _limit_ambient_clips_by_duration(ambient_clips, max_hours=ambient_max_hours)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    config = _default_config()
    print(f"Model: {model_path}")
    print(f"ambient: {len(ambient_clips)} file(s) from {ambient_label}")
    if ambient_source_regex:
        print(f"ambient_source_regex: {ambient_source_regex}")
    if ambient_max_hours is not None:
        print(f"ambient_limit: first ~{ambient_max_hours:.3f}h")

    if speaker is not None and speaker.enabled:
        print(
            f"speaker_verifier: model={speaker.model_path} "
            f"profile={speaker.profile_path} manifest={speaker.enrollment_manifest} "
            f"threshold={speaker.threshold}"
        )

    if ambient_grid:
        stage1_configs = [
            WakewordConfig(sensitivity=sensitivity, patience=patience, vad=vad)
            for patience, sensitivity, vad in product(
                patiences or list(GRID_PATIENCE),
                sensitivities or list(GRID_SENSITIVITY),
                vad_thresholds or list(GRID_VAD),
            )
        ]
        threshold_grid: list[float | None]
        probe_speaker = speaker
        if speaker is not None and speaker.enabled:
            threshold_grid = speaker_thresholds or _parse_threshold_grid("")
            probe_speaker = SpeakerEvalConfig(
                enabled=True,
                model_path=speaker.model_path,
                enrollment_manifest=speaker.enrollment_manifest,
                threshold=0.0,
                speaker_id=speaker.speaker_id,
            )
        else:
            threshold_grid = [None]

        results: list[AmbientEvalResult] = []
        for cfg in stage1_configs:
            try:
                duration_s, vad_effective, candidates = _eval_ambient_candidates(
                    model_path,
                    cfg,
                    ambient_clips,
                    probe_speaker,
                )
            except RuntimeError as exc:
                print(f"SKIP {cfg.label()}: {exc}")
                continue
            for threshold in threshold_grid:
                results.append(
                    _ambient_result_from_candidates(
                        cfg,
                        duration_s=duration_s,
                        vad_effective=vad_effective,
                        candidates=candidates,
                        speaker_threshold=threshold,
                    )
                )

        if not results:
            return 1
        _print_ambient_grid(results)
        best = min(results, key=lambda r: r.false_accepts_per_hour)
        if max_fa_per_hour is not None and best.false_accepts_per_hour > max_fa_per_hour:
            print(
                f"\nFAIL: best FA/hr {best.false_accepts_per_hour:.3f} > "
                f"--max-fa-per-hour {max_fa_per_hour:.3f}",
                file=sys.stderr,
            )
            return 1
        return 0

    try:
        result = _eval_ambient(model_path, config, ambient_clips, speaker)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_ambient_result(result)

    if max_fa_per_hour is not None and result.false_accepts_per_hour > max_fa_per_hour:
        print(
            f"\nFAIL: FA/hr {result.false_accepts_per_hour:.3f} > --max-fa-per-hour {max_fa_per_hour:.3f}",
            file=sys.stderr,
        )
        return 1
    return 0


def _run_clip_eval(args: argparse.Namespace, speaker: SpeakerEvalConfig | None = None) -> int:
    positives = _collect_wavs(args.positives)
    feedback_pos = _collect_wavs(args.feedback_positives)
    negatives = _collect_wavs(args.negatives)

    if not positives and not feedback_pos:
        print("No positive clips found.", file=sys.stderr)
        return 1

    model_path = args.model
    print(f"Model: {model_path}")
    print(f"positives_real: {len(positives)} | feedback_positives: {len(feedback_pos)} | negatives: {len(negatives)}")

    if speaker is not None and speaker.enabled:
        print(
            f"speaker_verifier: model={speaker.model_path} "
            f"profile={speaker.profile_path} manifest={speaker.enrollment_manifest} "
            f"threshold={speaker.threshold}"
        )

    config = _default_config()
    if args.speaker_threshold_grid is not None:
        return _run_speaker_threshold_grid(args, config)

    if args.failures:
        if not negatives:
            print("No negative clips to check.")
            return 0
        fired = _list_failures(model_path, config, negatives, speaker)
        print(f"\nStill firing ({len(fired)}/{len(negatives)}) at {config.label()}:")
        for path in fired:
            print(f"  {path.name}")
        return 0

    result: EvalResult | None = None

    if args.grid:
        results: list[EvalResult] = []
        patiences = _parse_int_grid(args.patience_grid) if args.patience_grid else list(GRID_PATIENCE)
        sensitivities = (
            _parse_float_grid(args.sensitivity_grid)
            if args.sensitivity_grid
            else list(GRID_SENSITIVITY)
        )
        vad_thresholds = _parse_float_grid(args.vad_grid) if args.vad_grid else list(GRID_VAD)
        for patience, sensitivity, vad in product(patiences, sensitivities, vad_thresholds):
            cfg = WakewordConfig(sensitivity=sensitivity, patience=patience, vad=vad)
            try:
                results.append(_eval_config(model_path, cfg, positives, negatives, speaker))
            except RuntimeError as exc:
                print(f"SKIP {cfg.label()}: {exc}")
        results.sort(key=lambda r: r.score, reverse=True)
        print("\nGrid (ranked by recall - false_rate):")
        for r in results:
            _print_result(r)
        if feedback_pos:
            best = results[0]
            fb = _eval_config(
                model_path,
                best.config,
                feedback_pos,
                [],
                speaker,
            )
            print(f"\nFeedback positives at best grid row: {fb.pos_fired}/{fb.pos_total} ({fb.pos_recall:.0%})")
        result = next((r for r in results if r.config == config), results[0] if results else None)
    else:
        result = _eval_config(model_path, config, positives, negatives, speaker)
        _print_result(result, label="defaults")
        if feedback_pos:
            fb = _eval_config(model_path, config, feedback_pos, [], speaker)
            print(f"feedback_positives: {fb.pos_fired}/{fb.pos_total} ({fb.pos_recall:.0%})")

    if args.min_recall is not None and result is not None and result.pos_recall < args.min_recall:
        print(f"\nFAIL: recall {result.pos_recall:.0%} < --min-recall {args.min_recall:.0%}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline wakeword clip eval via WakeWordService")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ONNX path (relative to backend/ or absolute)")
    parser.add_argument("--positives", type=Path, default=DEFAULT_POSITIVES)
    parser.add_argument("--feedback-positives", type=Path, default=DEFAULT_FEEDBACK_POS)
    parser.add_argument("--negatives", type=Path, default=DEFAULT_NEGATIVES)
    parser.add_argument("--grid", action="store_true", help="Sweep a small patience/threshold/VAD grid")
    parser.add_argument(
        "--sensitivity-grid",
        default=None,
        metavar="CSV",
        help="Override Stage-1 sensitivity grid, e.g. 0.90,0.93,0.95",
    )
    parser.add_argument(
        "--patience-grid",
        default=None,
        metavar="CSV",
        help="Override Stage-1 patience grid, e.g. 3,4,5",
    )
    parser.add_argument(
        "--vad-grid",
        default=None,
        metavar="CSV",
        help="Override Stage-1 VAD grid, e.g. 0.4,0.5",
    )
    parser.add_argument("--failures", action="store_true", help="List negative clips that still fire")
    parser.add_argument("--min-recall", type=float, default=None, help="Exit 1 if positive recall is below this")
    parser.add_argument(
        "--ambient",
        type=Path,
        default=None,
        metavar="WAV_OR_DIR",
        help="Continuous ambient audio (file or directory of .wav); measures false accepts per hour",
    )
    parser.add_argument(
        "--ambient-manifest",
        type=Path,
        default=None,
        metavar="JSONL",
        help="JSONL manifest of negative ambient WAVs; paths are relative to the manifest file",
    )
    parser.add_argument(
        "--max-fa-per-hour",
        type=float,
        default=None,
        help="Exit 1 if ambient FA/hr exceeds this (requires --ambient or --ambient-manifest)",
    )
    parser.add_argument(
        "--ambient-grid",
        action="store_true",
        help="Sweep Stage-1 params and speaker thresholds on ambient audio",
    )
    parser.add_argument(
        "--ambient-source-regex",
        default=None,
        help="Only replay ambient clips whose source label matches this regex",
    )
    parser.add_argument(
        "--ambient-max-hours",
        type=float,
        default=None,
        help="Limit ambient replay to the first N hours for faster tuning runs",
    )
    parser.add_argument(
        "--diagnose-feedback",
        action="store_true",
        help="Analyze feedback-positive recall vs positives_real (does not gate)",
    )
    parser.add_argument(
        "--diagnose-feedback-sample",
        type=int,
        default=None,
        metavar="N",
        help="Limit --diagnose-feedback to first N clips",
    )
    parser.add_argument(
        "--speaker-verifier",
        action="store_true",
        help="Enable Stage 2b speaker embedding verifier",
    )
    parser.add_argument(
        "--speaker-only",
        action="store_true",
        help="Bypass Stage 1 and score PCM clips against the enrolled speaker verifier (barge calibration)",
    )
    parser.add_argument(
        "--speaker-model",
        default=DEFAULT_SPEAKER_MODEL,
        help="Sherpa-ONNX speaker embedding model path (relative to backend/ or absolute)",
    )
    parser.add_argument(
        "--speaker-profile",
        type=Path,
        default=None,
        help="Speaker profile .npz path (relative to backend/ or absolute); required unless --speaker-enrollment-manifest is set",
    )
    parser.add_argument(
        "--tts-echo",
        type=Path,
        default=None,
        help="Directory of Jarvis TTS room-echo clips for --speaker-only barge calibration",
    )
    parser.add_argument(
        "--speaker-enrollment-manifest",
        type=Path,
        default=None,
        help="JSONL manifest of wake-positive enrollment clips (dev fallback / threshold grid)",
    )
    parser.add_argument(
        "--speaker-threshold",
        type=float,
        default=None,
        help="Cosine similarity acceptance threshold (defaults to VOICE.wakeword_speaker_threshold)",
    )
    parser.add_argument(
        "--speaker-threshold-grid",
        nargs="?",
        const="",
        default=None,
        metavar="CSV",
        help="Sweep speaker thresholds on enrollment manifest split=dev and feedback negatives",
    )
    parser.add_argument(
        "--speaker-id",
        default="enrolled_user",
        help="Enrolled speaker id for verifier decisions",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    speaker = _speaker_config_from_args(args)

    if args.speaker_only:
        return _run_speaker_only(args)

    try:
        sensitivities = _parse_float_grid(args.sensitivity_grid) if args.sensitivity_grid else None
        patiences = _parse_int_grid(args.patience_grid) if args.patience_grid else None
        vad_thresholds = _parse_float_grid(args.vad_grid) if args.vad_grid else None
        speaker_thresholds = (
            _parse_threshold_grid(args.speaker_threshold_grid)
            if args.speaker_threshold_grid is not None
            else None
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.diagnose_feedback:
        if args.grid or args.failures or args.ambient is not None or args.ambient_manifest is not None:
            print("--diagnose-feedback cannot be combined with other eval modes", file=sys.stderr)
            return 1
        return diagnose_feedback_positives(
            args.model,
            feedback_dir=args.feedback_positives,
            positives_dir=args.positives,
            sample_limit=args.diagnose_feedback_sample,
        )

    has_ambient_eval = args.ambient is not None or args.ambient_manifest is not None
    if has_ambient_eval:
        if args.ambient is not None and args.ambient_manifest is not None:
            print("--ambient and --ambient-manifest cannot be combined", file=sys.stderr)
            return 1
        if args.grid or args.failures:
            print("ambient eval cannot be combined with --grid or --failures; use --ambient-grid", file=sys.stderr)
            return 1
        if args.min_recall is not None:
            print("--min-recall is ignored with --ambient (use clip eval separately)", file=sys.stderr)
        return _run_ambient(
            args.model,
            ambient=args.ambient,
            ambient_manifest=args.ambient_manifest,
            max_fa_per_hour=args.max_fa_per_hour,
            ambient_grid=args.ambient_grid,
            ambient_source_regex=args.ambient_source_regex,
            ambient_max_hours=args.ambient_max_hours,
            sensitivities=sensitivities,
            patiences=patiences,
            vad_thresholds=vad_thresholds,
            speaker_thresholds=speaker_thresholds,
            speaker=speaker,
        )

    if args.max_fa_per_hour is not None:
        print("--max-fa-per-hour requires --ambient or --ambient-manifest", file=sys.stderr)
        return 1

    return _run_clip_eval(args, speaker)


if __name__ == "__main__":
    raise SystemExit(main())
