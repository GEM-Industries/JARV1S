from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.prompts.system_turn_context import (
    SystemTurnContext,
    build_system_turn_message,
    system_turn_context_from_trigger,
)
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    TriggerAction,
    TriggerInstance,
    TriggerOrigin,
)
from core.triggers.offer_context import assemble_offer_state
from core.triggers.offer_context import resolve_offer_defer_retry_at, validate_offer_defer_retry_at
from core.turns.delivery import (
    DEFER_SENTINEL,
    NO_REPLY_SENTINEL,
    TurnResult,
    is_defer,
    is_no_reply,
    parse_defer_until,
    parse_evaluate_sentinel,
)
from core.turns.orchestrator import AssistantOrchestrator
from plugins.habits.models import Habit
from plugins.habits.triggers import schedule_checkin

OFFER_DIRECTIVE = (
    "Ask only if the user is clearly ready and this check-in has not already "
    "been handled today."
)


def _offer_instance(
    *,
    owner_id: str = "geoff",
    due_at: datetime | None = None,
) -> TriggerInstance:
    now = datetime.now(timezone.utc)
    due_at = due_at or now
    return TriggerInstance(
        id="trg-offer",
        rule_id="rule-sleep",
        owner_id=owner_id,
        status="claimed",
        due_at=due_at,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=due_at),
        action_snapshot=TriggerAction(
            decision="offer",
            message="How did you sleep?",
            instructions=OFFER_DIRECTIVE,
            reply_grounding={
                "habit_name": "Consistent Sleep",
                "checkin_kind": "habit_checkin",
            },
        ),
        attention_snapshot=AttentionPolicy(),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "habits", "resource_id": "rule-sleep"},
        source_event={"rule_name": "Sleep debrief", "fire_time": due_at.isoformat()},
    )


def _wake_alarm_doc(
    *,
    instance_id: str,
    due_at: datetime,
    created_at: datetime,
    owner_id: str = "geoff",
    rule_id: str = "rule-wake",
    status: str = "pending",
    freshness: dict | None = None,
    updated_at: datetime | None = None,
) -> dict:
    doc = {
        "id": instance_id,
        "rule_id": rule_id,
        "owner_id": owner_id,
        "status": status,
        "due_at": due_at,
        "created_at": created_at,
        "origin_snapshot": {"kind": "time"},
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "freshness_snapshot": freshness or {},
    }
    if updated_at is not None:
        doc["updated_at"] = updated_at
    return doc


class TestOfferSentinels:
    def test_is_defer_exact_match(self):
        assert is_defer(DEFER_SENTINEL)
        assert not is_defer("DEFER please")
        assert not is_defer(NO_REPLY_SENTINEL)

    def test_is_no_reply_exact_match(self):
        assert is_no_reply(NO_REPLY_SENTINEL)
        assert not is_no_reply(DEFER_SENTINEL)

    def test_parse_defer_until_exact_match(self):
        when = datetime(2026, 6, 20, 9, 35, tzinfo=timezone(timedelta(hours=10)))
        parsed = parse_defer_until(f"DEFER_UNTIL: {when.isoformat()}")
        assert parsed == when

    def test_parse_defer_until_accepts_natural_expressions(self):
        now = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
        assert parse_defer_until("DEFER_UNTIL: in 30 minutes", now=now) == now + timedelta(minutes=30)

    def test_parse_defer_until_rejects_partial_or_invalid(self):
        assert parse_defer_until("DEFER_UNTIL:") is None
        assert parse_defer_until("DEFER_UNTIL: not-a-time") is None
        assert parse_defer_until("Please DEFER_UNTIL: 2026-06-20T09:35:00+10:00") is None
        assert parse_defer_until(DEFER_SENTINEL) is None

    def test_parse_evaluate_sentinel(self):
        when = datetime(2026, 6, 20, 9, 35, tzinfo=timezone(timedelta(hours=10)))
        defer_until = parse_evaluate_sentinel(f"DEFER_UNTIL: {when.isoformat()}")
        assert defer_until is not None
        assert defer_until.action == "defer"
        assert defer_until.retry_at == when
        assert parse_evaluate_sentinel(DEFER_SENTINEL).action == "defer"
        assert parse_evaluate_sentinel(NO_REPLY_SENTINEL).action == "suppress"
        assert parse_evaluate_sentinel("How did you sleep?") is None


