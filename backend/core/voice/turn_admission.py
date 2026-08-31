"""Pre-agent turn admission policy.

VAD/EOU own acoustic endpointing. This module decides whether a finalized or
barge-in candidate utterance should become a user turn. Barge-in is enforced
with a candidate WAIT loop. Follow-up is a terminal decision: owner-gated when
enrolled, fail-open when not. Directedness remains a future DDSD seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from core.voice.local_commands import (
    LocalVoiceCommand,
    has_wake_prefix,
    resolve_local_command,
)
from core.voice.speaker_verifier import SpeakerMatchStatus

AdmissionSource = Literal["wake", "barge_in", "followup", "push_to_talk"]


class AdmissionAction(StrEnum):
    WAIT = "wait"
    COMMIT = "commit"
    SUPPRESS = "suppress"


class Directedness(StrEnum):
    UNKNOWN = "unknown"
    DIRECTED = "directed"
    NOT_DIRECTED = "not_directed"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    action: AdmissionAction
    reason: str


@dataclass(frozen=True, slots=True)
class BargeInEvidence:
    transcript: str
    candidate_age_s: float
    endpointed: bool
    proactive: bool
    # NOT_ENROLLED (default) = no owner profile / legacy policy.
    # None = enrolled and score still pending.
    speaker_status: SpeakerMatchStatus | None = SpeakerMatchStatus.NOT_ENROLLED
    speaker_cosine: float | None = None
    directedness: Directedness = Directedness.UNKNOWN


@dataclass(frozen=True, slots=True)
class FollowupEvidence:
    """Finalized follow-up transcript. Handler always supplies a concrete speaker status."""

    transcript: str
    speaker_status: SpeakerMatchStatus = SpeakerMatchStatus.NOT_ENROLLED
    directedness: Directedness = Directedness.UNKNOWN


# Deliberately public safety controls: these may interrupt before speaker
# verification so anyone nearby can stop or acknowledge playback.
_PUBLIC_BARGE_CONTROLS = frozenset({
    LocalVoiceCommand.STOP,
    LocalVoiceCommand.ACKNOWLEDGE,
    LocalVoiceCommand.SNOOZE,
})


def is_explicit_barge_control(text: str) -> bool:
    return resolve_local_command(text) in _PUBLIC_BARGE_CONTROLS


def decide_barge_in_admission(
    evidence: BargeInEvidence,
    *,
    min_text_chars: int,
    min_delay_s: float,
    max_wait_s: float,
) -> AdmissionDecision:
    text = " ".join(evidence.transcript.split())
    text_chars = len(text)
    past_max_wait = evidence.candidate_age_s >= max_wait_s

    # Wake-prefix and emergency controls are intentionally public pre-commits.
    if has_wake_prefix(text):
        return AdmissionDecision(AdmissionAction.COMMIT, "wake_prefix")
    if is_explicit_barge_control(text):
        return AdmissionDecision(AdmissionAction.COMMIT, "explicit_control")

    if evidence.directedness is Directedness.NOT_DIRECTED:
        return AdmissionDecision(AdmissionAction.SUPPRESS, "not_directed")

    if not evidence.endpointed and evidence.candidate_age_s < min_delay_s:
        return AdmissionDecision(AdmissionAction.WAIT, "candidate_min_delay")

    speaker_status = evidence.speaker_status

    if speaker_status is not SpeakerMatchStatus.NOT_ENROLLED:
        if speaker_status is None:
            # Score once an endpoint arrives, or when max-wait forces a decision.
            terminal = evidence.endpointed or past_max_wait
            reason = "speaker_pending" if terminal else "candidate_pending"
            return AdmissionDecision(AdmissionAction.WAIT, reason)
        # Negatives are never terminal before max-wait. Short VAD endpoints during
        # TTS must not clear the candidate and restart the utterance.
        if speaker_status in {
            SpeakerMatchStatus.MISMATCH,
            SpeakerMatchStatus.UNAVAILABLE,
        }:
            if not past_max_wait:
                return AdmissionDecision(AdmissionAction.WAIT, "speaker_retry_pending")
            reason = (
                "speaker_mismatch"
                if speaker_status is SpeakerMatchStatus.MISMATCH
                else "speaker_unavailable"
            )
            return AdmissionDecision(AdmissionAction.SUPPRESS, reason)
        if speaker_status is SpeakerMatchStatus.MATCHED:
            # Match alone is not enough: soft-wait after endpoint re-resolves with
            # endpointed=False, so tiny text must stay age-gated on every path.
            if text_chars < min_text_chars:
                if not past_max_wait:
                    return AdmissionDecision(
                        AdmissionAction.WAIT, "speaker_text_pending"
                    )
                return AdmissionDecision(AdmissionAction.SUPPRESS, "empty_or_tiny")
            if evidence.endpointed:
                return AdmissionDecision(AdmissionAction.COMMIT, "owner_endpoint")
            if past_max_wait:
                return AdmissionDecision(AdmissionAction.COMMIT, "owner_max_wait")
            return AdmissionDecision(AdmissionAction.WAIT, "candidate_pending")
        return AdmissionDecision(AdmissionAction.WAIT, "speaker_pending")

    if evidence.proactive:
        if evidence.endpointed:
            if text_chars < min_text_chars:
                return AdmissionDecision(
                    AdmissionAction.SUPPRESS, "proactive_empty_or_tiny"
                )
            return AdmissionDecision(AdmissionAction.SUPPRESS, "proactive_side_speech")
        return AdmissionDecision(AdmissionAction.WAIT, "proactive_candidate_pending")

    if evidence.endpointed:
        if text_chars < min_text_chars:
            return AdmissionDecision(AdmissionAction.SUPPRESS, "empty_or_tiny")
        return AdmissionDecision(AdmissionAction.COMMIT, "endpoint_has_text")

    if past_max_wait:
        return AdmissionDecision(AdmissionAction.COMMIT, "max_wait")

    return AdmissionDecision(AdmissionAction.WAIT, "candidate_pending")


def decide_followup_admission(evidence: FollowupEvidence) -> AdmissionDecision:
    """Terminal follow-up admission. Owner-gated when enrolled; fail-open otherwise.

    Wake-prefix is not an identity bypass here. Barge-in keeps it public so anyone
    can interrupt playback; local halt commands already run before this function.
    """
    text = " ".join(evidence.transcript.split())
    if is_explicit_barge_control(text):
        return AdmissionDecision(AdmissionAction.COMMIT, "explicit_control")
    if evidence.directedness is Directedness.NOT_DIRECTED:
        return AdmissionDecision(AdmissionAction.SUPPRESS, "not_directed")
    if not text:
        return AdmissionDecision(AdmissionAction.SUPPRESS, "empty")
    if evidence.speaker_status is SpeakerMatchStatus.NOT_ENROLLED:
        return AdmissionDecision(AdmissionAction.COMMIT, "followup_open")
    if evidence.speaker_status is SpeakerMatchStatus.MATCHED:
        return AdmissionDecision(AdmissionAction.COMMIT, "owner_followup")
    if evidence.speaker_status is SpeakerMatchStatus.UNAVAILABLE:
        return AdmissionDecision(AdmissionAction.COMMIT, "followup_unscorable")
    return AdmissionDecision(AdmissionAction.SUPPRESS, "speaker_mismatch")
