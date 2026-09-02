"""Session-scoped enrolled-speaker verification.

Shared by wake Stage 2b, barge-in, and ACTIVE_IDLE follow-up identity.
Owns one Sherpa extractor and one atomically replaceable owner embedding
gallery. Raw PCM is never persisted.
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
# Below this, TitaNet embeddings are too unstable to treat as identity.
# Follow-up admission fail-opens on UNAVAILABLE instead of calling it a mismatch.
MIN_SCORE_SECONDS = 0.4
MIN_SCORE_SAMPLES = int(MIN_SCORE_SECONDS * SAMPLE_RATE)
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


def _gallery_cosines(query: np.ndarray, profile_embeddings: np.ndarray) -> np.ndarray:
    if profile_embeddings.ndim != 2 or profile_embeddings.shape[0] == 0:
        raise ValueError("Speaker profile embeddings must be a non-empty 2D gallery")
    normalized_query = l2_normalize(query)
    return np.asarray(
        [
            cosine_similarity(normalized_query, profile_embeddings[index])
            for index in range(profile_embeddings.shape[0])
        ],
        dtype=np.float32,
    )


def mean_cosine_score(query: np.ndarray, profile_embeddings: np.ndarray) -> float:
    """Average cosine similarity of a query against each gallery row."""
    return float(np.mean(_gallery_cosines(query, profile_embeddings)))


def max_cosine_score(query: np.ndarray, profile_embeddings: np.ndarray) -> float:
    """Best cosine similarity of a query against any gallery row."""
    return float(np.max(_gallery_cosines(query, profile_embeddings)))


def pcm_onset_window(
    pcm: bytes,
    *,
    max_seconds: float,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    """Keep the first ``max_seconds`` of PCM16. ``max_seconds<=0`` is a no-op."""
    if max_seconds <= 0.0 or not pcm:
        return pcm
    max_bytes = int(max_seconds * sample_rate) * 2
    return pcm[:max_bytes] if max_bytes and len(pcm) > max_bytes else pcm


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


SPEAKER_PROFILE_FORMAT_VERSION = 2
SPEAKER_PROFILE_MIN_FORMAT_VERSION = 1


def _normalize_gallery(gallery: np.ndarray, *, path: Path, embedding_dim: int | None) -> np.ndarray:
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


def _node_embeddings_from_arrays(
    node_ids: np.ndarray,
    node_embeddings: np.ndarray,
    *,
    path: Path,
    embedding_dim: int | None,
) -> dict[str, np.ndarray]:
    if node_embeddings.size == 0 or node_ids.size == 0:
        return {}
    gallery = _normalize_gallery(node_embeddings.astype(np.float32), path=path, embedding_dim=embedding_dim)
    labels = [str(item) for item in np.asarray(node_ids).reshape(-1)]
    if len(labels) != gallery.shape[0]:
        raise ValueError(f"Speaker profile node_ids length must match node_embeddings: {path}")
    packed: dict[str, np.ndarray] = {}
    for label, row in zip(labels, gallery, strict=True):
        if label:
            packed[label] = row
    return packed


def load_speaker_profile_parts(
    path: Path,
    *,
    model_id: str,
    embedding_dim: int | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Load enrollment embeddings plus optional per-node vectors."""
    if not path.is_file():
        raise FileNotFoundError(f"Speaker profile not found: {path}")
    loaded = np.load(path, allow_pickle=False)
    if not isinstance(loaded, np.lib.npyio.NpzFile):
        raise ValueError(f"Speaker profile must use the versioned .npz format: {path}")
    with loaded as payload:
        required = {"format_version", "model_id", "embeddings"}
        if not required.issubset(set(payload.files)):
            raise ValueError(f"Speaker profile has invalid fields: {path}")
        format_version = int(payload["format_version"].item())
        stored_model_id = str(payload["model_id"].item())
        gallery = payload["embeddings"].astype(np.float32)
        node_ids = payload["node_ids"] if "node_ids" in payload.files else np.array([], dtype="U1")
        node_embeddings = (
            payload["node_embeddings"].astype(np.float32)
            if "node_embeddings" in payload.files
            else np.zeros((0, gallery.shape[1] if gallery.ndim == 2 else 0), dtype=np.float32)
        )

    if (
        format_version < SPEAKER_PROFILE_MIN_FORMAT_VERSION
        or format_version > SPEAKER_PROFILE_FORMAT_VERSION
    ):
        raise ValueError(f"Unsupported speaker profile format: {format_version}")
    if stored_model_id != model_id:
        raise ValueError(
            f"Speaker profile model mismatch: expected {model_id}, got {stored_model_id}"
        )
    enrollment = _normalize_gallery(gallery, path=path, embedding_dim=embedding_dim)
    nodes = _node_embeddings_from_arrays(
        node_ids,
        node_embeddings,
        path=path,
        embedding_dim=embedding_dim if embedding_dim is not None else enrollment.shape[1],
    )
    return enrollment, nodes


