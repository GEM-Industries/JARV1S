"""Tests for OAuth provider metadata and PKCE flow helpers."""

from __future__ import annotations

import base64
import hashlib

import pytest

from core.auth import oauth_flow
from core.auth.models import ProviderConfig
from core.auth.oauth_flow import (
    build_authorize_url,
    consume_callback_nonce,
    consume_flow,
    generate_pkce_pair,
    issue_callback_nonce,
    issue_flow,
    token_exchange_payload,
)
from core.auth.providers import (
    has_product_metadata,
    provider_config_from_product,
    resolve_provider_config,
    scopes_for_provider,
)


def test_generate_pkce_pair_s256():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected


def test_issue_and_consume_flow_round_trip():
    state, challenge = issue_flow(
        "google",
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        scopes=["openid", "email"],
    )
    assert state.startswith("google:")
    assert challenge

    flow = consume_flow(state)
    assert flow is not None
    assert flow.provider == "google"
    assert flow.code_verifier
    assert consume_flow(state) is None


def test_issue_and_consume_callback_nonce_round_trip():
    nonce = issue_callback_nonce("gmail")
    assert consume_callback_nonce(nonce) == "gmail"
    assert consume_callback_nonce(nonce) is None


def test_consume_flow_rejects_expired(monkeypatch):
    state, _ = issue_flow(
        "microsoft",
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        scopes=["openid"],
    )
    _, nonce = state.split(":", 1)
    flow = oauth_flow._pending[nonce]
    oauth_flow._pending[nonce] = oauth_flow.PendingOAuthFlow(
        provider=flow.provider,
        code_verifier=flow.code_verifier,
        redirect_uri=flow.redirect_uri,
        scopes=flow.scopes,
        expires_at=0,
    )
    assert consume_flow(state) is None


def test_build_authorize_url_includes_pkce():
    config = ProviderConfig(
        provider="google",
        client_id="cid",
        client_secret="secret",
        token_uri="https://oauth2.googleapis.com/token",
        auth_uri="https://accounts.google.com/o/oauth2/auth",
    )
    url = build_authorize_url(
        config,
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        scopes=["openid"],
        state="google:abc",
        code_challenge="challenge123",
    )
    assert "code_challenge=challenge123" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=cid" in url


def test_token_exchange_payload_includes_verifier():
    config = ProviderConfig(
        provider="microsoft",
        client_id="cid",
        client_secret=None,
        token_uri="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        auth_uri="https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize",
    )
    payload = token_exchange_payload(
        config,
        code="auth-code",
        redirect_uri="http://localhost:5173/api/v1/auth/oauth/callback",
        code_verifier="verifier",
    )
    assert payload["code_verifier"] == "verifier"
    assert "client_secret" not in payload


def test_product_metadata_from_settings(monkeypatch):
    monkeypatch.setattr(
        "core.auth.providers.settings.GOOGLE_OAUTH_CLIENT_ID", "google-cid"
    )
    monkeypatch.setattr(
        "core.auth.providers.settings.GOOGLE_OAUTH_CLIENT_SECRET", "secret"
    )
    assert has_product_metadata("google")
    config = provider_config_from_product("google")
    assert config is not None
    assert config.client_id == "google-cid"
    assert config.client_secret == "secret"


@pytest.mark.asyncio
async def test_resolve_provider_config_prefers_product(monkeypatch):
    monkeypatch.setattr(
        "core.auth.providers.settings.GOOGLE_OAUTH_CLIENT_ID", "product-cid"
    )
    monkeypatch.setattr("core.auth.providers.settings.GOOGLE_OAUTH_CLIENT_SECRET", None)

    async def _fail(_provider: str):
        raise AssertionError(
            "should not read stored config when product metadata exists"
        )

    monkeypatch.setattr(
        "core.auth.manager.auth_manager.get_provider_config",
        _fail,
    )
    config, mode = await resolve_provider_config("google")
    assert mode == "product"
    assert config.client_id == "product-cid"


def test_scopes_for_provider_merges_base_and_registered():
    scopes = scopes_for_provider("google", ["https://www.googleapis.com/auth/calendar"])
    assert "openid" in scopes
    assert "https://www.googleapis.com/auth/userinfo.email" in scopes
    assert "https://www.googleapis.com/auth/calendar" in scopes
