"""Durable inbound event inbox + webhook route contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pymongo.errors import DuplicateKeyError

from api.routes import push as push_routes
from api.routes import webhooks as webhook_routes
from core.integrations.composio_webhooks import composio_idempotency_key
from core.integrations.external_webhooks import (
    ExternalTriggerBody,
    validate_external_body,
    verify_external_token,
)
from services.inbound_events import InboundEventService, _MAX_ATTEMPTS


class FakeInboundCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]):
        key = doc.get("idempotency_key")
        if key and any(existing.get("idempotency_key") == key for existing in self.docs):
            raise DuplicateKeyError("duplicate")
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def find_one(self, filt: dict[str, Any], projection=None, sort=None):
        matches = [doc for doc in self.docs if self._matches(doc, filt)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction < 0)
        if not matches:
            return None
        doc = dict(matches[0])
        if projection:
            return {k: doc.get(k) for k in projection if k != "_id"}
        return doc

    async def find_one_and_update(self, filt, update, sort=None, return_document=None):
        matches = [doc for doc in self.docs if self._matches(doc, filt)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction < 0)
        if not matches:
            return None
        doc = matches[0]
        self._apply(doc, update)
        return dict(doc)

    async def update_one(self, filt, update):
        for doc in self.docs:
            if self._matches(doc, filt):
                self._apply(doc, update)
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def aggregate(self, pipeline):
        # only used for status counts
        counts: dict[str, int] = {}
        for doc in self.docs:
            status = doc.get("status")
            counts[status] = counts.get(status, 0) + 1
        for status, count in counts.items():
            yield {"_id": status, "count": count}

    def find(self, filt):
        matches = [dict(doc) for doc in self.docs if self._matches(doc, filt)]

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def sort(self, *_args, **_kwargs):
                return self

            def limit(self, n):
                self._rows = self._rows[:n]
                return self

            def __aiter__(self):
                self._iter = iter(self._rows)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        return _Cursor(matches)

    def _matches(self, doc: dict[str, Any], filt: dict[str, Any]) -> bool:
        if "$or" in filt:
            return any(self._matches(doc, clause) for clause in filt["$or"])
        for key, expected in filt.items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$lte" in expected:
                    if actual is None or actual > expected["$lte"]:
                        return False
                continue
            if actual != expected:
                return False
        return True

    def _apply(self, doc: dict[str, Any], update: dict[str, Any]) -> None:
        for key, value in update.get("$set", {}).items():
            doc[key] = value
        for key, value in update.get("$inc", {}).items():
            doc[key] = int(doc.get(key) or 0) + int(value)


@pytest.fixture
def inbound_coll(monkeypatch):
    coll = FakeInboundCollection()
    db = SimpleNamespace(inbound_events=coll)
    monkeypatch.setattr("services.inbound_events.mongodb", SimpleNamespace(db=db))
    return coll


@pytest.mark.asyncio
async def test_enqueue_persists_and_dedups(inbound_coll):
    svc = InboundEventService()
    first = await svc.enqueue(
        idempotency_key="composio:wh_1",
        kind="composio",
        source="composio",
        payload={"ok": True},
    )
    second = await svc.enqueue(
        idempotency_key="composio:wh_1",
        kind="composio",
        source="composio",
        payload={"ok": True},
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.event_id == first.event_id
    assert len(inbound_coll.docs) == 1


@pytest.mark.asyncio
async def test_lease_reclaim_after_crash(inbound_coll, monkeypatch):
    svc = InboundEventService()
    await svc.enqueue(
        idempotency_key="composio:wh_lease",
        kind="composio",
        source="composio",
        payload={"metadata": {"log_id": "l1"}},
    )
    # Simulate crash mid-processing: lease expired.
    inbound_coll.docs[0]["status"] = "processing"
    inbound_coll.docs[0]["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=5)
    inbound_coll.docs[0]["lease_owner"] = "dead-worker"

    processed = AsyncMock()
    monkeypatch.setattr(
        "core.integrations.composio_webhooks.process_composio_inbound",
        processed,
    )
    claimed = await svc._claim_next()
    assert claimed is not None
    assert claimed["status"] == "processing"
    assert claimed["attempts"] == 1
    await svc._process(claimed)
    assert inbound_coll.docs[0]["status"] == "processed"
    processed.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_then_dead_letter_and_replay(inbound_coll, monkeypatch):
    svc = InboundEventService()
    await svc.enqueue(
        idempotency_key="composio:wh_fail",
        kind="composio",
        source="composio",
        payload={},
    )

    async def boom(_payload):
        raise RuntimeError("transient")

    monkeypatch.setattr("core.integrations.composio_webhooks.process_composio_inbound", boom)

    for attempt in range(1, _MAX_ATTEMPTS):
        claimed = await svc._claim_next()
        assert claimed is not None
        await svc._process(claimed)
        assert inbound_coll.docs[0]["status"] == "retry"
        # Make next attempt immediately claimable.
        inbound_coll.docs[0]["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    claimed = await svc._claim_next()
    assert claimed is not None
    await svc._process(claimed)
    assert inbound_coll.docs[0]["status"] == "dead_letter"

    summary = await svc.retry_event(inbound_coll.docs[0]["id"])
    assert summary is not None
    assert summary.status == "pending"


@pytest.mark.asyncio
async def test_composio_route_persists_before_200(monkeypatch, inbound_coll):
    secret = "whsec_test"
    body = b'{"metadata":{"log_id":"log-1","trigger_slug":"SLACK_RECEIVE_MESSAGE"},"data":{}}'
    wh_id = "msg_1"
    wh_ts = str(int(datetime.now(timezone.utc).timestamp()))
    signing_input = f"{wh_id}.{wh_ts}.".encode() + body
    sig = base64.b64encode(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()).decode()

    monkeypatch.setattr(webhook_routes, "resolve_composio_webhook_secret", lambda: secret)
    monkeypatch.setattr(webhook_routes, "inbound_event_service", InboundEventService())

    app = FastAPI()
    app.include_router(webhook_routes.router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks/composio",
            content=body,
            headers={
                "webhook-id": wh_id,
                "webhook-timestamp": wh_ts,
                "webhook-signature": f"v1,{sig}",
                "content-type": "application/json",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["duplicate"] is False
    assert len(inbound_coll.docs) == 1

    # Duplicate delivery
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        dup = await client.post(
            "/api/v1/webhooks/composio",
            content=body,
            headers={
                "webhook-id": wh_id,
                "webhook-timestamp": wh_ts,
                "webhook-signature": f"v1,{sig}",
                "content-type": "application/json",
            },
        )
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True
    assert len(inbound_coll.docs) == 1


@pytest.mark.asyncio
async def test_composio_route_returns_503_on_mongo_failure(monkeypatch):
    secret = "whsec_test"
    body = b'{"metadata":{"log_id":"log-2","trigger_slug":"SLACK_RECEIVE_MESSAGE"},"data":{}}'
    wh_id = "msg_2"
    wh_ts = str(int(datetime.now(timezone.utc).timestamp()))
    signing_input = f"{wh_id}.{wh_ts}.".encode() + body
    sig = base64.b64encode(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()).decode()

    monkeypatch.setattr(webhook_routes, "resolve_composio_webhook_secret", lambda: secret)
    enqueue = AsyncMock(side_effect=RuntimeError("mongo down"))
    monkeypatch.setattr(
        webhook_routes,
        "inbound_event_service",
        SimpleNamespace(enqueue=enqueue),
    )

    app = FastAPI()
    app.include_router(webhook_routes.router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks/composio",
            content=body,
            headers={
                "webhook-id": wh_id,
                "webhook-timestamp": wh_ts,
                "webhook-signature": f"v1,{sig}",
                "content-type": "application/json",
            },
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_push_route_persists_before_ack(monkeypatch, inbound_coll):
    adapter = SimpleNamespace(verify=AsyncMock(return_value=True))
    monkeypatch.setattr(
        push_routes,
        "push_registry",
        SimpleNamespace(get=lambda _source: adapter),
    )
    monkeypatch.setattr(push_routes, "inbound_event_service", InboundEventService())

    app = FastAPI()
    app.include_router(push_routes.router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/push/calendar",
            content=b"",
            headers={
                "x-goog-channel-id": "ch-1",
                "x-goog-resource-id": "res-1",
                "x-goog-message-number": "7",
                "x-goog-resource-state": "exists",
            },
        )
    assert response.status_code == 200
    assert len(inbound_coll.docs) == 1
    assert inbound_coll.docs[0]["idempotency_key"] == "push:calendar:ch-1:res-1:7"


@pytest.mark.asyncio
async def test_external_endpoint_auth_and_validation(monkeypatch, inbound_coll):
    monkeypatch.setattr(
        webhook_routes,
        "verify_external_token",
        AsyncMock(side_effect=lambda source, token: token == "good"),
    )
    monkeypatch.setattr(webhook_routes, "inbound_event_service", InboundEventService())

    app = FastAPI()
    app.include_router(webhook_routes.router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    body = {
        "event_id": "evt-1",
        "event_type": "task.completed",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"ok": True},
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad = await client.post(
            "/api/v1/webhooks/external/agentic",
            json=body,
            headers={"Authorization": "Bearer bad"},
        )
        assert bad.status_code == 401

        stale = dict(body)
        stale["occurred_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        stale_resp = await client.post(
            "/api/v1/webhooks/external/agentic",
            json=stale,
            headers={"Authorization": "Bearer good"},
        )
        assert stale_resp.status_code == 400

        missing = {"event_type": "task.completed", "payload": {}}
        missing_resp = await client.post(
            "/api/v1/webhooks/external/agentic",
            json=missing,
            headers={"Authorization": "Bearer good"},
        )
        assert missing_resp.status_code == 422

        ok = await client.post(
            "/api/v1/webhooks/external/agentic",
            json=body,
            headers={"Authorization": "Bearer good"},
        )
        assert ok.status_code == 200
        dup = await client.post(
            "/api/v1/webhooks/external/agentic",
            json=body,
            headers={"Authorization": "Bearer good"},
        )
        assert dup.status_code == 200
        assert dup.json()["duplicate"] is True
    assert len(inbound_coll.docs) == 1


def test_composio_idempotency_prefers_webhook_id():
    key = composio_idempotency_key(
        webhook_id="wh-abc",
        payload={"metadata": {"log_id": "log-xyz"}},
    )
    assert key == "composio:wh-abc"


def test_composio_idempotency_requires_stable_id():
    assert (
        composio_idempotency_key(
            webhook_id="",
            payload={"metadata": {"trigger_slug": "SLACK_RECEIVE_MESSAGE"}},
        )
        is None
    )


def test_external_body_rejects_stale_timestamp():
    body = ExternalTriggerBody(
        event_id="e1",
        event_type="ping",
        occurred_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        payload={},
    )
    with pytest.raises(ValueError):
        validate_external_body(body)


@pytest.mark.asyncio
async def test_verify_external_token_constant_time(monkeypatch):
    token = "jvx_secret"
    hashed = hashlib.sha256(token.encode()).hexdigest()
    monkeypatch.setattr(
        "core.integrations.external_webhooks.mongodb",
        SimpleNamespace(
            db=SimpleNamespace(
                external_trigger_credentials=SimpleNamespace(
                    find_one=AsyncMock(
                        return_value={"source": "agentic", "token_hash": hashed, "revoked": False}
                    )
                )
            )
        ),
    )
    assert await verify_external_token("agentic", token) is True
    assert await verify_external_token("agentic", "wrong") is False


@pytest.mark.asyncio
async def test_composio_subscription_rotates_missing_secret(monkeypatch):
    from core.integrations.composio_gateway import ComposioGateway

    monkeypatch.setattr(
        "core.integrations.composio_gateway.credential_store.get_secret",
        lambda _name: None,
    )
    gateway = ComposioGateway.__new__(ComposioGateway)
    gateway._callback_host = "https://host.example.ts.net"
    gateway._webhook_secret = None
    gateway._persist_webhook_secret = MagicMock()

    async def fake_request(method, url, json=None):
        if method == "GET":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "items": [
                        {
                            "id": "sub-1",
                            "webhook_url": f"{gateway._callback_host}/api/v1/webhooks/composio",
                        }
                    ]
                },
                text="",
            )
        if method == "POST" and url.endswith("/rotate_secret"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"secret": "rotated-secret"},
                text="",
            )
        raise AssertionError(f"unexpected {method} {url}")

    gateway._request = fake_request  # type: ignore[method-assign]
    await gateway.ensure_webhook_subscription()
    gateway._persist_webhook_secret.assert_called_once_with("rotated-secret")


@pytest.mark.asyncio
async def test_composio_subscription_patches_url_change(monkeypatch):
    from core.integrations.composio_gateway import ComposioGateway

    monkeypatch.setattr(
        "core.integrations.composio_gateway.credential_store.get_secret",
        lambda _name: None,
    )
    gateway = ComposioGateway.__new__(ComposioGateway)
    gateway._callback_host = "https://new.example.ts.net"
    gateway._webhook_secret = "existing"
    gateway._persist_webhook_secret = MagicMock()
    calls: list[tuple[str, str]] = []

    async def fake_request(method, url, json=None):
        calls.append((method, url))
        if method == "GET":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "items": [
                        {
                            "id": "sub-1",
                            "webhook_url": "https://old.example.ts.net/api/v1/webhooks/composio",
                        }
                    ]
                },
                text="",
            )
        if method == "PATCH":
            assert json["webhook_url"].endswith("/api/v1/webhooks/composio")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"secret": "patched-secret"},
                text="",
            )
        raise AssertionError(f"unexpected {method} {url}")

    gateway._request = fake_request  # type: ignore[method-assign]
    await gateway.ensure_webhook_subscription()
    assert any(method == "PATCH" for method, _ in calls)
    gateway._persist_webhook_secret.assert_called_once_with("patched-secret")


@pytest.mark.asyncio
async def test_ingress_store_save_merges_cache(monkeypatch):
    from core.integrations.external_ingress import ExternalIngressStore

    updates: list[dict[str, Any]] = []

    class _Col:
        async def update_one(self, *_args, **_kwargs):
            updates.append(_kwargs.get("update") or _args[1] if _args else {})
            return SimpleNamespace(matched_count=1)

        async def find_one(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        "services.database.mongodb.mongodb",
        SimpleNamespace(get_collection=lambda _name: _Col()),
    )
    store = ExternalIngressStore()
    store._cache = {
        "enabled": True,
        "base_url": "https://host.example.ts.net",
        "last_received_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    await store.save({"last_error": None, "composio_subscription_ok": True})
    assert store._cache["last_received_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert store._cache["composio_subscription_ok"] is True
    assert store._cache["base_url"] == "https://host.example.ts.net"
