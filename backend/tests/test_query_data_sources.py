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


def _load_query():
    scripts = (
        Path(__file__).resolve().parents[2]
        / ".cursor"
        / "skills"
        / "query-jarvis-data"
        / "scripts"
    )
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("jarvis_query_cli_test", scripts / "query.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_format_turn_dump_prints_ledger_not_just_content() -> None:
    query = _load_query()
    rows = [
        {
            "role": "user",
            "source": "trigger",
            "content": "Fade the bedroom lights.",
            "metadata": {
                "turn_type": "user",
                "decision": "act",
                "rule_id": "rule-lights",
                "instance_id": "inst-1",
            },
        },
        {
            "role": "assistant",
            "source": "trigger",
            "content": "ignored blob",
            "metadata": {
                "turn_type": "tool_call",
                "capability": "smart_home.control_lights",
                "arguments": {"query": "bedroom", "action": "on", "transition": "15 minutes"},
                "spoken": "Fading the bedroom lights.",
                "routed_tools": ["smart_home.control_lights"],
            },
        },
        {
            "role": "user",
            "source": "trigger",
            "content": "Home Assistant reports Bedroom on.",
            "metadata": {
                "turn_type": "tool_result",
                "capability": "smart_home.control_lights",
                "status": "succeeded",
                "output": "Home Assistant reports Bedroom on.",
                "invocations": [
                    {
                        "capability": "smart_home.control_lights",
                        "status": "succeeded",
                        "args_preview": {"query": "bedroom", "transition": "15 minutes"},
                    }
                ],
            },
        },
    ]
    dump = query.format_turn_dump(
        rows,
        perf={
            "modality": "voice",
            "status": "completed",
            "response_ms": 1200,
            "total_ms": 1500,
            "node_id": "bedroom",
            "tool_routing": {
                "matched_plugins": ["smart_home"],
                "routed_tool_count": 4,
            },
            "stages": [{"key": "turn_detector", "ms": 45.0}],
        },
    )
    assert "decision=act" in dump
    assert "rule_id=rule-lights" in dump
    assert "called: smart_home.control_lights" in dump
    assert "routed_tools: smart_home.control_lights" in dump
    assert "matched_plugins=smart_home" in dump
    assert "15 minutes" in dump
    assert "status=succeeded" in dump
    assert "Fading the bedroom lights." in dump
    assert "ignored blob" not in dump
    assert "turn_detector" not in dump
    assert "[ledger]" in dump
