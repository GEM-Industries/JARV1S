"""Agent/LLM behavior eval — trajectory checks via TurnResult.

Mock tier (default): scripted AgentEvents through _execute_turn.
Live tier (--live): real LLM; only cases with tier: live or tier: both.
Probe tier (--probe --live): prompt-tuning probes, excluded from regression gates.

Run from backend/:
    uv run python tools/evaluate_agent_behavior.py
    uv run python tools/evaluate_agent_behavior.py --priority P0 --label agent-p0
    uv run python tools/evaluate_agent_behavior.py --live --priority P0 --label agent-live-p0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.agent.agent import AgentEvent, AgentEventType, JarvisAgent
from core.config import settings
from core.id import generate_id
from core.plugins.capabilities import (
    CapabilityCall,
    CapabilityOutcome,
    InvocationRecord,
    InvocationStatus,
    capability_call_preview,
)
from core.prompts.system_turn_context import offer_evaluate_instruction
from core.turns.delivery import HeadlessDelivery, TurnResult
from core.turns.orchestrator import AssistantOrchestrator
from evals.agent_scorers import CaseScore, score_case
from evals.trace_extractor import TurnTraceSnapshot, extract_turn_trace

DEFAULT_CASES = BACKEND_DIR / "evals" / "agent_behavior.yaml"
DEFAULT_EVALS_DIR = BACKEND_DIR / "logs/evals"
DEFAULT_LIVE_TOOL_OUTPUT = "[Code executed successfully]"
_ROUTER_READY = False


@dataclass
class RunRow:
    case_id: str
    run: int
    tier: str
    passed: bool
    duration_ms: float
    snapshot: dict[str, Any]
    scores: list[dict[str, Any]]
    manifest_mode: str = "production"
    model: str = ""


@dataclass
class CaseSummary:
    case_id: str
    priority: str
    category: str
    tier: str
    runs: int
    pass_rate: float
    passed: bool
    failures: list[str] = field(default_factory=list)
    manifest_mode: str = "production"
    pass_threshold: float = 0.67


class AsyncIterEvents:
    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self) -> AgentEvent:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class LiveToolOutputStub:
    """Return canned dispatcher outcomes for live eval tool calls."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs or [DEFAULT_LIVE_TOOL_OUTPUT]
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append({"args": args, "kwargs": kwargs})
        if len(self.outputs) == 1:
            return self.outputs[0]
        idx = min(self._index, len(self.outputs) - 1)
        output = self.outputs[idx]
        self._index += 1
        return output

    async def dispatch(self, call: CapabilityCall) -> CapabilityOutcome:
        output = await self.execute(call)
        return CapabilityOutcome(
            call_id=call.call_id,
            capability=call.capability,
            status=InvocationStatus.SUCCEEDED,
            data=output,
            invocation=InvocationRecord(
                invocation_id=generate_id("inv-"),
                capability=call.capability,
                status=InvocationStatus.SUCCEEDED,
                source="structured",
                tool_call_id=call.call_id,
                args_preview=dict(call.arguments),
            ),
        )


def _git_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BACKEND_DIR.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip() or None
    except Exception:
        return None


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label.strip()).strip("-")
    return cleaned or "agent-behavior"


def _load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = yaml.safe_load(path.read_text()) or {}
    meta = payload.get("meta") or {}
    cases = payload.get("cases") or []
    return meta, cases


