"""Unit tests for Phase 9c PrefetchService and orchestrator cache consumption.

Covers:
- Window filter: only upcoming triggers inside [now, now+window] are scanned.
- Mode gating: silent / evaluate / non-protocol triggers are ignored.
- Idempotency: stale-running rows are reclaimed; fresh ready/running rows
  collapse to False via the unique-index DuplicateKeyError path.
- Cache consumption in the orchestrator: hit short-circuits to `_deliver_text`;
  miss / stale fire_time / expired falls through to the live path.

No real DB or agent is required — `mongodb.db` and the orchestrator's
`_run_headless_turn` are patched with AsyncMocks.

Run from backend/: `pytest tests/test_prefetch.py`
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError

from services.prefetch import PrefetchCandidate, PrefetchService


# --- Helpers --------------------------------------------------------------


def _async_iter(items: list[dict]):
    """Async iterator mimicking Motor's find() cursor."""

    class _Cursor:
        def __init__(self, items):
            self._iter = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    return _Cursor(items)


def _mock_prefetch_db(
    *,
    find_and_update_result=None,
    insert_raises=None,
    find_one_and_delete_result=None,
):
    """Stand-in for `mongodb.db` covering the surfaces PrefetchService touches."""
    prefetched = MagicMock()
    prefetched.find_one_and_update = AsyncMock(return_value=find_and_update_result)
    prefetched.insert_one = (
        AsyncMock(side_effect=insert_raises) if insert_raises else AsyncMock()
    )
    prefetched.update_one = AsyncMock()
    prefetched.find_one_and_delete = AsyncMock(return_value=find_one_and_delete_result)

    db = MagicMock()
    db.prefetched_results = prefetched
    return db


def _make_service_with_orchestrator() -> PrefetchService:
    """PrefetchService with a stub orchestrator wired in (skips start())."""
    svc = PrefetchService(poll_interval_s=60, window_min=5)
    svc._orchestrator = MagicMock()
    return svc


# --- _scan_automations ----------------------------------------------------


class TestScanAutomations:
    @pytest.mark.asyncio
    async def test_only_announce_protocol_fires_selected(self):
        now = datetime.now(timezone.utc)
        fire_time = now + timedelta(minutes=2)
        fires = [
            {
                "source": "automation",
                "rule_id": "r1",
                "item_id": "i1",
                "owner_id": "u1",
                "protocol_name": "daily_digest",
                "fire_time": fire_time,
                "rule": {"action": {
                    "protocol": "daily_digest",
                    "decision": "tell",
                    "message": "hi {title}",
                }},
                "item": {"id": "i1", "title": "thing"},
            },
            {
                "source": "automation",
                "rule_id": "r2",
                "item_id": "i2",
                "owner_id": "u1",
                "protocol_name": "daily_digest",
                "fire_time": fire_time,
                "rule": {"action": {"protocol": "daily_digest", "decision": "act"}},
                "item": {"id": "i2"},
            },
        ]

        svc = _make_service_with_orchestrator()
        with patch("services.prefetch.automation_service") as mock_auto, patch(
            "services.prefetch.build_protocol_context",
            new=AsyncMock(return_value="\nPROTOCOL STEPS:\n1. summarize"),
        ), patch(
            "services.prefetch.is_protocol_prefetch_safe",
            new=AsyncMock(return_value=True),
        ):
            mock_auto.iter_upcoming_protocol_fires = MagicMock(return_value=fires)
            candidates = await svc._scan_automations(now, now + timedelta(minutes=5))

        assert [c.trigger_id for c in candidates] == ["r1:i1"]
        # Template was rendered against the item before being passed to the builder.
        assert "hi thing" in candidates[0].system_context


# --- _claim_slot atomicity ------------------------------------------------


def _candidate(now: datetime) -> PrefetchCandidate:
    return PrefetchCandidate(
        source="trigger",
        trigger_id="trg-1",
        protocol_name="morning_briefing",
        owner_id="u1",
        fire_time=now + timedelta(minutes=3),
        system_context="SYSTEM EVENT: ...",
    )


