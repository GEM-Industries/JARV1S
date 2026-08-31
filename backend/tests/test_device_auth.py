from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.auth.device_models import DeviceLocation, PairConsumeRequest
from core.auth.device_service import (
    DeviceDisconnectedError,
    InvalidDeviceTokenError,
    InvalidPairingCodeError,
    InvalidWsTicketError,
    PairingRateLimitError,
    device_auth_service,
)
from core.auth.device_tokens import (
    format_device_token,
    generate_device_secret,
    generate_pairing_code,
    normalize_pairing_code,
    parse_device_token,
)
from core.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def test_pairing_code_is_short_and_unambiguous() -> None:
    code = generate_pairing_code()

    assert len(code) == 7
    assert code[3] == "-"
    assert all(char not in code for char in "01IO")


def test_pairing_code_normalization_accepts_common_formatting() -> None:
    assert normalize_pairing_code(" abc-234 ") == "ABC234"
    assert normalize_pairing_code("ABC 234") == "ABC234"


class InMemoryCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self._id = 0

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self._id += 1
        stored = dict(doc)
        stored["_id"] = self._id
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=self._id)

    async def find_one(self, query: dict, sort: list | None = None) -> dict | None:
        matches = [doc for doc in self.docs if self._query_matches(doc, query)]
        if not matches:
            return None
        if sort:
            key, direction = sort[0]
            matches.sort(
                key=lambda doc: doc.get(key) or "",
                reverse=direction == -1,
            )
        return matches[0]

    def _query_matches(self, doc: dict, query: dict) -> bool:
        for key, value in query.items():
            if key == "revoked_at" and value is None:
                if doc.get("revoked_at") is not None:
                    return False
            elif isinstance(value, dict) and "$ne" in value:
                if doc.get(key) == value["$ne"]:
                    return False
            elif isinstance(value, dict) and "$nin" in value:
                if doc.get(key) in value["$nin"]:
                    return False
            elif isinstance(value, dict) and "$gt" in value:
                # handled in _matches for find_one_and_update expires_at
                if not self._matches(doc, key, value):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def find_one_and_update(
        self,
        query: dict,
        update: dict,
        return_document: bool = False,
        upsert: bool = False,
    ):
        for doc in self.docs:
            if not all(self._matches(doc, key, value) for key, value in query.items()):
                continue
            self._apply_update(doc, update)
            return doc
        if upsert:
            doc = dict(query)
            doc.pop("expires_at", None)
            self._apply_update(doc, update)
            if "$setOnInsert" in update:
                doc.update(update["$setOnInsert"])
            await self.insert_one(doc)
            return self.docs[-1]
        return None

    def _apply_update(self, doc: dict, update: dict) -> None:
        for key, value in update.get("$set", {}).items():
            doc[key] = value
        for key in update.get("$unset", {}):
            doc.pop(key, None)
        for key, value in update.get("$setOnInsert", {}).items():
            doc.setdefault(key, value)
        for key, delta in update.get("$inc", {}).items():
            doc[key] = int(doc.get(key, 0)) + int(delta)

    def _matches(self, doc: dict, key: str, value) -> bool:
        if key == "expires_at" and isinstance(value, dict) and "$gt" in value:
            expires_at = doc.get("expires_at")
            return isinstance(expires_at, datetime) and expires_at > value["$gt"]
        if key == "consumed_at" and value is None:
            return doc.get("consumed_at") is None
        return doc.get(key) == value

    async def update_one(self, query: dict, update: dict):
        for doc in self.docs:
            if not self._query_matches(doc, query):
                continue
            self._apply_update(doc, update)
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def update_many(self, query: dict, update: dict):
        modified = 0
        for doc in self.docs:
            if not self._query_matches(doc, query):
                continue
            for key, value in update.get("$set", {}).items():
                doc[key] = value
            modified += 1
        return SimpleNamespace(modified_count=modified)

    def find(self, query: dict):
        async def _iter():
            for doc in self.docs:
                if self._query_matches(doc, query):
                    yield doc

        return _AsyncCursor(_iter())


class _AsyncCursor:
    def __init__(self, iterator):
        self._iterator = iterator

    def sort(self, *_args, **_kwargs):
        return self

    def __aiter__(self):
        return self._iterator


