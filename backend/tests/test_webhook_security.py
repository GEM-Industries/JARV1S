from datetime import datetime, timedelta, timezone

from core.integrations.composio_webhooks import composio_timestamp_is_recent


def test_webhook_timestamp_accepts_recent_delivery():
    now = datetime.now(timezone.utc)
    assert composio_timestamp_is_recent(str(int(now.timestamp())), now=now)


def test_webhook_timestamp_rejects_replay():
    now = datetime.now(timezone.utc)
    stale = now - timedelta(minutes=10)
    assert not composio_timestamp_is_recent(str(int(stale.timestamp())), now=now)


def test_webhook_timestamp_rejects_invalid_value():
    assert not composio_timestamp_is_recent("not-a-timestamp")
