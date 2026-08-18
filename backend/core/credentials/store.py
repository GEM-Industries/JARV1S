"""Host-only credential storage for bootstrap provider API keys."""

from __future__ import annotations

import os
from typing import Optional

from core.config import settings
from core.credentials.backends import (
    CredentialBackend,
    CredentialMode,
    select_backend,
)
from core.llm.providers import get_llm_provider
from core.setup.placeholders import is_placeholder_api_key

_CREDENTIALS_DIR = settings.DATA_DIR / "credentials"
_ENCRYPTED_FILE = _CREDENTIALS_DIR / "secrets.enc"
_SALT_FILE = _CREDENTIALS_DIR / "secrets.salt"

__all__ = ["CredentialMode", "CredentialStore", "credential_store"]


class CredentialStore:
    """Resolve and persist bootstrap secrets on the Jarvis Host only."""

    def __init__(self) -> None:
        self._memory: dict[str, str] = {}
        self._blob: dict[str, str] | None = None
        self._backend: CredentialBackend | None = None

    def active_storage_mode(self) -> CredentialMode | None:
        backend = self._active_backend()
        if not backend.available():
            return None
        return backend.mode

    def storage_detail(self) -> str | None:
        mode = self.active_storage_mode()
        if mode == CredentialMode.ENCRYPTED_FILE:
            return "Stored locally in encrypted dev vault."
        if mode == CredentialMode.OS_KEYRING:
            return "Stored securely on this Jarvis Host."
        return None

    def mode_for_secret(self, secret_name: str) -> CredentialMode | None:
        env_value = self._read_env(secret_name)
        if env_value and not is_placeholder_api_key(env_value):
            return CredentialMode.ENV
        stored_mode = self.mode_for_stored_secret(secret_name)
        if stored_mode:
            return stored_mode
        return None

    def get_secret(self, secret_name: str) -> Optional[str]:
        env_value = self._read_env(secret_name)
        if env_value and not is_placeholder_api_key(env_value):
            return env_value.strip()
        return self.get_stored_secret(secret_name)

    def get_stored_secret(self, secret_name: str) -> Optional[str]:
        value = self._load_blob().get(secret_name)
        if value and not is_placeholder_api_key(value):
            return value
        memory_value = self._memory.get(secret_name)
        if memory_value and not is_placeholder_api_key(memory_value):
            return memory_value
        return None

    def mode_for_stored_secret(self, secret_name: str) -> CredentialMode | None:
        if secret_name not in self._load_blob():
            if secret_name in self._memory and not is_placeholder_api_key(self._memory[secret_name]):
                backend = self._active_backend()
                return backend.mode if backend.available() else CredentialMode.ENCRYPTED_FILE
            return None
        backend = self._active_backend()
        if backend.available():
            return backend.mode
        return CredentialMode.ENCRYPTED_FILE

    def stored_source_for_secret(self, secret_name: str) -> CredentialMode | None:
        return self.mode_for_stored_secret(secret_name)

    def has_env_only_secret(self, secret_name: str) -> bool:
        env_value = self._read_env(secret_name)
        if not env_value or is_placeholder_api_key(env_value):
            return False
        return self.get_stored_secret(secret_name) is None

    def delete_secret(self, secret_name: str) -> None:
        self._memory.pop(secret_name, None)
        blob = self._load_blob()
        if secret_name not in blob:
            return
        blob.pop(secret_name, None)
        self._persist_blob(blob)

    def set_secret(self, secret_name: str, value: str, *, prefer_keyring: bool = True) -> CredentialMode:
        del prefer_keyring  # Vault backend selection is centralized in select_backend().
        if is_placeholder_api_key(value):
            raise ValueError("Refusing to store placeholder API key")
        stripped = value.strip()
        blob = self._load_blob()
        blob[secret_name] = stripped
        self._persist_blob(blob)
        self._memory[secret_name] = stripped
        backend = self._active_backend()
        return backend.mode if backend.available() else CredentialMode.ENCRYPTED_FILE

    def mask_secret(self, value: str | None) -> Optional[str]:
        if not value:
            return None
        stripped = value.strip()
        if len(stripped) <= 4:
            return "****"
        return f"…{stripped[-4:]}"

    def resolve_llm_api_key(self, provider_name: str) -> Optional[str]:
        provider = get_llm_provider(provider_name)
        for credential_name in provider.credential_names:
            value = self.get_stored_secret(credential_name)
            if value:
                return value
        return None

    def configured_llm_key_source(self, provider_name: str) -> tuple[Optional[str], Optional[CredentialMode]]:
        provider = get_llm_provider(provider_name)
        for credential_name in provider.credential_names:
            mode = self.mode_for_stored_secret(credential_name)
            if mode:
                return credential_name, mode
        return None, None

    def _read_env(self, secret_name: str) -> Optional[str]:
        env_value = os.environ.get(secret_name)
        if env_value:
            return env_value
        return getattr(settings, secret_name, None)

    def _active_backend(self) -> CredentialBackend:
        if self._backend is None:
            self._backend = select_backend(encrypted_file=_ENCRYPTED_FILE, salt_file=_SALT_FILE)
        return self._backend

    def _load_blob(self) -> dict[str, str]:
        if self._blob is not None:
            return self._blob

        backend = self._active_backend()
        self._blob = backend.read_all() if backend.available() else {}
        return self._blob

    def _persist_blob(self, blob: dict[str, str]) -> None:
        backend = self._active_backend()
        backend.write_all(blob)
        self._blob = dict(blob)


credential_store = CredentialStore()