@pytest.fixture
def mock_device_db(monkeypatch):
    devices = InMemoryCollection()
    pairing = InMemoryCollection()
    tickets = InMemoryCollection()
    attempts = InMemoryCollection()

    service = device_auth_service
    monkeypatch.setattr(service, "_devices", lambda: devices)
    monkeypatch.setattr(service, "_pairing", lambda: pairing)
    monkeypatch.setattr(service, "_tickets", lambda: tickets)
    monkeypatch.setattr(service, "_pairing_attempts", lambda: attempts)
    return SimpleNamespace(devices=devices, pairing=pairing, tickets=tickets, attempts=attempts)


def test_parse_device_token_roundtrip():
    device_id = "dev-abc123"
    secret = generate_device_secret()
    token = format_device_token(device_id, secret)
    parsed = parse_device_token(token)
    assert parsed == (device_id, secret)


@pytest.mark.asyncio
async def test_create_device_credential_is_owner_bearing(mock_device_db):
    summary, token = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="office-pi",
        capabilities=["mic", "speaker"],
        kind="satellite",
    )
    assert summary.owner_id == "owner-1"
    assert summary.node_id == "office-pi"
    assert parse_device_token(token) is not None


@pytest.mark.asyncio
async def test_pairing_code_consume_once(mock_device_db):
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-1")
    request = PairConsumeRequest(code=issued.code, node_id="browser-1")
    first = await device_auth_service.consume_pairing_code(request, client_key="127.0.0.1")
    assert first.owner_id == "owner-1"
    with pytest.raises(InvalidPairingCodeError):
        await device_auth_service.consume_pairing_code(request, client_key="127.0.0.1")


@pytest.mark.asyncio
async def test_pairing_code_bound_to_node_keeps_room(mock_device_db):
    issued = await device_auth_service.issue_pairing_code(
        owner_id="owner-1",
        node_id="bedroom-pi",
        location=DeviceLocation(
            provider="home_assistant",
            room_name="Bedroom",
            ha_area_id="bedroom",
        ),
    )
    with pytest.raises(InvalidPairingCodeError):
        await device_auth_service.consume_pairing_code(
            PairConsumeRequest(
                code=issued.code,
                node_id="kitchen-pi",
                client_surface="satellite",
            ),
            client_key="127.0.0.1",
        )
    result = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(
            code=issued.code,
            node_id="bedroom-pi",
            client_surface="satellite",
        ),
        client_key="127.0.0.1",
    )
    devices = await device_auth_service.list_devices(owner_id="owner-1")
    paired = next(device for device in devices if device.device_id == result.device_id)
    assert paired.node_id == "bedroom-pi"
    assert paired.location.room_name == "Bedroom"
    assert paired.location.ha_area_id == "bedroom"


@pytest.mark.asyncio
async def test_desktop_pairing_persists_device_kind(mock_device_db):
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-1")
    result = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(
            code=issued.code,
            node_id="browser-existing",
            client_surface="desktop_app",
        ),
        client_key="127.0.0.1",
    )

    devices = await device_auth_service.list_devices(owner_id="owner-1")
    paired = next(device for device in devices if device.device_id == result.device_id)
    assert paired.kind == "desktop"


@pytest.mark.asyncio
async def test_phone_pairing_persists_device_kind(mock_device_db):
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-1")
    result = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(
            code=issued.code,
            node_id="phone-existing",
            client_surface="phone",
        ),
        client_key="127.0.0.1",
    )

    devices = await device_auth_service.list_devices(owner_id="owner-1")
    paired = next(device for device in devices if device.device_id == result.device_id)
    assert paired.kind == "phone"


@pytest.mark.asyncio
async def test_pair_same_node_revokes_prior_active_credentials(mock_device_db):
    first_summary, _ = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="browser-same",
        kind="browser",
    )
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-1")
    result = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(code=issued.code, node_id="browser-same"),
        client_key="127.0.0.1",
    )
    await device_auth_service.retire_superseded_credentials(
        owner_id=result.owner_id,
        node_id=result.node_id,
        keep_device_id=result.device_id,
        kind="browser",
    )

    devices = await device_auth_service.list_devices(owner_id="owner-1")
    same_node = [d for d in devices if d.node_id == "browser-same"]
    active = [d for d in same_node if d.revoked_at is None]
    revoked = [d for d in same_node if d.revoked_at is not None]
    assert len(active) == 1
    assert active[0].device_id == result.device_id
    assert any(d.device_id == first_summary.device_id for d in revoked)


