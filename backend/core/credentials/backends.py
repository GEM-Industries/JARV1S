"""Credential persistence backends for Jarvis Host."""

from __future__ import annotations

import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

SERVICE = "jarvis.homecore"
VAULT_ACCOUNT = "JARVIS_CREDENTIAL_VAULT_KEY"


class CredentialMode(str, Enum):
    ENV = "env"
    OS_KEYRING = "os_keyring"
    ENCRYPTED_FILE = "encrypted_file"


def keyring_available() -> bool:
    try:
        import keyring  # type: ignore[import-not-found]

        backend = keyring.get_keyring()
        return backend.__class__.__name__ not in {"FailKeyring", "ChainerBackend"}
    except Exception:
        return False


class CredentialBackend(ABC):
    @property
    @abstractmethod
    def mode(self) -> CredentialMode: ...

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def read_all(self) -> dict[str, str]: ...

    @abstractmethod
    def write_all(self, secrets: dict[str, str]) -> None: ...


class KeychainVaultBackend(CredentialBackend):
    """One Keychain item protects an encrypted on-disk secrets blob."""

    def __init__(self, *, encrypted_file: Path) -> None:
        self._encrypted_file = encrypted_file
        self._fernet: Fernet | None = None

    @property
    def mode(self) -> CredentialMode:
        return CredentialMode.OS_KEYRING

    def available(self) -> bool:
        return keyring_available()

    def read_all(self) -> dict[str, str]:
        fernet = self._ensure_fernet()
        if fernet is None or not self._encrypted_file.exists():
            return {}
        try:
            payload = json.loads(fernet.decrypt(self._encrypted_file.read_bytes()).decode("utf-8"))
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items()}
        except (InvalidToken, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read vault credential file: %s", exc)
        return {}

    def write_all(self, secrets: dict[str, str]) -> None:
        fernet = self._ensure_fernet()
        if fernet is None:
            raise RuntimeError("OS keyring is unavailable for credential vault storage")
        self._encrypted_file.parent.mkdir(parents=True, exist_ok=True)
        self._encrypted_file.write_bytes(fernet.encrypt(json.dumps(secrets).encode("utf-8")))

    def _ensure_fernet(self) -> Fernet | None:
        if self._fernet is not None:
            return self._fernet
        if not self.available():
            return None
        try:
            import keyring  # type: ignore[import-not-found]

            existing = keyring.get_password(SERVICE, VAULT_ACCOUNT)
            if existing:
                self._fernet = Fernet(existing.encode("utf-8"))
                return self._fernet

            key = Fernet.generate_key()
            keyring.set_password(SERVICE, VAULT_ACCOUNT, key.decode("utf-8"))
            self._fernet = Fernet(key)
            return self._fernet
        except Exception as exc:
            logger.warning("Failed to unlock credential vault key: %s", exc)
            return None


class PassphraseFileBackend(CredentialBackend):
    """Dev/headless encrypted file protected by JARVIS_CREDENTIAL_PASSPHRASE."""

    def __init__(self, *, encrypted_file: Path, salt_file: Path) -> None:
        self._encrypted_file = encrypted_file
        self._salt_file = salt_file
        self._fernet: Fernet | None = None

    @property
    def mode(self) -> CredentialMode:
        return CredentialMode.ENCRYPTED_FILE

    def available(self) -> bool:
        return bool(os.environ.get("JARVIS_CREDENTIAL_PASSPHRASE"))

    def read_all(self) -> dict[str, str]:
        fernet = self._ensure_fernet()
        if fernet is None or not self._encrypted_file.exists():
            return {}
        try:
            payload = json.loads(fernet.decrypt(self._encrypted_file.read_bytes()).decode("utf-8"))
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items()}
        except (InvalidToken, json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read encrypted credential file: %s", exc)
        return {}

    def write_all(self, secrets: dict[str, str]) -> None:
        fernet = self._ensure_fernet()
        if fernet is None:
            raise RuntimeError(
                "Encrypted credential store requires JARVIS_CREDENTIAL_PASSPHRASE when OS keyring is unavailable"
            )
        self._encrypted_file.parent.mkdir(parents=True, exist_ok=True)
        self._encrypted_file.write_bytes(fernet.encrypt(json.dumps(secrets).encode("utf-8")))

    def _ensure_fernet(self) -> Fernet | None:
        if self._fernet is not None:
            return self._fernet
        passphrase = os.environ.get("JARVIS_CREDENTIAL_PASSPHRASE")
        if not passphrase:
            return None
        self._encrypted_file.parent.mkdir(parents=True, exist_ok=True)
        if self._salt_file.exists():
            salt = self._salt_file.read_bytes()
        else:
            salt = os.urandom(16)
            self._salt_file.write_bytes(salt)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=390_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
        self._fernet = Fernet(key)
        return self._fernet


def select_backend(*, encrypted_file: Path, salt_file: Path) -> CredentialBackend:
    if os.environ.get("JARVIS_CREDENTIAL_PASSPHRASE"):
        return PassphraseFileBackend(encrypted_file=encrypted_file, salt_file=salt_file)
    if keyring_available():
        return KeychainVaultBackend(encrypted_file=encrypted_file)
    return PassphraseFileBackend(encrypted_file=encrypted_file, salt_file=salt_file)
