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
from plugins.automations import (
    AutomationsPlugin,
    ConditionFieldInfo,
    TriggerInfo,
    _CatalogTrigger,
    _fail,
    delete_automation_rule,
)
from core.plugins.registry import build_capability_definition


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


def _mongo(*, insert_one=None, find_one=None, update_one=None, existing=None):
    trigger_rules = SimpleNamespace(
        insert_one=insert_one or AsyncMock(),
        find_one=find_one or AsyncMock(return_value=existing),
        update_one=update_one or AsyncMock(return_value=SimpleNamespace(matched_count=1)),
        delete_one=AsyncMock(return_value=SimpleNamespace(deleted_count=1)),
    )
    return SimpleNamespace(
        db=SimpleNamespace(
            trigger_rules=trigger_rules,
            automation_fired=SimpleNamespace(delete_many=AsyncMock()),
        )
    )


def _patch_rule_mongo(monkeypatch, mongo) -> None:
    monkeypatch.setattr("plugins.automations.mongodb", mongo)
    monkeypatch.setattr("core.triggers.service.mongodb", mongo)
    monkeypatch.setattr(
        "core.plugins.registry.registry",
        SimpleNamespace(bespoke_names={"calendar", "gmail"}),
    )


def _catalog_row(
    *,
    source: str,
    event: str,
    provider: str = "built-in",
    fields: list[str] | None = None,
    supported: bool = True,
    offset_supported: bool = True,
    slug: str | None = None,
    description: str = "",
) -> _CatalogTrigger:
    return _CatalogTrigger(
        info=TriggerInfo(
            source=source,
            event=event,
            description=description or f"{source}.{event}",
            provider=provider,
            condition_fields=[
                ConditionFieldInfo(
                    field=name,
                    type="string",
                    operators=["contains", "not_contains", "equals", "not_equals"],
                )
                for name in (fields or [])
            ],
            supported=supported,
            offset_supported=offset_supported,
        ),
        slug=slug,
    )


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
        source="time",
        event="interval",
        decision="offer",
        message="Move",
    )

    assert "Use scheduler.remind" in result
    assert "every 45m" in result


def test_create_rule_schema_is_flat() -> None:
    plugin = AutomationsPlugin()
    definition = build_capability_definition(
        plugin, "create_rule", plugin.create_rule, enabled=True
    )
    props = definition.input_schema.get("properties") or {}
    assert "source" in props
    assert "event" in props
    assert "offset" in props
    assert "decision" in props
    assert "message" in props
    assert "field" in props
    assert "op" in props
    assert "value" in props
    assert "trigger" not in props
    assert "action" not in props
    assert "conditions" not in props
    assert definition.input_schema.get("additionalProperties") is False


@pytest.mark.asyncio
async def test_create_rule_persists_immediately(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="event reminder",
        source="calendar",
        event="starting",
        offset=-5,
        decision="offer",
        message="Meeting soon",
    )

    text = getattr(result, "content", result)
    ui = list(getattr(result, "ui", None) or [])
    assert "created" in text
    assert "Preview only" not in text
    assert ui[0].data["display"] == "receipt"
    assert inserted["origin"]["source"] == "calendar"
    assert inserted["origin"]["event"] == "starting"


@pytest.mark.asyncio
async def test_create_rule_accepts_flat_field_filter(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="standup reminder",
        source="calendar",
        event="starting",
        offset=-5,
        decision="tell",
        message="Standup soon",
        field="title",
        op="contains",
        value="standup",
    )

    assert "created" in getattr(result, "content", result)
    assert inserted["conditions"] == [
        {"kind": "field", "parameters": {"field": "title", "op": "contains", "value": "standup"}},
    ]