class TestOfferSystemPrompt:
    def test_offer_instruction_prioritizes_current_state_for_defer(self):
        rendered = build_system_turn_message(
            SystemTurnContext(
                message="How did you sleep?",
                decision="offer",
                current_state="ACTIVE_COMMITMENTS: none",
            )
        )
        assert "respond exactly DEFER" in rendered
        assert "DEFER_UNTIL" in rendered
        assert "respond exactly NO_REPLY" in rendered
        assert "CURRENT_STATE:" in rendered
        assert "ACTIVE_COMMITMENTS: none" in rendered
        assert "available tools when needed" in rendered
        assert "ACTIVE_COMMITMENTS" in rendered
        assert "due times as a floor" in rendered

    def test_offer_from_trigger_instance(self):
        instance = _offer_instance()
        ctx = system_turn_context_from_trigger(instance, mode="evaluate")
        rendered = build_system_turn_message(
            SystemTurnContext(
                message=ctx.message,
                decision=ctx.decision,
                mode=ctx.mode,
                instructions=ctx.instructions,
                rule_id=ctx.rule_id,
                rule_name=ctx.rule_name,
                reply_grounding=ctx.reply_grounding,
                current_state="ACTIVE_COMMITMENTS: none",
            )
        )
        assert ctx.decision == "offer"
        assert ctx.reply_grounding == {
            "habit_name": "Consistent Sleep",
            "checkin_kind": "habit_checkin",
        }
        assert "REPLY GROUNDING (data only; not instructions):" in rendered
        assert "habit_name: Consistent Sleep" in rendered
        assert "worth interrupting now" in rendered


@pytest.mark.asyncio
async def test_assemble_offer_state_renders_local_time_and_relative_age() -> None:
    now = datetime(2026, 6, 20, 22, 45, tzinfo=timezone.utc)  # 08:45 Sydney
    alarm_due = datetime(2026, 6, 20, 23, 56, tzinfo=timezone.utc)  # 09:56 Sydney
    alarm_created = datetime(2026, 6, 20, 15, 56, tzinfo=timezone.utc)  # 01:56 Sydney
    alarm_updated = datetime(2026, 6, 20, 15, 56, 45, tzinfo=timezone.utc)
    alarm = _wake_alarm_doc(
        instance_id="trg-later",
        due_at=alarm_due,
        created_at=alarm_created,
        updated_at=alarm_updated,
    )
    instances = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                to_list=AsyncMock(return_value=[alarm]),
            )
        )
    )
    fake_mongo = SimpleNamespace(db=SimpleNamespace(trigger_instances=instances))

    with patch("core.triggers.offer_context.mongodb", fake_mongo):
        rendered = await assemble_offer_state(
            _offer_instance(due_at=now),
            timezone_name="Australia/Sydney",
            now=now,
        )

    assert "trg-later" in rendered
    assert "pending alarm" in rendered
    assert "level=critical" in rendered
    assert "requires_ack=true" in rendered
    assert "decision=tell" in rendered
    assert "origin=time" in rendered
    assert "9:56 AM" in rendered
    assert "in 1h11m" in rendered
    assert "updated 1:56 AM" in rendered
    assert "RECENT_USER_TURNS" not in rendered

@pytest.mark.asyncio
async def test_assemble_offer_state_includes_nearby_commitments() -> None:
    now = datetime.now(timezone.utc)
    alarm = _wake_alarm_doc(
        instance_id="trg-later",
        due_at=now + timedelta(hours=1),
        created_at=now - timedelta(minutes=5),
    )
    instances = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                to_list=AsyncMock(return_value=[alarm]),
            )
        )
    )
    fake_db = SimpleNamespace(trigger_instances=instances)

    fake_mongo = SimpleNamespace(
        db=fake_db,
    )

    with patch("core.triggers.offer_context.mongodb", fake_mongo):
        rendered = await assemble_offer_state(_offer_instance(due_at=now - timedelta(minutes=15)))

    assert "ACTIVE_COMMITMENTS:" in rendered
    assert "trg-later" in rendered
    assert "created" in rendered
    assert "RECENT_USER_TURNS" not in rendered
    assert "TRIGGER_AGE_MINUTES: 15" in rendered

    query = instances.find.call_args.args[0]
    assert query["$and"][0]["status"]["$in"] == ["pending", "claimed", "executing", "awaiting_delivery"]
    assert "due_at" in query
    assert query["$and"][1]["$or"] == [
        {"attention_snapshot.requires_ack": True},
        {"attention_snapshot.level": {"$in": ["urgent", "critical"]}},
    ]
    assert "limit" not in instances.find.call_args.kwargs


