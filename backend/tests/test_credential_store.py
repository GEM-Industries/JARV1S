import os
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from core.credentials.backends import (
    KeychainVaultBackend,
    VAULT_ACCOUNT,
)
from core.credentials.backends import CredentialMode
from core.credentials.store import CredentialStore
from core.setup.placeholders import is_placeholder_api_key


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    monkeypatch.setattr("core.credentials.store._CREDENTIALS_DIR", cred_dir)
    monkeypatch.setattr("core.credentials.store._ENCRYPTED_FILE", cred_dir / "secrets.enc")
    monkeypatch.setattr("core.credentials.store._SALT_FILE", cred_dir / "secrets.salt")
    return CredentialStore()


def test_mask_secret(isolated_store):
    assert isolated_store.mask_secret(None) is None
    assert isolated_store.mask_secret("ab") == "****"
    assert isolated_store.mask_secret("sk-abcdefghijklmnop") == "…mnop"


def test_env_mode_has_priority(isolated_store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key-12345678")
    mode = isolated_store.mode_for_secret("OPENROUTER_API_KEY")
    assert mode == CredentialMode.ENV
    assert isolated_store.get_secret("OPENROUTER_API_KEY") == "sk-env-key-12345678"


def test_llm_key_resolution_ignores_env(isolated_store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key-12345678")
    assert isolated_store.get_secret("OPENROUTER_API_KEY") == "sk-env-key-12345678"
    assert isolated_store.resolve_llm_api_key("openrouter") is None


def test_refuses_placeholder_storage(isolated_store, monkeypatch):
    monkeypatch.setenv("JARVIS_CREDENTIAL_PASSPHRASE", "test-passphrase")
    with pytest.raises(ValueError, match="placeholder"):
        isolated_store.set_secret("OPENROUTER_API_KEY", "your_openrouter_key")


def test_encrypted_file_fallback(isolated_store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_CREDENTIAL_PASSPHRASE", "test-passphrase")
    monkeypatch.setattr(isolated_store, "_read_env", lambda _name: None)
    mode = isolated_store.set_secret("OPENROUTER_API_KEY", "sk-file-key-12345678")
    assert mode == CredentialMode.ENCRYPTED_FILE
    assert isolated_store.get_secret("OPENROUTER_API_KEY") == "sk-file-key-12345678"
    assert isolated_store.mode_for_secret("OPENROUTER_API_KEY") == CredentialMode.ENCRYPTED_FILE
    assert isolated_store.storage_detail() == "Stored locally in encrypted dev vault."


def test_placeholder_env_is_treated_as_missing(isolated_store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "your_openrouter_key")
    assert isolated_store.get_secret("OPENROUTER_API_KEY") is None
    assert is_placeholder_api_key(os.environ["OPENROUTER_API_KEY"])


def test_encrypted_file_requires_explicit_passphrase(isolated_store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CREDENTIAL_PASSPHRASE", raising=False)
    monkeypatch.setattr(isolated_store, "_read_env", lambda _name: None)
    monkeypatch.setattr("core.credentials.backends.keyring_available", lambda: False)

    with pytest.raises(RuntimeError, match="JARVIS_CREDENTIAL_PASSPHRASE"):
        isolated_store.set_secret("OPENROUTER_API_KEY", "sk-file-key-12345678")


def test_vault_backend_stores_multiple_secrets_in_one_blob(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    encrypted_file = cred_dir / "secrets.enc"
    vault_key = Fernet.generate_key().decode("utf-8")
    keyring = MagicMock()
    keyring.get_password.side_effect = lambda service, account: (
        vault_key if account == VAULT_ACCOUNT else None
    )
    monkeypatch.setattr("core.credentials.backends.keyring_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "keyring", keyring)

    backend = KeychainVaultBackend(encrypted_file=encrypted_file)
    backend.write_all({"OPENROUTER_API_KEY": "sk-openrouter-12345678"})
    backend.write_all(
        {
            "OPENROUTER_API_KEY": "sk-openrouter-12345678",
            "CEREBRAS_API_KEY": "sk-cerebras-12345678",
        }
    )

    assert keyring.set_password.call_count == 0
    loaded = backend.read_all()
    assert loaded["OPENROUTER_API_KEY"] == "sk-openrouter-12345678"
    assert loaded["CEREBRAS_API_KEY"] == "sk-cerebras-12345678"


def test_vault_store_uses_single_keyring_item(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    monkeypatch.setattr("core.credentials.store._CREDENTIALS_DIR", cred_dir)
    monkeypatch.setattr("core.credentials.store._ENCRYPTED_FILE", cred_dir / "secrets.enc")
    monkeypatch.setattr("core.credentials.store._SALT_FILE", cred_dir / "secrets.salt")
    monkeypatch.delenv("JARVIS_CREDENTIAL_PASSPHRASE", raising=False)

    keyring = MagicMock()
    stored: dict[tuple[str, str], str] = {}

    def _get_password(service: str, account: str) -> str | None:
        return stored.get((service, account))

    def _set_password(service: str, account: str, value: str) -> None:
        stored[(service, account)] = value

    keyring.get_password.side_effect = _get_password
    keyring.set_password.side_effect = _set_password
    monkeypatch.setattr("core.credentials.backends.keyring_available", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "keyring", keyring)

    store = CredentialStore()
    store.set_secret("OPENROUTER_API_KEY", "sk-openrouter-12345678")
    store.set_secret("CEREBRAS_API_KEY", "sk-cerebras-12345678")

    vault_accounts = [call.args[1] for call in keyring.set_password.call_args_list]
    assert vault_accounts == [VAULT_ACCOUNT]
    assert store.get_stored_secret("OPENROUTER_API_KEY") == "sk-openrouter-12345678"
    assert store.get_stored_secret("CEREBRAS_API_KEY") == "sk-cerebras-12345678"
    assert store.active_storage_mode() == CredentialMode.OS_KEYRING
    assert store.storage_detail() == "Stored securely on this Jarvis Host."
    assert {call.args[1] for call in keyring.get_password.call_args_list} == {VAULT_ACCOUNT}
