import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError  # type: ignore[import-not-found]

from core.operations.projection import ManagedSetup
from core.triggers.models import TriggerRule
from services.automation import (
    AutomationService,
    TriggerEvent,
    evaluate_conditions,
    render_automation_message,
)
from core.triggers.conditions import field_conditions_from_dicts
from plugins.automations import AutomationsPlugin


class _AsyncCursor:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _rule() -> dict:
    return {
        "id": "rule-1",
        "owner_id": "geoff",
        "name": "test_rule",
        "enabled": True,
        "trigger": {"source": "calendar", "event": "starting", "offset": -5},
        "conditions": [],
        "action": {
            "decision": "tell",
            "message": "Event starting: {title}",
        },
    }


def _trigger_rule_doc(rule: dict | None = None) -> dict:
    legacy = rule or _rule()
    trigger = legacy.get("trigger", {})
    action = legacy.get("action", {})
    return {
        "id": legacy.get("id", "rule-1"),
        "owner_id": legacy.get("owner_id", "geoff"),
        "name": legacy.get("name", "test_rule"),
        "enabled": legacy.get("enabled", True),
        "surface": True,
        "created_at": legacy.get("created_at", datetime.now(timezone.utc)),
        "updated_at": legacy.get("updated_at", datetime.now(timezone.utc)),
        "origin": {
            "kind": "external",
            "source": trigger.get("source", "calendar"),
            "event": trigger.get("event", "starting"),
            "offset_minutes": trigger.get("offset", 0),
        },
        "conditions": [
            {"kind": "field", "parameters": condition}
            for condition in legacy.get("conditions", [])
        ],
        "action": {
            "decision": action.get("decision", "tell"),
            "message": action.get("message", "Automation triggered."),
            "protocol_name": action.get("protocol"),
            "instructions": action.get("instructions"),
            "reply_grounding": {},
        },
        "attention": {
            "level": "normal",
            "sound": "chime",
            "requires_ack": False,
        },
        "delivery": {
            "channel": "voice",
        },
        "freshness": {
            "stale_if_source_event_started": trigger.get("source", "calendar") == "calendar",
        },
        "paused_until": legacy.get("paused_until"),
        "suppressed_event_ids": legacy.get("suppressed_events", []),
        "management": {"provider": "automations", "resource_id": legacy.get("id", "rule-1")},
    }


def _item() -> dict:
    return {
        "id": "event-1",
        "title": "Standup",
        "start": "2026-04-28T01:00:00+00:00",
    }


def test_render_automation_message_adds_calendar_title_for_generic_message() -> None:
    rule = _rule()
    rule["action"]["message"] = "Your meeting is starting in five minutes"

    assert render_automation_message(TriggerRule.model_validate(_trigger_rule_doc(rule)), _item()) == (
        "Your meeting is starting in five minutes: Standup"
    )


@pytest.mark.asyncio
async def test_create_rule_rejects_interval_triggers_with_scheduler_hint() -> None:
    plugin = AutomationsPlugin()

    result = await plugin.create_rule(
        name="pushups",
        trigger={"source": "time", "event": "interval", "interval_minutes": 45},
        action={"decision": "offer", "message": "Move"},
    )

    assert "Use scheduler.remind" in result
    assert "every 45m" in result


@pytest.mark.asyncio
async def test_create_rule_returns_actionable_error_for_invalid_trigger_shape(
    monkeypatch,
) -> None:
    result = await AutomationsPlugin().create_rule(
        name="event reminder",
        trigger={"source": "calendar", "event": "starting", "lead_time": 5},
        action={"message": "Event soon"},
    )

    assert "Invalid trigger" in result
    assert "Accepted fields: source, event, offset" in result
    assert "lead_time: Extra inputs are not permitted" in result


