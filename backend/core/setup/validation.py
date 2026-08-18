"""Provider credential validation with actionable failure taxonomy."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from core.llm.providers import get_llm_provider
from core.llm.service import LLMService
from core.setup.llm_config import LOCAL_DUMMY_API_KEY
from core.setup.models import ValidationFailureCode, ValidationResult
from core.setup.placeholders import is_placeholder_api_key

logger = logging.getLogger(__name__)

_FAILURE_ACTIONS: dict[ValidationFailureCode, str] = {
    ValidationFailureCode.MISSING_KEY: "Paste your provider API key and try again.",
    ValidationFailureCode.PLACEHOLDER_KEY: "Replace the placeholder with a real API key from your provider.",
    ValidationFailureCode.BAD_KEY: "Check the API key in your provider dashboard and paste it again.",
    ValidationFailureCode.PERMISSION_DENIED: "Verify the key has access to the selected model.",
    ValidationFailureCode.QUOTA_OR_BILLING: "Add billing or credits in your provider account.",
    ValidationFailureCode.RATE_LIMITED: "Wait a moment and try again.",
    ValidationFailureCode.BAD_ENDPOINT: "Use the provider base URL only, without /chat/completions.",
    ValidationFailureCode.MODEL_UNAVAILABLE: "Choose a supported model for this provider.",
    ValidationFailureCode.NETWORK_UNREACHABLE: "Check your internet connection and try again.",
    ValidationFailureCode.TIMEOUT: "The provider took too long to respond. Try again.",
    ValidationFailureCode.PROVIDER_UNAVAILABLE: "The provider is temporarily unavailable. Try again shortly.",
    ValidationFailureCode.UNKNOWN: "Check your provider settings and try again.",
}


def _result(
    code: ValidationFailureCode,
    message: str,
    *,
    recommended_model: str | None = None,
) -> ValidationResult:
    return ValidationResult(
        ok=False,
        code=code,
        message=message,
        next_action=_FAILURE_ACTIONS[code],
        recommended_model=recommended_model,
    )


async def validate_llm_credentials(
    *,
    provider: str,
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout_s: float = 12.0,
) -> ValidationResult:
    preset = get_llm_provider(provider)
    if not preset.requires_api_key:
        return await _validate_local_llm(
            provider=provider,
            model=model,
            base_url=base_url,
            timeout_s=timeout_s,
        )

    if not api_key.strip():
        return _result(ValidationFailureCode.MISSING_KEY, "API key is required.")
    if is_placeholder_api_key(api_key):
        return _result(
            ValidationFailureCode.PLACEHOLDER_KEY, "That API key looks like a placeholder."
        )

    resolved_model = model or preset.model
    resolved_base = (base_url or preset.base_url).rstrip("/")
    if resolved_base.endswith("/chat/completions"):
        return _result(
            ValidationFailureCode.BAD_ENDPOINT,
            "Use the provider base URL only. Remove /chat/completions from the endpoint.",
        )

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            models_response = await client.get(
                f"{resolved_base}/models",
                headers={"Authorization": f"Bearer {api_key.strip()}"},
            )
            if models_response.status_code == 401:
                return _result(ValidationFailureCode.BAD_KEY, "The provider rejected this API key.")
            if models_response.status_code == 403:
                return _result(
                    ValidationFailureCode.PERMISSION_DENIED,
                    "This API key lacks permission for model access.",
                )
            if models_response.status_code == 404:
                return _result(
                    ValidationFailureCode.BAD_ENDPOINT, "The provider endpoint could not be found."
                )
            if models_response.status_code == 429:
                return _result(
                    ValidationFailureCode.RATE_LIMITED,
                    "The provider rate-limited the validation request.",
                )
            if models_response.status_code >= 500:
                return _result(
                    ValidationFailureCode.PROVIDER_UNAVAILABLE,
                    "The provider is temporarily unavailable.",
                )

        llm = LLMService(
            api_key=api_key.strip(),
            base_url=resolved_base,
            model=resolved_model,
            provider_name=preset.name,
            request_timeout_s=timeout_s,
            first_token_timeout_s=min(timeout_s, 8.0),
            first_token_retries=0,
        )
        await llm.initialize()
        await asyncio.wait_for(
            llm.chat("Reply with exactly: ok", dump_tag="setup_validation"),
            timeout=timeout_s,
        )
        return ValidationResult(ok=True, message="Provider credentials validated.")
    except AuthenticationError:
        return _result(ValidationFailureCode.BAD_KEY, "The provider rejected this API key.")
    except PermissionDeniedError:
        return _result(
            ValidationFailureCode.PERMISSION_DENIED,
            "This API key lacks permission for the selected model.",
        )
    except NotFoundError:
        return _result(
            ValidationFailureCode.MODEL_UNAVAILABLE,
            "The selected model or endpoint was not found.",
            recommended_model=preset.model or None,
        )
    except RateLimitError:
        return _result(
            ValidationFailureCode.RATE_LIMITED, "The provider rate-limited the validation request."
        )
    except APITimeoutError:
        return _result(ValidationFailureCode.TIMEOUT, "The provider took too long to respond.")
    except APIConnectionError:
        return _result(ValidationFailureCode.NETWORK_UNREACHABLE, "Could not reach the provider.")
    except httpx.TimeoutException:
        return _result(ValidationFailureCode.TIMEOUT, "The provider took too long to respond.")
    except httpx.ConnectError:
        return _result(ValidationFailureCode.NETWORK_UNREACHABLE, "Could not reach the provider.")
    except asyncio.TimeoutError:
        return _result(ValidationFailureCode.TIMEOUT, "The provider took too long to respond.")
    except Exception as exc:
        message = str(exc).lower()
        if (
            "402" in message
            or "quota" in message
            or "billing" in message
            or "insufficient" in message
        ):
            return _result(
                ValidationFailureCode.QUOTA_OR_BILLING,
                "The provider account needs billing or credits.",
            )
        logger.warning("Unknown provider validation failure: %s", exc)
        return _result(
            ValidationFailureCode.UNKNOWN, "Validation failed. Check your provider settings."
        )


async def _validate_local_llm(
    *,
    provider: str,
    model: Optional[str],
    base_url: Optional[str],
    timeout_s: float,
) -> ValidationResult:
    preset = get_llm_provider(provider)
    resolved_model = model or preset.model
    resolved_base = (base_url or preset.base_url).rstrip("/")
    if not resolved_model:
        return _result(ValidationFailureCode.MODEL_UNAVAILABLE, "Choose a local model to use.")
    if resolved_base.endswith("/chat/completions"):
        return _result(
            ValidationFailureCode.BAD_ENDPOINT,
            "Use the provider base URL only. Remove /chat/completions from the endpoint.",
        )

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            models_response = await client.get(f"{resolved_base}/models")
            if models_response.status_code == 404 and provider == "ollama":
                models_response = await client.get(resolved_base.replace("/v1", "") + "/api/tags")
            if models_response.status_code >= 500:
                return _result(
                    ValidationFailureCode.PROVIDER_UNAVAILABLE,
                    "The local server is temporarily unavailable.",
                )
            if models_response.status_code in {401, 403}:
                return _result(
                    ValidationFailureCode.PERMISSION_DENIED,
                    "The local server requires authentication. Use localhost or configure access in your runtime.",
                )
            if models_response.status_code >= 400 and models_response.status_code not in {404}:
                return _result(
                    ValidationFailureCode.NETWORK_UNREACHABLE,
                    "Could not reach the local model server.",
                )

        llm = LLMService(
            api_key=LOCAL_DUMMY_API_KEY,
            base_url=resolved_base,
            model=resolved_model,
            provider_name=provider,
            request_timeout_s=timeout_s,
            first_token_timeout_s=min(timeout_s, 8.0),
            first_token_retries=0,
        )
        await llm.initialize()
        if not llm.is_initialized:
            return _result(
                ValidationFailureCode.NETWORK_UNREACHABLE,
                "Could not initialize the local model client.",
            )
        await asyncio.wait_for(
            llm.chat("Reply with exactly: ok", dump_tag="setup_validation_local"),
            timeout=timeout_s,
        )
        return ValidationResult(ok=True, message="Local model endpoint validated.")
    except APIConnectionError:
        return _result(
            ValidationFailureCode.NETWORK_UNREACHABLE,
            "Local model server is not running. Start your runtime and try again.",
        )
    except NotFoundError:
        return _result(
            ValidationFailureCode.MODEL_UNAVAILABLE,
            "The selected model was not found on the local server.",
            recommended_model=preset.model or None,
        )
    except APITimeoutError:
        return _result(ValidationFailureCode.TIMEOUT, "The local server took too long to respond.")
    except httpx.TimeoutException:
        return _result(ValidationFailureCode.TIMEOUT, "The local server took too long to respond.")
    except httpx.ConnectError:
        return _result(
            ValidationFailureCode.NETWORK_UNREACHABLE,
            "Local model server is not running. Start your runtime and try again.",
        )
    except asyncio.TimeoutError:
        return _result(ValidationFailureCode.TIMEOUT, "The local server took too long to respond.")
    except Exception as exc:
        logger.warning("Unknown local LLM validation failure: %s", exc)
        return _result(
            ValidationFailureCode.UNKNOWN, "Local validation failed. Check your runtime settings."
        )


_PROBE_PROVIDER_NAME = "system__think"
_PROBE_FQN = "system.think"
_PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": _PROBE_PROVIDER_NAME,
            "description": "Record a short internal thought. Call this once with thought set to probe.",
            "parameters": {
                "type": "object",
                "properties": {"thought": {"type": "string"}},
                "required": ["thought"],
            },
        },
    }
]


async def probe_action_capability(llm: LLMService, *, timeout_s: float = 12.0) -> bool:
    """Live action/result round trip that must resolve to a CapabilityCall.

    Static provider catalogs are a hint, not proof. Always run the live probe.
    """
    from core.llm.types import tool_result_message
    from core.plugins.capabilities import CapabilityCall
    from core.plugins.registry import registry

    messages = [
        {
            "role": "system",
            "content": "You can call tools. Call the think tool once with thought='probe'.",
        },
        {"role": "user", "content": "Confirm tool calling works by calling the think tool."},
    ]
    try:
        result = await asyncio.wait_for(
            llm.complete(
                messages=messages,
                tools=_PROBE_TOOLS,
                temperature=0.0,
                dump_tag="action_capability_probe",
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("Action capability probe failed: %s", exc)
        return False

    if not result.tool_calls:
        logger.info("Action capability probe: no structured tool call from %s", llm.model)
        return False
    wire = result.tool_calls[0]
    definition = registry.resolve_provider_name(wire.name)
    capability = definition.fqn if definition is not None else (
        _PROBE_FQN if wire.name == _PROBE_PROVIDER_NAME else ""
    )
    if not capability or not isinstance(wire.arguments, dict):
        return False
    call = CapabilityCall(
        capability=capability,
        arguments=dict(wire.arguments),
        call_id=wire.call_id,
    )
    if call.capability != _PROBE_FQN and (definition is None or definition.fqn != call.capability):
        return False
    try:
        await asyncio.wait_for(
            llm.complete(
                messages=[
                    *messages,
                    result.message,
                    tool_result_message(call.call_id, "ok", wire.name),
                ],
                tools=_PROBE_TOOLS,
                temperature=0.0,
                dump_tag="action_capability_probe_result",
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("Action capability result round trip failed: %s", exc)
        return False
    logger.info("Action capability probe passed for %s", llm.model)
    return True
