"""Activity timeline invalidation shared by domain write paths."""

from services.events import Event, EventType, event_bus


async def publish_activity_changed(owner_id: str) -> None:
    await event_bus.publish(
        Event(
            type=EventType.ACTIVITY_CHANGED,
            source="activity",
            data={"owner_id": owner_id},
        )
    )