@pytest.mark.asyncio
async def test_create_rule_aliases_sender_id_to_catalog_user(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    catalog = [
        _catalog_row(
            source="slack",
            event="receive_direct_message",
            provider="composio",
            fields=["user", "text"],
            offset_supported=False,
            slug="SLACK_RECEIVE_DIRECT_MESSAGE",
        )
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    monkeypatch.setattr("plugins.automations._ensure_push_registered", AsyncMock(return_value=None))
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="Direct messages",
        source="slack",
        event="receive_direct_message",
        field="sender_id",
        value="U123",
        decision="tell",
        message="New direct message",
    )

    assert "created" in getattr(result, "content", result)
    assert inserted["conditions"] == [
        {"kind": "field", "parameters": {"field": "user", "op": "equals", "value": "U123"}},
    ]


@pytest.mark.asyncio
async def test_create_rule_invalid_event_includes_related_fields(monkeypatch) -> None:
    insert_one = AsyncMock()
    catalog = [
        _catalog_row(
            source="slack",
            event="receive_message",
            provider="composio",
            fields=["user", "text"],
            offset_supported=False,
            slug="SLACK_RECEIVE_MESSAGE",
        ),
        _catalog_row(
            source="slack",
            event="receive_direct_message",
            provider="composio",
            fields=["user"],
            offset_supported=False,
            slug="SLACK_RECEIVE_DIRECT_MESSAGE",
        ),
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="Slack DMs",
        source="slack",
        event="message_received",
        decision="tell",
        message="New Slack message",
    )

    assert "event='message_received' is not valid" in result
    assert "receive_message" in result
    assert "fields=['user', 'text']" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_persists_action_instructions(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="event reminder",
        source="calendar",
        event="starting",
        offset=-5,
        decision="offer",
        message="Meeting soon",
        instructions="only if the meeting is not cancelled",
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
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="Meeting Quiet Mode",
        source="calendar",
        event="starting",
        offset=0,
        decision="act",
        message="Set quiet mode for this meeting",
        instructions="Call jarvis.attention.mute for this meeting.",
    )

    assert "created" in result
    assert inserted["freshness"]["stale_if_source_event_started"] is False


@pytest.mark.asyncio
async def test_create_rule_does_not_stale_at_start_calendar_notification(monkeypatch) -> None:
    inserted: dict = {}

    async def insert_one(doc):
        inserted.update(doc)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=AsyncMock(side_effect=insert_one)))

    result = await AutomationsPlugin().create_rule(
        name="Meeting starts now",
        source="calendar",
        event="starting",
        offset=0,
        decision="tell",
        message="Meeting {title} starts now.",
    )

    assert "created" in result
    assert inserted["freshness"]["stale_if_source_event_started"] is False


@pytest.mark.asyncio
async def test_create_rule_rejects_duplicate_name_case_insensitive(monkeypatch) -> None:
    insert_one = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(
        monkeypatch,
        _mongo(insert_one=insert_one, existing={"id": "rule-1", "name": "Event Reminder"}),
    )

    result = await AutomationsPlugin().create_rule(
        name="event reminder",
        source="calendar",
        event="starting",
        offset=-5,
        decision="offer",
        message="Meeting soon",
    )

    assert "already exists" in result
    assert "automations.update_rule" in result
    insert_one.assert_not_awaited()


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
    monkeypatch.setattr("plugins.protocol.protocol_exists", AsyncMock(return_value=True))
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )

        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            decision="tell",
            protocol="Meeting Start",
            instructions="Run the Meeting Start protocol.",
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
            offset=-5,
        )

    assert "updated" in result
    assert updated["origin"]["source"] == "calendar"
    assert updated["origin"]["event"] == "starting"
    assert updated["origin"]["offset_minutes"] == -5
    assert updated["freshness"]["stale_if_source_event_started"] is True


