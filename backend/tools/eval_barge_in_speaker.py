"""Offline barge-in speaker scoreboard.

Mix near-end clips with TTS at several SER levels, score with
EnrolledSpeakerVerifier, and print match rates vs threshold.

Rows are grouped by label / length / channel so short replies and
satellite audio are visible separately from laptop phrases.

Run from backend/:

    uv run python tools/eval_barge_in_speaker.py --owner-id geoff
    uv run python tools/eval_barge_in_speaker.py --profile /path/to/owner.npz
    uv run python tools/eval_barge_in_speaker.py --node-clip /path/to/room_jarvis.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import wave
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.voice.speaker_profile import resolve_owner_profile_path  # noqa: E402
from core.voice.speaker_verifier import (  # noqa: E402
    EnrolledSpeakerVerifier,
    SAMPLE_RATE,
    SpeakerMatchStatus,
    embed_pcm16,
    l2_normalize,
    load_speaker_extractor,
    load_speaker_profile_parts,
    pcm16_bytes_to_float32,
    pcm_onset_window,
    save_speaker_profile,
    speaker_model_id,
)

DEFAULT_CLIPS = REPO_ROOT / "training/voice/clips"
DEFAULT_MODEL = BACKEND_DIR / "resources/models/speaker/nemo_en_titanet_small.onnx"
SHORT_PREFIX_SECONDS = 0.5
SHORT_DURATION_MAX = 0.5
EVAL_NODE_ID = "eval-node"


@dataclass(frozen=True)
class Clip:
    id: str
    path: Path
    speaker: str
    role: str
    channel: str = "laptop"
    length: str = "phrase"
    derived: bool = False
    tags: tuple[str, ...] = ()


def _load_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            raise ValueError(
                f"{path}: expected 16 kHz mono 16-bit PCM, got "
                f"{wf.getframerate()} Hz {wf.getnchannels()}ch {wf.getsampwidth() * 8}-bit"
            )
        return wf.readframes(wf.getnframes())


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))


def mix_near_far(near_pcm: bytes, far_pcm: bytes, *, ser_db: float) -> bytes:
    """Overlay far-end onto near-end at the given signal-to-echo ratio (dB).

    ``ser_db=inf`` returns near-end alone. Far-end is looped/truncated to near length.
    """
    near = pcm16_bytes_to_float32(near_pcm)
    if not np.isfinite(ser_db):
        return near_pcm

    far = pcm16_bytes_to_float32(far_pcm)
    if near.size == 0:
        raise ValueError("near-end clip is empty")
    if far.size == 0:
        raise ValueError("far-end clip is empty")

    if far.size < near.size:
        reps = int(np.ceil(near.size / far.size))
        far = np.tile(far, reps)[: near.size]
    else:
        far = far[: near.size]

    near_rms = _rms(near)
    far_rms = _rms(far)
    if near_rms <= 0.0 or far_rms <= 0.0:
        raise ValueError("cannot mix silent near/far audio")

    # SER = near_power / far_power → scale far so near/far matches ser_db.
    target_far_rms = near_rms / (10.0 ** (ser_db / 20.0))
    mixed = near + far * (target_far_rms / far_rms)
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1.0:
        mixed = mixed / peak
    return (np.clip(mixed, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def pcm_prefix(pcm: bytes, *, seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    return pcm_onset_window(pcm, max_seconds=seconds, sample_rate=sample_rate)


def classify_channel(path: Path, tags: tuple[str, ...] = ()) -> str:
    lowered = {tag.lower().replace("-", "_") for tag in tags}
    parts = {part.lower() for part in path.parts}
    if lowered & {"satellite", "far_field"} or "satellite" in parts:
        return "satellite"
    return "laptop"


def classify_length(
    pcm: bytes,
    path: Path,
    tags: tuple[str, ...] = (),
    *,
    sample_rate: int = SAMPLE_RATE,
) -> str:
    lowered = {tag.lower() for tag in tags}
    parts = {part.lower() for part in path.parts}
    if "short" in lowered or "short" in parts:
        return "short"
    duration = (len(pcm) // 2) / float(sample_rate)
    return "short" if duration <= SHORT_DURATION_MAX else "phrase"


def scoreboard_key(label: str, length: str, channel: str) -> str:
    return f"{label}/{length}/{channel}"


def _scan_dir(root: Path, speaker: str, role: str) -> list[Clip]:
    if not root.is_dir():
        return []
    clips: list[Clip] = []
    for path in sorted(root.rglob("*.wav")):
        rel = path.relative_to(root).with_suffix("")
        clip_id = str(rel).replace("/", "_")
        clips.append(
            Clip(
                id=clip_id,
                path=path.resolve(),
                speaker=speaker,
                role=role,
                channel=classify_channel(path),
            )
        )
    return clips


def load_clips_from_dirs(clips_root: Path) -> tuple[list[Clip], list[Clip], list[Clip]]:
    owner = _scan_dir(clips_root / "owner", "owner", "near_end")
    other = _scan_dir(clips_root / "other", "other", "near_end")
    tts = _scan_dir(clips_root / "tts", "jarvis", "far_end")
    return owner, other, tts


def _tags_from_row(row: dict) -> tuple[str, ...]:
    raw = row.get("tags") or []
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    return ()


def load_clips_from_manifest(path: Path) -> tuple[list[Clip], list[Clip], list[Clip]]:
    owner: list[Clip] = []
    other: list[Clip] = []
    tts: list[Clip] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: row must be an object")
            clip_id = str(row.get("id") or "").strip()
            raw_path = row.get("path")
            speaker = str(row.get("speaker") or "").strip()
            role = str(row.get("role") or "").strip()
            if not clip_id or not isinstance(raw_path, str) or not raw_path or not speaker:
                raise ValueError(f"{path}:{line_no}: id, path, and speaker are required")
            clip_path = Path(raw_path)
            if not clip_path.is_absolute():
                clip_path = (path.parent / clip_path).resolve()
            if not clip_path.is_file():
                raise FileNotFoundError(f"{path}:{line_no}: missing audio {clip_path}")
            if not role:
                role = "far_end" if speaker == "jarvis" else "near_end"
            tags = _tags_from_row(row)
            clip = Clip(
                id=clip_id,
                path=clip_path,
                speaker=speaker,
                role=role,
                channel=classify_channel(clip_path, tags),
                tags=tags,
            )
            if role == "far_end" or speaker == "jarvis":
                tts.append(clip)
            elif speaker == "owner":
                owner.append(clip)
            else:
                other.append(clip)
    return owner, other, tts


def annotate_clip_length(clip: Clip, pcm: bytes) -> Clip:
    return replace(clip, length=classify_length(pcm, clip.path, clip.tags))


def expand_short_prefixes(
    clips: list[Clip],
    pcm_by_id: dict[str, bytes],
    *,
    seconds: float = SHORT_PREFIX_SECONDS,
) -> list[Clip]:
    extra: list[Clip] = []
    for clip in clips:
        if clip.role != "near_end" or clip.derived:
            continue
        pcm = pcm_by_id.get(clip.id)
        if not pcm:
            continue
        if classify_length(pcm, clip.path, clip.tags) == "short":
            continue
        prefix = pcm_prefix(pcm, seconds=seconds)
        if not prefix:
            continue
        prefix_id = f"{clip.id}__short_prefix"
        extra.append(
            replace(
                clip,
                id=prefix_id,
                length="short",
                derived=True,
            )
        )
        pcm_by_id[prefix_id] = prefix
    return clips + extra


def _parse_floats(raw: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token in {"inf", "+inf", "clean"}:
            values.append(float("inf"))
        else:
            values.append(float(token))
    if not values:
        raise ValueError("expected at least one float")
    return values


def _ser_label(ser_db: float) -> str:
    return "clean" if not np.isfinite(ser_db) else f"{ser_db:g}"


def _onset_or_none(seconds: float) -> float | None:
    return None if seconds <= 0.0 else seconds


def _score_pcm(verifier: EnrolledSpeakerVerifier, pcm: bytes, *, threshold: float, score: str, onset: float | None):
    return verifier.verify_pcm(
        pcm,
        threshold=threshold,
        score=score,
        max_seconds=onset,
    )


def _record_trial(
    scores: dict[str, dict[str, list[float]]],
    unscorable: dict[str, dict[str, int]],
    *,
    key: str,
    ser_key: str,
    evidence,
) -> int:
    if evidence.status is SpeakerMatchStatus.UNAVAILABLE or evidence.cosine is None:
        unscorable[key][ser_key] += 1
        return 1
    scores[key][ser_key].append(evidence.cosine)
    return 1


def _print_board(
    *,
    scores: dict[str, dict[str, list[float]]],
    unscorable: dict[str, dict[str, int]],
    thresholds: list[float],
    ser_keys: list[str],
) -> None:
    thr_headers = " ".join(f"@{t:.2f}" for t in thresholds)
    print(f"{'group':<28} {'ser':<7} {'n':>4} {'unsc':>4} {'mean':>7} {'p50':>7} {thr_headers}")
    keys = sorted({*scores, *unscorable})
    for key in keys:
        row_sers = ["clean"] if key.startswith("tts_only/") else ser_keys
        for ser_key in row_sers:
            values = scores.get(key, {}).get(ser_key) or []
            skipped = unscorable.get(key, {}).get(ser_key, 0)
            if not values and skipped == 0:
                continue
            if values:
                arr = np.asarray(values, dtype=np.float64)
                match_cols = " ".join(f"{float(np.mean(arr >= thr)):.2f}" for thr in thresholds)
                mean = f"{float(np.mean(arr)):.3f}"
                p50 = f"{float(np.median(arr)):.3f}"
                n = len(arr)
            else:
                match_cols = " ".join("--" for _ in thresholds)
                mean = "--"
                p50 = "--"
                n = 0
            print(
                f"{key:<28} {ser_key:<7} {n:>4} {skipped:>4} "
                f"{mean:>7} {p50:>7} {match_cols}"
            )


def _build_node_profile(
    *,
    profile: Path,
    model_path: Path,
    node_clip: Path,
    destination: Path,
) -> None:
    model_id = speaker_model_id(model_path)
    enrollment, nodes = load_speaker_profile_parts(profile, model_id=model_id)
    extractor = load_speaker_extractor(model_path, num_threads=1)
    embedding = l2_normalize(embed_pcm16(extractor, _load_wav_pcm(node_clip)))
    nodes = {**nodes, EVAL_NODE_ID: embedding}
    save_speaker_profile(
        destination,
        model_id=model_id,
        embeddings=enrollment,
        node_embeddings=nodes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Barge-in speaker duplex scoreboard")
    parser.add_argument("--clips-root", type=Path, default=DEFAULT_CLIPS)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional Clip Manifest v0 JSONL (default: scan clips-root folders)",
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--profile", type=Path, default=None, help="Owner .npz profile")
    parser.add_argument("--owner-id", default=None, help="Resolve profile from DATA_DIR")
    parser.add_argument(
        "--ser",
        default="inf,10,5,0,-5",
        help="Comma SER dB list; use inf/clean for near-end only",
    )
    parser.add_argument(
        "--thresholds",
        default="0.15,0.18,0.21,0.24,0.27",
        help="Comma thresholds for match-rate columns",
    )
    parser.add_argument(
        "--score-mode",
        choices=("max", "mean"),
        default="max",
        help="Gallery reduction. Production uses max.",
    )
    parser.add_argument(
        "--onset-seconds",
        type=float,
        default=0.0,
        help="Score only the first N seconds of each file (0 = full clip). "
        "Use 0.8 only as a live-barge-in ablation on speech-onset-aligned PCM.",
    )
    parser.add_argument(
        "--no-short-prefixes",
        action="store_true",
        help="Do not auto-derive 0.5s prefixes from phrase clips.",
    )
    parser.add_argument(
        "--node-clip",
        type=Path,
        default=None,
        help="Optional room-mic WAV. Prints enroll-only vs enroll+node rows.",
    )
    args = parser.parse_args()

    profile = args.profile
    if profile is None and args.owner_id:
        profile = resolve_owner_profile_path(args.owner_id)
    if profile is None or not Path(profile).is_file():
        print(
            "No speaker profile found. Pass --profile PATH or --owner-id with "
            "JARVIS_DATA_DIR pointing at the app data dir.",
            file=sys.stderr,
        )
        return 2

    if args.manifest is not None:
        owner, other, tts = load_clips_from_manifest(args.manifest)
    else:
        owner, other, tts = load_clips_from_dirs(args.clips_root)

    near_clips = owner + other
    if not near_clips:
        print(
            f"No near-end clips under {args.clips_root}/{{owner,other}} "
            "(or in --manifest). Add 16 kHz mono WAVs first.",
            file=sys.stderr,
        )
        return 2

    sers = _parse_floats(args.ser)
    thresholds = _parse_floats(args.thresholds)
    onset = _onset_or_none(args.onset_seconds)
    verifier = EnrolledSpeakerVerifier(
        owner_id=args.owner_id,
        model_path=args.model,
        profile_path=Path(profile),
        speaker_id=args.owner_id or "owner",
    )
    if not verifier.enrolled:
        print(f"Failed to load profile: {profile}", file=sys.stderr)
        return 2

    near_pcm = {clip.id: _load_wav_pcm(clip.path) for clip in near_clips}
    tts_pcm = {clip.id: _load_wav_pcm(clip.path) for clip in tts}
    near_clips = [annotate_clip_length(clip, near_pcm[clip.id]) for clip in near_clips]
    if not args.no_short_prefixes:
        near_clips = expand_short_prefixes(near_clips, near_pcm)

    scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    unscorable: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    trials = 0

    for near in near_clips:
        label = "owner" if near.speaker == "owner" else "other"
        key = scoreboard_key(label, near.length, near.channel)
        for ser_db in sers:
            if not np.isfinite(ser_db):
                evidence = _score_pcm(
                    verifier,
                    near_pcm[near.id],
                    threshold=thresholds[0],
                    score=args.score_mode,
                    onset=onset,
                )
                trials += _record_trial(scores, unscorable, key=key, ser_key=_ser_label(ser_db), evidence=evidence)
                continue
            if not tts:
                continue
            for far in tts:
                pcm = mix_near_far(near_pcm[near.id], tts_pcm[far.id], ser_db=ser_db)
                evidence = _score_pcm(
                    verifier,
                    pcm,
                    threshold=thresholds[0],
                    score=args.score_mode,
                    onset=onset,
                )
                trials += _record_trial(scores, unscorable, key=key, ser_key=_ser_label(ser_db), evidence=evidence)

    for far in tts:
        evidence = _score_pcm(
            verifier,
            tts_pcm[far.id],
            threshold=thresholds[0],
            score=args.score_mode,
            onset=onset,
        )
        trials += _record_trial(
            scores,
            unscorable,
            key=scoreboard_key("tts_only", "phrase", "laptop"),
            ser_key="clean",
            evidence=evidence,
        )

    if trials == 0:
        print("No trials (need near-end clips; duplex rows need tts/ clips).", file=sys.stderr)
        return 2

    print(
        f"profile={profile} near={len(near_clips)} tts={len(tts)} trials={trials} "
        f"score={args.score_mode} onset={args.onset_seconds:g}s"
    )
    _print_board(
        scores=scores,
        unscorable=unscorable,
        thresholds=thresholds,
        ser_keys=[_ser_label(s) for s in sers],
    )
    print(
        "\nRead as: owner match rate should stay high; other/tts_only low. "
        "unsc = too-short/empty (UNAVAILABLE), not a mismatch. "
        "Keep 0.21 unless owner still under and other-FA is still safe at a new point."
    )

    if args.node_clip is None:
        return 0
    if not args.node_clip.is_file():
        print(f"Node clip not found: {args.node_clip}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        node_profile = Path(tmp) / "owner-with-node.npz"
        _build_node_profile(
            profile=Path(profile),
            model_path=args.model,
            node_clip=args.node_clip,
            destination=node_profile,
        )
        node_verifier = EnrolledSpeakerVerifier(
            owner_id=args.owner_id,
            node_id=EVAL_NODE_ID,
            model_path=args.model,
            profile_path=node_profile,
            speaker_id=args.owner_id or "owner",
        )
        if not node_verifier.enrolled:
            print("Failed to load enroll+node profile", file=sys.stderr)
            return 2

        owner_near = [clip for clip in near_clips if clip.speaker == "owner"]
        pair_scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        pair_unsc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for near in owner_near:
            pcm = near_pcm[near.id]
            base_key = scoreboard_key("owner", near.length, near.channel)
            without = _score_pcm(
                verifier,
                pcm,
                threshold=thresholds[0],
                score=args.score_mode,
                onset=onset,
            )
            with_node = _score_pcm(
                node_verifier,
                pcm,
                threshold=thresholds[0],
                score=args.score_mode,
                onset=onset,
            )
            _record_trial(
                pair_scores,
                pair_unsc,
                key=f"{base_key}|enroll",
                ser_key="clean",
                evidence=without,
            )
            _record_trial(
                pair_scores,
                pair_unsc,
                key=f"{base_key}|enroll+node",
                ser_key="clean",
                evidence=with_node,
            )

        print(f"\nnode-clip={args.node_clip} (clean owner only, enroll vs enroll+node)")
        _print_board(
            scores=pair_scores,
            unscorable=pair_unsc,
            thresholds=thresholds,
            ser_keys=["clean"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