def _events_from_script(script: list[dict[str, Any]]) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    tool_call_id: str | None = None
    pending_capabilities: list[str] = []
    pending_arguments: dict[str, Any] = {}
    for step in script:
        kind = step["type"]
        if kind == "text":
            events.append(AgentEvent(type=AgentEventType.TEXT, content=step.get("content", "")))
        elif kind == "tool_call":
            if "code" in step and "capability" not in step and "capabilities" not in step:
                raise ValueError("script tool_call must use capability/arguments, not code")
            capability = str(step.get("capability") or "")
            capabilities = [
                str(item) for item in (step.get("capabilities") or ([capability] if capability else []))
                if item
            ]
            arguments = dict(step.get("arguments") or {})
            tool_call_id = generate_id("tcall-")
            pending_capabilities = capabilities
            pending_arguments = arguments
            events.append(AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content=capability_call_preview(capability, arguments) if capability else "",
                tool_call_id=tool_call_id,
                capability=capability or None,
                arguments=arguments,
            ))
        elif kind == "tool_output":
            capability = pending_capabilities[-1] if pending_capabilities else None
            record = (
                InvocationRecord(
                    invocation_id=generate_id("inv-"),
                    capability=capability,
                    status=InvocationStatus.SUCCEEDED,
                    source="test",
                    tool_call_id=tool_call_id,
                    args_preview=pending_arguments,
                )
                if capability
                else None
            )
            events.append(AgentEvent(
                type=AgentEventType.TOOL_OUTPUT,
                content=step.get("content", ""),
                tool_call_id=tool_call_id,
                capability=capability,
                arguments=pending_arguments,
                outcome=CapabilityOutcome(
                    call_id=tool_call_id or "",
                    capability=capability or "",
                    status=InvocationStatus.SUCCEEDED,
                    data=step.get("content", ""),
                    invocation=record,
                ) if record is not None else None,
            ))
            tool_call_id = None
            pending_capabilities = []
            pending_arguments = {}
        else:
            raise ValueError(f"unknown script event type: {kind}")
    return events


def _should_run_live(case: dict[str, Any], *, live: bool) -> bool:
    return live and case.get("tier", "mock") in {"live", "both", "probe"}


def _resolve_live_temperature(case: dict[str, Any], meta: dict[str, Any]) -> float:
    if case.get("temperature") is not None:
        return float(case["temperature"])
    return float(meta.get("default_live_temperature", 0))


def _live_tool_output_sequence(case: dict[str, Any]) -> list[str]:
    outputs = case.get("live_tool_outputs")
    if outputs:
        return [str(item) for item in outputs]
    single = case.get("live_tool_output", DEFAULT_LIVE_TOOL_OUTPUT)
    return [str(single)]


def _use_pinned_manifest(case: dict[str, Any]) -> bool:
    return bool(case.get("pin_manifest"))


def _manifest_mode(case: dict[str, Any]) -> str:
    return "pinned" if _use_pinned_manifest(case) else "production"


def _runs_for_case(case: dict[str, Any], *, args: argparse.Namespace, meta: dict[str, Any]) -> int:
    if args.runs is not None:
        return args.runs
    if case.get("runs") is not None:
        return int(case["runs"])

    default_mock_runs = int(meta.get("default_mock_runs", meta.get("default_runs", 1)))
    default_live_runs = int(meta.get("default_live_runs", meta.get("default_runs", 3)))
    default_probe_runs = int(meta.get("default_probe_runs", default_live_runs))
    if case.get("tier") == "probe":
        return default_probe_runs
    return default_live_runs if _should_run_live(case, live=args.live) else default_mock_runs


def _pass_threshold_for_case(
    case: dict[str, Any],
    meta: dict[str, Any],
    *,
    live: bool,
) -> float:
    priority = case.get("priority", "P1")
    tier = case.get("tier", "mock")
    if tier == "probe":
        return float(case.get("pass_threshold", meta.get("probe_pass_threshold", 0.67)))
    if priority == "P0" and live and tier in {"live", "both"}:
        return 1.0
    if case.get("pass_threshold") is not None:
        return float(case["pass_threshold"])
    return float(meta.get("pass_threshold", 0.67))


def _make_orchestrator(agent: JarvisAgent) -> AssistantOrchestrator:
    orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orch.agent = agent
    orch.router = MagicMock()
    orch.router.route = AsyncMock(return_value=agent)
    return orch


async def _ensure_eval_plugins_loaded() -> None:
    global _ROUTER_READY
    if _ROUTER_READY:
        return

    from core.tool_router import tool_router
    from evals.bootstrap import ensure_eval_plugins_loaded

    await ensure_eval_plugins_loaded()
    if not tool_router._utterance_vectors:
        await tool_router.initialize(llm_service=None)
    _ROUTER_READY = True


@contextmanager
def _live_temperature_patch(agent: Any, temperature: float):
    original_chat_stream = agent.llm.chat_stream

    def chat_stream_with_temperature(*args: Any, **kwargs: Any):
        kwargs["temperature"] = temperature
        return original_chat_stream(*args, **kwargs)

    with patch.object(agent.llm, "chat_stream", chat_stream_with_temperature):
        yield


