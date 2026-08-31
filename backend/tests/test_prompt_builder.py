from pathlib import Path

import pytest

from core.home import HomeSnapshot, SkillMeta
from core.prompts.background import BackgroundPromptBuilder, build_background_context
from core.prompts.builder import PromptBuilder, format_runtime_context


@pytest.fixture(autouse=True)
def _empty_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.prompts.builder.load_home_snapshot",
        HomeSnapshot.empty,
    )
    monkeypatch.setattr(
        "core.prompts.background.load_home_snapshot",
        HomeSnapshot.empty,
    )


def _runtime_context(context: dict) -> str:
    return format_runtime_context(context)


def test_voice_runtime_context_requires_tool_call_not_spoken_plan():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "voice",
            "local_time": "Thursday, 2026-05-21 14:54",
        }
    )

    assert "[CURRENT VOICE TURN]" in prompt
    assert "This text is a speech transcript" in prompt
    assert "A new state change, repeated request, failed requested state" in prompt
    assert "confirming tool result from this response" in prompt


def test_text_runtime_context_does_not_include_voice_turn_reminder():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "text",
        }
    )

    assert "Input Modality: text" in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


def test_background_runtime_context_has_no_interactive_reminder():
    prompt = _runtime_context(
        {
            "source": "background",
            "modality": "voice",
        }
    )

    assert "Input Modality: voice" in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


def test_product_prompt_contains_cross_cutting_contracts():
    prompt = PromptBuilder().build()
    assert "Never claim an action ran or succeeded" in prompt.static
    assert "Use tools for lookups, diagnoses, and current external state" in prompt.static
    assert "A requested state change requires a tool call" in prompt.static
    assert "blocked pending approval means the action has not executed" in prompt.static
    assert "Use `recall` only for older topics" in prompt.static
    assert "Domain tools attach their own widgets" in prompt.static
    assert "For a system alert, deliver the message directly" in prompt.static
    assert "respond with exactly NO_REPLY" in prompt.static
    assert "For voice output" in prompt.static


def test_system_runtime_context_does_not_duplicate_turn_instructions():
    prompt = _runtime_context(
        {
            "source": "system",
            "modality": "voice",
            "trigger_decision": "offer",
        }
    )

    assert "Input Modality: voice" in prompt
    assert "[DECISION EVALUATION]" not in prompt
    assert "[IMPORTANT]" not in prompt
    assert "[CURRENT VOICE TURN]" not in prompt


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


def test_product_prompt_handles_optional_tool_surface():
    prompt = str(PromptBuilder().build(runtime_context={"source": "user"}))
    assert "When tools are offered, use only those tools" in prompt
    assert "If a needed capability is missing and `search_tools` is offered" in prompt


def test_runtime_context_omits_empty_open_work_block():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "text",
            "open_work_block": "",
        }
    )

    assert "[OPEN WORK]" not in prompt
    assert "Open Work Block:" not in prompt


def test_runtime_context_renders_open_work_before_conversation():
    prompt = _runtime_context(
        {
            "source": "user",
            "modality": "text",
            "has_history": True,
            "open_work_block": (
                "[OPEN WORK]\n"
                "- 2713 review — completed, open, ~/dev/aetheron-connect-v2"
            ),
        }
    )

    assert "[OPEN WORK]" in prompt
    assert "2713 review" in prompt
    assert "Open Work Block:" not in prompt
    assert prompt.index("[OPEN WORK]") < prompt.index("[CONVERSATION]")


@pytest.mark.asyncio
async def test_build_background_context_uses_dispatch_timezone(monkeypatch, tool_context):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "plugins.profile.get_profile_block",
        AsyncMock(return_value=""),
    )

    with tool_context(timezone="Australia/Sydney"):
        ctx = await build_background_context("geoff")

    assert ctx["timezone"] == "Australia/Sydney"


def test_user_home_and_profile_are_dynamic_not_static(monkeypatch: pytest.MonkeyPatch):
    skill_path = Path("/tmp/jarvis-home/skills/briefing/SKILL.md")
    snapshot = HomeSnapshot(
        root=Path("/tmp/jarvis-home"),
        prompt="Call me Geoff. Prefer short answers.",
        skills=(
            SkillMeta(
                name="briefing",
                description="Morning briefing.",
                compatibility=None,
                path=skill_path,
            ),
        ),
        issues=(),
    )
    monkeypatch.setattr("core.prompts.builder.load_home_snapshot", lambda: snapshot)
    prompt = PromptBuilder().build(
        user_profile="Geoff likes espresso.",
        runtime_context={"source": "user", "modality": "text"},
    )

    assert "Geoff likes espresso." not in prompt.static
    assert "Call me Geoff." not in prompt.static
    assert "Prefer short answers." not in prompt.static
    assert "Geoff likes espresso." in prompt.dynamic
    assert "Call me Geoff." in prompt.dynamic
    assert "Prefer short answers." in prompt.dynamic
    assert str(skill_path) in prompt.dynamic
    assert "files.read" in prompt.dynamic
    assert "Do not inject this body" not in str(prompt)
    assert prompt.dynamic.index("[USER PROMPT]") < prompt.dynamic.index("Geoff likes espresso.")
    assert prompt.dynamic.index("Geoff likes espresso.") < prompt.dynamic.index("[RUNTIME CONTEXT]")


def test_background_prompt_includes_skills_without_home_personality_or_interactive_rules(
    monkeypatch: pytest.MonkeyPatch,
):
    snapshot = HomeSnapshot(
        root=Path("/tmp/jarvis-home"),
        prompt="Call me Geoff. Address me as sir.",
        skills=(
            SkillMeta(
                name="briefing",
                description="Morning briefing.",
                compatibility=None,
                path=Path("/tmp/jarvis-home/skills/briefing/SKILL.md"),
            ),
        ),
        issues=(),
    )
    monkeypatch.setattr("core.prompts.background.load_home_snapshot", lambda: snapshot)

    prompt = BackgroundPromptBuilder().build(
        user_profile="[USER CONTEXT]\nGeoff uses geoff@example.com.",
        runtime_context={
            "source": "background",
            "timezone": "Australia/Sydney",
        },
    )
    text = str(prompt)

    assert "You are a background worker for JARV1S." in prompt.static
    assert "Never claim work succeeded without confirming evidence." in prompt.static
    assert "Call me Geoff" not in text
    assert "briefing" in prompt.dynamic
    assert "files.read" in prompt.dynamic
    assert "NO_REPLY" not in text
    assert "For voice output" not in text
    assert "geoff@example.com" in prompt.dynamic
    assert "User Timezone: Australia/Sydney" in prompt.dynamic
