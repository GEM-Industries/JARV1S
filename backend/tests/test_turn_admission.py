"""Pure turn-admission policy tests (barge-in + owner-gated follow-up)."""

from core.voice.speaker_verifier import SpeakerMatchStatus
from core.voice.turn_admission import (
    AdmissionAction,
    BargeInEvidence,
    Directedness,
    FollowupEvidence,
    decide_barge_in_admission,
    decide_followup_admission,
)


def test_proactive_arbitrary_side_speech_suppresses() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="okay okay i will check",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=True,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "proactive_side_speech"


def test_proactive_wake_prefixed_text_commits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="Jarvis not that one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=True,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "wake_prefix"


def test_normal_answer_endpointed_text_commits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "endpoint_has_text"


def test_normal_answer_max_wait_commits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="",
            candidate_age_s=1.0,
            endpointed=False,
            proactive=False,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "max_wait"


def test_enrolled_owner_endpoint_commits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.82,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "owner_endpoint"


def test_enrolled_owner_max_wait_commits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually stop there",
            candidate_age_s=1.0,
            endpointed=False,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.81,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "owner_max_wait"


def test_enrolled_matched_tiny_at_max_wait_suppresses() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="I",
            candidate_age_s=1.0,
            endpointed=False,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.82,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "empty_or_tiny"


def test_enrolled_mismatch_suppresses_normal_answer() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=1.0,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MISMATCH,
            speaker_cosine=0.21,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_enrolled_early_endpoint_mismatch_waits_until_max_wait() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="Wow",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MISMATCH,
            speaker_cosine=0.21,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "speaker_retry_pending"


def test_enrolled_mismatch_past_max_wait_suppresses_without_endpoint() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=1.0,
            endpointed=False,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MISMATCH,
            speaker_cosine=0.21,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_enrolled_mismatch_suppresses_proactive_side_speech() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="okay okay i will check",
            candidate_age_s=1.0,
            endpointed=True,
            proactive=True,
            speaker_status=SpeakerMatchStatus.MISMATCH,
            speaker_cosine=0.18,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_enrolled_owner_can_interrupt_proactive() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="stop reading that",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=True,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.84,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "owner_endpoint"


def test_enrolled_pending_speaker_waits() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=None,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "speaker_pending"


def test_enrolled_speaker_is_not_scored_before_endpoint_or_max_wait() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually maybe",
            candidate_age_s=0.4,
            endpointed=False,
            proactive=False,
            speaker_status=None,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "candidate_pending"


def test_enrolled_unavailable_suppresses() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=1.0,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.UNAVAILABLE,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_unavailable"


def test_enrolled_early_unavailable_waits_for_endpoint_retry() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.UNAVAILABLE,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "speaker_retry_pending"


def test_enrolled_matched_empty_waits_for_stt_before_max_wait() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="I",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.82,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "speaker_text_pending"


def test_wake_prefix_beats_speaker_mismatch() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="Jarvis stop",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=True,
            speaker_status=SpeakerMatchStatus.MISMATCH,
            speaker_cosine=0.1,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "wake_prefix"


def test_proactive_max_wait_keeps_waiting_without_endpoint() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="still talking nearby",
            candidate_age_s=1.0,
            endpointed=False,
            proactive=True,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.WAIT
    assert decision.reason == "proactive_candidate_pending"


def test_barge_in_explicit_not_directed_suppresses() -> None:
    decision = decide_barge_in_admission(
        BargeInEvidence(
            transcript="actually tell me the next one",
            candidate_age_s=0.4,
            endpointed=True,
            proactive=False,
            speaker_status=SpeakerMatchStatus.MATCHED,
            speaker_cosine=0.9,
            directedness=Directedness.NOT_DIRECTED,
        ),
        min_text_chars=4,
        min_delay_s=0.15,
        max_wait_s=0.8,
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "not_directed"


def test_followup_unenrolled_commits_nonempty_including_short_answers() -> None:
    for text in ("actually what time is it", "no", "yes", "cool"):
        decision = decide_followup_admission(
            FollowupEvidence(transcript=text, directedness=Directedness.UNKNOWN)
        )
        assert decision.action is AdmissionAction.COMMIT
        assert decision.reason == "followup_open"


def test_followup_explicit_not_directed_suppresses() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="that is so cool",
            directedness=Directedness.NOT_DIRECTED,
        )
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "not_directed"


def test_followup_empty_suppresses() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(transcript="   ", directedness=Directedness.UNKNOWN)
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "empty"


def test_followup_unenrolled_directed_still_fail_open() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="set a timer for five minutes",
            directedness=Directedness.DIRECTED,
        )
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "followup_open"


def test_followup_matched_short_yes_commits() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="yes",
            speaker_status=SpeakerMatchStatus.MATCHED,
        )
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "owner_followup"


def test_followup_mismatch_suppresses() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="what time is it",
            speaker_status=SpeakerMatchStatus.MISMATCH,
        )
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_followup_unavailable_commits() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="what time is it",
            speaker_status=SpeakerMatchStatus.UNAVAILABLE,
        )
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "followup_unscorable"


def test_followup_wake_prefix_does_not_bypass_mismatch() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="Jarvis archive my mail",
            speaker_status=SpeakerMatchStatus.MISMATCH,
        )
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_followup_identity_wins_over_directed() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="set a timer for five minutes",
            speaker_status=SpeakerMatchStatus.MISMATCH,
            directedness=Directedness.DIRECTED,
        )
    )

    assert decision.action is AdmissionAction.SUPPRESS
    assert decision.reason == "speaker_mismatch"


def test_followup_explicit_control_commits_despite_mismatch() -> None:
    decision = decide_followup_admission(
        FollowupEvidence(
            transcript="stop",
            speaker_status=SpeakerMatchStatus.MISMATCH,
        )
    )

    assert decision.action is AdmissionAction.COMMIT
    assert decision.reason == "explicit_control"
