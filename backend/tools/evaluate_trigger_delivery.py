"""Offline trigger delivery eval.

Verifies code-level contracts for the TriggerInstance delivery pipeline.

Unlike evaluate_tool_routing.py, this does not call an LLM. It checks that
required symbols, patterns, and absence-patterns hold in source files and
importable modules.

Run from backend/:
    uv run python tools/evaluate_trigger_delivery.py --mode new
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

EVAL_FILE = BACKEND_DIR / "evals" / "trigger_delivery.yaml"


@dataclass
class CheckResult:
    scenario_id: str
    check_kind: str
    description: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario_id: str
    description: str
    check_results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.check_results)


def _load_scenarios(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        data = yaml.safe_load(f)
    return data.get("scenarios", [])


def _resolve_symbol(module_path: str, symbol_dotted: str) -> tuple[bool, str]:
    """Import module and resolve a dotted attribute chain."""
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        return False, f"import failed: {exc}"
    obj = mod
    for part in symbol_dotted.split("."):
        obj = getattr(obj, part, _MISSING)
        if obj is _MISSING:
            return False, f"attribute '{part}' not found in chain '{symbol_dotted}'"
    return True, "ok"


_MISSING = object()


def _check_pattern_in_source(file_rel: str, pattern: str) -> tuple[bool, str]:
    path = BACKEND_DIR / file_rel
    if not path.exists():
        return False, f"file not found: {path}"
    source = path.read_text(encoding="utf-8")
    if re.search(pattern, source):
        return True, "pattern found"
    return False, f"pattern not found: {pattern!r}"


def _check_pattern_not_in_source(file_rel: str, pattern: str) -> tuple[bool, str]:
    path = BACKEND_DIR / file_rel
    if not path.exists():
        # Missing file satisfies "pattern not in source" – absence is compliant.
        return True, "file does not exist (pattern absent by definition)"
    source = path.read_text(encoding="utf-8")
    if not re.search(pattern, source):
        return True, "pattern absent"
    return False, f"pattern still present: {pattern!r}"


def _run_check(scenario_id: str, check: dict[str, Any]) -> CheckResult:
    kind = check["kind"]
    base = CheckResult(
        scenario_id=scenario_id,
        check_kind=kind,
        description=f"{kind}: {check.get('symbol') or check.get('pattern') or ''}",
        passed=False,
    )
    if kind == "symbol_exists":
        module = check["module"]
        symbol = check["symbol"]
        passed, detail = _resolve_symbol(module, symbol)
        base.passed = passed
        base.detail = detail
    elif kind == "pattern_in_source":
        passed, detail = _check_pattern_in_source(check["file"], check["pattern"])
        base.passed = passed
        base.detail = detail
    elif kind == "pattern_not_in_source":
        passed, detail = _check_pattern_not_in_source(check["file"], check["pattern"])
        base.passed = passed
        base.detail = detail
    else:
        base.passed = False
        base.detail = f"unknown check kind: {kind!r}"
    return base


def run_eval(mode: str) -> list[ScenarioResult]:
    raw_scenarios = _load_scenarios(EVAL_FILE)
    results: list[ScenarioResult] = []

    for raw in raw_scenarios:
        scenario_id = raw["id"]
        description = raw.get("description", "")
        mode_checks = raw.get(mode, {}).get("checks", [])

        result = ScenarioResult(scenario_id=scenario_id, description=description)
        for check in mode_checks:
            cr = _run_check(scenario_id, check)
            result.check_results.append(cr)
        results.append(result)

    return results


def _print_results(results: list[ScenarioResult], mode: str) -> int:
    total = 0
    failed = 0
    print(f"\n{'=' * 60}")
    print(f"  Trigger Delivery Eval  [mode={mode}]")
    print(f"{'=' * 60}\n")

    for sr in results:
        status = "PASS" if sr.passed else "FAIL"
        print(f"[{status}] {sr.scenario_id}")
        print(f"       {sr.description}")
        for cr in sr.check_results:
            check_status = "✓" if cr.passed else "✗"
            print(f"       {check_status}  {cr.description}")
            if not cr.passed:
                print(f"          → {cr.detail}")
        print()
        total += 1
        if not sr.passed:
            failed += 1

    print(f"{'=' * 60}")
    print(f"  {total - failed}/{total} scenarios passed")
    print(f"{'=' * 60}\n")
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline trigger delivery eval")
    parser.add_argument(
        "--mode",
        choices=["new"],
        default="new",
        help="Eval mode. Only the post-refactor trigger path is supported.",
    )
    args = parser.parse_args()

    results = run_eval(args.mode)
    failed = _print_results(results, args.mode)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