@pytest.mark.asyncio
async def test_update_rule_returns_actionable_error_for_invalid_trigger_shape(
    monkeypatch,
) -> None:
    existing = _trigger_rule_doc(_rule())
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            trigger={"offset_minutes": -10},
        )

    assert "Invalid trigger" in result
    assert "Accepted fields: source, event, offset" in result
    assert "offset_minutes: Extra inputs are not permitted" in result
    mock_mongo.db.trigger_rules.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_previews_before_persisting(monkeypatch) -> None:
    insert_one = AsyncMock()

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            automations=SimpleNamespace(insert_one=insert_one)
        )

        result = await AutomationsPlugin().create_rule(
            name="event reminder",
            trigger={"source": "calendar", "event": "starting", "offset": -5},
            action={"decision": "offer", "message": "Meeting soon"},
        )

    text = getattr(result, "content", result)
    ui = list(getattr(result, "ui", None) or [])
    assert "Preview only" in text
    assert ui[0].component == "ContentWidget"
    assert ui[0].data["title"] == "Automation Preview"
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_persists_action_instructions(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._auto_register_push_trigger", AsyncMock(return_value=None))
    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(insert_one=AsyncMock(side_effect=insert_one))
        )

        result = await AutomationsPlugin().create_rule(
            name="event reminder",
            trigger={"source": "calendar", "event": "starting", "offset": -5},
            action={
                "message": "Meeting soon",
                "decision": "offer",
                "instructions": "only if the meeting is not cancelled",
            },
            confirmed=True,
        )

    assert "created" in result
    assert inserted["action"]["instructions"] == "only if the meeting is not cancelled"
    assert inserted["action"]["decision"] == "offer"
    assert inserted["origin"]["kind"] == "external"
    assert inserted["origin"]["source"] == "calendar"
    assert inserted["freshness"]["stale_if_source_event_started"] is True
    assert "delivery_mode" not in inserted["action"]


@pytest.mark.asyncio
async def test_create_rule_does_not_stale_at_start_silent_calendar_action(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._auto_register_push_trigger", AsyncMock(return_value=None))
    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(insert_one=AsyncMock(side_effect=insert_one))
        )

        result = await AutomationsPlugin().create_rule(
            name="Meeting Quiet Mode",
            trigger={"source": "calendar", "event": "starting", "offset": 0},
            action={
                "decision": "act",
                "message": "Set quiet mode for this meeting",
                "instructions": "Call jarvis.attention.mute for this meeting.",
            },
            confirmed=True,
        )

    assert "created" in result
    assert inserted["freshness"]["stale_if_source_event_started"] is False


@pytest.mark.asyncio
async def test_create_rule_does_not_stale_at_start_calendar_notification(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._auto_register_push_trigger", AsyncMock(return_value=None))
    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(insert_one=AsyncMock(side_effect=insert_one))
        )

        result = await AutomationsPlugin().create_rule(
            name="Meeting starts now",
            trigger={"source": "calendar", "event": "starting", "offset": 0},
            action={"decision": "tell", "message": "Meeting {title} starts now."},
            confirmed=True,
        )

    assert "created" in result
    assert inserted["freshness"]["stale_if_source_event_started"] is False


@pytest.mark.asyncio
async def test_create_rule_accepts_top_level_instructions(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._auto_register_push_trigger", AsyncMock(return_value=None))
    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(insert_one=AsyncMock(side_effect=insert_one))
        )

        await AutomationsPlugin().create_rule(
            name="event reminder",
            trigger={"source": "calendar", "event": "starting", "offset": -5},
            action={"decision": "offer", "message": "Meeting soon"},
            instructions="only if the meeting is not cancelled",
            confirmed=True,
        )

    assert inserted["action"]["instructions"] == "only if the meeting is not cancelled"


@pytest.mark.asyncio
async def test_update_rule_merges_partial_action_with_existing_action(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "action": {
            "decision": "act",
            "message": "Set quiet mode for this meeting",
            "instructions": "Call jarvis.attention.mute().",
        },
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            action={
                "decision": "tell",
                "protocol": "Meeting Start",
                "instructions": "Run the Meeting Start protocol.",
            },
        )

    assert "updated" in result
    assert updated["action"]["decision"] == "tell"
    assert updated["action"]["protocol_name"] == "Meeting Start"
    assert updated["action"]["instructions"] == "Run the Meeting Start protocol."
    assert updated["action"]["message"] == "Set quiet mode for this meeting"
    assert updated["attention"]["sound"] == "chime"