def load_speaker_profile(
    path: Path,
    *,
    model_id: str,
    embedding_dim: int | None = None,
) -> np.ndarray:
    """Load a model-bound speaker embedding gallery (enrollment rows only)."""
    enrollment, _nodes = load_speaker_profile_parts(
        path,
        model_id=model_id,
        embedding_dim=embedding_dim,
    )
    return enrollment


def _pack_node_embeddings(
    node_embeddings: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    labels: list[str] = []
    rows: list[np.ndarray] = []
    for node_id, vector in node_embeddings.items():
        if not node_id:
            continue
        arr = np.asarray(vector, dtype=np.float32)
        if arr.ndim != 1 or arr.size == 0:
            raise ValueError("Node embedding must be a non-empty 1D vector")
        labels.append(node_id)
        rows.append(l2_normalize(arr))
    if not rows:
        return np.array([], dtype="U1"), np.zeros((0, 0), dtype=np.float32)
    return np.asarray(labels, dtype="U64"), np.stack(rows, axis=0)


def save_speaker_profile(
    path: Path,
    *,
    model_id: str,
    embeddings: np.ndarray,
    node_embeddings: dict[str, np.ndarray] | None = None,
) -> None:
    """Write a versioned, model-bound speaker embedding gallery."""
    gallery = np.asarray(embeddings, dtype=np.float32)
    if gallery.ndim != 2 or gallery.shape[0] == 0 or gallery.shape[1] == 0:
        raise ValueError("Speaker profile embeddings must be a non-empty 2D gallery")
    node_ids, packed_nodes = _pack_node_embeddings(node_embeddings or {})
    format_version = SPEAKER_PROFILE_FORMAT_VERSION if node_ids.size else 1
    payload: dict[str, np.ndarray] = {
        "format_version": np.array(format_version, dtype=np.int64),
        "model_id": np.array(model_id),
        "embeddings": gallery,
    }
    if node_ids.size:
        payload["node_ids"] = node_ids
        payload["node_embeddings"] = packed_nodes
    with path.open("wb") as fh:
        np.savez_compressed(fh, **payload)


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
    node_embeddings: dict[str, np.ndarray]
    speaker_id: str


class EnrolledSpeakerVerifier:
    """One extractor + atomically replaceable owner embedding gallery per session."""

    def __init__(
        self,
        *,
        owner_id: str | None = None,
        node_id: str | None = None,
        model_path: Path | None = None,
        profile_path: Path | None = None,
        enrollment_manifest: Path | None = None,
        enrollment_split: str = "enroll",
        speaker_id: str | None = None,
        num_threads: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._node_id = node_id
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
            profile_embeddings, node_embeddings = load_speaker_profile_parts(
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
            node_embeddings = {}
        else:
            raise ValueError("Either speaker profile_path or enrollment_manifest is required")

        next_state = _VerifierState(
            extractor=extractor,
            profile_embeddings=profile_embeddings,
            node_embeddings=node_embeddings,
            speaker_id=self._speaker_id,
        )
        with self._lock:
            self._state = next_state
        logger.info(
            "Speaker verifier ready | speaker_id=%s gallery=%s nodes=%s",
            self._speaker_id,
            profile_embeddings.shape,
            sorted(node_embeddings),
        )

    def verify_pcm(
        self,
        pcm: bytes,
        *,
        threshold: float,
        max_seconds: float | None = None,
        score: str = "max",
    ) -> SpeakerEvidence:
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(f"Speaker threshold must be in [0, 1], got {threshold}")
        if score not in {"max", "mean"}:
            raise ValueError(f"Speaker score must be 'max' or 'mean', got {score}")

        with self._lock:
            state = self._state

        if state is None:
            return SpeakerEvidence(status=SpeakerMatchStatus.NOT_ENROLLED)

        window = pcm_onset_window(pcm, max_seconds=max_seconds or 0.0)
        waveform = pcm16_bytes_to_float32(window)
        if waveform.size < MIN_SCORE_SAMPLES:
            return SpeakerEvidence(
                status=SpeakerMatchStatus.UNAVAILABLE,
                speaker_id=state.speaker_id,
                cosine=None,
                threshold=threshold,
            )

        try:
            embedding = l2_normalize(embed_waveform(state.extractor, waveform, SAMPLE_RATE))
            score_fn = max_cosine_score if score == "max" else mean_cosine_score
            cosine = score_fn(embedding, state.profile_embeddings)
            node_vector = state.node_embeddings.get(self._node_id or "")
            if node_vector is not None:
                cosine = max(cosine, cosine_similarity(embedding, node_vector))
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

        matched = cosine >= threshold
        return SpeakerEvidence(
            status=SpeakerMatchStatus.MATCHED if matched else SpeakerMatchStatus.MISMATCH,
            speaker_id=state.speaker_id,
            cosine=cosine,
            threshold=threshold,
        )
