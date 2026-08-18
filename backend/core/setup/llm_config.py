"""Resolved LLM configuration — single boundary for setup and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from core.credentials.store import CredentialMode, credential_store
from core.llm.providers import get_llm_provider, normalize_llm_provider
from core.setup.placeholders import is_placeholder_api_key

_LLM_CONFIG_KEY = "llm_config"
LOCAL_DUMMY_API_KEY = "local"


class LlmConfigSource(str, Enum):
    PERSISTED = "persisted"
    DEFAULT = "default"


class SupportsLlmConfigure(Protocol):
    """Duck type for LLMService.configure — keeps setup → llm dependency one-way."""

    def configure(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider_name: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedLlmConfig:
    provider: str
    model: str
    base_url: str
    requires_api_key: bool
    api_key: str | None
    source: LlmConfigSource
    key_env_name: str | None = None
    key_mode: CredentialMode | None = None
    action_capable: bool | None = None

    @property
    def attemptable(self) -> bool:
        if not self.provider or not self.model or not self.base_url:
            return False
        if self.requires_api_key:
            return bool(self.api_key) and not is_placeholder_api_key(self.api_key)
        return True

    def apply_to(self, llm: SupportsLlmConfigure) -> None:
        """Push the full resolved config onto an LLM facade.

        Callers must use this instead of unpacking fields so provider cannot be
        dropped (LiteLLM requires it for local model routing).
        """
        llm.configure(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            provider_name=self.provider,
        )


class LlmConfigStore:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    async def load_persisted(self) -> dict[str, Any] | None:
        if self._cache is not None:
            return self._cache
        try:
            from services.database.mongodb import mongodb

            col = mongodb.get_collection("system_config")
            doc = await col.find_one({"_id": _LLM_CONFIG_KEY})
            if not doc:
                return None
            action_capable = doc.get("action_capable")
            self._cache = {
                "provider": str(doc.get("provider") or ""),
                "model": str(doc.get("model") or ""),
                "base_url": str(doc.get("base_url") or "").rstrip("/"),
                "action_capable": action_capable if isinstance(action_capable, bool) else None,
            }
            return self._cache
        except Exception:
            return None

    async def save(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        action_capable: bool | None = None,
    ) -> None:
        from services.database.mongodb import mongodb

        payload: dict[str, Any] = {
            "provider": normalize_llm_provider(provider),
            "model": model,
            "base_url": base_url.rstrip("/"),
        }
        if action_capable is not None:
            payload["action_capable"] = action_capable
        elif self._cache and isinstance(self._cache.get("action_capable"), bool):
            payload["action_capable"] = self._cache["action_capable"]
        col = mongodb.get_collection("system_config")
        await col.update_one({"_id": _LLM_CONFIG_KEY}, {"$set": payload}, upsert=True)
        self._cache = payload

    async def delete(self) -> None:
        from services.database.mongodb import mongodb

        col = mongodb.get_collection("system_config")
        await col.delete_one({"_id": _LLM_CONFIG_KEY})
        self._cache = None

    def clear_cache(self) -> None:
        self._cache = None


llm_config_store = LlmConfigStore()


def _build_resolved(
    *,
    provider_name: str,
    model: str,
    base_url: str,
    source: LlmConfigSource,
    include_secret: bool = True,
    action_capable: bool | None = None,
) -> ResolvedLlmConfig:
    preset = get_llm_provider(provider_name)
    resolved_model = model or preset.model
    resolved_base = (base_url or preset.base_url).rstrip("/")

    requires_api_key = preset.requires_api_key
    api_key: str | None = None
    key_env_name: str | None = None
    key_mode: CredentialMode | None = None

    if requires_api_key and include_secret:
        api_key = credential_store.resolve_llm_api_key(provider_name)
        key_env_name, key_mode = credential_store.configured_llm_key_source(provider_name)
    elif not requires_api_key:
        api_key = LOCAL_DUMMY_API_KEY

    return ResolvedLlmConfig(
        provider=provider_name,
        model=resolved_model,
        base_url=resolved_base,
        requires_api_key=requires_api_key,
        api_key=api_key,
        source=source,
        key_env_name=key_env_name,
        key_mode=key_mode,
        action_capable=action_capable,
    )


def resolve_llm_config_sync() -> ResolvedLlmConfig:
    persisted = llm_config_store._cache
    if persisted and persisted.get("provider"):
        return _build_resolved(
            provider_name=persisted["provider"],
            model=persisted.get("model", ""),
            base_url=persisted.get("base_url", ""),
            source=LlmConfigSource.PERSISTED,
            action_capable=persisted.get("action_capable") if isinstance(persisted.get("action_capable"), bool) else None,
        )

    default_preset = get_llm_provider("openrouter")
    return _build_resolved(
        provider_name=default_preset.name,
        model=default_preset.model,
        base_url=default_preset.base_url,
        source=LlmConfigSource.DEFAULT,
        include_secret=False,
    )


async def resolve_llm_config() -> ResolvedLlmConfig:
    await llm_config_store.load_persisted()
    return resolve_llm_config_sync()
