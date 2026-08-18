from core.triggers.priority import (
    SOUND_BY_LEVEL,
    attention_policy_fields,
    breaks_through,
    commitment_attention_mongo_filter,
)


def test_breaks_through_quiet_requires_urgent_or_higher():
    assert not breaks_through("quiet", "normal")
    assert breaks_through("quiet", "urgent")
    assert breaks_through("quiet", "critical")
    assert not breaks_through("paused", "critical")


def test_attention_policy_fields_derive_sound_from_importance():
    assert attention_policy_fields("normal") == {
        "level": "normal",
        "requires_ack": False,
        "sound": SOUND_BY_LEVEL["normal"],
    }
    assert attention_policy_fields("urgent") == {
        "level": "urgent",
        "requires_ack": False,
        "sound": SOUND_BY_LEVEL["urgent"],
    }
    assert attention_policy_fields("critical", requires_ack=True) == {
        "level": "critical",
        "requires_ack": True,
        "sound": "alarm",
    }


def test_commitment_attention_filter_uses_priority_axis():
    assert commitment_attention_mongo_filter() == {
        "$or": [
            {"attention_snapshot.requires_ack": True},
            {"attention_snapshot.level": {"$in": ["urgent", "critical"]}},
        ],
    }