@pytest.mark.asyncio
async def test_update_rule_merges_partial_trigger_with_existing_trigger(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "trigger": {"source": "calendar", "event": "starting", "offset": 0}
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            trigger={"offset": -5},
        )

    assert "updated" in result
    assert updated["origin"]["source"] == "calendar"
    assert updated["origin"]["event"] == "starting"
    assert updated["origin"]["offset_minutes"] == -5


@pytest.mark.asyncio
async def test_create_rule_rejects_missing_protocol_before_persisting(monkeypatch) -> None:
    insert_one = AsyncMock()

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.protocol.protocol_exists", AsyncMock(return_value=False))
    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(trigger_rules=SimpleNamespace(insert_one=insert_one))

        result = await AutomationsPlugin().create_rule(
            name="brief on bank emails",
            trigger={"source": "gmail", "event": "new_message", "offset": 0},
            action={
                "message": "Bank email briefing",
                "protocol": "missing_protocol",
                "decision": "tell",
            },
        )

    assert "Protocol 'missing_protocol' not found" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_rejects_invalid_builtin_trigger_event(monkeypatch) -> None:
    insert_one = AsyncMock()

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_trigger_info",
        lambda source: [{"event": "starting", "description": "Calendar event starts"}],
    )

    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(trigger_rules=SimpleNamespace(insert_one=insert_one))

        result = await AutomationsPlugin().create_rule(
            name="Meeting Quiet Mode",
            trigger={"source": "calendar", "event": "event_starting"},
            action={"message": "Entering quiet mode", "decision": "act"},
        )

    assert "event='event_starting' is not valid" in result
    assert "Use event='starting'" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_rejects_unknown_builtin_condition_field(monkeypatch) -> None:
    insert_one = AsyncMock()

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_condition_fields",
        lambda source: [{"field": "title", "type": "string"}],
    )

    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(trigger_rules=SimpleNamespace(insert_one=insert_one))

        result = await AutomationsPlugin().create_rule(
            name="Meeting Reminder",
            trigger={"source": "calendar", "event": "starting", "offset": -1},
            conditions=[{"field": "is_cancelled", "op": "equals", "value": "false"}],
            action={"message": "Meeting soon", "decision": "tell"},
        )

    assert "Invalid condition field" in result
    assert "is_cancelled" in result
    assert "instructions" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rule_rejects_unknown_builtin_condition_field(monkeypatch) -> None:
    update_one = AsyncMock()
    existing = _trigger_rule_doc({
        **_rule(),
        "trigger": {"source": "calendar", "event": "starting", "offset": -1},
        "conditions": [],
        "action": {"decision": "tell", "message": "Meeting soon"},
    })

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_condition_fields",
        lambda source: [{"field": "title", "type": "string"}],
    )

    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=update_one,
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            conditions=[{"field": "is_cancelled", "op": "equals", "value": "false"}],
        )

    assert "Invalid condition field" in result
    assert "is_cancelled" in result
    assert "list_available_triggers('calendar')" in result
    update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rule_accepts_advertised_builtin_condition_field(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "trigger": {"source": "calendar", "event": "starting", "offset": -1},
        "conditions": [],
        "action": {"decision": "tell", "message": "Meeting soon"},
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_condition_fields",
        lambda source: [{"field": "title", "type": "string"}],
    )

    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            conditions=[{"field": "title", "op": "not_contains", "value": "Canceled:"}],
        )

    assert "updated" in result
    assert updated["conditions"] == [
        {"kind": "field", "parameters": {"field": "title", "op": "not_contains", "value": "Canceled:"}},
    ]


@pytest.mark.asyncio
async def test_pause_all_accepts_duration_string(monkeypatch) -> None:
    pause = AsyncMock()
    monkeypatch.setattr(
        "services.automation.automation_service",
        SimpleNamespace(pause=pause),
    )

    result = await AutomationsPlugin().pause_all(duration_minutes="2h")

    assert "paused for 120 minutes" in result
    until = pause.await_args.kwargs["until"]
    delta = until - datetime.now(timezone.utc)
    assert 118 * 60 < delta.total_seconds() < 122 * 60


