"""Per-device WebSocket credential lifecycle."""

from __future__ import annotations

import ipaddress
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from core.config import settings
from core.id import generate_id
from services.database.mongodb import mongodb

from .device_models import (
    DeviceAuthResult,
    DeviceCredentialRecord,
    DeviceCredentialSummary,
    DeviceKind,
    DeviceLocation,
    PairConsumeRequest,
    PairConsumeResult,
    PairingCodeIssueResult,
    WsTicketResponse,
    device_kind_override_for_client_surface,
)
from .device_tokens import (
    format_device_token,
    generate_device_secret,
    generate_pairing_code,
    generate_ws_ticket,
    hash_secret,
    normalize_pairing_code,
    parse_device_token,
    verify_secret,
)

DEVICES_COLLECTION = "ws_device_credentials"
PAIRING_COLLECTION = "ws_pairing_codes"
TICKETS_COLLECTION = "ws_tickets"
PAIRING_ATTEMPTS_COLLECTION = "ws_pairing_attempts"


class DeviceAuthError(Exception):
    """Base device auth failure."""


class InvalidDeviceTokenError(DeviceAuthError):
    pass


class InvalidPairingCodeError(DeviceAuthError):
    pass


class InvalidWsTicketError(DeviceAuthError):
    pass


class DeviceDisconnectedError(DeviceAuthError):
    pass


class PairingRateLimitError(DeviceAuthError):
    pass


