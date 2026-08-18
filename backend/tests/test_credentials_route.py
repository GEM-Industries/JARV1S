import pytest
from fastapi import HTTPException
from starlette.responses import Response

from api.routes import credentials as credentials_routes


@pytest.mark.asyncio
async def test_save_unknown_credential_returns_404():
    with pytest.raises(HTTPException) as exc:
        await credentials_routes.save_credential(
            "unknown",
            credentials_routes.CredentialUpsertRequest(value="secret-value-123"),
            Response(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_credentials_sets_no_store_header(monkeypatch):
    async def list_credentials():
        return None

    monkeypatch.setattr(credentials_routes.credentials_service, "list_credentials", list_credentials)
    response = Response()

    await credentials_routes.list_credentials(response)

    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_save_storage_error_returns_400(monkeypatch):
    async def fail_save(_credential_id: str, _value: str):
        raise RuntimeError("Encrypted credential store requires passphrase")

    monkeypatch.setattr(credentials_routes.credentials_service, "save_credential", fail_save)

    with pytest.raises(HTTPException) as exc:
        await credentials_routes.save_credential(
            "exa",
            credentials_routes.CredentialUpsertRequest(value="secret-value-123"),
            Response(),
        )

    assert exc.value.status_code == 400