@asynccontextmanager
async def _router_patch(case: dict[str, Any], *, live: bool) -> AsyncIterator[MagicMock | None]:
    if not live or not _use_pinned_manifest(case):
        yield None
        return

    with patch("core.tool_router.tool_router") as mock_router:
        mock_router.route = AsyncMock(return_value=set(case.get("routed_tools") or []))
        mock_router.get_last_diagnostics = MagicMock(return_value=None)
        yield mock_router


def _prepare_eval_transcript(case: dict[str, Any]) -> str:
    """Append the canonical offer evaluate instruction when running offer eval cases."""
    raw = case["input"]
    if case.get("trigger_decision") != "offer":
        return raw
    lines = raw.splitlines()
    body = "\n".join(line for line in lines if not line.startswith("INSTRUCTION:")).rstrip()
    has_authored = any(line.startswith("INSTRUCTIONS:") for line in lines)
    return f"{body}\nINSTRUCTION: {offer_evaluate_instruction(has_authored_instructions=has_authored)}"


async def _execute_eval_turn(
    orchestrator: AssistantOrchestrator,
    case: dict[str, Any],
    *,
    connection_id: str,
    live: bool,
    meta: dict[str, Any],
    executor_stub: LiveToolOutputStub | None = None,
) -> TurnTraceSnapshot:
    owner_id = settings.DEFAULT_USER_ID or "eval-owner"
    session_context = {
        "owner_id": owner_id,
        "connection_id": connection_id,
        "timezone": case.get("timezone", "Australia/Sydney"),
    }
    case_session = case.get("session_context")
    if isinstance(case_session, dict):
        session_context.update(case_session)
    temperature = _resolve_live_temperature(case, meta) if live else None
    result = TurnResult()

    if live and executor_stub is None:
        raise RuntimeError("Live agent evals require a dispatcher stub; refusing to run real tools.")

    async with _router_patch(case, live=live):
        with (
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.turns.execution.get_profile_block", AsyncMock(return_value="")),
            patch(
                "plugins.agents.work.load_open_roster",
                AsyncMock(return_value=str(case.get("open_work_block") or "")),
            ),
            patch("core.turns.execution.require_llm_ready", lambda: None),
            _live_temperature_patch(orchestrator.agent, temperature) if temperature is not None else nullcontext(),
        ):
            mock_db.get_history = AsyncMock(return_value=case.get("history") or [])
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            execute_patch = (
                patch(
                    "core.agent.agent.dispatcher.dispatch",
                    AsyncMock(side_effect=executor_stub.dispatch),
                )
                if live and executor_stub is not None
                else nullcontext()
            )

            with execute_patch:
                await orchestrator._execute_turn(
                    transcript=_prepare_eval_transcript(case),
                    source=case.get("source", "user"),
                    connection_id=connection_id,
                    owner_id=owner_id,
                    session_context=session_context,
                    text_input=bool(case.get("text_input", case.get("source", "user") == "user")),
                    attachments=None,
                    delivery=HeadlessDelivery(),
                    result=result,
                    routing_hint=case.get("routing_hint"),
                    trigger_decision=case.get("trigger_decision"),
                )

    return extract_turn_trace(result)


async def _run_mock_case(
    case: dict[str, Any],
    *,
    connection_id: str,
    meta: dict[str, Any],
) -> TurnTraceSnapshot:
    script = case.get("script") or []
    events = _events_from_script(script)
    agent = MagicMock()
    agent.llm = MagicMock()
    agent.llm.model = case.get("model") or "mock-model"
    agent.process_stream = MagicMock(return_value=AsyncIterEvents(events))
    orchestrator = _make_orchestrator(agent)

    with (
        patch("core.tool_router.tool_router") as mock_router,
    ):
        mock_router.route = AsyncMock(return_value=set(case.get("routed_tools") or []))
        mock_router.get_last_diagnostics = MagicMock(return_value=None)
        return await _execute_eval_turn(
            orchestrator,
            case,
            connection_id=connection_id,
            live=False,
            meta=meta,
        )