class DeviceAuthService:
    def _devices(self):
        return mongodb.get_collection(DEVICES_COLLECTION)

    def _pairing(self):
        return mongodb.get_collection(PAIRING_COLLECTION)

    def _tickets(self):
        return mongodb.get_collection(TICKETS_COLLECTION)

    def _pairing_attempts(self):
        return mongodb.get_collection(PAIRING_ATTEMPTS_COLLECTION)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _parse_capabilities(
        self, raw: str | None, *, fallback: list[str] | None = None
    ) -> list[str]:
        if not raw:
            return list(fallback or ["mic", "speaker", "display"])
        values = [part.strip().lower() for part in raw.split(",") if part.strip()]
        return values or list(fallback or ["mic", "speaker", "display"])

    def _clamp_capabilities(
        self, requested: list[str], allowed: list[str]
    ) -> list[str]:
        allowed_set = set(allowed)
        clamped = [value for value in requested if value in allowed_set]
        return clamped or list(allowed)

    def _location_from_request(
        self,
        *,
        provider: str | None,
        room_id: str | None,
        room_name: str | None,
        ha_area_id: str | None,
        ha_device_id: str | None,
        ha_entity_id: str | None,
        fallback: DeviceLocation | None = None,
    ) -> DeviceLocation:
        if not any(
            (provider, room_id, room_name, ha_area_id, ha_device_id, ha_entity_id)
        ):
            return fallback or DeviceLocation()
        normalized_provider = (provider or "").strip() or "unknown"
        if normalized_provider not in {"manual", "home_assistant", "unknown"}:
            normalized_provider = "unknown"
        if (room_id or room_name) and normalized_provider == "unknown":
            normalized_provider = "manual"
        if (
            ha_area_id or ha_device_id or ha_entity_id
        ) and normalized_provider == "unknown":
            normalized_provider = "home_assistant"
        return DeviceLocation(
            provider=normalized_provider,  # type: ignore[arg-type]
            room_id=room_id,
            room_name=room_name,
            ha_area_id=ha_area_id,
            ha_device_id=ha_device_id,
            ha_entity_id=ha_entity_id,
        )

    async def create_device_credential(
        self,
        *,
        owner_id: str | None = None,
        node_id: str,
        node_label: str | None = None,
        capabilities: list[str] | None = None,
        location: DeviceLocation | None = None,
        kind: DeviceKind = "satellite",
    ) -> tuple[DeviceCredentialSummary, str]:
        resolved_owner = owner_id or settings.DEFAULT_USER_ID
        device_id = generate_id("dev-")
        secret = generate_device_secret()
        record = DeviceCredentialRecord(
            device_id=device_id,
            owner_id=resolved_owner,
            node_id=node_id,
            node_label=node_label,
            capabilities=capabilities or ["mic", "speaker"],
            location=location or DeviceLocation(),
            token_hash=hash_secret(secret),
            kind=kind,
        )
        await self._devices().insert_one(record.model_dump(mode="json"))
        token = format_device_token(device_id, secret)
        return self._to_summary(record), token

    async def create_satellite_credential(
        self,
        *,
        owner_id: str | None = None,
        node_id: str,
        node_label: str | None = None,
        capabilities: list[str] | None = None,
        location: DeviceLocation | None = None,
    ) -> tuple[DeviceCredentialSummary, str]:
        return await self.create_device_credential(
            owner_id=owner_id,
            node_id=node_id,
            node_label=node_label,
            capabilities=capabilities,
            location=location,
            kind="satellite",
        )

    async def issue_pairing_code(
        self,
        *,
        owner_id: str | None = None,
        node_label: str | None = None,
        capabilities: list[str] | None = None,
        location: DeviceLocation | None = None,
        node_id: str | None = None,
    ) -> PairingCodeIssueResult:
        resolved_owner = owner_id or settings.DEFAULT_USER_ID
        code = generate_pairing_code()
        expires_at = self._now() + timedelta(
            seconds=settings.DEVICE_AUTH_PAIRING_CODE_TTL_S
        )
        bound_node = (node_id or "").strip() or None
        doc = {
            "code_hash": hash_secret(normalize_pairing_code(code)),
            "owner_id": resolved_owner,
            "node_label": node_label,
            "capabilities": capabilities or ["mic", "speaker", "display"],
            "location": (location or DeviceLocation()).model_dump(mode="json"),
            "expires_at": expires_at,
            "consumed_at": None,
            "attempt_count": 0,
        }
        if bound_node:
            doc["node_id"] = bound_node
        await self._pairing().insert_one(doc)
        return PairingCodeIssueResult(
            code=code, expires_at=expires_at, owner_id=resolved_owner
        )

    async def _record_pairing_attempt(self, *, client_key: str) -> None:
        now = self._now()
        window_start = now - timedelta(
            seconds=settings.DEVICE_AUTH_PAIRING_ATTEMPT_WINDOW_S
        )
        doc = await self._pairing_attempts().find_one_and_update(
            {"client_key": client_key},
            {
                "$inc": {"attempt_count": 1},
                "$set": {"last_attempt_at": now},
                "$setOnInsert": {"window_started_at": now},
            },
            upsert=True,
            return_document=True,
        )
        window_started = doc.get("window_started_at") if doc else now
        if isinstance(window_started, datetime) and window_started < window_start:
            await self._pairing_attempts().update_one(
                {"client_key": client_key},
                {
                    "$set": {
                        "attempt_count": 1,
                        "window_started_at": now,
                        "last_attempt_at": now,
                    }
                },
            )
            attempt_count = 1
        else:
            attempt_count = int((doc or {}).get("attempt_count") or 1)
        if attempt_count > settings.DEVICE_AUTH_PAIRING_MAX_ATTEMPTS:
            raise PairingRateLimitError("Too many pairing attempts. Try again later.")

    async def consume_pairing_code(
        self,
        request: PairConsumeRequest,
        *,
        client_key: str,
    ) -> PairConsumeResult:
        await self._record_pairing_attempt(client_key=client_key)
        normalized = normalize_pairing_code(request.code)
        if not normalized:
            raise InvalidPairingCodeError("Invalid pairing code")

        node_id = request.node_id.strip()
        if not node_id:
            raise InvalidPairingCodeError("node_id is required")

        now = self._now()
        lookup = {
            "code_hash": hash_secret(normalized),
            "consumed_at": None,
            "expires_at": {"$gt": now},
        }
        found = await self._pairing().find_one(lookup)
        if not found:
            raise InvalidPairingCodeError(
                "Invalid, expired, or already used pairing code"
            )
        expected_node = str(found.get("node_id") or "").strip()
        if expected_node and expected_node != node_id:
            raise InvalidPairingCodeError(
                "Invalid, expired, or already used pairing code"
            )
        doc = await self._pairing().find_one_and_update(
            {"_id": found["_id"], "consumed_at": None},
            {"$set": {"consumed_at": now}},
            return_document=True,
        )
        if not doc:
            raise InvalidPairingCodeError(
                "Invalid, expired, or already used pairing code"
            )

        allowed_capabilities = list(
            doc.get("capabilities") or ["mic", "speaker", "display"]
        )
        requested_capabilities = self._parse_capabilities(
            request.capabilities,
            fallback=allowed_capabilities,
        )
        capabilities = self._clamp_capabilities(
            requested_capabilities, allowed_capabilities
        )
        location = self._location_from_request(
            provider=request.location_provider,
            room_id=request.room_id,
            room_name=request.room_name,
            ha_area_id=request.ha_area_id,
            ha_device_id=request.ha_device_id,
            ha_entity_id=request.ha_entity_id,
            fallback=DeviceLocation.model_validate(doc.get("location") or {}),
        )

        kind = (
            device_kind_override_for_client_surface(request.client_surface) or "browser"
        )
        summary, token = await self.create_device_credential(
            owner_id=str(doc["owner_id"]),
            node_id=node_id,
            node_label=request.node_label or doc.get("node_label"),
            capabilities=capabilities,
            location=location,
            kind=kind,
        )
        await self._pairing().update_one(
            {"_id": doc["_id"]},
            {"$set": {"device_id": summary.device_id}},
        )
        return PairConsumeResult(
            device_id=summary.device_id,
            owner_id=summary.owner_id,
            node_id=summary.node_id,
            device_token=token,
        )

    async def _load_device_by_token(self, device_token: str) -> DeviceCredentialRecord:
        parsed = parse_device_token(device_token)
        if not parsed:
            raise InvalidDeviceTokenError("Malformed device token")
        device_id, secret = parsed
        doc = await self._devices().find_one({"device_id": device_id})
        if not doc:
            raise InvalidDeviceTokenError("Unknown device token")
        record = DeviceCredentialRecord.model_validate(doc)
        if record.revoked_at is not None:
            raise InvalidDeviceTokenError("Device revoked")
        if not verify_secret(secret, record.token_hash):
            raise InvalidDeviceTokenError("Invalid device token")
        return record

    async def authenticate_device_token(self, device_token: str) -> DeviceAuthResult:
        """Validate a durable device token for REST (reusable, not single-use)."""
        record = await self._load_device_by_token(device_token)
        now = self._now()
        if record.last_seen_at is None or record.last_seen_at < now - timedelta(
            minutes=1
        ):
            await self._devices().update_one(
                {"device_id": record.device_id},
                {"$set": {"last_seen_at": now}},
            )
        return DeviceAuthResult(
            device_id=record.device_id,
            owner_id=record.owner_id,
            node_id=record.node_id,
            node_label=record.node_label,
            capabilities=list(record.capabilities),
            location=record.location,
            kind=record.kind,
        )

    async def mint_ws_ticket(self, device_token: str) -> WsTicketResponse:
        record = await self._load_device_by_token(device_token)
        if record.disconnected_at is not None:
            raise DeviceDisconnectedError("Device disconnected")
        ticket = generate_ws_ticket()
        expires_at = self._now() + timedelta(
            seconds=settings.DEVICE_AUTH_WS_TICKET_TTL_S
        )
        await self._tickets().insert_one(
            {
                "ticket_hash": hash_secret(ticket),
                "device_id": record.device_id,
                "owner_id": record.owner_id,
                "expires_at": expires_at,
                "consumed_at": None,
            }
        )
        return WsTicketResponse(ticket=ticket, expires_at=expires_at)

    async def authenticate_ws_ticket(self, ticket: str) -> DeviceAuthResult:
        if not ticket.strip():
            raise InvalidWsTicketError("Missing ticket")
        doc = await self._tickets().find_one_and_update(
            {
                "ticket_hash": hash_secret(ticket),
                "consumed_at": None,
                "expires_at": {"$gt": self._now()},
            },
            {"$set": {"consumed_at": self._now()}},
            return_document=True,
        )
        if not doc:
            raise InvalidWsTicketError("Invalid or expired ticket")

        device_doc = await self._devices().find_one({"device_id": doc["device_id"]})
        if not device_doc:
            raise InvalidWsTicketError("Device not found")
        record = DeviceCredentialRecord.model_validate(device_doc)
        if record.revoked_at is not None:
            raise InvalidWsTicketError("Device revoked")
        if record.disconnected_at is not None:
            raise InvalidWsTicketError("Device disconnected")

        await self._devices().update_one(
            {"device_id": record.device_id},
            {"$set": {"last_seen_at": self._now()}},
        )
        return DeviceAuthResult(
            device_id=record.device_id,
            owner_id=record.owner_id,
            node_id=record.node_id,
            node_label=record.node_label,
            capabilities=list(record.capabilities),
            location=record.location,
            kind=record.kind,
        )

    async def revoke_device(self, device_id: str) -> bool:
        result = await self._devices().update_one(
            {"device_id": device_id, "revoked_at": None},
            {"$set": {"revoked_at": self._now()}},
        )
        return result.modified_count > 0

    async def disconnect_device(self, device_id: str) -> bool:
        result = await self._devices().update_one(
            {"device_id": device_id, "revoked_at": None},
            {"$set": {"disconnected_at": self._now()}},
        )
        return result.modified_count > 0

    async def resume_device(self, device_id: str) -> bool:
        result = await self._devices().update_one(
            {
                "device_id": device_id,
                "revoked_at": None,
                "disconnected_at": {"$ne": None},
            },
            {"$unset": {"disconnected_at": ""}},
        )
        return result.modified_count > 0

    async def retire_superseded_credentials(
        self,
        *,
        owner_id: str,
        node_id: str,
        keep_device_id: str,
        kind: DeviceKind,
        live_node_ids: frozenset[str] = frozenset(),
    ) -> int:
        """Retire older credentials replaced by a successful pairing."""
        query: dict[str, Any] = {
            "owner_id": owner_id,
            "revoked_at": None,
            "device_id": {"$ne": keep_device_id},
        }
        if kind == "phone":
            # The newly paired node is not yet connected. Excluding it from the
            # live set also retires duplicate credentials for the same node.
            other_live_phones = sorted(live_node_ids - {node_id})
            query.update({"kind": "phone", "node_id": {"$nin": other_live_phones}})
        else:
            query["node_id"] = node_id
        result = await self._devices().update_many(
            query,
            {"$set": {"revoked_at": self._now()}},
        )
        return int(result.modified_count)

    async def update_device_kind(self, device_id: str, kind: DeviceKind) -> bool:
        result = await self._devices().update_one(
            {"device_id": device_id, "revoked_at": None},
            {"$set": {"kind": kind}},
        )
        return result.modified_count > 0

    async def list_devices(
        self, *, owner_id: str | None = None
    ) -> list[DeviceCredentialSummary]:
        query: dict[str, Any] = {}
        if owner_id:
            query["owner_id"] = owner_id
        cursor = self._devices().find(query).sort("created_at", -1)
        summaries: list[DeviceCredentialSummary] = []
        async for doc in cursor:
            summaries.append(
                self._to_summary(DeviceCredentialRecord.model_validate(doc))
            )
        return summaries

    async def update_node_location(
        self,
        *,
        owner_id: str,
        node_id: str,
        location: DeviceLocation,
    ) -> int:
        """Persist room binding on all unrevoked credentials for this node."""
        result = await self._devices().update_many(
            {"owner_id": owner_id, "node_id": node_id, "revoked_at": None},
            {"$set": {"location": location.model_dump(mode="json")}},
        )
        return int(result.modified_count)

    async def update_area_room_name(
        self,
        *,
        owner_id: str,
        ha_area_id: str,
        room_name: str,
    ) -> list[str]:
        """Best-effort display cache update for credentials bound to an HA area."""
        query = {
            "owner_id": owner_id,
            "revoked_at": None,
        }
        docs: list[dict[str, Any]] = []
        async for doc in self._devices().find(query):
            location = DeviceLocation.model_validate(doc.get("location") or {})
            if location.ha_area_id == ha_area_id:
                docs.append(doc)
        if not docs:
            return []
        room_id = room_name.strip().lower().replace(" ", "_") or ha_area_id
        for doc in docs:
            location = DeviceLocation.model_validate(doc.get("location") or {})
            updated = location.model_copy(
                update={
                    "provider": "home_assistant",
                    "room_id": room_id,
                    "room_name": room_name,
                }
            )
            await self._devices().update_one(
                {"device_id": doc["device_id"]},
                {"$set": {"location": updated.model_dump(mode="json")}},
            )
        return sorted({str(doc["node_id"]) for doc in docs if doc.get("node_id")})

    async def clear_area_location(
        self,
        *,
        owner_id: str,
        ha_area_id: str,
    ) -> list[str]:
        """Clear endpoint bindings for a deleted HA area."""
        query = {
            "owner_id": owner_id,
            "revoked_at": None,
        }
        docs: list[dict[str, Any]] = []
        async for doc in self._devices().find(query):
            location = DeviceLocation.model_validate(doc.get("location") or {})
            if location.ha_area_id == ha_area_id:
                docs.append(doc)
        if not docs:
            return []
        for doc in docs:
            await self._devices().update_one(
                {"device_id": doc["device_id"]},
                {"$set": {"location": DeviceLocation().model_dump(mode="json")}},
            )
        return sorted({str(doc["node_id"]) for doc in docs if doc.get("node_id")})

    async def get_node_location(
        self, *, owner_id: str, node_id: str
    ) -> DeviceLocation | None:
        doc = await self._devices().find_one(
            {"owner_id": owner_id, "node_id": node_id, "revoked_at": None},
            sort=[("last_seen_at", -1)],
        )
        if not doc:
            return None
        location = DeviceLocation.model_validate(doc.get("location") or {})
        if not location.ha_area_id and not location.room_name:
            return None
        return location

    async def list_bound_room_names(self, *, owner_id: str) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        async for doc in self._devices().find(
            {"owner_id": owner_id, "revoked_at": None}
        ):
            location = DeviceLocation.model_validate(doc.get("location") or {})
            label = (
                location.room_name or location.room_id or location.ha_area_id or ""
            ).strip()
            if not label:
                continue
            key = self._normalize_location_name(label)
            if key in seen:
                continue
            seen.add(key)
            names.append(label)
        return sorted(names, key=str.casefold)

    @staticmethod
    def _normalize_location_name(value: str) -> str:
        return " ".join(value.strip().casefold().split())

    @classmethod
    def _location_matches_area_name(
        cls, location: DeviceLocation, area_name: str
    ) -> bool:
        needle = cls._normalize_location_name(area_name)
        if not needle:
            return False
        for candidate in (location.room_name, location.room_id, location.ha_area_id):
            if candidate and cls._normalize_location_name(str(candidate)) == needle:
                return True
        return False

    async def resolve_location_ref_for_area_name(
        self,
        *,
        owner_id: str,
        area_name: str,
    ) -> dict[str, str | None] | None:
        async for doc in self._devices().find(
            {"owner_id": owner_id, "revoked_at": None}
        ):
            location = DeviceLocation.model_validate(doc.get("location") or {})
            if not location.ha_area_id and not location.room_name:
                continue
            if not self._location_matches_area_name(location, area_name):
                continue
            return {
                "provider": location.provider,
                "room_id": location.room_id,
                "room_name": location.room_name,
                "ha_area_id": location.ha_area_id,
                "ha_device_id": location.ha_device_id,
                "ha_entity_id": location.ha_entity_id,
            }
        return None

    def _to_summary(self, record: DeviceCredentialRecord) -> DeviceCredentialSummary:
        return DeviceCredentialSummary(
            device_id=record.device_id,
            owner_id=record.owner_id,
            node_id=record.node_id,
            node_label=record.node_label,
            capabilities=list(record.capabilities),
            location=record.location,
            kind=record.kind,
            revoked_at=record.revoked_at,
            disconnected_at=record.disconnected_at,
            created_at=record.created_at,
            last_seen_at=record.last_seen_at,
        )

    @staticmethod
    def is_loopback_host(host: str | None) -> bool:
        normalized = (host or "").strip().lower().strip("[]")
        if normalized == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    def local_bypass_allowed(self, *, host: str | None) -> bool:
        """Trust only direct localhost clients in dev or the packaged Host app."""
        if not self.is_loopback_host(host):
            return False
        packaged_host = os.environ.get("JARVIS_APP_MODE") == "1"
        return packaged_host or (
            settings.DEVICE_AUTH_DEV_BYPASS and settings.is_development
        )

    @staticmethod
    def local_bypass_device_kind() -> DeviceKind:
        """Classify the server-trusted local client."""
        return "desktop" if os.environ.get("JARVIS_APP_MODE") == "1" else "browser"

    def origin_allowed(self, origin: str | None, *, request_host: str | None = None) -> bool:
        if not origin:
            return True
        allowed = {settings.FRONTEND_ORIGIN.rstrip("/")}
        for item in settings.BACKEND_CORS_ORIGINS:
            if item == "*":
                continue
            allowed.add(str(item).rstrip("/"))
        cleaned = origin.rstrip("/")
        if cleaned in allowed:
            return True
        if not request_host:
            return False
        parsed = urlsplit(cleaned)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc.casefold() == request_host.strip().casefold()
        )


device_auth_service = DeviceAuthService()
