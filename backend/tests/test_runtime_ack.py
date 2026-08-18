"""Isolated runtime-ack policy. Delete with core.turns.runtime_ack."""

from core.turns.runtime_ack import PHRASES, phrase_for


def test_lookup_tools_get_a_spoken_phrase():
    assert phrase_for("search.web") in PHRASES
    assert phrase_for("calendar.get_events") in PHRASES
    assert phrase_for("agents.dispatch") in PHRASES


def test_phrases_sound_spoken_not_stock():
    assert len(PHRASES) >= 12
    for phrase in PHRASES:
        assert phrase.endswith(".")
        assert "!" not in phrase
        assert len(phrase.split()) <= 5
    assert sum(", sir." in phrase for phrase in PHRASES) <= 3


def test_consecutive_lookups_do_not_repeat_until_pool_cycles():
    from core.turns import runtime_ack

    runtime_ack._unused.clear()
    drawn = [phrase_for("search.web") for _ in PHRASES]
    assert len(drawn) == len(set(drawn))
    assert set(drawn) == set(PHRASES)


def test_instant_controls_stay_quiet():
    assert phrase_for("system.set_volume") is None
    assert phrase_for("spotify.pause") is None
    assert phrase_for("smart_home.control_lights") is None


def test_missing_capability_stays_quiet():
    assert phrase_for(None) is None
    assert phrase_for("") is None