class TestClaimSlot:
    @pytest.mark.asyncio
    async def test_replaces_stale_or_failed_row_in_one_round_trip(self):
        """find_one_and_update succeeds → True without a follow-up insert."""
        now = datetime.now(timezone.utc)
        svc = PrefetchService(poll_interval_s=60, window_min=5)
        replaced = {"status": "running", "created_at": now - timedelta(seconds=600)}
        db = _mock_prefetch_db(find_and_update_result=replaced)

        with patch("services.prefetch.mongodb") as mock_mongo:
            mock_mongo.db = db
            claimed = await svc._claim_slot(_candidate(now), now)

        assert claimed is True
        db.prefetched_results.find_one_and_update.assert_awaited_once()
        db.prefetched_results.insert_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_row_inserts_fresh_running(self):
        now = datetime.now(timezone.utc)
        svc = PrefetchService(poll_interval_s=60, window_min=5)
        db = _mock_prefetch_db(find_and_update_result=None)

        with patch("services.prefetch.mongodb") as mock_mongo:
            mock_mongo.db = db
            claimed = await svc._claim_slot(_candidate(now), now)

        assert claimed is True
        inserted = db.prefetched_results.insert_one.await_args.args[0]
        assert inserted["status"] == "running"
        assert inserted["source"] == "trigger"
        assert inserted["trigger_id"] == "trg-1"
        assert inserted["expires_at"] == _candidate(now).fire_time + timedelta(minutes=5)

    @pytest.mark.asyncio
    async def test_fresh_ready_or_running_collapses_via_duplicate_key(self):
        """Unique-index race: insert raises DuplicateKeyError → return False."""
        now = datetime.now(timezone.utc)
        svc = PrefetchService(poll_interval_s=60, window_min=5)
        db = _mock_prefetch_db(
            find_and_update_result=None,
            insert_raises=DuplicateKeyError("duplicate"),
        )

        with patch("services.prefetch.mongodb") as mock_mongo:
            mock_mongo.db = db
            claimed = await svc._claim_slot(_candidate(now), now)

        assert claimed is False

    @pytest.mark.asyncio
    async def test_filter_targets_stale_or_failed_only(self):
        """Filter must NOT match fresh ready/running rows."""
        now = datetime.now(timezone.utc)
        svc = PrefetchService(poll_interval_s=60, window_min=5)
        db = _mock_prefetch_db(find_and_update_result=None)

        with patch("services.prefetch.mongodb") as mock_mongo:
            mock_mongo.db = db
            await svc._claim_slot(_candidate(now), now)

        filter_arg = db.prefetched_results.find_one_and_update.await_args.args[0]
        statuses = {clause.get("status") for clause in filter_arg["$or"]}
        assert statuses == {"failed", "running"}
        # The "running" branch must require a stale `created_at`.
        running_clause = next(c for c in filter_arg["$or"] if c.get("status") == "running")
        assert "created_at" in running_clause
        assert "$lt" in running_clause["created_at"]


# --- Orchestrator cache consumption --------------------------------------


def _make_orchestrator():
    """Minimal orchestrator instance for exercising _try_prefetched_delivery."""
    from core.turns.orchestrator import AssistantOrchestrator

    return AssistantOrchestrator.__new__(AssistantOrchestrator)


def _make_session():
    return SimpleNamespace(current_run_task=None)