@pytest.mark.asyncio
async def test_create_rule_rejects_missing_protocol_before_persisting(monkeypatch) -> None:
    insert_one = AsyncMock()

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.protocol.protocol_exists", AsyncMock(return_value=False))
    mongo = _mongo(insert_one=insert_one)
    _patch_rule_mongo(monkeypatch, mongo)

    result = await AutomationsPlugin().create_rule(
        name="brief on bank emails",
        source="gmail",
        event="",
        offset=0,
        message="Bank email briefing",
        protocol="missing_protocol",
        decision="tell",
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
    monkeypatch.setattr(
        "core.plugins.registry.registry",
        SimpleNamespace(bespoke_names={"calendar", "gmail"}),
    )

    with patch("core.triggers.service.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(trigger_rules=SimpleNamespace(insert_one=insert_one))

        result = await AutomationsPlugin().create_rule(
            name="Meeting Quiet Mode",
            source="calendar",
            event="event_starting",
            decision="tell",
            message="Entering quiet mode",
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
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="Meeting Reminder",
        source="calendar",
        event="starting",
        offset=-1,
        field="is_cancelled",
        op="equals",
        value="false",
        decision="tell",
        message="Meeting soon",
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
    assert results[0].condition_fields[0].field == "title"
    assert "contains" in results[0].condition_fields[0].operators
    assert results[0].supported is True
    assert results[0].offset_supported is True


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
async def test_update_rule_by_unique_name_uses_resolved_id(monkeypatch) -> None:
    row = _automation_setup("rule-helen", "Email from Helen McCosker")
    existing = _trigger_rule_doc({**_rule(), "id": "rule-helen", "name": row.name})
    updated: dict = {}

    async def update_one(_query, update):
        updated.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=row))
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(side_effect=[None, existing]),
                update_one=AsyncMock(side_effect=update_one),
            )
        )
        result = await AutomationsPlugin().update_rule(
            rule_id="Email from Helen McCosker",
            name="Email from Helen",
        )

    assert result == "Rule 'rule-helen' updated."
    assert updated["name"] == "Email from Helen"


@pytest.mark.asyncio
async def test_update_rule_ambiguous_name_does_not_write(monkeypatch) -> None:
    rows = [
        _automation_setup("rule-helen", "Email from Helen McCosker"),
        _automation_setup("rule-helen-cal", "Helen calendar digest"),
    ]
    update_one = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=rows))
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=None),
                update_one=update_one,
            )
        )
        result = await AutomationsPlugin().update_rule(rule_id="Helen", name="Helen updated")

    assert "Ambiguous automation" in result
    assert "resource_ref" in result
    update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rule_unknown_name_does_not_write(monkeypatch) -> None:
    update_one = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.resolve_managed_setup", AsyncMock(return_value=None))
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=None),
                update_one=update_one,
            )
        )
        result = await AutomationsPlugin().update_rule(
            rule_id="Email from Helen McCosker",
            name="Email from Helen",
        )

    assert "No automation matching" in result
    update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_available_triggers_uses_composio_payload_fields(monkeypatch) -> None:
    class Gateway:
        async def list_trigger_types(self, source):
            return [
                {
                    "slug": "SLACK_RECEIVE_DIRECT_MESSAGE",
                    "description": "Direct message received",
                    "config": {},
                    "payload": {
                        "properties": {
                            "user": {"type": "string"},
                            "text": {"type": "string"},
                            "channel": {"type": "object", "properties": {"id": {"type": "string"}}},
                        }
                    },
                }
            ]

    monkeypatch.setattr("core.plugins.registry.registry", SimpleNamespace(bespoke_names=set()))
    monkeypatch.setattr(
        "services.automation.automation_service.watcher_trigger_info",
        lambda source: [],
    )
    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: Gateway(),
    )

    results = await AutomationsPlugin().list_available_triggers("slack")

    assert len(results) == 1
    assert results[0].event == "receive_direct_message"
    assert {field.field for field in results[0].condition_fields} == {"user", "text"}
    assert results[0].supported is True
    assert results[0].offset_supported is False