@pytest.mark.asyncio
async def test_phone_pair_revokes_offline_phone_but_keeps_live_phone(mock_device_db):
    offline_summary, _ = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="phone-old",
        kind="phone",
        node_label="iPhone",
    )
    live_summary, _ = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="phone-live",
        kind="phone",
        node_label="iPhone",
    )
    satellite, _ = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="jarvis-satellite-1",
        kind="satellite",
    )

    issued = await device_auth_service.issue_pairing_code(owner_id="owner-1")
    result = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(
            code=issued.code,
            node_id="phone-new",
            client_surface="phone",
        ),
        client_key="127.0.0.1",
    )
    await device_auth_service.retire_superseded_credentials(
        owner_id=result.owner_id,
        node_id=result.node_id,
        keep_device_id=result.device_id,
        kind="phone",
        live_node_ids=frozenset({live_summary.node_id}),
    )

    devices = {d.device_id: d for d in await device_auth_service.list_devices(owner_id="owner-1")}
    assert devices[result.device_id].revoked_at is None
    assert devices[offline_summary.device_id].revoked_at is not None
    assert devices[live_summary.device_id].revoked_at is None
    assert devices[satellite.device_id].revoked_at is None


@pytest.mark.asyncio
async def test_update_device_kind(mock_device_db):
    summary, _ = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="browser-existing",
        kind="browser",
    )

    updated = await device_auth_service.update_device_kind(summary.device_id, "desktop")

    devices = await device_auth_service.list_devices(owner_id="owner-1")
    paired = next(device for device in devices if device.device_id == summary.device_id)
    assert updated is True
    assert paired.kind == "desktop"


@pytest.mark.asyncio
async def test_ws_ticket_single_use(mock_device_db):
    _, token = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="browser-1",
        kind="browser",
    )
    ticket = await device_auth_service.mint_ws_ticket(token)
    auth = await device_auth_service.authenticate_ws_ticket(ticket.ticket)
    assert auth.owner_id == "owner-1"
    with pytest.raises(InvalidWsTicketError):
        await device_auth_service.authenticate_ws_ticket(ticket.ticket)


@pytest.mark.asyncio
async def test_revoked_device_rejected(mock_device_db):
    summary, token = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="browser-1",
        kind="browser",
    )
    await device_auth_service.revoke_device(summary.device_id)
    with pytest.raises(InvalidDeviceTokenError):
        await device_auth_service.mint_ws_ticket(token)


@pytest.mark.asyncio
async def test_disconnected_device_cannot_mint_ticket_until_resume(mock_device_db):
    summary, token = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="bedroom-sat",
        kind="satellite",
    )
    assert await device_auth_service.disconnect_device(summary.device_id) is True
    listed = await device_auth_service.list_devices(owner_id="owner-1")
    held = next(item for item in listed if item.device_id == summary.device_id)
    assert held.revoked_at is None
    assert held.disconnected_at is not None

    with pytest.raises(DeviceDisconnectedError):
        await device_auth_service.mint_ws_ticket(token)

    assert await device_auth_service.resume_device(summary.device_id) is True
    ticket = await device_auth_service.mint_ws_ticket(token)
    auth = await device_auth_service.authenticate_ws_ticket(ticket.ticket)
    assert auth.device_id == summary.device_id


@pytest.mark.asyncio
async def test_disconnected_device_rejects_in_flight_ticket(mock_device_db):
    summary, token = await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="bedroom-sat",
        kind="satellite",
    )
    ticket = await device_auth_service.mint_ws_ticket(token)
    assert await device_auth_service.disconnect_device(summary.device_id) is True
    with pytest.raises(InvalidWsTicketError, match="disconnected"):
        await device_auth_service.authenticate_ws_ticket(ticket.ticket)


