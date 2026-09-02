"""Deterministic scorers for agent-behavior eval cases."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.turns.delivery import is_no_reply

from evals.trace_extractor import TurnTraceSnapshot

_COMPLETION_CLAIM_RE = re.compile(
    r"\b("
    r"deleted|removed|sent|scheduled|updated|completed|created|"
    r"cleared|cancelled|canceled|booked|added|set up|set the"
    r")\b",
    re.IGNORECASE,
)
_NON_SUCCESS_PREFIXES = (
    "Approval needed:",
)
_NON_SUCCESS_MARKERS = (
    "requires re-authorization",
    "was not approved in background mode",
    "has not executed yet",
)


def _is_successful_tool_output(output: str | None) -> bool:
    if not output:
        return False
    text = output.lstrip()
    if text.startswith(_NON_SUCCESS_PREFIXES):
        return False
    lowered = text.lower()
    return not any(marker in lowered for marker in _NON_SUCCESS_MARKERS)


@dataclass
class ScoreResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseScore:
    passed: bool
    results: list[ScoreResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append(ScoreResult(name=name, passed=passed, detail=detail))
        if not passed:
            self.passed = False


def score_case(snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> CaseScore:
    case_score = CaseScore(passed=True)
    asserts = asserts or {}

    _score_tools_required(case_score, snapshot, asserts.get("tools_called") or [])
    _score_tools_forbidden(case_score, snapshot, asserts.get("tools_forbidden") or [])
    _score_literal_arguments(case_score, snapshot, asserts.get("expected_arguments") or {})
    _score_forbidden_arguments(case_score, snapshot, asserts.get("forbidden_arguments") or {})
    _score_no_reply(case_score, snapshot, asserts)
    _score_false_completion(case_score, snapshot, asserts)
    _score_final_facts(case_score, snapshot, asserts)
    _score_pre_tool_speech(case_score, snapshot, asserts)
    _score_budget(case_score, snapshot, asserts.get("max_tool_calls"))
    _score_response_patterns(case_score, snapshot, asserts)
    return case_score


def _normalize_tool_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("jarvis."):
        ref = ref[len("jarvis.") :]
    return ref


def _tool_matches(called: str, expected: str) -> bool:
    called_norm = _normalize_tool_ref(called)
    expected_norm = _normalize_tool_ref(expected)
    return called_norm == expected_norm or called_norm.endswith(f".{expected_norm}")


def _score_tools_required(case_score: CaseScore, snapshot: TurnTraceSnapshot, required: list[str]) -> None:
    if not required:
        return
    missing = [
        tool for tool in required
        if not any(_tool_matches(called, tool) for called in snapshot.tools_called)
    ]
    case_score.add(
        "tool_required",
        not missing,
        f"missing={missing}" if missing else "",
    )


def _score_tools_forbidden(case_score: CaseScore, snapshot: TurnTraceSnapshot, forbidden: list[str]) -> None:
    if not forbidden:
        return
    hits = [
        tool for tool in forbidden
        if any(_tool_matches(called, tool) for called in snapshot.tools_called)
    ]
    case_score.add(
        "tool_forbidden",
        not hits,
        f"forbidden_hit={hits}" if hits else "",
    )


def _score_literal_arguments(
    case_score: CaseScore,
    snapshot: TurnTraceSnapshot,
    expected_arguments: dict[str, str],
) -> None:
    if not expected_arguments:
        return
    code = snapshot.all_code
    failures: list[str] = []
    for key, pattern in expected_arguments.items():
        if isinstance(pattern, str) and pattern.startswith("regex:"):
            regex = pattern[len("regex:") :]
            if not re.search(regex, code):
                failures.append(f"{key}~/{regex}/")
        elif str(pattern) not in code:
            failures.append(f"{key}={pattern}")
    case_score.add(
        "argument_match",
        not failures,
        ", ".join(failures),
    )


def _score_forbidden_arguments(
    case_score: CaseScore,
    snapshot: TurnTraceSnapshot,
    forbidden_arguments: dict[str, Any],
) -> None:
    if not forbidden_arguments:
        return
    hits = [
        f"{call.capability}.{key}={value}"
        for call in snapshot.tool_calls
        for key, value in forbidden_arguments.items()
        if call.arguments.get(key) == value
    ]
    case_score.add(
        "arguments_forbidden",
        not hits,
        f"forbidden_arguments={hits}" if hits else "",
    )


def _score_no_reply(case_score: CaseScore, snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> None:
    expect = asserts.get("expect_no_reply")
    if expect is None:
        return
    actual = is_no_reply(snapshot.full_response)
    if expect:
        case_score.add(
            "no_reply",
            actual,
            f"expected NO_REPLY, got {snapshot.full_response!r}",
        )
    else:
        case_score.add(
            "no_reply",
            not actual,
            f"unexpected NO_REPLY: {snapshot.full_response!r}",
        )


def _score_false_completion(case_score: CaseScore, snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> None:
    if asserts.get("allow_completion_claim"):
        return
    response = snapshot.full_response
    if not response or is_no_reply(response):
        return
    if not _COMPLETION_CLAIM_RE.search(response):
        return

    has_successful_tool = any(
        _is_successful_tool_output(output) for output in snapshot.tool_outputs
    )
    case_score.add(
        "no_false_completion_claim",
        has_successful_tool,
        "response claims completion but no successful tool output was recorded",
    )


def _score_final_facts(case_score: CaseScore, snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> None:
    contains = asserts.get("response_contains") or asserts.get("expected_response") or []
    if isinstance(contains, str):
        contains = [contains]
    if not contains:
        return
    text = snapshot.full_response.lower()
    missing = [fact for fact in contains if str(fact).lower() not in text]
    case_score.add(
        "final_fact_match",
        not missing,
        f"missing_facts={missing}" if missing else "",
    )


def _score_pre_tool_speech(case_score: CaseScore, snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> None:
    expected = asserts.get("expect_pre_tool_speech")
    if expected is None:
        return

    first_call = snapshot.tool_calls[0] if snapshot.tool_calls else None
    spoken = (first_call.spoken or "").strip() if first_call else ""
    if expected != "none":
        case_score.add(
            "pre_tool_speech",
            False,
            f"unknown expectation={expected!r}",
        )
        return

    case_score.add(
        "pre_tool_speech",
        not spoken,
        f"unexpected={spoken!r}" if spoken else "",
    )


def _score_budget(case_score: CaseScore, snapshot: TurnTraceSnapshot, max_tool_calls: int | None) -> None:
    if max_tool_calls is None:
        return
    count = len(snapshot.tool_calls)
    case_score.add(
        "budget",
        count <= max_tool_calls,
        f"tool_calls={count} max={max_tool_calls}",
    )


def _score_response_patterns(case_score: CaseScore, snapshot: TurnTraceSnapshot, asserts: dict[str, Any]) -> None:
    response = snapshot.full_response
    must_not = asserts.get("response_must_not_contain") or []
    if isinstance(must_not, str):
        must_not = [must_not]
    if must_not:
        hits = [term for term in must_not if term.lower() in response.lower()]
        case_score.add(
            "response_must_not_contain",
            not hits,
            f"forbidden_terms={hits}" if hits else "",
        )

    tool_output_contains = asserts.get("tool_output_contains") or []
    if isinstance(tool_output_contains, str):
        tool_output_contains = [tool_output_contains]
    if tool_output_contains:
        blob = "\n".join(snapshot.tool_outputs).lower()
        missing = [term for term in tool_output_contains if term.lower() not in blob]
        case_score.add(
            "tool_output_contains",
            not missing,
            f"missing_in_tool_output={missing}" if missing else "",
        )