async def _run_live_case(
    case: dict[str, Any],
    *,
    connection_id: str,
    meta: dict[str, Any],
) -> TurnTraceSnapshot:
    from core.home import seed_home
    from core.llm.service import LLMService
    from core.setup.llm_config import llm_config_store, resolve_llm_config

    seed_home()
    await _ensure_eval_plugins_loaded()
    from services.database.mongodb import mongodb
    if mongodb.db is None:
        await mongodb.connect()
    await llm_config_store.load_persisted()
    config = await resolve_llm_config()
    if not config.attemptable:
        raise RuntimeError(
            "LLM is not configured; complete setup or persist provider settings before running live evals."
        )

    llm = LLMService(
        api_key=config.api_key,
        base_url=config.base_url,
        model=case.get("model") or config.model,
        provider_name=config.provider,
    )
    await llm.initialize()
    if not llm.is_initialized:
        raise RuntimeError("LLM client not initialized; configure the assistant brain before running live evals.")

    agent = JarvisAgent(llm)
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.stt = MagicMock()
    orchestrator.llm = llm
    orchestrator.agent = agent
    orchestrator.tts = MagicMock()
    orchestrator.headless_pool = MagicMock()
    orchestrator.router = MagicMock()
    orchestrator.router.route = AsyncMock(return_value=agent)

    executor_stub = LiveToolOutputStub(_live_tool_output_sequence(case))
    return await _execute_eval_turn(
        orchestrator,
        case,
        connection_id=connection_id,
        live=True,
        meta=meta,
        executor_stub=executor_stub,
    )


def _snapshot_dict(snapshot: TurnTraceSnapshot) -> dict[str, Any]:
    return {
        "model": snapshot.model,
        "routed_tools": snapshot.routed_tools,
        "tools_called": snapshot.tools_called,
        "full_response": snapshot.full_response,
        "tool_outputs": snapshot.tool_outputs,
        "tool_calls": [
            {
                "fqns": list(call.fqns),
                "capability": call.capability,
                "arguments": call.arguments,
                "code": call.code,
                "spoken": call.spoken,
                "output": call.output,
            }
            for call in snapshot.tool_calls
        ],
        "interrupted": snapshot.interrupted,
    }


def _score_to_dict(score: CaseScore) -> list[dict[str, Any]]:
    return [asdict(item) for item in score.results]


async def _run_case(
    case: dict[str, Any],
    *,
    run_index: int,
    live: bool,
    meta: dict[str, Any],
) -> RunRow:
    case_id = case["id"]
    use_live = _should_run_live(case, live=live)
    connection_id = f"eval-{case_id}-{run_index}"
    started = time.perf_counter()

    if use_live:
        snapshot = await _run_live_case(case, connection_id=connection_id, meta=meta)
        effective_tier = "live"
    else:
        snapshot = await _run_mock_case(case, connection_id=connection_id, meta=meta)
        effective_tier = "mock"

    case_score = score_case(snapshot, case.get("assert") or {})
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return RunRow(
        case_id=case_id,
        run=run_index,
        tier=effective_tier,
        passed=case_score.passed,
        duration_ms=duration_ms,
        snapshot=_snapshot_dict(snapshot),
        scores=_score_to_dict(case_score),
        manifest_mode=_manifest_mode(case) if use_live else "n/a",
        model=snapshot.model,
    )


def _summarize_case(
    case: dict[str, Any],
    rows: list[RunRow],
    *,
    threshold: float,
) -> CaseSummary:
    case_rows = [row for row in rows if row.case_id == case["id"]]
    passed_runs = sum(1 for row in case_rows if row.passed)
    pass_rate = passed_runs / len(case_rows) if case_rows else 0.0
    failures: list[str] = []
    for row in case_rows:
        if row.passed:
            continue
        for score in row.scores:
            if not score["passed"]:
                failures.append(f"run {row.run}: {score['name']} — {score.get('detail', '')}")
    return CaseSummary(
        case_id=case["id"],
        priority=case.get("priority", "P1"),
        category=case.get("category", "uncategorized"),
        tier=case.get("tier", "mock"),
        runs=len(case_rows),
        pass_rate=round(pass_rate, 4),
        passed=pass_rate >= threshold,
        failures=failures[:6],
        manifest_mode=case_rows[0].manifest_mode if case_rows else _manifest_mode(case),
        pass_threshold=threshold,
    )


