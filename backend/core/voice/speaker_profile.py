"""Owner speaker-profile helpers for wakeword Stage 2b enrollment.

Stores a normalized embedding gallery under DATA_DIR. Raw enrollment PCM is never persisted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np

from core import settings
from core.voice.speaker_verifier import (
    SAMPLE_RATE,
    cosine_similarity,
    embed_pcm16,
    l2_normalize,
    load_speaker_extractor,
    load_speaker_profile,
    mean_centroid,
    pcm16_bytes_to_float32,
    save_speaker_profile,
    speaker_model_id,
)

logger = logging.getLogger(__name__)

REQUIRED_CLIP_COUNT = 5
MIN_CLIP_SECONDS = 0.8
MAX_CLIP_SECONDS = 4.0
MIN_RMS = 0.01
CLIPPING_RATIO_LIMIT = 0.01
OUTLIER_COSINE_MIN = 0.35
MAX_CLIP_BYTES = int(MAX_CLIP_SECONDS * SAMPLE_RATE * 2)

ProfileStatusValue = Literal["not_enrolled", "enrolled"]


class SpeakerProfileError(ValueError):
    def __init__(
        self,
        reason: str,
        message: str | None = None,
        *,
        clip_index: int | None = None,
    ) -> None:
        self.reason = reason
        self.clip_index = clip_index
        super().__init__(message or reason)


@dataclass(frozen=True, slots=True)
class SpeakerProfileStatus:
    status: ProfileStatusValue
    updated_at: datetime | None = None


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _owner_lock(owner_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(owner_id)
        if lock is None:
            lock = threading.Lock()
            _locks[owner_id] = lock
        return lock


def profile_dir() -> Path:
    return Path(settings.DATA_DIR) / "voice" / "speaker-profiles"


def profile_path(owner_id: str) -> Path:
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return profile_dir() / f"{digest}.npz"


def get_profile_status(owner_id: str) -> SpeakerProfileStatus:
    path = profile_path(owner_id)
    if not path.is_file():
        return SpeakerProfileStatus(status="not_enrolled")
    try:
        model_path = _model_path()
        gallery = load_speaker_profile(
            path,
            model_id=speaker_model_id(model_path),
        )
    except (OSError, ValueError):
        logger.warning("Ignoring invalid speaker profile | path=%s", path)
        return SpeakerProfileStatus(status="not_enrolled")
    if gallery.shape[0] != REQUIRED_CLIP_COUNT:
        logger.warning("Ignoring incomplete speaker profile | path=%s", path)
        return SpeakerProfileStatus(status="not_enrolled")
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return SpeakerProfileStatus(
        status="enrolled",
        updated_at=updated_at,
    )


def _pcm_stats(pcm: bytes) -> tuple[float, float, float]:
    samples = pcm16_bytes_to_float32(pcm)
    if samples.size == 0:
        return 0.0, 0.0, 0.0
    duration_s = samples.size / float(SAMPLE_RATE)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    clipping_ratio = float(np.mean(np.abs(samples) >= 0.99))
    return duration_s, rms, clipping_ratio


def validate_clips(clips: list[bytes]) -> None:
    if len(clips) != REQUIRED_CLIP_COUNT:
        raise SpeakerProfileError(
            "processing_failed",
            f"Expected {REQUIRED_CLIP_COUNT} enrollment clips, got {len(clips)}",
        )

    for index, pcm in enumerate(clips, start=1):
        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) == 0:
            raise SpeakerProfileError("too_short", f"Clip {index} is empty", clip_index=index)
        if len(pcm) % 2 != 0:
            raise SpeakerProfileError(
                "processing_failed",
                f"Clip {index} is not valid PCM16",
                clip_index=index,
            )

        duration_s, rms, clipping_ratio = _pcm_stats(bytes(pcm))
        if duration_s < MIN_CLIP_SECONDS:
            raise SpeakerProfileError(
                "too_short",
                f"Clip {index} is too short",
                clip_index=index,
            )
        if duration_s > MAX_CLIP_SECONDS:
            raise SpeakerProfileError(
                "processing_failed",
                f"Clip {index} is too long",
                clip_index=index,
            )
        if rms < MIN_RMS:
            raise SpeakerProfileError(
                "too_quiet",
                f"Clip {index} is too quiet",
                clip_index=index,
            )
        if clipping_ratio > CLIPPING_RATIO_LIMIT:
            raise SpeakerProfileError(
                "clipped",
                f"Clip {index} is clipped",
                clip_index=index,
            )


def _validate_embedding_consistency(embeddings: list[np.ndarray]) -> None:
    if len(embeddings) < 2:
        return
    normalized = [l2_normalize(embedding) for embedding in embeddings]
    for index, embedding in enumerate(normalized, start=1):
        others = [item for i, item in enumerate(normalized) if i != index - 1]
        mean_other = mean_centroid(others)
        score = cosine_similarity(embedding, mean_other)
        if score < OUTLIER_COSINE_MIN:
            raise SpeakerProfileError(
                "inconsistent_samples",
                f"Clip {index} does not match the other enrollment samples",
                clip_index=index,
            )


def _model_path() -> Path:
    raw = settings.VOICE.wakeword_speaker_model_path
    if not raw:
        raise SpeakerProfileError("processing_failed", "Speaker embedding model is not configured")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path.resolve()


def write_profile(owner_id: str, clips: list[bytes]) -> SpeakerProfileStatus:
    validate_clips(clips)
    with _owner_lock(owner_id):
        try:
            extractor = load_speaker_extractor(
                _model_path(),
                num_threads=settings.VOICE.wakeword_speaker_num_threads,
            )
            embeddings: list[np.ndarray] = []
            for index, clip in enumerate(clips, start=1):
                try:
                    embeddings.append(embed_pcm16(extractor, bytes(clip)))
                except Exception as exc:
                    raise SpeakerProfileError(
                        "processing_failed",
                        f"Clip {index} could not be processed",
                        clip_index=index,
                    ) from exc
            _validate_embedding_consistency(embeddings)
            gallery = np.stack([l2_normalize(embedding) for embedding in embeddings], axis=0)
        except SpeakerProfileError:
            raise
        except Exception as exc:
            logger.exception("Speaker profile enrollment failed for owner=%s", owner_id)
            raise SpeakerProfileError("processing_failed", str(exc)) from exc

        destination = profile_path(owner_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".speaker-profile-",
            suffix=".npz",
            dir=destination.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            os.close(fd)
            save_speaker_profile(
                tmp_path,
                model_id=speaker_model_id(_model_path()),
                embeddings=gallery,
            )
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, destination)
            os.chmod(destination, 0o600)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        logger.info(
            "Wrote speaker profile for owner=%s path=%s shape=%s",
            owner_id,
            destination,
            gallery.shape,
        )
        return get_profile_status(owner_id)


def delete_profile(owner_id: str) -> SpeakerProfileStatus:
    with _owner_lock(owner_id):
        path = profile_path(owner_id)
        if path.exists():
            path.unlink()
            logger.info("Deleted speaker profile for owner=%s path=%s", owner_id, path)
        return SpeakerProfileStatus(status="not_enrolled")


def resolve_owner_profile_path(owner_id: str | None) -> Path | None:
    if not owner_id:
        return None
    path = profile_path(owner_id)
    return path if get_profile_status(owner_id).status == "enrolled" else None
