"""Tests for GET /api/v1/health fail-closed infrastructure semantics."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import base as base_routes
from core.setup.models import ReadinessPhase
from core.setup.runtime import jarvis_runtime


@pytest.fixture(autouse=True)
def reset_runtime():
    jarvis_runtime.core_ready = False
    jarvis_runtime.initializing = False
    jarvis_runtime.last_error = None
    yield


def _llm_config(*, attemptable: bool = True):
    return type("LLMConfig", (), {"attemptable": attemptable})()


@pytest.fixture
def client(monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(base_routes.router, prefix="/api/v1")
    return TestClient(app)


def test_health_returns_200_when_infra_up_and_ready(client: TestClient, monkeypatch):
    jarvis_runtime.core_ready = True
    monkeypatch.setattr(
        base_routes.mongodb,
        "health_check",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(base_routes, "get_readiness_phase", lambda: ReadinessPhase.READY)
    monkeypatch.setattr(
        base_routes,
        "resolve_llm_config",
        AsyncMock(return_value=_llm_config(attemptable=True)),
    )

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["services"]["database"] == "up"
    assert body["services"]["voice"] == "optional"


def test_health_returns_200_for_needs_setup_when_infra_up(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        base_routes.mongodb,
        "health_check",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(base_routes, "get_readiness_phase", lambda: ReadinessPhase.NEEDS_SETUP)
    monkeypatch.setattr(
        base_routes,
        "resolve_llm_config",
        AsyncMock(return_value=_llm_config(attemptable=False)),
    )

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "needs_setup"


def test_health_returns_503_when_database_down(client: TestClient, monkeypatch):
    jarvis_runtime.core_ready = True
    monkeypatch.setattr(
        base_routes.mongodb,
        "health_check",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(base_routes, "get_readiness_phase", lambda: ReadinessPhase.READY)
    monkeypatch.setattr(
        base_routes,
        "resolve_llm_config",
        AsyncMock(return_value=_llm_config(attemptable=True)),
    )

    response = client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["services"]["database"] == "down"


def test_health_returns_200_degraded_when_llm_not_ready_but_infra_up(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(
        base_routes.mongodb,
        "health_check",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(base_routes, "get_readiness_phase", lambda: ReadinessPhase.DEGRADED)
    monkeypatch.setattr(
        base_routes,
        "resolve_llm_config",
        AsyncMock(return_value=_llm_config(attemptable=True)),
    )

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
