"""Contract tests for AuthManager vault custody and lifecycle OAuth grants."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from core.auth.exceptions import ScopeGapError
from core.auth.manager import AuthManager
from core.auth.models import OAuthToken, ProviderConfig
from core.credentials.store import CredentialStore
from core.integrations.lifecycle.oauth import (
    disconnect_grant,
    start_authorize,
)
from core.integrations.manager import IntegrationManager, NeedsReauth
from plugins.calendar.providers.google import GOOGLE_CALENDAR_SCOPES
from plugins.gmail.client import GMAIL_SCOPES


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def find_one(self, query: dict) -> dict | None:
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                return dict(doc)
        return None

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        doc = next(
            (
                existing
                for existing in self.docs
                if all(existing.get(key) == value for key, value in query.items())
            ),
            None,
        )
        if doc is None:
            if not upsert:
                return
            doc = dict(query)
            self.docs.append(doc)
        if "$set" in update:
            doc.update(update["$set"])
        if "$unset" in update:
            for key in update["$unset"]:
                doc.pop(key, None)

    async def delete_many(self, query: dict) -> None:
        self.docs = [
            doc
            for doc in self.docs
            if not all(doc.get(key) == value for key, value in query.items())
        ]


class FakeMongo:
    def __init__(self) -> None:
        self.db = {
            "oauth_tokens": FakeCollection(),
            "oauth_provider_configs": FakeCollection(),
        }


@pytest.fixture
def auth_harness(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    monkeypatch.setattr("core.credentials.store._CREDENTIALS_DIR", cred_dir)
    monkeypatch.setattr("core.credentials.store._ENCRYPTED_FILE", cred_dir / "secrets.enc")
    monkeypatch.setattr("core.credentials.store._SALT_FILE", cred_dir / "secrets.salt")
    monkeypatch.setenv("JARVIS_CREDENTIAL_PASSPHRASE", "test-passphrase")
    store = CredentialStore()
    monkeypatch.setattr("core.auth.manager.credential_store", store)

    mongo = FakeMongo()
    monkeypatch.setattr("services.database.mongodb.mongodb", mongo)

    manager = AuthManager()
    monkeypatch.setattr("core.auth.manager.auth_manager", manager)
    monkeypatch.setattr("core.integrations.lifecycle.oauth.auth_manager", manager)
    return manager, store, mongo


def _token(**overrides) -> OAuthToken:
    now = datetime.now(timezone.utc)
    values = dict(
        provider="google",
        account_email="user@gmail.com",
        access_token="access-1",
        refresh_token="refresh-1",
        token_expiry=now + timedelta(hours=1),
        granted_scopes=["https://www.googleapis.com/auth/calendar.events"],
        created_at=now,
        last_refreshed_at=now,
    )
    values.update(overrides)
    return OAuthToken(**values)


def _google_config() -> ProviderConfig:
    return ProviderConfig(
        provider="google",
        client_id="cid",
        client_secret="secret",
        token_uri="https://oauth2.googleapis.com/token",
        auth_uri="https://accounts.google.com/o/oauth2/auth",
    )


@pytest.mark.asyncio
async def test_store_token_keeps_secrets_out_of_mongo(auth_harness):
    manager, store, mongo = auth_harness
    await manager.store_token(_token())

    doc = await mongo.db["oauth_tokens"].find_one({"provider": "google"})
    assert doc is not None
    assert "access_token" not in doc
    assert "refresh_token" not in doc
    assert doc["account_email"] == "user@gmail.com"

    loaded = await manager.get_token("google")
    assert loaded.access_token == "access-1"
    assert loaded.refresh_token == "refresh-1"
    payload = json.loads(store.get_stored_secret("oauth.google"))
    assert payload["access_token"] == "access-1"


@pytest.mark.asyncio
async def test_lifts_legacy_mongo_secrets_then_unsets(auth_harness):
    manager, _store, mongo = auth_harness
    now = datetime.now(timezone.utc)
    mongo.db["oauth_tokens"].docs.append(
        {
            "provider": "google",
            "account_email": "user@gmail.com",
            "access_token": "legacy-access",
            "refresh_token": "legacy-refresh",
            "token_expiry": now + timedelta(hours=1),
            "granted_scopes": ["openid"],
            "created_at": now,
            "last_refreshed_at": now,
        }
    )

    loaded = await manager.get_token("google")
    assert loaded.access_token == "legacy-access"
    assert loaded.refresh_token == "legacy-refresh"

    doc = await mongo.db["oauth_tokens"].find_one({"provider": "google"})
    assert "access_token" not in doc
    assert "refresh_token" not in doc

    manager._token_cache.clear()
    again = await manager.get_token("google")
    assert again.access_token == "legacy-access"


@pytest.mark.asyncio
async def test_ensure_scopes_requires_exact_identifier(auth_harness):
    manager, _, _ = auth_harness
    await manager.store_token(_token(granted_scopes=["https://www.googleapis.com/auth/calendar"]))

    with pytest.raises(ScopeGapError) as exc:
        await manager.ensure_scopes("google", ["calendar"])
    assert exc.value.missing_scopes == ["calendar"]

    matched = await manager.ensure_scopes(
        "google", ["https://www.googleapis.com/auth/calendar"]
    )
    assert matched.access_token == "access-1"


@pytest.mark.asyncio
async def test_empty_refresh_token_preserves_existing(auth_harness):
    manager, _, _ = auth_harness
    await manager.store_token(_token())
    await manager.store_token(_token(access_token="access-2", refresh_token=""))

    loaded = await manager.get_token("google")
    assert loaded.access_token == "access-2"
    assert loaded.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_concurrent_refresh_hits_provider_once(auth_harness, monkeypatch):
    manager, _, _ = auth_harness
    await manager.store_token(_token(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1)))

    async def _config(_provider):
        return _google_config(), "product"

    monkeypatch.setattr("core.auth.providers.resolve_provider_config", _config)
    posts = 0

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            nonlocal posts
            posts += 1
            await asyncio.sleep(0.05)
            return _Response()

    monkeypatch.setattr("core.auth.manager.httpx.AsyncClient", lambda **kwargs: _Client())

    first, second = await asyncio.gather(
        manager.refresh_token("google"),
        manager.refresh_token("google"),
    )
    assert posts == 1
    assert first.access_token == "rotated-access"
    assert second.access_token == "rotated-access"
    assert first.refresh_token == "rotated-refresh"
    assert second.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503])
async def test_refresh_non_grant_error_does_not_clear_grant(auth_harness, monkeypatch, status):
    manager, store, _ = auth_harness
    await manager.store_token(_token(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1)))

    async def _config(_provider):
        return _google_config(), "product"

    monkeypatch.setattr("core.auth.providers.resolve_provider_config", _config)

    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    response = httpx.Response(status, request=request)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    monkeypatch.setattr("core.auth.manager.httpx.AsyncClient", lambda **kwargs: _Client())

    with pytest.raises(httpx.HTTPStatusError):
        await manager.refresh_token("google")

    assert store.get_stored_secret("oauth.google")
    loaded = await manager.peek_grant("google")
    assert loaded is not None
    assert loaded.refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_invalid_grant_clears_grant(auth_harness, monkeypatch):
    manager, store, mongo = auth_harness
    await manager.store_token(_token(token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1)))

    async def _config(_provider):
        return _google_config(), "product"

    monkeypatch.setattr("core.auth.providers.resolve_provider_config", _config)
    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    response = httpx.Response(400, json={"error": "invalid_grant"}, request=request)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.HTTPStatusError("rejected", request=request, response=response)

    monkeypatch.setattr("core.auth.manager.httpx.AsyncClient", lambda **kwargs: _Client())

    with pytest.raises(NeedsReauth):
        await manager.refresh_token("google")

    assert store.get_stored_secret("oauth.google") is None
    assert await mongo.db["oauth_tokens"].find_one({"provider": "google"}) is None


@pytest.mark.asyncio
async def test_disconnect_grant_keeps_provider_config(auth_harness, monkeypatch):
    manager, store, mongo = auth_harness
    await manager.store_provider_config(_google_config())
    await manager.store_token(_token())

    async def _revoke(token):
        return True

    monkeypatch.setattr("core.integrations.lifecycle.oauth._revoke_google", _revoke)
    monkeypatch.setattr(
        "core.integrations.lifecycle.oauth._evict_provider_clients",
        AsyncMock(),
    )

    result = await disconnect_grant("google")
    assert result.remote_disconnected is True
    assert store.get_stored_secret("oauth.google") is None
    assert await mongo.db["oauth_tokens"].find_one({"provider": "google"}) is None
    config = await manager.get_provider_config("google")
    assert config.client_id == "cid"


def _install_product_oauth(monkeypatch, tmp_path, **providers: dict) -> None:
    path = tmp_path / "product_oauth.json"
    path.write_text(json.dumps(providers), encoding="utf-8")
    monkeypatch.setenv("JARVIS_PRODUCT_OAUTH", str(path))


@pytest.mark.asyncio
async def test_start_authorize_calendar_omits_gmail_scopes(monkeypatch, tmp_path):
    _install_product_oauth(
        monkeypatch,
        tmp_path,
        google={"client_id": "cid", "client_secret": "secret"},
    )

    mgr = IntegrationManager()
    mgr.register("calendar", lambda _c: object())
    mgr.register_aux_provider_scopes(
        "google",
        GOOGLE_CALENDAR_SCOPES,
        integration_name="calendar",
    )
    mgr.register(
        "gmail",
        lambda _c: object(),
        provider="google",
        required_scopes=GMAIL_SCOPES,
    )
    monkeypatch.setattr("core.integrations.lifecycle.oauth.integrations", mgr)

    url = await start_authorize(
        "google",
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        plugin="calendar",
    )
    assert "calendar.events" in url
    assert "gmail.modify" not in url
    assert "gmail.readonly" not in url


@pytest.mark.asyncio
async def test_start_authorize_gmail_omits_calendar_scopes(monkeypatch, tmp_path):
    _install_product_oauth(
        monkeypatch,
        tmp_path,
        google={"client_id": "cid", "client_secret": "secret"},
    )

    mgr = IntegrationManager()
    mgr.register("calendar", lambda _c: object())
    mgr.register_aux_provider_scopes(
        "google",
        GOOGLE_CALENDAR_SCOPES,
        integration_name="calendar",
    )
    mgr.register(
        "gmail",
        lambda _c: object(),
        provider="google",
        required_scopes=GMAIL_SCOPES,
    )
    monkeypatch.setattr("core.integrations.lifecycle.oauth.integrations", mgr)

    url = await start_authorize(
        "google",
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        plugin="gmail",
    )
    assert "gmail.modify" in url
    assert "gmail.readonly" in url
    assert "calendar.events" not in url
    assert "calendar.calendarlist" not in url


def test_get_scopes_for_plugin_does_not_union_gmail():
    mgr = IntegrationManager()
    mgr.register("calendar", lambda _c: object())
    mgr.register_aux_provider_scopes(
        "google",
        GOOGLE_CALENDAR_SCOPES,
        integration_name="calendar",
    )
    mgr.register(
        "gmail",
        lambda _c: object(),
        provider="google",
        required_scopes=GMAIL_SCOPES,
    )
    calendar_scopes = mgr.get_scopes_for_plugin("calendar", "google")
    assert set(GOOGLE_CALENDAR_SCOPES) <= set(calendar_scopes)
    assert not set(GMAIL_SCOPES) & set(calendar_scopes)
    union = mgr.get_scopes_for_provider("google")
    assert set(GMAIL_SCOPES) <= set(union)