@pytest.mark.asyncio
async def test_create_rule_rejects_unknown_composio_condition_field(monkeypatch) -> None:
    insert_one = AsyncMock()
    catalog = [
        _catalog_row(
            source="slack",
            event="receive_direct_message",
            provider="composio",
            fields=["user", "text"],
            offset_supported=False,
            slug="SLACK_RECEIVE_DIRECT_MESSAGE",
        )
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="Direct messages",
        source="slack",
        event="receive_direct_message",
        field="channel_topic",
        op="contains",
        value="alerts",
        decision="tell",
        message="New direct message",
    )

    assert "channel_topic" in result
    assert "user" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_rejects_configured_composio_trigger(monkeypatch) -> None:
    insert_one = AsyncMock()
    catalog = [
        _catalog_row(
            source="gmail",
            event="new_gmail_message",
            provider="composio",
            fields=["sender"],
            supported=False,
            offset_supported=False,
            slug="GMAIL_NEW_GMAIL_MESSAGE",
        )
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="New mail",
        source="gmail",
        event="new_gmail_message",
        decision="tell",
        message="New mail",
    )

    assert "requires provider configuration" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_does_not_persist_when_registration_fails(monkeypatch) -> None:
    insert_one = AsyncMock()
    catalog = [
        _catalog_row(
            source="slack",
            event="receive_direct_message",
            provider="composio",
            fields=["user"],
            offset_supported=False,
            slug="SLACK_RECEIVE_DIRECT_MESSAGE",
        )
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    monkeypatch.setattr(
        "plugins.automations._ensure_push_registered",
        AsyncMock(return_value=_fail("Could not register push trigger for 'slack.receive_direct_message'.")),
    )
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="Leo DMs",
        source="slack",
        event="receive_direct_message",
        decision="tell",
        message="Leo messaged you",
    )

    assert "Could not register" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_rejects_nonzero_offset_on_reactive_trigger(monkeypatch) -> None:
    insert_one = AsyncMock()
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="new mail",
        source="gmail",
        event="",
        offset=-5,
        decision="tell",
        message="New mail",
    )

    assert "offset is only valid for anticipated events" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rule_rejects_conditions_when_payload_has_no_fields(monkeypatch) -> None:
    insert_one = AsyncMock()
    catalog = [
        _catalog_row(
            source="github",
            event="commit_event",
            provider="composio",
            fields=[],
            offset_supported=False,
            slug="GITHUB_COMMIT_EVENT",
        )
    ]
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._catalog_for_source", AsyncMock(return_value=catalog))
    monkeypatch.setattr("plugins.automations._ensure_push_registered", AsyncMock(return_value=None))
    _patch_rule_mongo(monkeypatch, _mongo(insert_one=insert_one))

    result = await AutomationsPlugin().create_rule(
        name="GitHub commits",
        source="github",
        event="commit_event",
        field="author",
        op="equals",
        value="geoff",
        decision="tell",
        message="New commit",
    )

    assert "no filterable payload fields" in result
    assert "instructions" in result
    insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rule_registers_replacement_before_write(monkeypatch) -> None:
    existing = _trigger_rule_doc({
        **_rule(),
        "trigger": {"source": "slack", "event": "receive_direct_message", "offset": 0},
    })
    order: list[str] = []
    catalog = _catalog_row(
        source="calendar",
        event="starting",
        fields=["title"],
        offset_supported=True,
    )

    async def register(_catalog):
        order.append("register")
        return None

    async def update_one(_query, update):
        order.append("write")
        return SimpleNamespace(matched_count=1)

    async def unused(*_args, **_kwargs):
        order.append("cleanup")

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations._resolve_catalog_trigger", AsyncMock(return_value=catalog))
    monkeypatch.setattr("plugins.automations._ensure_push_registered", register)
    monkeypatch.setattr("plugins.automations._deregister_if_unused", unused)
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(return_value=existing),
                update_one=AsyncMock(side_effect=update_one),
            )
        )
        result = await AutomationsPlugin().update_rule(
            rule_id="rule-1",
            source="calendar",
            event="starting",
            offset=-10,
        )

    assert "updated" in result
    assert order == ["register", "write", "cleanup"]