async def _drain_background_tasks(service: AutomationService) -> None:
    tasks = list(service._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_claim_fire_uses_insert_only_dedup() -> None:
    service = AutomationService()
    insert_one = AsyncMock(side_effect=DuplicateKeyError("duplicate"))
    db = SimpleNamespace(automation_fired=SimpleNamespace(insert_one=insert_one))

    with patch("services.automation.mongodb") as mock_mongo:
        mock_mongo.db = db

        claimed = await service._claim_fire(
            ("rule-1", "event-1"),
            datetime.now(timezone.utc),
        )

    assert claimed is False
    insert_one.assert_awaited_once()
    assert ("rule-1", "event-1") in service._fired


@pytest.mark.asyncio
async def test_push_event_duplicate_claim_publishes_once() -> None:
    service = AutomationService()
    legacy_rule = _rule()
    legacy_rule["trigger"]["event"] = "starting"
    event = TriggerEvent(
        source="calendar",
        event_type="starting",
        event_id="event-1",
        occurred_at=datetime.now(timezone.utc),
        provider="test",
        payload=_item(),
    )

    insert_one = AsyncMock(side_effect=[SimpleNamespace(inserted_id="first"), DuplicateKeyError("duplicate")])
    notif_insert = AsyncMock(return_value=SimpleNamespace(inserted_id="notif-1"))
    trigger_rules = SimpleNamespace(
        find=MagicMock(side_effect=[
            _AsyncCursor([_trigger_rule_doc(legacy_rule)]),
            _AsyncCursor([_trigger_rule_doc(legacy_rule)]),
        ]),
        find_one=AsyncMock(return_value=_trigger_rule_doc(legacy_rule)),
    )
    db = SimpleNamespace(
        trigger_rules=trigger_rules,
        automation_fired=SimpleNamespace(insert_one=insert_one, update_one=AsyncMock()),
        trigger_instances=SimpleNamespace(insert_one=notif_insert, find_one=AsyncMock(return_value=None)),
    )

    with patch("services.automation.mongodb") as mock_mongo, \
         patch("core.triggers.service.mongodb") as mock_notif_mongo, \
         patch("services.automation.event_bus") as mock_bus:
        mock_mongo.db = db
        mock_notif_mongo.db = db
        mock_bus.publish = AsyncMock()

        await service.on_push_event(event)
        await _drain_background_tasks(service)

        # Simulate a second backend process: no in-memory _fired cache, same DB key.
        second_service = AutomationService()
        await second_service.on_push_event(event)
        await _drain_background_tasks(second_service)

    assert insert_one.await_count == 2
    mock_bus.publish.assert_awaited_once()
    trigger_doc = notif_insert.await_args.args[0]
    assert trigger_doc["dedup_key"] == "rule-1:event-1"


@pytest.mark.asyncio
async def test_scheduled_timer_claims_before_fire_and_blocks_next_poll() -> None:
    service = AutomationService()
    key = ("rule-1", "event-1")
    rule = TriggerRule.model_validate(_trigger_rule_doc(_rule()))
    item = _item()

    insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id="first"))
    notif_insert = AsyncMock(return_value=SimpleNamespace(inserted_id="notif-1"))
    rule_doc = _trigger_rule_doc(_rule())
    db = SimpleNamespace(
        trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=rule_doc)),
        automation_fired=SimpleNamespace(insert_one=insert_one, update_one=AsyncMock()),
        trigger_instances=SimpleNamespace(insert_one=notif_insert, find_one=AsyncMock(return_value=None)),
    )

    with patch("services.automation.mongodb") as mock_mongo, \
         patch("core.triggers.service.mongodb") as mock_notif_mongo, \
         patch("services.automation.event_bus") as mock_bus:
        mock_mongo.db = db
        mock_notif_mongo.db = db
        mock_bus.publish = AsyncMock()

        handle = service._schedule_fire(key, rule, item, 0.01)
        service._pending[key] = SimpleNamespace(handle=handle)
        await asyncio.sleep(0.05)
        await _drain_background_tasks(service)

        assert key in service._fired
        mock_bus.publish.assert_awaited_once()

        watcher = SimpleNamespace(trigger_mode="anticipated")
        await service._evaluate_source(
            "calendar",
            watcher,
            [item],
            [rule],
            datetime.now(timezone.utc),
        )

    insert_one.assert_awaited_once()
    mock_bus.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_uses_live_rule_instructions_not_pending_snapshot() -> None:
    """Anticipated PendingFire keeps a schedule-time rule; dispatch must read Mongo."""
    service = AutomationService()
    key = ("rule-1", "event-1")
    stale = TriggerRule.model_validate(_trigger_rule_doc({
        **_rule(),
        "action": {
            "decision": "act",
            "message": "Automation triggered.",
            "instructions": "Call jarvis.attention.mute()",
        },
    }))
    live_doc = _trigger_rule_doc({
        **_rule(),
        "action": {
            "decision": "act",
            "message": "Automation triggered.",
            "instructions": "Call jarvis.attention.set_mode('quiet')",
        },
    })
    item = _item()
    notif_insert = AsyncMock(return_value=SimpleNamespace(inserted_id="notif-1"))
    db = SimpleNamespace(
        trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=live_doc)),
        trigger_instances=SimpleNamespace(insert_one=notif_insert, find_one=AsyncMock(return_value=None)),
    )

    with patch("services.automation.mongodb") as mock_mongo, \
         patch("core.triggers.service.mongodb") as mock_notif_mongo, \
         patch("services.automation.event_bus") as mock_bus:
        mock_mongo.db = db
        mock_notif_mongo.db = db
        mock_bus.publish = AsyncMock()

        await service._fire(key, stale, item)

    trigger_doc = notif_insert.await_args.args[0]
    assert trigger_doc["action_snapshot"]["instructions"] == (
        "Call jarvis.attention.set_mode('quiet')"
    )
    mock_bus.publish.assert_awaited_once()


