"""Benchmark indexed Activity projection growth against disposable fixtures."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from datetime import datetime, timedelta, timezone

from core.activity.page import ActivityQuery, activity_page
from services.database.mongodb import mongodb

OWNER_ID = "__activity_benchmark__"
BATCH_SIZE = 5_000


def _documents(count: int) -> tuple[list[dict], list[dict], list[dict]]:
    base = datetime.now(timezone.utc)
    conversations: list[dict] = []
    instances: list[dict] = []
    tasks: list[dict] = []
    for index in range(count):
        occurred_at = base - timedelta(seconds=index)
        ident = f"{index:09d}"
        source = index % 5
        if source in {0, 1}:
            conversations.append(
                {
                    "owner_id": OWNER_ID,
                    "role": "user",
                    "source": "user",
                    "content": f"Benchmark conversation {ident}",
                    "timestamp": occurred_at,
                    "metadata": {"turn_id": f"bench-turn-{ident}"},
                }
            )
        elif source in {2, 3}:
            external = source == 3
            instances.append(
                {
                    "owner_id": OWNER_ID,
                    "id": f"bench-instance-{ident}",
                    "rule_id": f"bench-rule-{ident}",
                    "status": "completed",
                    "created_at": occurred_at,
                    "updated_at": occurred_at,
                    "origin_snapshot": {
                        "kind": "external" if external else "time",
                        "source": "benchmark",
                    },
                    "source_event": {
                        "rule_id": f"bench-rule-{ident}",
                        "rule_name": f"Benchmark rule {ident}",
                    },
                    "action_snapshot": {"message": f"Benchmark action {ident}"},
                    "result_text": f"Benchmark result {ident}",
                }
            )
        else:
            tasks.append(
                {
                    "owner_id": OWNER_ID,
                    "task_id": f"bench-task-{ident}",
                    "status": "completed",
                    "source": "benchmark",
                    "prompt": f"Benchmark task {ident}",
                    "created_at": occurred_at,
                    "completed_at": occurred_at,
                }
            )
    return conversations, instances, tasks


async def _insert_many(collection, documents: list[dict]) -> None:
    for start in range(0, len(documents), BATCH_SIZE):
        await collection.insert_many(documents[start : start + BATCH_SIZE], ordered=False)


async def _seed(count: int) -> datetime:
    conversations, instances, tasks = _documents(count)
    await asyncio.gather(
        _insert_many(mongodb.db.conversations, conversations),
        _insert_many(mongodb.db.trigger_instances, instances),
        _insert_many(mongodb.db.background_tasks, tasks),
    )
    return max(
        conversations[0]["timestamp"],
        instances[0]["updated_at"],
        tasks[0]["created_at"],
    )


async def _cleanup() -> None:
    await asyncio.gather(
        mongodb.db.conversations.delete_many({"owner_id": OWNER_ID}),
        mongodb.db.trigger_instances.delete_many({"owner_id": OWNER_ID}),
        mongodb.db.background_tasks.delete_many({"owner_id": OWNER_ID}),
    )


async def run(sizes: list[int], runs: int, target_ms: float) -> int:
    await mongodb.connect()
    largest = max(sizes)
    try:
        await _cleanup()
        newest = await _seed(largest)
        failed = False
        for size in sizes:
            since = newest - timedelta(seconds=size - 1)
            query = ActivityQuery(since=since)
            await activity_page(OWNER_ID, query=query, limit=50)
            timings: list[float] = []
            for _ in range(runs):
                started = time.perf_counter()
                page = await activity_page(OWNER_ID, query=query, limit=50)
                timings.append((time.perf_counter() - started) * 1_000)
                if len(page.items) != 50:
                    raise RuntimeError(f"Expected 50 rows, received {len(page.items)}")
            ordered = sorted(timings)
            p95 = ordered[min(len(ordered) - 1, round(0.95 * len(ordered)) - 1)]
            print(
                f"{size:>6} candidates: median={statistics.median(timings):6.1f}ms "
                f"p95={p95:6.1f}ms target={target_ms:.0f}ms"
            )
            failed = failed or p95 > target_ms
        return 1 if failed else 0
    finally:
        await _cleanup()
        await mongodb.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[1_000, 10_000, 100_000])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--target-ms", type=float, default=400)
    args = parser.parse_args()
    if args.runs < 1 or any(size < 50 for size in args.sizes):
        parser.error("runs must be positive and every size must be at least 50")
    return asyncio.run(run(args.sizes, args.runs, args.target_ms))


if __name__ == "__main__":
    raise SystemExit(main())
