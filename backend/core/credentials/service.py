"""Product credential card registry and mutations."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.credentials.models import (
    CredentialActionResult,
    CredentialCard,
    CredentialCardStatus,
    CredentialValidationResult,
    CredentialsListResponse,
    ExternalTriggersStatus,
)
from core.credentials.store import CredentialMode, credential_store
from core.integrations.composio_gateway import reset_composio_gateway
from core.integrations.manager import integrations
from core.setup.placeholders import is_placeholder_api_key
from core.voice.config import resolve_voice_config_sync
from core.voice.service import ensure_voice_config_available


@dataclass(frozen=True, slots=True)
class _CredentialSpec:
    id: str
    label: str
    description: str
    secret_name: str
    missing_action: str
    stored_detail: str | None = None


_PRODUCT_CREDENTIALS: tuple[_CredentialSpec, ...] = (
    _CredentialSpec(
        id="cartesia",
        label="Cartesia voice",
        description="Cloud speech input and spoken replies",
        secret_name="CARTESIA_API_KEY",
        missing_action="Paste your Cartesia API key to enable cloud voice.",
        stored_detail="Stored securely on this Jarvis Host.",
    ),
    _CredentialSpec(
        id="exa",
        label="Exa search upgrade",
        description="Optional quality upgrade — built-in search already works without a key",
        secret_name="EXA_API_KEY",
        missing_action="Paste your Exa API key for higher-quality search.",
        stored_detail="Exa search upgrade is active.",
    ),
    _CredentialSpec(
        id="composio",
        label="Composio",
        description="Connect third-party apps and tools",
        secret_name="COMPOSIO_API_KEY",
        missing_action="Paste your Composio API key to browse and connect apps.",
        stored_detail="Composio broker is available — connect apps in Apps.",
    ),
    _CredentialSpec(
        id="background_agents",
        label="Background agents",
        description="Delegated research, integration scans, and code-mode agents",
        secret_name="ANTHROPIC_API_KEY",
        missing_action="Paste your Anthropic API key to enable background agents.",
        stored_detail="Background agent runtime is available.",
    ),
)

_SPEC_BY_ID = {spec.id: spec for spec in _PRODUCT_CREDENTIALS}


def _voice_detail() -> str | None:
    config = resolve_voice_config_sync()
    if config.tts_provider != "cartesia" or not config.cartesia_voice_id:
        return "Cartesia key stored. Choose Cartesia under Spoken replies to enable cloud voice."
    if config.stt_provider != "cartesia":
        return (
            "Cartesia key stored. Select Cartesia in Voice settings to use cloud voice input."
        )
    return None


def _build_card(spec: _CredentialSpec) -> CredentialCard:
    stored = credential_store.get_stored_secret(spec.secret_name)
    env_only = credential_store.has_env_only_secret(spec.secret_name)

    if stored:
        status = CredentialCardStatus.STORED
        source = credential_store.stored_source_for_secret(spec.secret_name)
        masked = credential_store.mask_secret(stored)
        next_action = None
        # Keep product-specific status copy. Generic "stored securely" messaging
        # belongs once in the Settings shell, not on every card.
        detail = spec.stored_detail
        if spec.id == "cartesia":
            detail = _voice_detail() or detail
    elif env_only:
        status = CredentialCardStatus.ENV_DEPRECATED
        source = CredentialMode.ENV.value
        masked = credential_store.mask_secret(credential_store.get_secret(spec.secret_name))
        next_action = "Save the key in Settings — .env is no longer used for this capability."
        detail = "Legacy .env value detected. Move it here to keep managing it in the app."
    else:
        status = CredentialCardStatus.MISSING
        source = None
        masked = None
        next_action = spec.missing_action
        detail = None

    return CredentialCard(
        id=spec.id,
        label=spec.label,
        description=spec.description,
        secret_name=spec.secret_name,
        status=status,
        source=source.value if isinstance(source, CredentialMode) else source,
        masked_suffix=masked,
        next_action=next_action,
        detail=detail,
    )


async def list_credentials() -> CredentialsListResponse:
    from core.integrations.external_ingress import get_external_ingress_state

    ingress = await get_external_ingress_state()
    base = ingress.base_url or ""
    return CredentialsListResponse(
        items=[_build_card(spec) for spec in _PRODUCT_CREDENTIALS],
        external_triggers=ExternalTriggersStatus(
            enabled=ingress.enabled,
            base_url=base,
            provider=ingress.provider,
            last_received_at=ingress.last_received_at.isoformat() if ingress.last_received_at else None,
            inbox_pending=ingress.inbox_pending,
            inbox_dead_letter=ingress.inbox_dead_letter,
            last_error=ingress.last_error,
            detail=ingress.detail
            or (
                "Managed in Settings → Availability → External triggers."
                if ingress.enabled
                else "Off — polling still active. Enable External triggers in Availability."
            ),
        ),
    )


def get_spec(credential_id: str) -> _CredentialSpec:
    spec = _SPEC_BY_ID.get(credential_id)
    if spec is None:
        raise KeyError(credential_id)
    return spec


async def _apply_side_effects(secret_name: str) -> None:
    if secret_name == "EXA_API_KEY":
        await integrations.reset("search")
    elif secret_name == "COMPOSIO_API_KEY":
        await reset_composio_gateway()
        await integrations.reset()
    elif secret_name == "CARTESIA_API_KEY":
        await ensure_voice_config_available()
        from api.websockets.handlers import tts
        from core.voice.config import resolve_voice_config_sync

        await tts.close()
        if (
            credential_store.get_stored_secret("CARTESIA_API_KEY")
            and resolve_voice_config_sync().tts_provider == "cartesia"
        ):
            await tts.initialize()
    elif secret_name == "ANTHROPIC_API_KEY":
        from core.setup.runtime import jarvis_runtime

        await jarvis_runtime.initialize_if_ready(force=True)


async def save_credential(credential_id: str, value: str) -> CredentialActionResult:
    spec = get_spec(credential_id)
    stripped = value.strip()
    if not stripped:
        raise ValueError("API key is required.")
    if is_placeholder_api_key(stripped):
        raise ValueError("That value looks like a placeholder.")
    if credential_id == "cartesia":
        validation = await validate_credential(credential_id, stripped)
        if not validation.ok:
            raise ValueError(validation.message)

    credential_store.set_secret(spec.secret_name, stripped)
    await _apply_side_effects(spec.secret_name)
    card = _build_card(spec)
    return CredentialActionResult(ok=True, message=f"{spec.label} saved.", card=card)


async def remove_credential(credential_id: str) -> CredentialActionResult:
    spec = get_spec(credential_id)
    credential_store.delete_secret(spec.secret_name)
    await _apply_side_effects(spec.secret_name)
    card = _build_card(spec)
    return CredentialActionResult(ok=True, message=f"{spec.label} removed.", card=card)


async def validate_credential(
    credential_id: str, value: str
) -> CredentialValidationResult:
    spec = get_spec(credential_id)
    stripped = value.strip()
    if not stripped:
        return CredentialValidationResult(ok=False, message="API key is required.")
    if is_placeholder_api_key(stripped):
        return CredentialValidationResult(ok=False, message="That value looks like a placeholder.")
    if len(stripped) < 8:
        return CredentialValidationResult(ok=False, message="API key looks too short.")
    if credential_id == "cartesia":
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    "https://api.cartesia.ai/voices",
                    params={"limit": 1},
                    headers={
                        "X-API-Key": stripped,
                        "Cartesia-Version": "2026-03-01",
                    },
                )
            if response.status_code in {401, 403}:
                return CredentialValidationResult(
                    ok=False, message="Cartesia rejected this API key."
                )
            if response.status_code == 429:
                return CredentialValidationResult(
                    ok=False,
                    message="Cartesia rate-limited the check. Wait a moment and retry.",
                )
            if response.status_code >= 400:
                return CredentialValidationResult(
                    ok=False,
                    message="Cartesia could not validate this key right now.",
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            return CredentialValidationResult(
                ok=False,
                message="Could not reach Cartesia. Check your connection and retry.",
            )
        return CredentialValidationResult(
            ok=True, message="Cartesia API key validated."
        )
    return CredentialValidationResult(ok=True, message=f"{spec.label} key format looks valid.")
