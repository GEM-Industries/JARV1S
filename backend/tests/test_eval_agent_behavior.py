"""Tests for tools/evaluate_agent_behavior.py."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = BACKEND_DIR / "tools"

from tools.evaluate_agent_behavior import (
    CaseSummary,
    LiveToolOutputStub,
    RunRow,
    _execute_eval_turn,
    _live_temperature_patch,
    _live_tool_output_sequence,
    _manifest_mode,
    _pass_threshold_for_case,
    _resolve_live_temperature,
    _runs_for_case,
    _should_run_live,
    _summarize_case,
    _use_pinned_manifest,
)


def test_eval_agent_behavior_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "evaluate_agent_behavior.py"), "--help"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--priority" in proc.stdout
    assert "--live" in proc.stdout
    assert "--probe" in proc.stdout


def test_eval_agent_behavior_probe_requires_live() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "evaluate_agent_behavior.py"), "--probe"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "--probe requires --live" in proc.stderr


def test_runs_for_case_mock_vs_live() -> None:
    meta = {"default_mock_runs": 1, "default_live_runs": 3, "default_probe_runs": 5}
    args = argparse.Namespace(runs=None, live=False)
    mock_case = {"id": "mock_case", "tier": "mock"}
    live_case = {"id": "live_case", "tier": "live"}
    probe_case = {"id": "probe_case", "tier": "probe"}

    assert _runs_for_case(mock_case, args=args, meta=meta) == 1
    assert _runs_for_case(live_case, args=argparse.Namespace(runs=None, live=True), meta=meta) == 3
    assert _runs_for_case(probe_case, args=argparse.Namespace(runs=None, live=True), meta=meta) == 5


def test_should_run_live_includes_probe_only_when_live() -> None:
    probe_case = {"id": "probe_case", "tier": "probe"}

    assert _should_run_live(probe_case, live=True)
    assert not _should_run_live(probe_case, live=False)


def test_live_tool_output_sequence_string_and_list() -> None:
    single = {"live_tool_output": "APPROVAL_NEEDED"}
    multi = {"live_tool_outputs": ["first", "second"]}

    assert _live_tool_output_sequence(single) == ["APPROVAL_NEEDED"]
    assert _live_tool_output_sequence(multi) == ["first", "second"]


@pytest.mark.asyncio
async def test_live_tool_output_stub_sequence() -> None:
    stub = LiveToolOutputStub(["first", "second"])
    assert await stub.execute() == "first"
    assert await stub.execute() == "second"
    assert await stub.execute() == "second"


@pytest.mark.asyncio
async def test_live_tool_output_stub_single_repeats() -> None:
    stub = LiveToolOutputStub(["same"])
    assert await stub.execute() == "same"
    assert await stub.execute() == "same"


def test_manifest_mode_production_vs_pinned() -> None:
    production = {"id": "prod"}
    pinned = {"id": "pinned", "pin_manifest": True, "routed_tools": ["scheduler.remind"]}
    routed_without_pin = {"id": "not_pinned", "routed_tools": ["scheduler.remind"]}

    assert not _use_pinned_manifest(production)
    assert _manifest_mode(production) == "production"
    assert _use_pinned_manifest(pinned)
    assert _manifest_mode(pinned) == "pinned"
    assert not _use_pinned_manifest(routed_without_pin)


def test_pass_threshold_for_case_strict_p0_live() -> None:
    meta = {"pass_threshold": 0.67}
    live_p0 = {"priority": "P0", "tier": "live"}
    mock_p0 = {"priority": "P0", "tier": "mock"}

    assert _pass_threshold_for_case(live_p0, meta, live=True) == 1.0
    assert _pass_threshold_for_case(mock_p0, meta, live=False) == 0.67


def test_resolve_live_temperature_defaults_to_meta() -> None:
    assert _resolve_live_temperature({}, {"default_live_temperature": 0}) == 0
    assert _resolve_live_temperature({"temperature": 0.2}, {"default_live_temperature": 0}) == 0.2


def test_live_temperature_patch_sets_chat_stream_temperature() -> None:
    class FakeLLM:
        def __init__(self) -> None:
            self.kwargs = {}

        def chat_stream(self, **kwargs):
            self.kwargs = kwargs
            return iter(())

    class FakeAgent:
        def __init__(self) -> None:
            self.llm = FakeLLM()

    agent = FakeAgent()

    with _live_temperature_patch(agent, 0):
        agent.llm.chat_stream(user_message="")

    assert agent.llm.kwargs["temperature"] == 0


@pytest.mark.asyncio
async def test_live_eval_requires_executor_stub() -> None:
    class FakeOrchestrator:
        pass

    with pytest.raises(RuntimeError, match="require a dispatcher stub"):
        await _execute_eval_turn(
            FakeOrchestrator(),
            {"id": "live_case", "input": "delete something"},
            connection_id="eval-live-case-1",
            live=True,
            meta={},
            executor_stub=None,
        )


def test_summarize_case_requires_all_runs_for_p0_live() -> None:
    case = {"id": "live_case", "priority": "P0", "category": "tool_selection", "tier": "live"}
    rows = [
        RunRow("live_case", 1, "live", True, 1.0, {}, [], manifest_mode="production", model="m1"),
        RunRow("live_case", 2, "live", True, 1.0, {}, [], manifest_mode="production", model="m1"),
        RunRow("live_case", 3, "live", False, 1.0, {}, [{"name": "tool_required", "passed": False, "detail": "x"}], manifest_mode="production", model="m1"),
    ]

    summary = _summarize_case(case, rows, threshold=1.0)

    assert isinstance(summary, CaseSummary)
    assert summary.pass_rate == 0.6667
    assert not summary.passed
