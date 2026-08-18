from core.prompts.builder import PromptBuilder, PromptMode


def _runtime_context(context: dict) -> str:
    return PromptBuilder.__new__(PromptBuilder)._format_runtime_context(context)


def test_voice_runtime_context_requires_tool_call_not_spoken_plan():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "local_time": "Thursday, 2026-05-21 14:54",
        }
    )

    assert "[CURRENT VOICE TURN]" in prompt
    assert "Spoken claims are not tool results" in prompt
    assert "A new state change, repeat, or contradiction still needs a tool call" in prompt


def test_text_runtime_context_does_not_include_voice_turn_reminder():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "text",
        }
    )

    assert "[OUTPUT FORMAT] This is a text session." in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


def test_background_runtime_context_does_not_include_voice_turn_reminder():
    prompt = _runtime_context(
        {
            "source": "background",
            "modality": "voice",
        }
    )

    assert "[EXECUTION MODE] You are a background agent." in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


def test_full_prompt_includes_silent_reply_contract():
    builder = PromptBuilder()
    prompt = builder.build(mode=PromptMode.FULL)
    text = str(prompt)
    assert "SILENT REPLY" in text
    assert "respond with exactly NO_REPLY" in text


def test_background_prompt_omits_silent_reply_contract():
    builder = PromptBuilder()
    prompt = builder.build(mode=PromptMode.BACKGROUND)
    text = str(prompt)
    assert "SILENT REPLY" not in text


def test_system_runtime_context_tell_uses_proactive_alert():
    prompt = _runtime_context(
        {
            "source": "system",
            "modality": "voice",
            "trigger_decision": "tell",
        }
    )

    assert "[IMPORTANT] This is a PROACTIVE ALERT." in prompt
    assert "[DECISION EVALUATION]" not in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


def test_system_runtime_context_offer_uses_decision_evaluation():
    prompt = _runtime_context(
        {
            "source": "system",
            "modality": "voice",
            "trigger_decision": "offer",
        }
    )

    assert "[DECISION EVALUATION]" in prompt
    assert "[IMPORTANT] This is a PROACTIVE ALERT." not in prompt
    assert "Do not announce preemptively" in prompt


def test_system_runtime_context_defaults_to_proactive_alert():
    prompt = _runtime_context(
        {
            "source": "system",
            "modality": "voice",
        }
    )

    assert "[IMPORTANT] This is a PROACTIVE ALERT." in prompt


def test_runtime_context_marks_current_time_as_authoritative():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "timezone": "Australia/Sydney",
            "local_time": "Saturday, 2026-06-13 14:22",
            "local_time_clock": "2:22 PM",
            "local_time_iso": "2026-06-13T14:22:29+10:00",
            "utc_time": "2026-06-13T04:22:29+00:00",
        }
    )

    assert "Authoritative Current Local Time: Saturday, 2026-06-13 14:22 (2:22 PM; 2026-06-13T14:22:29+10:00)" in prompt
    assert "User Timezone: Australia/Sydney" in prompt
    assert "[CURRENT TIME]" in prompt


def test_current_time_rule_sits_after_history_reminder():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "has_history": True,
            "timezone": "Australia/Sydney",
            "local_time": "Saturday, 2026-06-13 16:00",
            "local_time_clock": "4:00 PM",
            "local_time_iso": "2026-06-13T16:00:54+10:00",
            "utc_time": "2026-06-13T06:00:54+00:00",
        }
    )

    assert prompt.index("[CONVERSATION]") < prompt.index("[CURRENT TIME]")


def test_runtime_context_formats_geo_and_room_without_raw_dicts():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "location": {
                "latitude": -33.86,
                "longitude": 151.2,
                "source": "gps",
            },
            "location_ref": {
                "provider": "home_assistant",
                "room_name": "Bedroom",
                "ha_area_id": "area-bedroom",
            },
            "node_id": "jarvis-satellite-1",
            "device_kind": "satellite",
        }
    )

    assert "Geographic Position Available: yes (source=device" in prompt
    assert "resolve omitted location inputs automatically" in prompt
    assert "Speaking From Room: Bedroom (provider=home_assistant)" in prompt
    assert "Node Id: jarvis-satellite-1" in prompt
    assert "Client Surface:" not in prompt
    assert "Location:" not in prompt
    assert "Location Ref:" not in prompt
    assert "'latitude': -33.86" not in prompt


def test_runtime_context_does_not_report_stale_phone_gps_as_available():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "location": {
                "latitude": -33.86,
                "longitude": 151.2,
                "source": "gps",
                "captured_at": "2020-01-01T00:00:00+00:00",
            },
            "device_kind": "phone",
        }
    )

    assert "Geographic Position Available: no" in prompt


def test_chat_only_omits_capability_interface():
    prompt = str(PromptBuilder().build(action_capable=False, runtime_context={"source": "user"}))
    assert "You act through the tools offered this turn" not in prompt
    assert "search_tools" not in prompt


def test_action_capable_includes_capability_interface():
    prompt = str(PromptBuilder().build(
        action_capable=True,
        runtime_context={"source": "user", "modality": "voice"},
    ))
    assert "You act through the tools offered this turn" in prompt