@pytest.mark.asyncio
async def test_pairing_attempt_rate_limit(mock_device_db, monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_PAIRING_MAX_ATTEMPTS", 2)
    for _ in range(2):
        with pytest.raises(InvalidPairingCodeError):
            await device_auth_service.consume_pairing_code(
                PairConsumeRequest(code="ZZZZ-9999", node_id="browser-1"),
                client_key="10.0.0.5",
            )
    with pytest.raises(PairingRateLimitError) as exc:
        await device_auth_service.consume_pairing_code(
            PairConsumeRequest(code="ZZZZ-9999", node_id="browser-1"),
            client_key="10.0.0.5",
        )
    assert "Too many pairing attempts" in str(exc.value)


@pytest.mark.asyncio
async def test_pairing_clamps_client_capabilities_to_issued_allowance(mock_device_db):
    issued = await device_auth_service.issue_pairing_code(
        owner_id="owner-1",
        capabilities=["mic", "display"],
    )
    response = await device_auth_service.consume_pairing_code(
        PairConsumeRequest(
            code=issued.code,
            node_id="browser-1",
            capabilities="mic,speaker,display",
        ),
        client_key="127.0.0.1",
    )
    doc = await mock_device_db.devices.find_one({"device_id": response.device_id})
    assert doc["capabilities"] == ["mic", "display"]


def test_origin_allowed_respects_frontend_origin():
    service = device_auth_service
    assert service.origin_allowed(settings.FRONTEND_ORIGIN) is True
    assert service.origin_allowed("https://evil.example") is False
    assert (
        service.origin_allowed(
            "https://macbook-pro.example.ts.net:8443",
            request_host="macbook-pro.example.ts.net:8443",
        )
        is True
    )
    assert (
        service.origin_allowed(
            "https://evil.example",
            request_host="macbook-pro.example.ts.net:8443",
        )
        is False
    )


@pytest.mark.asyncio
async def test_update_node_location_persists_on_credentials(mock_device_db):
    location = DeviceLocation(
        provider="home_assistant",
        room_id="bedroom",
        room_name="Bedroom",
        ha_area_id="area-bedroom",
    )
    await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="sat-1",
        kind="satellite",
    )
    modified = await device_auth_service.update_node_location(
        owner_id="owner-1",
        node_id="sat-1",
        location=location,
    )
    assert modified == 1
    stored = await device_auth_service.get_node_location(owner_id="owner-1", node_id="sat-1")
    assert stored is not None
    assert stored.ha_area_id == "area-bedroom"


@pytest.mark.asyncio
async def test_resolve_location_ref_for_bound_room(mock_device_db):
    location = DeviceLocation(
        provider="home_assistant",
        room_id="bedroom",
        room_name="Bedroom",
        ha_area_id="area-bedroom",
    )
    await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="sat-1",
        location=location,
        kind="satellite",
    )
    ref = await device_auth_service.resolve_location_ref_for_area_name(
        owner_id="owner-1",
        area_name="bedroom",
    )
    assert ref is not None
    assert ref["ha_area_id"] == "area-bedroom"
    assert await device_auth_service.list_bound_room_names(owner_id="owner-1") == ["Bedroom"]


@pytest.mark.asyncio
async def test_update_area_room_name_updates_cached_binding_label(mock_device_db):
    location = DeviceLocation(
        provider="home_assistant",
        room_id="bedroom",
        room_name="Bedroom",
        ha_area_id="area-bedroom",
    )
    await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="sat-1",
        location=location,
        kind="satellite",
    )

    nodes = await device_auth_service.update_area_room_name(
        owner_id="owner-1",
        ha_area_id="area-bedroom",
        room_name="Main Bedroom",
    )

    assert nodes == ["sat-1"]
    stored = await device_auth_service.get_node_location(owner_id="owner-1", node_id="sat-1")
    assert stored is not None
    assert stored.ha_area_id == "area-bedroom"
    assert stored.room_name == "Main Bedroom"
    assert stored.room_id == "main_bedroom"


@pytest.mark.asyncio
async def test_clear_area_location_removes_deleted_area_binding(mock_device_db):
    location = DeviceLocation(
        provider="home_assistant",
        room_id="bedroom",
        room_name="Bedroom",
        ha_area_id="area-bedroom",
    )
    await device_auth_service.create_device_credential(
        owner_id="owner-1",
        node_id="sat-1",
        location=location,
        kind="satellite",
    )

    nodes = await device_auth_service.clear_area_location(
        owner_id="owner-1",
        ha_area_id="area-bedroom",
    )

    assert nodes == ["sat-1"]
    assert await device_auth_service.get_node_location(owner_id="owner-1", node_id="sat-1") is None


def test_build_presence_from_auth_ignores_forged_owner():
    from api.websockets.presence import build_presence_from_auth
    from core.auth.device_models import DeviceAuthResult

    auth = DeviceAuthResult(
        device_id="dev-1",
        owner_id="trusted-owner",
        node_id="browser-1",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="browser",
    )
    presence = build_presence_from_auth(auth, {"owner_id": "forged", "node_id": "forged-node"})
    assert presence.owner_id == "trusted-owner"
    assert presence.node_id == "browser-1"
