"""Offline barge-in speaker scoreboard.

Mix near-end clips with TTS at several SER levels, score with
EnrolledSpeakerVerifier, and print match rates vs threshold.

Run from backend/:

    uv run python tools/eval_barge_in_speaker.py --owner-id geoff
    uv run python tools/eval_barge_in_speaker.py --profile /path/to/owner.npz
    uv run python tools/eval_barge_in_speaker.py --manifest ../training/voice/barge_in/manifests/scoreboard.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.voice.speaker_profile import resolve_owner_profile_path  # noqa: E402
from core.voice.speaker_verifier import (  # noqa: E402
    EnrolledSpeakerVerifier,
    pcm16_bytes_to_float32,
)

DEFAULT_CLIPS = REPO_ROOT / "training/voice/clips"
DEFAULT_MODEL = BACKEND_DIR / "resources/models/speaker/nemo_en_titanet_small.onnx"
DEFAULT_SERS = (float("inf"), 10.0, 5.0, 0.0, -5.0)
DEFAULT_THRESHOLDS = (0.15, 0.18, 0.21, 0.24, 0.27)


@dataclass(frozen=True)
class Clip:
    id: str
    path: Path
    speaker: str
    role: str


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


def _scan_dir(root: Path, speaker: str, role: str) -> list[Clip]:
    if not root.is_dir():
        return []
    clips: list[Clip] = []
    for path in sorted(root.glob("*.wav")):
        clips.append(Clip(id=path.stem, path=path.resolve(), speaker=speaker, role=role))
    return clips


def load_clips_from_dirs(clips_root: Path) -> tuple[list[Clip], list[Clip], list[Clip]]:
    owner = _scan_dir(clips_root / "owner", "owner", "near_end")
    other = _scan_dir(clips_root / "other", "other", "near_end")
    tts = _scan_dir(clips_root / "tts", "jarvis", "far_end")
    return owner, other, tts


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
            clip = Clip(id=clip_id, path=clip_path, speaker=speaker, role=role)
            if role == "far_end" or speaker == "jarvis":
                tts.append(clip)
            elif speaker == "owner":
                owner.append(clip)
            else:
                other.append(clip)
    return owner, other, tts


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
    verifier = EnrolledSpeakerVerifier(
        owner_id=args.owner_id,
        model_path=args.model,
        profile_path=Path(profile),
        speaker_id=args.owner_id or "owner",
    )
    if not verifier.enrolled:
        print(f"Failed to load profile: {profile}", file=sys.stderr)
        return 2

    # label -> ser -> list[cosine]
    scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    trials = 0

    near_pcm = {clip.id: _load_wav_pcm(clip.path) for clip in near_clips}
    tts_pcm = {clip.id: _load_wav_pcm(clip.path) for clip in tts}

    for near in near_clips:
        label = "owner" if near.speaker == "owner" else "other"
        for ser_db in sers:
            if not np.isfinite(ser_db):
                pcm = near_pcm[near.id]
                evidence = verifier.verify_pcm(pcm, threshold=thresholds[0])
                if evidence.cosine is not None:
                    scores[label][_ser_label(ser_db)].append(evidence.cosine)
                    trials += 1
                continue
            if not tts:
                continue
            for far in tts:
                pcm = mix_near_far(near_pcm[near.id], tts_pcm[far.id], ser_db=ser_db)
                evidence = verifier.verify_pcm(pcm, threshold=thresholds[0])
                if evidence.cosine is not None:
                    scores[label][_ser_label(ser_db)].append(evidence.cosine)
                    trials += 1

    # TTS-only baseline: far-end alone should not look like the owner.
    for far in tts:
        evidence = verifier.verify_pcm(tts_pcm[far.id], threshold=thresholds[0])
        if evidence.cosine is not None:
            scores["tts_only"]["clean"].append(evidence.cosine)
            trials += 1

    if trials == 0:
        print("No scorable trials (need near-end clips; duplex rows need tts/ clips).", file=sys.stderr)
        return 2

    thr_headers = " ".join(f"@{t:.2f}" for t in thresholds)
    print(
        f"profile={profile} near={len(near_clips)} tts={len(tts)} trials={trials}"
    )
    print(f"{'label':<10} {'ser':<7} {'n':>4} {'mean':>7} {'p50':>7} {thr_headers}")
    ser_keys = [_ser_label(s) for s in sers]
    for label in ("owner", "other", "tts_only"):
        keys = ["clean"] if label == "tts_only" else ser_keys
        for ser_key in keys:
            values = scores.get(label, {}).get(ser_key)
            if not values:
                continue
            arr = np.asarray(values, dtype=np.float64)
            match_cols = " ".join(
                f"{float(np.mean(arr >= thr)):.2f}" for thr in thresholds
            )
            print(
                f"{label:<10} {ser_key:<7} {len(arr):>4} "
                f"{float(np.mean(arr)):.3f} {float(np.median(arr)):.3f} {match_cols}"
            )

    print(
        "\nRead as: owner match rate should stay high; other/tts_only low. "
        "Pick the lowest threshold that keeps other FA acceptable under duplex SER."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