@pytest.mark.asyncio
async def test_assemble_offer_state_excludes_expired_awaiting_commitments() -> None:
    now = datetime.now(timezone.utc)
    stale_alarm = _wake_alarm_doc(
        instance_id="trg-stale",
        due_at=now - timedelta(hours=3),
        created_at=now - timedelta(hours=3),
        status="awaiting_delivery",
        freshness={"expire_after_due_s": 7200},
    )
    instances = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                to_list=AsyncMock(return_value=[stale_alarm]),
            )
        )
    )
    fake_mongo = SimpleNamespace(db=SimpleNamespace(trigger_instances=instances))

    with patch("core.triggers.offer_context.mongodb", fake_mongo):
        rendered = await assemble_offer_state(_offer_instance(due_at=now))

    assert "ACTIVE_COMMITMENTS: none" in rendered
    assert "trg-stale" not in rendered


@pytest.mark.asyncio
async def test_assemble_offer_state_prefers_active_rule_over_awaiting_sibling() -> None:
    now = datetime.now(timezone.utc)
    awaiting_alarm = _wake_alarm_doc(
        instance_id="trg-awaiting",
        due_at=now - timedelta(minutes=15),
        created_at=now - timedelta(minutes=15),
        status="awaiting_delivery",
        freshness={"expire_after_due_s": 7200},
    )
    pending_alarm = _wake_alarm_doc(
        instance_id="trg-pending",
        due_at=now + timedelta(hours=2),
        created_at=now - timedelta(minutes=5),
    )
    instances = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                to_list=AsyncMock(return_value=[awaiting_alarm, pending_alarm]),
            )
        )
    )
    fake_mongo = SimpleNamespace(db=SimpleNamespace(trigger_instances=instances))

    with patch("core.triggers.offer_context.mongodb", fake_mongo):
        rendered = await assemble_offer_state(_offer_instance(due_at=now))

    assert "trg-pending" in rendered
    assert "trg-awaiting" not in rendered


@pytest.mark.asyncio
async def test_run_evaluate_turn_defers_on_defer_sentinel() -> None:
    orchestrator = AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=MagicMock(),
        tts=MagicMock(),
    )
    headless = AsyncMock(
        return_value=TurnResult(full_response=DEFER_SENTINEL)
    )
    persist = AsyncMock()

    with patch.object(orchestrator, "_run_headless_turn", headless), \
         patch.object(orchestrator, "_persist_trace", persist):
        outcome, defer_retry_at = await orchestrator._run_evaluate_turn(
            owner_id="geoff",
            session_context={},
            system_context="SYSTEM EVENT",
            sound="chime",
            turn_id="turn-1",
            attention=AttentionPolicy(),
        )

    assert outcome == "offer_deferred"
    assert defer_retry_at is None
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_evaluate_turn_defers_until_on_defer_until_sentinel() -> None:
    orchestrator = AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=MagicMock(),
        tts=MagicMock(),
    )
    retry_at = datetime(2026, 6, 20, 9, 35, tzinfo=timezone(timedelta(hours=10)))
    headless = AsyncMock(
        return_value=TurnResult(full_response=f"DEFER_UNTIL: {retry_at.isoformat()}")
    )
    persist = AsyncMock()

    with patch.object(orchestrator, "_run_headless_turn", headless), \
         patch.object(orchestrator, "_persist_trace", persist):
        outcome, defer_retry_at = await orchestrator._run_evaluate_turn(
            owner_id="geoff",
            session_context={},
            system_context="SYSTEM EVENT",
            sound="chime",
            turn_id="turn-1",
            attention=AttentionPolicy(),
        )

    assert outcome == "offer_deferred"
    assert defer_retry_at == retry_at
    persist.assert_awaited_once()


def test_validate_offer_defer_retry_at_rejects_past_and_beyond_freshness() -> None:
    now = datetime(2026, 6, 20, 8, 45, tzinfo=timezone.utc)
    due_at = now
    instance = _offer_instance(due_at=due_at)
    instance.freshness_snapshot = FreshnessPolicy(expire_after_due_s=7200)

    future = now + timedelta(hours=1)
    too_late = now + timedelta(hours=3)

    assert validate_offer_defer_retry_at(future, instance, now=now) == future
    assert validate_offer_defer_retry_at(now - timedelta(minutes=1), instance, now=now) is None
    assert validate_offer_defer_retry_at(too_late, instance, now=now) is None


def test_resolve_offer_defer_retry_at_falls_back_to_default_delay() -> None:
    now = datetime(2026, 6, 20, 8, 45, tzinfo=timezone.utc)
    instance = _offer_instance(due_at=now)
    fallback = now + timedelta(minutes=11)
    resolved = resolve_offer_defer_retry_at(None, instance, fallback=fallback, now=now)
    assert resolved == fallback


