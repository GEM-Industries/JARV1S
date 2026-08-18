"""Session-scoped enrolled-speaker verification.

Shared by wake Stage 2b and barge-in. Owns one Sherpa extractor and one
atomically replaceable owner embedding gallery. Raw PCM is never persisted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import numpy as np

from core import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_extractor_locks: dict[int, threading.Lock] = {}
_extractor_locks_guard = threading.Lock()


class SpeakerMatchStatus(StrEnum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    NOT_ENROLLED = "not_enrolled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SpeakerEvidence:
    status: SpeakerMatchStatus
    speaker_id: str | None = None
    cosine: float | None = None
    threshold: float | None = None

    @property
    def matched(self) -> bool:
        return self.status is SpeakerMatchStatus.MATCHED


def pcm16_bytes_to_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.array([], dtype=np.float32)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return samples / 32768.0


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0 or not np.isfinite(norm):
        raise ValueError("Cannot normalize a zero or non-finite embedding")
    return arr / norm


def mean_centroid(embeddings: list[np.ndarray]) -> np.ndarray:
    if not embeddings:
        raise ValueError("At least one embedding is required to build a centroid")
    normalized = [l2_normalize(embedding) for embedding in embeddings]
    centroid = np.mean(np.stack(normalized, axis=0), axis=0)
    return l2_normalize(centroid)


def mean_cosine_score(query: np.ndarray, profile_embeddings: np.ndarray) -> float:
    """Average cosine similarity of a query against each gallery row."""
    if profile_embeddings.ndim != 2 or profile_embeddings.shape[0] == 0:
        raise ValueError("Speaker profile embeddings must be a non-empty 2D gallery")
    normalized_query = l2_normalize(query)
    scores = [
        cosine_similarity(normalized_query, profile_embeddings[index])
        for index in range(profile_embeddings.shape[0])
    ]
    return float(np.mean(np.asarray(scores, dtype=np.float32)))


@lru_cache(maxsize=4)
def _load_speaker_extractor(model_path: Path, num_threads: int):
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(model_path),
        num_threads=num_threads,
        debug=False,
        provider="cpu",
    )
    if not config.validate():
        raise ValueError(f"Invalid Sherpa speaker extractor config: {config}")
    return sherpa_onnx.SpeakerEmbeddingExtractor(config)


def load_speaker_extractor(model_path: Path, *, num_threads: int = 1):
    """Load one immutable extractor per model/thread configuration."""
    resolved = model_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Speaker embedding model not found: {resolved}")
    return _load_speaker_extractor(resolved, num_threads)


@lru_cache(maxsize=8)
def _speaker_model_id(
    model_path: Path,
    size: int,
    modified_ns: int,
) -> str:
    del size, modified_ns  # Included in the cache key so replaced assets are re-hashed.
    digest = hashlib.sha256()
    with model_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"{model_path.stem}:sha256:{digest.hexdigest()}"


def speaker_model_id(model_path: Path) -> str:
    """Return an identity tied to the exact model artifact."""
    resolved = model_path.resolve()
    stat = resolved.stat()
    return _speaker_model_id(resolved, stat.st_size, stat.st_mtime_ns)


def _extractor_lock(extractor: object) -> threading.Lock:
    key = id(extractor)
    with _extractor_locks_guard:
        lock = _extractor_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _extractor_locks[key] = lock
        return lock


def embed_waveform(extractor, waveform: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    with _extractor_lock(extractor):
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=waveform)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise RuntimeError("Speaker embedding extractor not ready for input")
        embedding = np.array(extractor.compute(stream), dtype=np.float32)
    if embedding.ndim != 1 or embedding.size == 0 or not np.isfinite(embedding).all():
        raise ValueError("Speaker embedding must be a finite 1D vector")
    return embedding


def embed_pcm16(extractor, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    waveform = pcm16_bytes_to_float32(pcm)
    if waveform.size == 0:
        raise ValueError("Cannot embed empty PCM audio")
    return embed_waveform(extractor, waveform, sample_rate)


def embed_wav(extractor, path: Path) -> np.ndarray:
    import soundfile as sf

    samples, sample_rate = sf.read(str(path), always_2d=True, dtype="float32")
    waveform = np.ascontiguousarray(samples[:, 0])
    return embed_waveform(extractor, waveform, sample_rate)


SPEAKER_PROFILE_FORMAT_VERSION = 1


def load_speaker_profile(
    path: Path,
    *,
    model_id: str,
    embedding_dim: int | None = None,
) -> np.ndarray:
    """Load a model-bound speaker embedding gallery."""
    if not path.is_file():
        raise FileNotFoundError(f"Speaker profile not found: {path}")
    loaded = np.load(path, allow_pickle=False)
    if not isinstance(loaded, np.lib.npyio.NpzFile):
        raise ValueError(f"Speaker profile must use the versioned .npz format: {path}")
    with loaded as payload:
        required = {"format_version", "model_id", "embeddings"}
        if set(payload.files) != required:
            raise ValueError(f"Speaker profile has invalid fields: {path}")
        format_version = int(payload["format_version"].item())
        stored_model_id = str(payload["model_id"].item())
        gallery = payload["embeddings"].astype(np.float32)

    if format_version != SPEAKER_PROFILE_FORMAT_VERSION:
        raise ValueError(f"Unsupported speaker profile format: {format_version}")
    if stored_model_id != model_id:
        raise ValueError(
            f"Speaker profile model mismatch: expected {model_id}, got {stored_model_id}"
        )
    if gallery.ndim != 2:
        raise ValueError(f"Speaker profile embeddings must be 2D: {path}")
    if gallery.shape[0] == 0 or gallery.shape[1] == 0:
        raise ValueError(f"Speaker profile is empty: {path}")
    if embedding_dim is not None and gallery.shape[1] != embedding_dim:
        raise ValueError(
            f"Speaker profile dimension mismatch: expected {embedding_dim}, got {gallery.shape[1]}"
        )
    if not np.isfinite(gallery).all():
        raise ValueError(f"Speaker profile contains non-finite values: {path}")
    return np.stack([l2_normalize(row) for row in gallery], axis=0)


def save_speaker_profile(path: Path, *, model_id: str, embeddings: np.ndarray) -> None:
    """Write a versioned, model-bound speaker embedding gallery."""
    gallery = np.asarray(embeddings, dtype=np.float32)
    if gallery.ndim != 2 or gallery.shape[0] == 0 or gallery.shape[1] == 0:
        raise ValueError("Speaker profile embeddings must be a non-empty 2D gallery")
    with path.open("wb") as fh:
        np.savez_compressed(
            fh,
            format_version=np.array(SPEAKER_PROFILE_FORMAT_VERSION, dtype=np.int64),
            model_id=np.array(model_id),
            embeddings=gallery,
        )


def load_enrollment_paths(
    manifest_path: Path,
    *,
    split: str = "enroll",
) -> list[Path]:
    """Load WAV paths from a speaker enrollment JSONL manifest."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Speaker enrollment manifest not found: {manifest_path}")

    paths: list[Path] = []
    with manifest_path.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{manifest_path}:{line_no}: manifest row must be an object")

            row_split = str(row.get("split") or "enroll").strip()
            if row_split != split:
                continue

            raw_path = row.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"{manifest_path}:{line_no}: missing string field 'path'")

            clip_path = Path(raw_path)
            if not clip_path.is_absolute():
                clip_path = (manifest_path.parent / clip_path).resolve()
            if not clip_path.exists():
                raise FileNotFoundError(f"{manifest_path}:{line_no}: enrollment audio not found: {clip_path}")
            paths.append(clip_path)

    if not paths:
        raise ValueError(f"No enrollment clips with split={split!r} in {manifest_path}")
    return paths


