"""Tests for quiet windows, effective-attention derivation, and reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.attention.models import AttentionState, ManualOverride, QuietWindow
from core.attention.resolver import (
    EffectiveAttention,
    ScheduledAttentionResolution,
    resolve_effective_attention,
    resolve_scheduled_attention,
)
from core.attention.service import AttentionService


def _window(
    *,
    window_id: str = "sched-1",
    start: str = "22:00",
    end: str = "07:00",
    tz: str = "UTC",
    days: list[str] | None = None,
) -> QuietWindow:
    return QuietWindow(
        id=window_id,
        owner_id="owner-1",
        name="Night",
        start_time=start,
        end_time=end,
        timezone=tz,
        days=days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    )


class TestScheduledAttentionResolver:
    def test_cross_midnight_window_active_after_start(self):
        now = datetime(2026, 5, 28, 23, 0, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(now, [_window()])
        assert resolution.mode == "quiet"
        assert resolution.effective_until == datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc)

    def test_cross_midnight_window_active_before_end(self):
        now = datetime(2026, 5, 29, 6, 30, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(now, [_window()])
        assert resolution.mode == "quiet"
        assert resolution.effective_until == datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc)

    def test_cross_midnight_window_inactive_midday(self):
        now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(now, [_window()])
        assert resolution.mode is None

    def test_same_day_window(self):
        now = datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(now, [_window(start="09:00", end="17:00")])
        assert resolution.mode == "quiet"
        assert resolution.effective_until == datetime(2026, 5, 28, 17, 0, tzinfo=timezone.utc)

    def test_weekday_start_spills_into_next_morning(self):
        window = _window(days=["fri"])
        friday_night = datetime(2026, 5, 29, 23, 0, tzinfo=timezone.utc)
        saturday_morning = datetime(2026, 5, 30, 6, 0, tzinfo=timezone.utc)
        assert resolve_scheduled_attention(friday_night, [window]).mode == "quiet"
        assert resolve_scheduled_attention(saturday_morning, [window]).mode == "quiet"

    def test_overlapping_windows_coalesce_to_latest_end(self):
        now = datetime(2026, 5, 29, 6, 30, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(
            now,
            [
                _window(window_id="a", start="22:00", end="07:00"),
                _window(window_id="b", start="06:00", end="09:00"),
            ],
        )
        assert resolution.mode == "quiet"
        assert set(resolution.active_schedule_ids) == {"a", "b"}
        assert resolution.effective_until == datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc)

    def test_future_overlap_extends_current_effective_window(self):
        now = datetime(2026, 5, 28, 23, 30, tzinfo=timezone.utc)
        resolution = resolve_scheduled_attention(
            now,
            [
                _window(window_id="a", start="22:00", end="07:00"),
                _window(window_id="b", start="06:00", end="09:00"),
            ],
        )
        assert resolution.mode == "quiet"
        assert set(resolution.active_schedule_ids) == {"a", "b"}
        assert resolution.effective_until == datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc)

    def test_timezone_wall_clock(self):
        now = datetime(2026, 5, 28, 3, 0, tzinfo=timezone.utc)  # 23:00 EDT prior day
        resolution = resolve_scheduled_attention(
            now, [_window(start="22:00", end="07:00", tz="America/New_York")]
        )
        assert resolution.mode == "quiet"

    def test_spring_forward_nonexistent_start_shifts_forward(self):
        window = _window(start="02:30", end="05:00", tz="America/New_York", days=["sun"])
        now = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)  # 03:30 EDT
        assert resolve_scheduled_attention(now, [window]).mode == "quiet"

    def test_fall_back_ambiguous_start_is_consistent(self):
        window = _window(start="01:30", end="03:30", tz="America/New_York", days=["sun"])
        first = datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)  # 01:45 EDT
        second = datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc)  # 01:45 EST
        assert resolve_scheduled_attention(first, [window]).mode == "quiet"
        assert resolve_scheduled_attention(second, [window]).mode == "quiet"


class TestEffectiveAttention:
    """The pure combiner: manual override vs. quiet windows."""

    def test_no_override_no_window_is_active(self):
        now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
        eff = resolve_effective_attention(now, None, [])
        assert eff == EffectiveAttention("active", "default", None, ())

    def test_window_applies_when_no_override(self):
        now = datetime(2026, 5, 28, 23, 0, tzinfo=timezone.utc)
        eff = resolve_effective_attention(now, None, [_window()])
        assert eff.mode == "quiet"
        assert eff.source == "schedule"
        assert eff.active_window_ids == ("sched-1",)
        assert eff.expires_at == datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc)

    def test_live_override_beats_window(self):
        now = datetime(2026, 5, 28, 23, 0, tzinfo=timezone.utc)
        override = ManualOverride(mode="paused", source="tool", set_at=now)
        eff = resolve_effective_attention(now, override, [_window()])
        assert eff.mode == "paused"
        assert eff.source == "tool"
        assert eff.active_window_ids == ()

    def test_manual_active_suppresses_window_until_boundary(self):
        now = datetime(2026, 5, 28, 23, 0, tzinfo=timezone.utc)
        boundary = datetime(2026, 5, 29, 7, 0, tzinfo=timezone.utc)
        override = ManualOverride(mode="active", source="local_command", set_at=now, expires_at=boundary)
        eff = resolve_effective_attention(now, override, [_window()])
        assert eff.mode == "active"
        assert eff.expires_at == boundary

    def test_expired_override_falls_through_to_window(self):
        now = datetime(2026, 5, 29, 6, 30, tzinfo=timezone.utc)
        expired = ManualOverride(
            mode="active",
            source="tool",
            set_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=1),
        )
        eff = resolve_effective_attention(now, expired, [_window()])
        assert eff.mode == "quiet"
        assert eff.source == "schedule"


class TestAttentionServiceDerivation:
    def _mock_db(self, *, state_doc: dict | None = None, windows: list[dict] | None = None):
        state_col = AsyncMock()
        state_col.find_one = AsyncMock(return_value=state_doc)
        state_col.update_one = AsyncMock()

        schedule_col = AsyncMock()

        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            def sort(self, *_a, **_k):
                return self

            async def to_list(self, _limit):
                return self._docs

        schedule_col.find = MagicMock(return_value=_Cursor(windows or []))
        schedule_col.update_one = AsyncMock()
        schedule_col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))

        mock_mongodb = AsyncMock()
        mock_mongodb.db = MagicMock()

        def getitem(name):
            if name == "attention_state":
                return state_col
            if name == "attention_schedules":
                return schedule_col
            raise KeyError(name)

        mock_mongodb.db.__getitem__ = MagicMock(side_effect=getitem)
        return mock_mongodb, state_col, schedule_col

    @pytest.mark.asyncio
    async def test_get_state_derives_quiet_from_window(self):
        svc = AttentionService()
        mock_mongodb, state_col, _ = self._mock_db(state_doc=None, windows=[_window().model_dump(mode="json")])
        eff = EffectiveAttention("quiet", "schedule", None, ("sched-1",))
        with patch("core.attention.service.mongodb", mock_mongodb), \
             patch("core.attention.service.resolve_effective_attention", return_value=eff):
            state = await svc.get_state("owner-1")
        assert state.mode == "quiet"
        assert state.source == "schedule"
        # Reads never write.
        state_col.update_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_publishes_on_change(self):
        svc = AttentionService()
        mock_mongodb, state_col, _ = self._mock_db(
            state_doc={"owner_id": "owner-1", "published_mode": "active"},
            windows=[_window().model_dump(mode="json")],
        )
        eff = EffectiveAttention("quiet", "schedule", None, ("sched-1",))
        with patch("core.attention.service.mongodb", mock_mongodb), \
             patch("core.attention.service.event_bus.publish", AsyncMock()) as publish, \
             patch("core.attention.service.resolve_effective_attention", return_value=eff):
            result = await svc.reconcile_owner("owner-1")
        assert result.mode == "quiet"
        state_col.update_one.assert_awaited()
        publish.assert_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_noop_when_published_matches(self):
        svc = AttentionService()
        mock_mongodb, state_col, _ = self._mock_db(
            state_doc={"owner_id": "owner-1", "published_mode": "quiet"},
            windows=[_window().model_dump(mode="json")],
        )
        eff = EffectiveAttention("quiet", "schedule", None, ("sched-1",))
        with patch("core.attention.service.mongodb", mock_mongodb), \
             patch("core.attention.service.event_bus.publish", AsyncMock()) as publish, \
             patch("core.attention.service.resolve_effective_attention", return_value=eff):
            result = await svc.reconcile_owner("owner-1")
        assert result.mode == "quiet"
        state_col.update_one.assert_not_awaited()
        publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_mode_active_during_window_bounds_override(self):
        svc = AttentionService()
        boundary = datetime.now(timezone.utc) + timedelta(hours=2)
        mock_mongodb, state_col, _ = self._mock_db(windows=[_window().model_dump(mode="json")])
        scheduled = ScheduledAttentionResolution(
            mode="quiet", active_schedule_ids=("sched-1",), effective_until=boundary
        )
        with patch("core.attention.service.mongodb", mock_mongodb), \
             patch("core.attention.service.event_bus.publish", AsyncMock()), \
             patch("core.attention.service.resolve_scheduled_attention", return_value=scheduled):
            state = await svc.set_mode("owner-1", "active", source="local_command")
        assert state.mode == "active"
        assert state.expires_at == boundary
        assert state.source == "local_command"
        assert state.active_window_ids == ()
        persisted = state_col.update_one.await_args.args[1]["$set"]
        assert persisted["override"]["mode"] == "active"
        assert persisted["override"]["source"] == "local_command"

    @pytest.mark.asyncio
    async def test_set_mode_active_without_window_clears_override(self):
        svc = AttentionService()
        mock_mongodb, state_col, _ = self._mock_db(windows=[])
        with patch("core.attention.service.mongodb", mock_mongodb), \
             patch("core.attention.service.event_bus.publish", AsyncMock()):
            state = await svc.set_mode("owner-1", "active", source="tool")
        assert state.mode == "active"
        assert state.source == "default"
        persisted = state_col.update_one.await_args.args[1]["$set"]
        assert persisted["override"] is None


class TestAttentionPluginQuietWindows:
    @pytest.mark.asyncio
    async def test_set_quiet_window_persists_and_reconciles(self, tool_context):
        from plugins.attention import AttentionPlugin

        with patch("plugins.attention.get_tz", return_value="UTC"), \
             patch("plugins.attention.attention_service") as mock_service:
            mock_service.list_quiet_windows = AsyncMock(return_value=[])
            mock_service.upsert_quiet_window = AsyncMock(
                return_value=QuietWindow(
                    id="sched-1",
                    owner_id="owner-1",
                    name="Quiet 22:00-07:00",
                    start_time="22:00",
                    end_time="07:00",
                )
            )
            mock_service.get_state = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="quiet", source="schedule")
            )
            result = await AttentionPlugin().set_quiet_window("22:00", "07:00")

        mock_service.upsert_quiet_window.assert_awaited_once()
        assert "Quiet window set" in result

    @pytest.mark.asyncio
    async def test_set_quiet_window_replaces_existing_name(self, tool_context):
        from plugins.attention import AttentionPlugin

        existing = QuietWindow(
            id="sched-existing",
            owner_id="owner-1",
            name="Night",
            start_time="21:00",
            end_time="06:00",
        )
        with patch("plugins.attention.get_tz", return_value="UTC"), \
             patch("plugins.attention.attention_service") as mock_service:
            mock_service.list_quiet_windows = AsyncMock(return_value=[existing])
            mock_service.upsert_quiet_window = AsyncMock(return_value=existing)
            mock_service.get_state = AsyncMock(return_value=AttentionState(owner_id="owner-1"))
            result = await AttentionPlugin().set_quiet_window("22:00", "07:00", name="Night")

        saved = mock_service.upsert_quiet_window.await_args.args[0]
        assert saved.id == "sched-existing"
        assert saved.start_time == "22:00"
        assert saved.end_time == "07:00"
        assert "Quiet window set" in result

    @pytest.mark.asyncio
    async def test_set_quiet_window_accepts_day_range_and_clock_seconds(self, tool_context):
        from plugins.attention import AttentionPlugin

        with patch("plugins.attention.get_tz", return_value="UTC"), \
             patch("plugins.attention.attention_service") as mock_service:
            mock_service.list_quiet_windows = AsyncMock(return_value=[])
            mock_service.upsert_quiet_window = AsyncMock()
            mock_service.get_state = AsyncMock(return_value=AttentionState(owner_id="owner-1"))
            await AttentionPlugin().set_quiet_window("21:30:00", "07:00:00", days="Mon-Fri")

        saved = mock_service.upsert_quiet_window.await_args.args[0]
        assert saved.start_time == "21:30"
        assert saved.end_time == "07:00"
        assert saved.days == ["mon", "tue", "wed", "thu", "fri"]

    @pytest.mark.asyncio
    async def test_clear_quiet_window_deletes(self, tool_context):
        from plugins.attention import AttentionPlugin

        window = QuietWindow(
            id="sched-1",
            owner_id="owner-1",
            name="Night",
            start_time="22:00",
            end_time="07:00",
        )
        with patch("plugins.attention.attention_service") as mock_service:
            mock_service.list_quiet_windows = AsyncMock(return_value=[window])
            mock_service.delete_quiet_window = AsyncMock(return_value=True)
            mock_service.get_state = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="active")
            )
            result = await AttentionPlugin().clear_quiet_window("Night")

        mock_service.delete_quiet_window.assert_awaited_once_with("owner-1", "sched-1")
        assert "Removed quiet window" in result