def test_evaluate_conditions_handles_boolean_and_list_fields() -> None:
    assert evaluate_conditions(
        field_conditions_from_dicts([{"field": "is_unread", "op": "equals", "value": "true"}]),
        {"is_unread": True},
    )
    assert evaluate_conditions(
        field_conditions_from_dicts([{"field": "labels", "op": "contains", "value": "inbox"}]),
        {"labels": ["INBOX", "UNREAD"]},
    )
    assert not evaluate_conditions(
        field_conditions_from_dicts([{"field": "labels", "op": "contains", "value": "spam"}]),
        {"labels": ["INBOX", "UNREAD"]},
    )


@pytest.mark.asyncio
async def test_update_rule_add_conditions_appends_without_dropping_existing(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "conditions": [
            {"field": "title", "op": "not_contains", "value": "standup"},
        ],
        "action": {"decision": "tell", "message": "Meeting soon"},
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            add_conditions=[{"field": "attendee_count", "op": "greater_than", "value": "1"}],
        )

    assert "updated" in result
    assert updated["conditions"] == [
        {"kind": "field", "parameters": {"field": "title", "op": "not_contains", "value": "standup"}},
        {"kind": "field", "parameters": {"field": "attendee_count", "op": "greater_than", "value": "1"}},
    ]


@pytest.mark.asyncio
async def test_update_rule_remove_conditions_drops_exact_match(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "conditions": [
            {"field": "title", "op": "not_contains", "value": "standup"},
            {"field": "attendee_count", "op": "greater_than", "value": "1"},
        ],
        "action": {"decision": "tell", "message": "Meeting soon"},
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            remove_conditions=[{"field": "title", "op": "not_contains", "value": "standup"}],
        )

    assert "updated" in result
    assert updated["conditions"] == [
        {"kind": "field", "parameters": {"field": "attendee_count", "op": "greater_than", "value": "1"}},
    ]


@pytest.mark.asyncio
async def test_update_rule_top_level_instructions_updates_action(monkeypatch) -> None:
    updated: dict = {}
    existing = _trigger_rule_doc({
        **_rule(),
        "action": {"decision": "offer", "message": "Meeting soon", "instructions": "old policy"},
    })

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            instructions="only if the meeting is not cancelled",
        )

    assert "updated" in result
    assert updated["action"]["instructions"] == "only if the meeting is not cancelled"


