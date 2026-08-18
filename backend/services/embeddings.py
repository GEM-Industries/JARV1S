"""
Shared embedding service for JARV1S.

Used by the Memory plugin for semantic recall and the Tool Router
for per-turn plugin selection. Model is lazy-loaded on first use.
"""

import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastembed import TextEmbedding


def _default_fastembed_cache() -> str:
    from core.config import settings

    return str(settings.CACHE_DIR / "fastembed")


# Prefer explicit env override; otherwise use the app data cache directory.
_FASTEMBED_CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or _default_fastembed_cache()


class EmbeddingService:
    """
    Lightweight wrapper around fastembed's ONNX-backed TextEmbedding.
    Singleton — import `embedding_service` directly.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model_name
        self._model: "TextEmbedding | None" = None

    def _ensure_model(self) -> None:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self._model_name, cache_dir=_FASTEMBED_CACHE_DIR)

    def warmup(self) -> None:
        """Load the ONNX model once so router startup fails fast if unavailable."""
        self._ensure_model()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns a list of 384-dim float vectors."""
        self._ensure_model()
        return [e.tolist() for e in self._model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text string."""
        return self.embed([text])[0]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


embedding_service = EmbeddingService()
