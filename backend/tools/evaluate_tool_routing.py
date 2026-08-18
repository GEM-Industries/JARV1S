"""Offline router evaluation for plugin-level tool routing.

Runs without calling the LLM. It loads local plugins, initializes the
ToolRouter from hand-written/curated/heuristic utterances, and compares named
router policies against `backend/evals/tool_routing.yaml`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.plugins.registry import registry  # noqa: E402
from core.routing.helpers import expand_plugins_to_fqns  # noqa: E402
from core.routing.policies import (  # noqa: E402
    BASELINE_POLICY,
    SYSTEM_POLICY,
    TEXT_POLICY,
    VOICE_POLICY,
    RoutingPolicy,
)
from core.tool_router import ToolRouter  # noqa: E402
from evals.bootstrap import ensure_eval_plugins_loaded  # noqa: E402


DEFAULT_CASES = BACKEND_DIR / "evals" / "tool_routing.yaml"


@dataclass(frozen=True)
class TurnLabels:
    user: str
    required_plugins: set[str]
    optional_plugins: set[str]
    hard_negative_plugins: set[str]
    depends_on_previous_turn: bool = False


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    turns: list[TurnLabels]


POLICIES: dict[str, RoutingPolicy] = {
    "baseline": BASELINE_POLICY,
    "voice_default": VOICE_POLICY,
    "text_default": TEXT_POLICY,
    "system_hint": SYSTEM_POLICY,
}


def _load_cases(path: Path) -> list[EvalCase]:
    payload = yaml.safe_load(path.read_text()) or []
    cases: list[EvalCase] = []
    for raw in payload:
        turns = [
            TurnLabels(
                user=str(turn["user"]),
                required_plugins=set(turn.get("required_plugins") or []),
                optional_plugins=set(turn.get("optional_plugins") or []),
                hard_negative_plugins=set(
                    turn.get("hard_negative_plugins")
                    or turn.get("excluded_plugins")
                    or []
                ),
                depends_on_previous_turn=bool(turn.get("depends_on_previous_turn")),
            )
            for turn in raw.get("turns", [])
        ]
        cases.append(
            EvalCase(
                case_id=str(raw["id"]),
                category=str(raw.get("category") or "uncategorized"),
                turns=turns,
            )
        )
    return cases


def _plugins_from_fqns(fqns: set[str]) -> set[str]:
    return {fqn.split(".", 1)[0] for fqn in fqns}


def _expand_plugins(plugin_names: set[str]) -> set[str]:
    return expand_plugins_to_fqns(plugin_names, registry)


def _schema_stats(fqns: set[str]) -> tuple[int, int]:
    return registry.estimate_schema_stats(fqns)


def _score_turn(labels: TurnLabels, routed_plugins: set[str], routed_fqns: set[str]) -> dict[str, Any]:
    required = labels.required_plugins
    allowed = labels.required_plugins | labels.optional_plugins
    required_hit = required & routed_plugins
    hard_negative_hit = labels.hard_negative_plugins & routed_plugins
    schema_chars, schema_tokens = _schema_stats(routed_fqns)

    precision_denominator = len(routed_plugins)
    precision = (
        len(routed_plugins & allowed) / precision_denominator
        if precision_denominator
        else 1.0 if not required else 0.0
    )
    required_recall = len(required_hit) / len(required) if required else 1.0

    return {
        "required_recall": required_recall,
        "all_required_hit": required_hit == required,
        "no_tool_clean": not required and not routed_plugins,
        "oracle_gap": sorted(required - routed_plugins),
        "precision": precision,
        "hard_negative_hit_count": len(hard_negative_hit),
        "routed_plugin_count": len(routed_plugins),
        "routed_tool_count": len(routed_fqns),
        "schema_chars": schema_chars,
        "schema_tokens": schema_tokens,
    }


async def _initialize_router() -> ToolRouter:
    await ensure_eval_plugins_loaded()
    router = ToolRouter()
    await router.initialize(llm_service=None)
    return router


async def _run_oracle(cases: list[EvalCase]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        for turn_index, labels in enumerate(case.turns):
            plugin_names = labels.required_plugins | labels.optional_plugins
            routed_fqns = _expand_plugins(plugin_names)
            routed_plugins = _plugins_from_fqns(routed_fqns)
            rows.append({
                "policy": "oracle",
                "case_id": case.case_id,
                "category": case.category,
                "turn_index": turn_index,
                "depends_on_previous_turn": labels.depends_on_previous_turn,
                "route_latency_ms": 0.0,
                **_score_turn(labels, routed_plugins, routed_fqns),
            })
    return rows


async def _run_policy(
    cases: list[EvalCase],
    policy: RoutingPolicy,
    router: ToolRouter,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        session_id = f"eval-{policy.name}-{case.case_id}"
        router.clear_session(session_id)
        for turn_index, labels in enumerate(case.turns):
            routed_fqns = await router.route(
                labels.user,
                session_id,
                policy=policy,
            )
            routed_plugins = _plugins_from_fqns(routed_fqns)
            diagnostics = router.get_last_diagnostics(session_id)
            rows.append({
                "policy": policy.name,
                "case_id": case.case_id,
                "category": case.category,
                "turn_index": turn_index,
                "depends_on_previous_turn": labels.depends_on_previous_turn,
                "match_mode": diagnostics.match_mode if diagnostics else None,
                "matched_plugins": diagnostics.matched_plugins if diagnostics else sorted(routed_plugins),
                "route_latency_ms": diagnostics.route_latency_ms if diagnostics else 0.0,
                **_score_turn(labels, routed_plugins, routed_fqns),
            })
            if labels.required_plugins:
                # Offline evals do not execute tools. Record oracle plugin focus so
                # the next depends_on_previous_turn row exercises production's
                # tool-result focus path without an LLM/tool runtime.
                router.record_tool_focus(
                    session_id,
                    tools=_expand_plugins(labels.required_plugins),
                )
    return rows


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return ordered[index]


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    policies = sorted({row["policy"] for row in rows})
    summaries: list[dict[str, Any]] = []
    for policy in policies:
        subset = [row for row in rows if row["policy"] == policy]
        no_tool_subset = [row for row in subset if row["category"] == "no_tool"]
        summaries.append({
            "policy": policy,
            "turns": len(subset),
            "all_required_hit_rate": _mean([float(row["all_required_hit"]) for row in subset]),
            "no_tool_clean_rate": _mean([float(row["no_tool_clean"]) for row in no_tool_subset]),
            "required_recall": _mean([row["required_recall"] for row in subset]),
            "precision": _mean([row["precision"] for row in subset]),
            "hard_negative_hit_count": sum(row["hard_negative_hit_count"] for row in subset),
            "avg_schema_tokens": round(_mean([row["schema_tokens"] for row in subset]), 1),
            "p95_route_latency_ms": round(_p95([row["route_latency_ms"] for row in subset]), 1),
        })
    return summaries


def _summarize_categories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = sorted({(row["policy"], row["category"]) for row in rows})
    summaries: list[dict[str, Any]] = []
    for policy, category in keys:
        subset = [row for row in rows if row["policy"] == policy and row["category"] == category]
        summaries.append({
            "policy": policy,
            "category": category,
            "turns": len(subset),
            "all_required_hit_rate": _mean([float(row["all_required_hit"]) for row in subset]),
            "required_recall": _mean([row["required_recall"] for row in subset]),
            "precision": _mean([row["precision"] for row in subset]),
            "no_tool_clean_rate": _mean([float(row["no_tool_clean"]) for row in subset])
            if category == "no_tool" else None,
            "hard_negative_hit_count": sum(row["hard_negative_hit_count"] for row in subset),
            "avg_schema_tokens": round(_mean([row["schema_tokens"] for row in subset]), 1),
        })
    return summaries


def _print_summary(summaries: list[dict[str, Any]]) -> None:
    print("policy                         turns  hit    recall precision no_tool schema_tok p95_ms hard_neg")
    print("-" * 96)
    for row in summaries:
        print(
            f"{row['policy']:<30} "
            f"{row['turns']:>5} "
            f"{row['all_required_hit_rate']:.2f}   "
            f"{row['required_recall']:.2f}   "
            f"{row['precision']:.2f}      "
            f"{row['no_tool_clean_rate']:.2f}    "
            f"{row['avg_schema_tokens']:>7} "
            f"{row['p95_route_latency_ms']:>6} "
            f"{row['hard_negative_hit_count']:>8}"
        )


def _print_category_summary(rows: list[dict[str, Any]], policy: str) -> None:
    subset = [row for row in rows if row["policy"] == policy]
    if not subset:
        return
    print(f"\nCategory breakdown for {policy}:")
    print("category                           turns hit  recall precision no_tool schema_tok hard_neg")
    for row in _summarize_categories(subset):
        no_tool = "-" if row["no_tool_clean_rate"] is None else f"{row['no_tool_clean_rate']:.2f}"
        print(
            f"{row['category']:<34} "
            f"{row['turns']:>5} "
            f"{row['all_required_hit_rate']:.2f} "
            f"{row['required_recall']:.2f} "
            f"{row['precision']:.2f} "
            f"{no_tool:>7} "
            f"{row['avg_schema_tokens']:>8} "
            f"{row['hard_negative_hit_count']:>8}"
        )


def _voice_sweep_policies() -> dict[str, RoutingPolicy]:
    policies: dict[str, RoutingPolicy] = {}
    for max_matched in (2, 3, 4):
        for threshold in (0.70, 0.72, 0.74):
            for fallback_threshold, fallback_top_k in ((0.65, 1), (0.68, 1), (1.01, 0)):
                for segment_top_k in (1, 2):
                    for schema_budget in (8_000, 12_000, 16_000):
                        name = (
                            f"voice_m{max_matched}_t{int(threshold * 100)}"
                            f"_fb{int(fallback_threshold * 100) if fallback_top_k else 'off'}"
                            f"_s{segment_top_k}_b{schema_budget // 1000}k"
                        )
                        policies[name] = RoutingPolicy(
                            name=name,
                            threshold=threshold,
                            fallback_threshold=fallback_threshold,
                            fallback_top_k=fallback_top_k,
                            max_matched=max_matched,
                            segment_top_k=segment_top_k,
                            multi_intent=True,
                            session_carryover=True,
                            schema_char_budget=schema_budget,
                        )
    return policies


def _voice_score(summary: dict[str, Any]) -> float:
    return (
        summary["required_recall"] * 100
        + summary["all_required_hit_rate"] * 35
        + summary["no_tool_clean_rate"] * 25
        + summary["precision"] * 15
        - summary["hard_negative_hit_count"] * 3
        - max(0.0, summary["avg_schema_tokens"] - 2200) / 120
        - max(0.0, summary["p95_route_latency_ms"] - 25) / 10
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate JARV1S tool routing policies.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--policy",
        choices=["all", "oracle", *POLICIES.keys()],
        default="all",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument("--categories", action="store_true", help="Print category summaries.")
    parser.add_argument("--sweep", action="store_true", help="Run a voice-policy grid search.")
    parser.add_argument("--top", type=int, default=10, help="Number of sweep policies to print.")
    args = parser.parse_args()

    cases = _load_cases(args.cases)
    rows: list[dict[str, Any]] = []
    if args.sweep:
        router = await _initialize_router()
        rows = []
        rows.extend(await _run_oracle(cases))
        for policy in _voice_sweep_policies().values():
            rows.extend(await _run_policy(cases, policy, router))
        summaries = _summarize(rows)
        ranked = sorted(
            [row for row in summaries if row["policy"] != "oracle"],
            key=_voice_score,
            reverse=True,
        )
        if args.json:
            print(json.dumps({"summary": summaries, "ranked": ranked, "rows": rows}, indent=2))
        else:
            print("Top voice policies:")
            _print_summary(ranked[:args.top])
            _print_category_summary(rows, ranked[0]["policy"])
        return

    if args.policy in {"all", "oracle"}:
        await ensure_eval_plugins_loaded()
        rows.extend(await _run_oracle(cases))

    selected = POLICIES.keys() if args.policy == "all" else [args.policy]
    if any(name in POLICIES for name in selected):
        router = await _initialize_router()
        for name in selected:
            if name in POLICIES:
                rows.extend(await _run_policy(cases, POLICIES[name], router))

    summaries = _summarize(rows)
    if args.json:
        print(json.dumps({
            "summary": summaries,
            "categories": _summarize_categories(rows),
            "rows": rows,
        }, indent=2))
    else:
        _print_summary(summaries)
        if args.categories:
            for policy in sorted({row["policy"] for row in rows}):
                _print_category_summary(rows, policy)
        failures = [
            row for row in rows
            if row["policy"] != "oracle" and not row["all_required_hit"]
        ][:20]
        if failures:
            print("\nFirst misses:")
            for row in failures:
                print(
                    f"- {row['policy']} {row['case_id']}#{row['turn_index']}: "
                    f"missing={row['oracle_gap']} matched={row.get('matched_plugins')}"
                )


if __name__ == "__main__":
    asyncio.run(main())