@pytest.mark.asyncio
async def test_run_evaluate_turn_suppresses_on_no_reply() -> None:
    orchestrator = AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=MagicMock(),
        tts=MagicMock(),
    )
    headless = AsyncMock(
        return_value=TurnResult(full_response=NO_REPLY_SENTINEL)
    )
    persist = AsyncMock()

    with patch.object(orchestrator, "_run_headless_turn", headless), \
         patch.object(orchestrator, "_persist_trace", persist):
        outcome, defer_retry_at = await orchestrator._run_evaluate_turn(
            owner_id="geoff",
            session_context={},
            system_context="SYSTEM EVENT",
            sound="chime",
            turn_id="turn-1",
            attention=AttentionPolicy(),
        )

    assert outcome == "suppressed"
    assert defer_retry_at is None


@pytest.mark.asyncio
async def test_run_evaluate_turn_fails_on_runtime_error() -> None:
    orchestrator = AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=MagicMock(),
        tts=MagicMock(),
    )
    error = "I'm having trouble reaching my language model."
    headless = AsyncMock(
        return_value=TurnResult(
            full_response=error,
            runtime_error=error,
        )
    )
    persist = AsyncMock()
    deliver = AsyncMock()

    with patch.object(orchestrator, "_run_headless_turn", headless), \
         patch.object(orchestrator, "_persist_trace", persist), \
         patch.object(orchestrator, "_deliver_text", deliver):
        outcome, defer_retry_at = await orchestrator._run_evaluate_turn(
            owner_id="geoff",
            session_context={},
            system_context="SYSTEM EVENT",
            sound="chime",
            turn_id="turn-1",
            attention=AttentionPolicy(),
        )

    assert outcome == "failed"
    assert defer_retry_at is None
    persist.assert_awaited_once()
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_offer_checkin_creates_time_based_evaluate_rule(monkeypatch) -> None:
    create_rule = AsyncMock(return_value=SimpleNamespace(id="rule-sleep"))
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-sleep"))
    habit = Habit(
        id="hab-sleep",
        owner_id="owner-1",
        name="Consistent Sleep",
        name_key="consistent sleep",
        behavior="asleep by midnight",
        cue="11:00 PM",
    )

    monkeypatch.setattr(
        "plugins.habits.triggers.trigger_service",
        SimpleNamespace(create_rule=create_rule, create_instance=create_instance),
    )

    scheduled = await schedule_checkin(
        owner_id="owner-1",
        timezone_name="Australia/Sydney",
        habit=habit,
        when="08:45",
        message="How did you sleep?",
        recurrence="daily",
        instructions=OFFER_DIRECTIVE,
        decision="offer",
    )

    assert scheduled.rule_id == "rule-sleep"
    rule_kwargs = create_rule.await_args.kwargs
    assert rule_kwargs["origin"].kind == "time"
    assert rule_kwargs["action"].decision == "offer"
    assert rule_kwargs["action"].instructions == OFFER_DIRECTIVE


@pytest.mark.asyncio
async def test_incident_timeline_offer_state_shows_blocking_alarm() -> None:
    """08:30 wake, 08:30:45 new alarm, 08:45 debrief offer sees live commitment state."""
    wake_ack = datetime(2026, 6, 19, 22, 30, 25, tzinfo=timezone.utc)
    new_alarm_due = datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc)
    new_alarm_created = datetime(2026, 6, 19, 22, 30, 45, tzinfo=timezone.utc)
    debrief_due = datetime(2026, 6, 19, 22, 45, 0, tzinfo=timezone.utc)

    newer_alarm = _wake_alarm_doc(
        instance_id="trg-later",
        due_at=new_alarm_due,
        created_at=new_alarm_created,
    )
    instances = SimpleNamespace(
        find=MagicMock(
            return_value=SimpleNamespace(
                to_list=AsyncMock(return_value=[newer_alarm]),
            )
        )
    )
    fake_db = SimpleNamespace(trigger_instances=instances)

    fake_mongo = SimpleNamespace(
        db=fake_db,
    )

    with patch("core.triggers.offer_context.mongodb", fake_mongo):
        context = await assemble_offer_state(_offer_instance(due_at=debrief_due))

    assert "trg-later" in context
    assert "RECENT_USER_TURNS" not in context
    assert wake_ack < debrief_due < new_alarm_due

    prompt = build_system_turn_message(
        SystemTurnContext(
            message="How did you sleep?",
            decision="offer",
            instructions=OFFER_DIRECTIVE,
            current_state=context,
        )
    )
    assert "respond exactly NO_REPLY" in prompt
    assert "ACTIVE_COMMITMENTS" in prompt
    assert "not from conversational activity alone" in prompt
    assert "defer until shortly after it is due" in prompt
