from __future__ import annotations

import logging

from core import settings
from core.voice.speaker_verifier import EnrolledSpeakerVerifier
from core.voice.wakeword.speaker_verifier import SpeakerEmbeddingWakeVerifier
from core.voice.wakeword.types import WakeVerifier
from core.voice.wakeword.verifiers import AcceptAllWakeVerifier

logger = logging.getLogger(__name__)


def build_wake_adapter(
    verifier: EnrolledSpeakerVerifier,
    *,
    threshold: float | None = None,
) -> SpeakerEmbeddingWakeVerifier:
    """Wrap a shared enrolled-speaker verifier as the wake Stage 2b adapter."""
    return SpeakerEmbeddingWakeVerifier(
        speaker_id=verifier.owner_id or "eval_override",
        threshold=(
            settings.VOICE.wakeword_speaker_threshold if threshold is None else threshold
        ),
        verifier=verifier,
    )


def build_default_wake_verifiers(
    owner_id: str | None = None,
    *,
    speaker_verifier: EnrolledSpeakerVerifier | None = None,
) -> list[WakeVerifier]:
    """Resolve Stage 2b verifiers for an owner session.

    Order:
    1. verifier globally disabled -> AcceptAll
    2. use the shared verifier, or build one for standalone callers
    3. enrolled -> wake adapter; no profile -> AcceptAll
    """
    voice = settings.VOICE
    if not voice.wakeword_speaker_verifier_enabled:
        return [AcceptAllWakeVerifier()]

    verifier = speaker_verifier or EnrolledSpeakerVerifier(owner_id=owner_id)
    if not verifier.enrolled:
        logger.info("Wake verifier chain: accept-all (no owner speaker profile)")
        return [AcceptAllWakeVerifier()]

    logger.info("Wake verifier chain: enrolled speaker | owner=%s", verifier.owner_id)
    return [build_wake_adapter(verifier)]