@pytest.mark.asyncio
async def test_list_available_triggers_includes_condition_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_trigger_info",
        lambda source: [{"event": "starting", "description": "Calendar event starts"}],
    )
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_condition_fields",
        lambda source: [{"field": "title", "type": "string"}],
    )
    monkeypatch.setattr(
        "core.plugins.registry.registry",
        SimpleNamespace(bespoke_names=set()),
    )

    results = await AutomationsPlugin().list_available_triggers("calendar")

    assert len(results) == 1
    assert results[0].condition_fields == [{"field": "title", "type": "string"}]


_AUTOMATION_HOLD = (
    "External automations are globally paused. Matching rules stay active "
    "but will not fire. Call automations.resume_all."
)


@pytest.mark.asyncio
async def test_test_rule_prefixes_hold_and_still_lists_matches(monkeypatch, tool_context):
    monkeypatch.setattr(
        "services.automation.automation_service.test_rule",
        AsyncMock(
            return_value=[
                {
                    "title": "Standup",
                    "fire_time": "2026-08-20T08:24:00+00:00",
                    "seconds_until_fire": 660,
                    "already_fired": False,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "services.automation.automation_service.pause_observation",
        lambda now=None: _AUTOMATION_HOLD,
    )
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=_trigger_rule_doc({"id": "rule-1"})),
            )
        )
        with tool_context(timezone="UTC"):
            result = await AutomationsPlugin().test_rule("rule-1")

    assert result.startswith(_AUTOMATION_HOLD)
    assert "Standup" in result
    assert "This rule would fire for 1 event(s):" in result


def _automation_setup(rule_id: str, name: str) -> ManagedSetup:
    return ManagedSetup(
        resource_ref=f"automations:automation:{rule_id}",
        resource_id=rule_id,
        setup_type="automation",
        managed_by="automations",
        kind="automation",
        name=name,
        rule_id=rule_id,
        status="active",
        trigger_label="gmail.new_email",
        supported_actions=["pause", "resume", "delete"],
        edit_tool="automations.update_rule",
    )


@pytest.mark.asyncio
async def test_delete_rule_by_unique_name_uses_resolved_id(monkeypatch) -> None:
    row = _automation_setup("rule-helen", "Email from Helen McCosker")
    delete_rule = AsyncMock(return_value=object())
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=row))
    monkeypatch.setattr("plugins.automations.delete_automation_rule", delete_rule)
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=None)),
        )
        result = await AutomationsPlugin().delete_rule("Email from Helen McCosker")

    assert "deleted" in result
    delete_rule.assert_awaited_once_with("geoff", "rule-helen")


@pytest.mark.asyncio
async def test_delete_rule_ambiguous_name_does_not_write(monkeypatch) -> None:
    rows = [
        _automation_setup("rule-helen", "Email from Helen McCosker"),
        _automation_setup("rule-helen-cal", "Helen calendar digest"),
    ]
    delete_rule = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=rows))
    monkeypatch.setattr("plugins.automations.delete_automation_rule", delete_rule)
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=None)),
        )
        result = await AutomationsPlugin().delete_rule("Helen")

    assert "Ambiguous automation" in result
    assert "resource_ref" in result
    assert "setups.find" not in result
    delete_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_rule_unknown_name_does_not_point_at_setups_find(monkeypatch) -> None:
    delete_rule = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=None))
    monkeypatch.setattr("plugins.automations.delete_automation_rule", delete_rule)
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=None)),
        )
        result = await AutomationsPlugin().delete_rule("Email from Helen McCosker")

    assert "No automation matching" in result
    assert "setups.find" not in result
    delete_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rule_by_unique_name_uses_resolved_id(monkeypatch) -> None:
    row = _automation_setup("rule-helen", "Email from Helen McCosker")
    existing = _trigger_rule_doc({**_rule(), "id": "rule-helen", "name": row.name})
    updated: dict = {}

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=row))
    patch_lifecycle = AsyncMock()
    monkeypatch.setattr("plugins.automations.patch_rule_lifecycle", patch_lifecycle)
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(side_effect=[None, existing]),
                update_one=AsyncMock(side_effect=update_one),
            )
        )
        result = await AutomationsPlugin().update_rule(
            rule_id="Email from Helen McCosker",
            enabled=False,
        )

    assert result == "Rule 'rule-helen' updated."
    patch_lifecycle.assert_awaited_once()
    mock_mongo.db.trigger_rules.update_one.assert_not_awaited()
