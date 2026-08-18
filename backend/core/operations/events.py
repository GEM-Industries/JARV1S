"""Domain events for operations definitions shown in the Operations panel."""

from typing import Literal

from services.events import Event, EventType, event_bus

OperationScope = Literal["automations", "protocols", "schedules"]


async def publish_operations_changed(owner_id: str, scope: OperationScope) -> None:
    """Notify live clients that one operations definition list is stale."""
    await event_bus.publish(
        Event(
            type=EventType.OPERATIONS_CHANGED,
            source="operations",
            data={"owner_id": owner_id, "scope": scope},
        )
    )
