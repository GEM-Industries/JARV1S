from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_sources():
    path = (
        Path(__file__).resolve().parents[2]
        / ".cursor"
        / "skills"
        / "query-jarvis-data"
        / "scripts"
        / "sources.py"
    )
    spec = importlib.util.spec_from_file_location("jarvis_query_sources_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_app_source_never_falls_back_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _load_sources()
    dev_pinged = False

    async def ping(url: str) -> bool:
        nonlocal dev_pinged
        if url == sources.DEV_URL:
            dev_pinged = True
        return False

    monkeypatch.setattr(sources, "socket_up", lambda _path: False)
    monkeypatch.setattr(sources, "ping", ping)

    with pytest.raises(SystemExit, match="app database is unavailable"):
        await sources.pick_source("app")
    assert dev_pinged is False


@pytest.mark.asyncio
async def test_dev_source_requires_explicit_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _load_sources()

    async def ping(_url: str) -> bool:
        return True

    monkeypatch.setattr(sources, "ping", ping)
    selected = await sources.pick_source("dev")
    assert selected.name == "dev"

    with pytest.raises(SystemExit, match="use app or dev"):
        await sources.pick_source("auto")