class TestOrchestratorCacheConsult:
    @pytest.mark.asyncio
    async def test_current_run_task_clears_when_tracked_task_finishes(self):
        orch = _make_orchestrator()
        session = _make_session()

        async def runner():
            return None

        task = orch._set_current_run_task(session, asyncio.create_task(runner()))
        assert session.current_run_task is task

        await task
        await asyncio.sleep(0)

        assert session.current_run_task is None

    @pytest.mark.asyncio
    async def test_hit_short_circuits_to_deliver_text(self):
        orch = _make_orchestrator()
        session = _make_session()
        now = datetime.now(timezone.utc)
        trigger_data = {"owner_id": "u1", "instance_id": "trg-1", "trigger_time": now}

        db = _mock_prefetch_db(find_one_and_delete_result={
            "status": "ready",
            "text": "Good morning. Your first meeting is at 9.",
            "expires_at": now + timedelta(minutes=5),
            "fire_time": now,
        })

        with patch("core.turns.orchestrator.mongodb") as mock_mongo, patch(
            "core.turns.orchestrator.perf"
        ) as mock_perf:
            mock_mongo.db = db
            orch._schedule_runner = MagicMock(return_value=MagicMock())
            ok = await orch._try_prefetched_delivery(
                session=session,
                trigger_data=trigger_data,
                sound="chime",
                protocol_name="morning_briefing",
                triggered_by="scheduler",
                turn_id="turn-test001",
            )

        assert ok is True
        db.prefetched_results.find_one_and_delete.assert_awaited_once()
        mock_perf.start.assert_called_once_with(
            "turn_latency", "u1",
            turn_id="turn-test001", source="system", scenario="prefetched",
            owner_id="u1", connection_id="u1",
        )
        orch._schedule_runner.assert_called_once()

    @pytest.mark.asyncio
    async def test_miss_falls_through(self):
        orch = _make_orchestrator()
        session = _make_session()
        db = _mock_prefetch_db(find_one_and_delete_result=None)

        with patch("core.turns.orchestrator.mongodb") as mock_mongo:
            mock_mongo.db = db
            ok = await orch._try_prefetched_delivery(
                session=session,
                trigger_data={"owner_id": "u1", "instance_id": "trg-1"},
                sound="chime",
                protocol_name="morning_briefing",
                triggered_by="scheduler",
                turn_id="turn-test002",
            )

        assert ok is False

    @pytest.mark.asyncio
    async def test_stale_fire_time_rejects_cache(self):
        orch = _make_orchestrator()
        session = _make_session()
        now = datetime.now(timezone.utc)
        db = _mock_prefetch_db(find_one_and_delete_result={
            "status": "ready",
            "text": "stale briefing",
            "expires_at": now + timedelta(minutes=5),
            "fire_time": now + timedelta(minutes=10),
        })

        with patch("core.turns.orchestrator.mongodb") as mock_mongo:
            mock_mongo.db = db
            ok = await orch._try_prefetched_delivery(
                session=session,
                trigger_data={"owner_id": "u1", "instance_id": "trg-1", "trigger_time": now},
                sound="chime",
                protocol_name="morning_briefing",
                triggered_by="scheduler",
                turn_id="turn-test003",
            )

        assert ok is False

    @pytest.mark.asyncio
    async def test_expired_row_rejected(self):
        orch = _make_orchestrator()
        session = _make_session()
        now = datetime.now(timezone.utc)
        db = _mock_prefetch_db(find_one_and_delete_result={
            "status": "ready",
            "text": "expired",
            "expires_at": now - timedelta(seconds=1),
            "fire_time": now,
        })

        with patch("core.turns.orchestrator.mongodb") as mock_mongo:
            mock_mongo.db = db
            ok = await orch._try_prefetched_delivery(
                session=session,
                trigger_data={"owner_id": "u1", "instance_id": "trg-1", "trigger_time": now},
                sound="chime",
                protocol_name="morning_briefing",
                triggered_by="scheduler",
                turn_id="turn-test004",
            )

        assert ok is False

    @pytest.mark.asyncio
    async def test_automation_trigger_id_reconstructed_from_rule_and_item(self):
        orch = _make_orchestrator()
        session = _make_session()
        now = datetime.now(timezone.utc)
        trigger_data = {
            "owner_id": "u1", "rule_id": "r1", "item_id": "i1", "fire_time": now,
        }
        db = _mock_prefetch_db(find_one_and_delete_result={
            "status": "ready",
            "text": "auto briefing",
            "expires_at": now + timedelta(minutes=5),
            "fire_time": now,
        })

        with patch("core.turns.orchestrator.mongodb") as mock_mongo, patch(
            "core.turns.orchestrator.perf"
        ):
            mock_mongo.db = db
            orch._schedule_runner = MagicMock(return_value=MagicMock())
            ok = await orch._try_prefetched_delivery(
                session=session,
                trigger_data=trigger_data,
                sound="chime",
                protocol_name="daily_digest",
                triggered_by="automation",
                turn_id="turn-test005",
            )

        assert ok is True
        key = db.prefetched_results.find_one_and_delete.await_args.args[0]
        assert key == {
            "source": "automation",
            "trigger_id": "r1:i1",
            "protocol_name": "daily_digest",
        }