def _write_artifacts(
    run_dir: Path,
    *,
    args: argparse.Namespace,
    meta: dict[str, Any],
    summaries: list[CaseSummary],
    rows: list[RunRow],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tool": "evaluate_agent_behavior",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "cases_file": str(args.cases),
        "priority": args.priority,
        "live": args.live,
        "git_sha": _git_sha(),
        "meta": meta,
        "models": sorted({row.model for row in rows if row.model}),
    }
    summary = {
        "cases": len(summaries),
        "passed_cases": sum(1 for item in summaries if item.passed),
        "failed_cases": sum(1 for item in summaries if not item.passed),
        "pass_threshold": meta.get("pass_threshold", 0.67),
        "case_summaries": [asdict(item) for item in summaries],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row)) + "\n")

    lines = [
        "# Agent Behavior Eval",
        "",
        f"- Cases: {summary['cases']}",
        f"- Passed: {summary['passed_cases']}",
        f"- Failed: {summary['failed_cases']}",
        f"- Default pass threshold: {summary['pass_threshold']:.0%}",
        "",
        "## Cases",
        "",
    ]
    for item in summaries:
        status = "PASS" if item.passed else "FAIL"
        lines.append(
            f"- `{item.case_id}` [{status}] pass_rate={item.pass_rate:.0%} "
            f"({item.runs} runs, threshold={item.pass_threshold:.0%}, manifest={item.manifest_mode})"
        )
        for failure in item.failures:
            lines.append(f"  - {failure}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_summary(summaries: list[CaseSummary]) -> None:
    print("case_id                          pri  category         pass_rate runs status")
    print("-" * 78)
    for item in summaries:
        status = "PASS" if item.passed else "FAIL"
        print(
            f"{item.case_id:<32} "
            f"{item.priority:<4} "
            f"{item.category:<16} "
            f"{item.pass_rate:>8.0%} "
            f"{item.runs:>4} "
            f"{status}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate JARV1S agent/LLM behavior.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--priority", default="P0", help="only run cases with this priority")
    parser.add_argument("--case", dest="case_id", default=None, help="run a single case id")
    parser.add_argument("--runs", type=int, default=None, help="override per-case run count")
    parser.add_argument("--live", action="store_true", help="use live LLM for live/both tier cases")
    parser.add_argument("--probe", action="store_true", help="run prompt-tuning probe cases (requires --live)")
    parser.add_argument("--label", default=None, help="write run artifacts under logs/evals")
    parser.add_argument("--json", action="store_true", help="emit JSON summary to stdout")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    if args.probe and not args.live:
        print("--probe requires --live; probe cases measure real model behavior.", file=sys.stderr)
        return 1
    meta, cases = _load_cases(args.cases)
    if args.probe:
        cases = [case for case in cases if case.get("tier") == "probe"]
    else:
        if args.priority:
            cases = [case for case in cases if case.get("priority") == args.priority]
        if not args.live:
            cases = [case for case in cases if case.get("tier", "mock") not in {"live", "probe"}]
        else:
            cases = [case for case in cases if case.get("tier", "mock") != "probe"]
    if args.case_id:
        cases = [case for case in cases if case["id"] == args.case_id]
    if not cases:
        print("No matching agent behavior cases.", file=sys.stderr)
        return 1

    rows: list[RunRow] = []

    for case in cases:
        runs = _runs_for_case(case, args=args, meta=meta)
        for run_index in range(1, runs + 1):
            rows.append(await _run_case(case, run_index=run_index, live=args.live, meta=meta))

    summaries = [
        _summarize_case(
            case,
            rows,
            threshold=_pass_threshold_for_case(case, meta, live=args.live),
        )
        for case in cases
    ]

    if args.label:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = DEFAULT_EVALS_DIR / f"{timestamp}_{_safe_label(args.label)}"
        _write_artifacts(run_dir, args=args, meta=meta, summaries=summaries, rows=rows)
        print(f"wrote_eval={run_dir}")

    if args.json:
        print(json.dumps({"summaries": [asdict(item) for item in summaries], "rows": [asdict(row) for row in rows]}, indent=2))
    else:
        _print_summary(summaries)

    failed = [item for item in summaries if not item.passed]
    if args.probe:
        return 0
    if failed:
        print("\nFailures:")
        for item in failed:
            print(f"- {item.case_id}: pass_rate={item.pass_rate:.0%}")
            for detail in item.failures:
                print(f"    {detail}")
        return 1
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