@pytest.mark.asyncio
async def test_delete_automation_rule_keeps_shared_push_subscription(monkeypatch) -> None:
    rule = _trigger_rule_doc({
        **_rule(),
        "id": "rule-a",
        "trigger": {"source": "slack", "event": "receive_direct_message", "offset": 0},
    })
    deregister = AsyncMock()
    monkeypatch.setattr("plugins.automations.cancel_open_instances_for_rule", AsyncMock())
    monkeypatch.setattr("plugins.automations._deregister_push_trigger", deregister)
    monkeypatch.setattr("plugins.automations.publish_operations_changed", AsyncMock())
    mongo = _mongo(find_one=AsyncMock(side_effect=[rule, {"id": "rule-b"}]))
    monkeypatch.setattr("plugins.automations.mongodb", mongo)

    deleted = await delete_automation_rule("geoff", "rule-a")

    assert deleted is not None
    deregister.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_automation_rule_deregisters_when_last_subscriber(monkeypatch) -> None:
    rule = _trigger_rule_doc({
        **_rule(),
        "id": "rule-a",
        "trigger": {"source": "slack", "event": "receive_direct_message", "offset": 0},
    })
    deregister = AsyncMock()
    monkeypatch.setattr("plugins.automations.cancel_open_instances_for_rule", AsyncMock())
    monkeypatch.setattr("plugins.automations._deregister_push_trigger", deregister)
    monkeypatch.setattr("plugins.automations.publish_operations_changed", AsyncMock())
    mongo = _mongo(find_one=AsyncMock(side_effect=[rule, None]))
    monkeypatch.setattr("plugins.automations.mongodb", mongo)

    deleted = await delete_automation_rule("geoff", "rule-a")

    assert deleted is not None
    deregister.assert_awaited_once_with("slack", "receive_direct_message")


@pytest.mark.asyncio
async def test_delete_rule_removes_named_automation(monkeypatch) -> None:
    rule = _trigger_rule_doc({
        **_rule(),
        "id": "rule-a",
        "name": "Slack mentions",
        "trigger": {"source": "slack", "event": "receive_direct_message", "offset": 0},
    })

    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.automations.cancel_open_instances_for_rule", AsyncMock())
    monkeypatch.setattr("plugins.automations._deregister_if_unused", AsyncMock())
    monkeypatch.setattr("plugins.automations.publish_operations_changed", AsyncMock())
    mongo = _mongo(find_one=AsyncMock(side_effect=[rule, rule]))
    monkeypatch.setattr("plugins.automations.mongodb", mongo)

    result = await AutomationsPlugin().delete_rule("rule-a")

    assert result == "Deleted automation Slack mentions."
    mongo.db.trigger_rules.delete_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_rule_is_owner_scoped(monkeypatch) -> None:
    find_one = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "services.automation.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_rules=SimpleNamespace(find_one=find_one))),
    )
    from services.automation import AutomationService

    result = await AutomationService().test_rule("rule-1", owner_id="geoff")

    assert result == []
    find_one.assert_awaited_once()
    query = find_one.await_args.args[0]
    assert query["id"] == "rule-1"
    assert query["owner_id"] == "geoff"


@pytest.mark.asyncio
async def test_test_rule_push_trigger_does_not_claim_end_to_end(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.automation.automation_service.test_rule",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "services.automation.automation_service.pause_observation",
        lambda now=None: None,
    )
    monkeypatch.setattr("plugins.automations.get_owner_id", lambda: "geoff")
    with patch("plugins.automations.mongodb") as mock_mongo:
        mock_mongo.db = SimpleNamespace(
            trigger_rules=SimpleNamespace(
                find_one=AsyncMock(
                    return_value=_trigger_rule_doc({
                        **_rule(),
                        "trigger": {
                            "source": "slack",
                            "event": "receive_direct_message",
                            "offset": 0,
                        },
                    })
                ),
            )
        )
        result = await AutomationsPlugin().test_rule("rule-1")

    assert "cannot verify delivery" in result
    assert "will fire" not in result
    assert "slack" in result
