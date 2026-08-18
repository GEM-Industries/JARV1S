"""Tests for default wake verifier factory."""

from __future__ import annotations

from types import SimpleNamespace

from core.voice.wakeword.factory import build_default_wake_verifiers
from core.voice.wakeword.speaker_verifier import SpeakerEmbeddingWakeVerifier
from core.voice.wakeword.verifiers import AcceptAllWakeVerifier


def test_build_default_wake_verifiers_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.voice.wakeword.factory.settings.VOICE.wakeword_speaker_verifier_enabled",
        False,
    )
    verifiers = build_default_wake_verifiers()
    assert len(verifiers) == 1
    assert isinstance(verifiers[0], AcceptAllWakeVerifier)


def test_build_default_wake_verifiers_accept_all_without_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.voice.wakeword.factory.settings.VOICE.wakeword_speaker_verifier_enabled",
        True,
    )
    monkeypatch.setattr(
        "core.voice.speaker_profile.resolve_owner_profile_path",
        lambda _owner_id: None,
    )

    verifiers = build_default_wake_verifiers(owner_id="owner-a")
    assert len(verifiers) == 1
    assert isinstance(verifiers[0], AcceptAllWakeVerifier)


def test_build_default_wake_verifiers_uses_shared_enrolled_verifier(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.voice.wakeword.factory.settings.VOICE.wakeword_speaker_verifier_enabled",
        True,
    )
    shared = SimpleNamespace(enrolled=True, owner_id="owner-a")

    verifiers = build_default_wake_verifiers(
        owner_id="owner-a",
        speaker_verifier=shared,
    )

    assert len(verifiers) == 1
    assert isinstance(verifiers[0], SpeakerEmbeddingWakeVerifier)
    assert verifiers[0].enrolled_verifier is shared