def build_profile_from_manifest(
    extractor,
    manifest_path: Path,
    *,
    split: str = "enroll",
) -> np.ndarray:
    enrollment_paths = load_enrollment_paths(manifest_path, split=split)
    embeddings = [l2_normalize(embed_wav(extractor, path)) for path in enrollment_paths]
    return np.stack(embeddings, axis=0)


def _resolve_model_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return (Path(__file__).resolve().parents[2] / path).resolve()


@dataclass(frozen=True, slots=True)
class _VerifierState:
    extractor: object
    profile_embeddings: np.ndarray
    speaker_id: str


class EnrolledSpeakerVerifier:
    """One extractor + atomically replaceable owner embedding gallery per session."""

    def __init__(
        self,
        *,
        owner_id: str | None = None,
        model_path: Path | None = None,
        profile_path: Path | None = None,
        enrollment_manifest: Path | None = None,
        enrollment_split: str = "enroll",
        speaker_id: str | None = None,
        num_threads: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._speaker_id = speaker_id or owner_id or "eval_override"
        self._num_threads = (
            num_threads
            if num_threads is not None
            else settings.VOICE.wakeword_speaker_num_threads
        )
        self._enabled = (
            settings.VOICE.wakeword_speaker_verifier_enabled if enabled is None else enabled
        )
        self._model_path = model_path or _resolve_model_path(
            settings.VOICE.wakeword_speaker_model_path
        )
        self._lock = threading.RLock()
        self._state: _VerifierState | None = None

        if not self._enabled:
            return

        if profile_path is not None or enrollment_manifest is not None:
            self._load_state(
                profile_path=profile_path,
                enrollment_manifest=enrollment_manifest,
                enrollment_split=enrollment_split,
            )
            return

        self.reload_profile()

    @property
    def enrolled(self) -> bool:
        with self._lock:
            return self._state is not None

    @property
    def owner_id(self) -> str | None:
        return self._owner_id

    def reload_profile(self) -> bool:
        """Reload owner profile from DATA_DIR. Returns True when enrolled after reload."""
        if not self._enabled:
            with self._lock:
                self._state = None
            return False

        # Lazy import avoids speaker_profile <-> speaker_verifier cycle at module load.
        from core.voice.speaker_profile import resolve_owner_profile_path

        override = settings.VOICE.wakeword_speaker_profile_path
        override_path = _resolve_model_path(override) if override else None
        if override_path is not None and not override_path.is_file():
            logger.warning(
                "Speaker verifier override profile missing (%s); falling back to owner profile",
                override_path,
            )
            override_path = None

        owner_profile = resolve_owner_profile_path(self._owner_id)
        profile_path = override_path or owner_profile
        if profile_path is None:
            with self._lock:
                self._state = None
            logger.info(
                "Speaker verifier not enrolled | owner=%s",
                self._owner_id or "none",
            )
            return False

        self._load_state(profile_path=profile_path)
        return True

    def _load_state(
        self,
        *,
        profile_path: Path | None = None,
        enrollment_manifest: Path | None = None,
        enrollment_split: str = "enroll",
    ) -> None:
        if self._model_path is None:
            raise ValueError("wakeword_speaker_model_path is required when speaker verifier is enabled")
        model_id = speaker_model_id(self._model_path)

        with self._lock:
            current = self._state
        extractor = (
            current.extractor
            if current is not None
            else load_speaker_extractor(self._model_path, num_threads=self._num_threads)
        )
        if profile_path is not None:
            embedding_dim = getattr(extractor, "dim", None)
            profile_embeddings = load_speaker_profile(
                profile_path,
                model_id=model_id,
                embedding_dim=embedding_dim if isinstance(embedding_dim, int) else None,
            )
        elif enrollment_manifest is not None:
            profile_embeddings = build_profile_from_manifest(
                extractor,
                enrollment_manifest,
                split=enrollment_split,
            )
        else:
            raise ValueError("Either speaker profile_path or enrollment_manifest is required")

        next_state = _VerifierState(
            extractor=extractor,
            profile_embeddings=profile_embeddings,
            speaker_id=self._speaker_id,
        )
        with self._lock:
            self._state = next_state
        logger.info(
            "Speaker verifier ready | speaker_id=%s gallery=%s",
            self._speaker_id,
            profile_embeddings.shape,
        )

    def verify_pcm(self, pcm: bytes, *, threshold: float) -> SpeakerEvidence:
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(f"Speaker threshold must be in [0, 1], got {threshold}")

        with self._lock:
            state = self._state

        if state is None:
            return SpeakerEvidence(status=SpeakerMatchStatus.NOT_ENROLLED)

        if not pcm:
            return SpeakerEvidence(
                status=SpeakerMatchStatus.MISMATCH,
                speaker_id=state.speaker_id,
                cosine=0.0,
                threshold=threshold,
            )

        try:
            waveform = pcm16_bytes_to_float32(pcm)
            if waveform.size == 0:
                return SpeakerEvidence(
                    status=SpeakerMatchStatus.MISMATCH,
                    speaker_id=state.speaker_id,
                    cosine=0.0,
                    threshold=threshold,
                )
            embedding = l2_normalize(embed_waveform(state.extractor, waveform, SAMPLE_RATE))
            score = mean_cosine_score(embedding, state.profile_embeddings)
        except Exception:
            logger.exception(
                "Speaker verification failed | speaker_id=%s",
                state.speaker_id,
            )
            return SpeakerEvidence(
                status=SpeakerMatchStatus.UNAVAILABLE,
                speaker_id=state.speaker_id,
                threshold=threshold,
            )

        matched = score >= threshold
        return SpeakerEvidence(
            status=SpeakerMatchStatus.MATCHED if matched else SpeakerMatchStatus.MISMATCH,
            speaker_id=state.speaker_id,
            cosine=score,
            threshold=threshold,
        )
