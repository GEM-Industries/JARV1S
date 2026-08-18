"""Attention plugin — user-facing attention mode controls.

Tools mount as ``jarvis.attention.*``. Local voice commands fast-track
``set_mode_for_identity()`` and manage session-local mute without going
through the LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from core.attention.models import AttentionMode, AttentionState, QuietWindow
from core.attention.service import attention_service, new_window_id
from core.context import get_ctx, get_owner_id, get_tz
from core.decorators import tool
from core.plugins.result import ToolResult
from core.plugins.ui import receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope
from core.time import parse_duration
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


logger = logging.getLogger(__name__)


class AttentionPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="attention",
        version="1.0.0",
        description="Control proactive interruption policy and explicit session mute state.",
        utterances=[
            "mute",
            "mute yourself",
            "go quiet",
            "do not disturb",
            "stop notifications",
            "pause all Jarvis notifications",
            "wake up",
            "resume notifications",
            "unmute",
            "quiet hours",
            "do not disturb at night",
            "set quiet hours",
            "make me quiet every weekday night",
            "list my quiet windows",
            "clear my quiet hours",
        ],
    )

    @tool
    async def set_mode(
        self,
        mode: Literal["active", "quiet", "paused"],
        duration_minutes: int | str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """Set proactive notification policy only; does not soft-mute the mic.

        Use quiet for DND ("go quiet", "do not disturb"). For mute or stop listening, call mute().

        Args:
            mode: "active", "quiet" (urgent/critical still break through), or "paused".
            duration_minutes: Optional auto-resume duration in minutes or a string like "30m" or "1h".
        """
        owner_id = get_owner_id()
        try:
            duration_mins = _coerce_duration_minutes(duration_minutes)
        except ValueError as exc:
            return _fail(str(exc))
        state = await attention_service.set_mode(
            owner_id, mode, duration_minutes=duration_mins, source="tool"
        )
        if state.mode == "active":
            await _resume_live_session()
        content = _mode_confirmation(state)
        return ToolResult(content=content, ui=[_mode_receipt(state, content)])

    @tool
    async def mute(self, duration_minutes: int | str | None = None) -> ToolResult | CapabilityErrorDetail:
        """Soft-mute this node (stop listening until unmute) and set attention quiet.

        Use for mute / stop listening / stop responding — not set_mode("quiet").

        Args:
            duration_minutes: Optional auto-unmute duration in minutes or a string like "30m" or "1h".
        """
        owner_id = get_owner_id()
        try:
            duration_mins = _coerce_duration_minutes(duration_minutes)
        except ValueError as exc:
            return _fail(str(exc))
        state = await attention_service.set_mode(
            owner_id, "quiet", duration_minutes=duration_mins, source="tool"
        )
        muted = await _soft_mute_live_session(owner_id=owner_id, duration_minutes=duration_mins)
        content = (
            "Muted — not listening until you unmute."
            if muted
            else _mode_confirmation(state)
        )
        return ToolResult(content=content, ui=[_mode_receipt(state, content, label="Muted")])

    @tool
    async def get_mode(self) -> str:
        """Return the current attention mode and when it expires (if timed)."""
        owner_id = get_owner_id()
        state = await attention_service.get_state(owner_id)
        msg = f"Attention mode is **{state.mode}**."
        if state.expires_at:
            msg += f" Expires at {state.expires_at.strftime('%H:%M')} UTC."
        return msg

    @tool
    async def resume(self) -> ToolResult:
        """Resume notifications and clear soft mute — use for wake up, resume, or unmute."""
        owner_id = get_owner_id()
        state = await attention_service.set_mode(owner_id, "active", source="tool")
        await _resume_live_session()
        content = _mode_confirmation(state)
        return ToolResult(content=content, ui=[_mode_receipt(state, content)])

    @tool
    async def set_quiet_window(
        self,
        start_time: str,
        end_time: str,
        days: str | None = None,
        timezone_name: str | None = None,
        name: str | None = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """Create or replace a recurring quiet-hours window.
        Use for recurring DND requests like quiet hours, overnight quiet, or weekday quiet. Times are local wall-clock values; days can be "weekdays", "weekends", or comma-separated day names.
        """
        owner_id = get_owner_id()
        try:
            tz_name = timezone_name or get_tz()
            start_hhmm = _parse_window_time(start_time)
            end_hhmm = _parse_window_time(end_time)
            day_list = _parse_days(days)
        except ValueError as exc:
            return _fail(str(exc))

        schedule_name = name or f"Quiet {start_hhmm}-{end_hhmm}"
        existing = _match_schedule(await attention_service.list_quiet_windows(owner_id), schedule_name)
        schedule = QuietWindow(
            id=existing.id if existing else new_window_id(),
            owner_id=owner_id,
            name=schedule_name,
            start_time=start_hhmm,
            end_time=end_hhmm,
            timezone=tz_name,
            days=day_list,
            created_at=existing.created_at if existing else None,
        )
        await attention_service.upsert_quiet_window(schedule)
        state = await attention_service.get_state(owner_id)
        content = (
            f"Quiet window set for {start_hhmm}-{end_hhmm} {tz_name} "
            f"({', '.join(day_list)})."
        )
        return ToolResult(content=content, ui=[_mode_receipt(state, content, label="Quiet Window")])

    @tool
    async def list_quiet_windows(self) -> str:
        """List recurring quiet-hours windows."""
        owner_id = get_owner_id()
        schedules = await attention_service.list_quiet_windows(owner_id)
        if not schedules:
            return "No quiet windows configured."
        lines = []
        for schedule in schedules:
            status = "enabled" if schedule.enabled else "disabled"
            lines.append(
                f"- {schedule.name} ({schedule.id}): {schedule.start_time}-{schedule.end_time} "
                f"{schedule.timezone} [{', '.join(schedule.days)}] · {status}"
            )
        return "\n".join(lines)

    @tool
    async def clear_quiet_window(self, window_id_or_name: str) -> ToolResult | CapabilityErrorDetail:
        """Remove a recurring quiet-hours window by id or exact name."""
        owner_id = get_owner_id()
        schedules = await attention_service.list_quiet_windows(owner_id)
        target = _match_schedule(schedules, window_id_or_name)
        if not target:
            return _fail(f"Quiet window '{window_id_or_name}' not found.")
        deleted = await attention_service.delete_quiet_window(owner_id, target.id)
        if not deleted:
            return _fail(f"Could not delete quiet window '{target.name}'.")
        state = await attention_service.get_state(owner_id)
        content = f"Removed quiet window '{target.name}'."
        return ToolResult(content=content, ui=[_mode_receipt(state, content, label="Quiet Window")])

    # ------------------------------------------------------------------
    # Non-tool helper — called directly by local voice command handler
    # without going through the LLM or binding ToolRuntimeContext.
    # ------------------------------------------------------------------

    async def set_mode_for_identity(
        self,
        owner_id: str,
        node_id: str | None,
        mode: AttentionMode,
        duration_minutes: int | None = None,
        source: str = "local_command",
    ) -> AttentionState:
        """Fast-path setter for local commands. Does not require tool context."""
        return await attention_service.set_mode(
            owner_id, mode, duration_minutes=duration_minutes, source=source
        )

    async def get_mode_for_identity(self, owner_id: str) -> AttentionMode:
        """Fast-path getter for local commands. Does not require tool context."""
        return await attention_service.get_mode(owner_id)


def _mode_confirmation(state: AttentionState) -> str:
    labels = {
        "active": "I'm active and will notify you as usual.",
        "quiet": "Going quiet — I'll still break through for urgent timers and alarms.",
        "paused": "All proactive notifications paused until you resume.",
    }
    msg = labels.get(state.mode, f"Mode set to {state.mode}.")
    if state.expires_at:
        msg += f" Auto-resuming in {state.expires_at.strftime('%H:%M')} UTC."
    return msg


def _mode_receipt(
    state: AttentionState,
    content: str,
    *,
    label: str | None = None,
) -> UIEnvelope:
    mode_label = label or state.mode.title()
    line = f"{mode_label} · attention {state.mode}"
    return receipt_envelope("Attention", line, sublabel=content)


_TIME_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))$",
    re.IGNORECASE,
)
_DAY_ALIASES = {
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "mon-fri": ["mon", "tue", "wed", "thu", "fri"],
    "monday-friday": ["mon", "tue", "wed", "thu", "fri"],
    "weekends": ["sat", "sun"],
    "daily": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "every day": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
}
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _coerce_duration_minutes(value: int | str | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    now = datetime.now(timezone.utc)
    parsed = parse_duration(value, now=now)
    if parsed is None:
        raise ValueError(f"Invalid duration_minutes={value!r}. Use minutes or a duration like '30m' or '1h'.")
    seconds = max(1, int(round((parsed - now).total_seconds())))
    return max(1, (seconds + 59) // 60)


def _parse_window_time(value: str) -> str:
    normalized = value.strip().lower().replace(" ", "")
    meridiem_match = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", normalized)
    if meridiem_match:
        hour = int(meridiem_match.group(1))
        minute = int(meridiem_match.group(2) or 0)
        meridiem = meridiem_match.group(3)
        if minute > 59 or hour < 1 or hour > 12:
            raise ValueError(f"Invalid time '{value}'.")
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return f"{hour:02d}:{minute:02d}"

    clock_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", normalized)
    if clock_match:
        hour = int(clock_match.group(1))
        minute = int(clock_match.group(2))
        second = int(clock_match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError(f"Invalid time '{value}'.")
        return f"{hour:02d}:{minute:02d}"

    raise ValueError(f"Invalid time '{value}'. Use HH:MM or am/pm format.")


def _parse_days(value: str | None) -> list[str]:
    if not value:
        return _DAY_ALIASES["daily"]
    key = value.strip().lower()
    if key in _DAY_ALIASES:
        return _DAY_ALIASES[key]
    if "-" in key:
        start, end = [part.strip()[:3] for part in key.split("-", 1)]
        if start in _DAYS and end in _DAYS:
            start_idx = _DAYS.index(start)
            end_idx = _DAYS.index(end)
            if start_idx <= end_idx:
                return _DAYS[start_idx : end_idx + 1]
            return _DAYS[start_idx:] + _DAYS[: end_idx + 1]
    days = [part.strip().lower()[:3] for part in value.split(",") if part.strip()]
    valid = set(_DAYS)
    invalid = [day for day in days if day not in valid]
    if invalid:
        raise ValueError(f"Invalid day(s): {', '.join(invalid)}")
    return days


def _match_schedule(
    schedules: list[QuietWindow],
    window_id_or_name: str,
) -> QuietWindow | None:
    needle = window_id_or_name.strip().lower()
    for schedule in schedules:
        if schedule.id.lower() == needle or schedule.name.lower() == needle:
            return schedule
    return None


async def _apply_soft_mute() -> ToolResult:
    owner_id = get_owner_id()
    state = await attention_service.set_mode(owner_id, "quiet", source="tool")
    muted = await _soft_mute_live_session(owner_id=owner_id)
    content = (
        "Soft muted — I'll still break through for urgent timers and alarms."
        if muted
        else _mode_confirmation(state)
    )
    return ToolResult(content=content, ui=[_mode_receipt(state, content, label="Soft Muted")])


async def _get_live_session() -> Any | None:
    connection_id = get_ctx().get("connection_id")
    if not connection_id:
        return None

    from api.websockets.connection import manager

    return manager.get_session(connection_id)


async def _soft_mute_live_session(
    *,
    owner_id: str,
    duration_minutes: int | None = None,
) -> bool:
    """Apply the session-local soft mute used by local voice commands."""
    session = await _get_live_session()
    if not session:
        return False

    apply_soft_mute_for_session(
        session,
        reason="attention_tool.mute",
        owner_id=owner_id,
        duration_minutes=duration_minutes,
    )
    await _publish_session_state(session)
    return True


async def _resume_live_session() -> None:
    """Clear session-local soft mute for resume/active attention commands."""
    session = await _get_live_session()
    if not session:
        return

    clear_soft_mute_for_session(session)
    await _publish_session_state(session)


def apply_soft_mute_for_session(
    session: Any,
    *,
    reason: str,
    owner_id: str | None = None,
    duration_minutes: int | None = None,
) -> None:
    """Set session-local soft mute and optionally schedule a matching clear."""
    clear_soft_mute_for_session(session)
    session.soft_muted = True
    session.processor.force_passive(reason=reason)
    clear_preroll = getattr(session.processor, "clear_preroll", None)
    if clear_preroll is not None:
        clear_preroll()
    if owner_id and duration_minutes and duration_minutes > 0:
        session.soft_mute_resume_task = asyncio.create_task(
            _auto_resume_soft_mute(session, owner_id, duration_minutes * 60)
        )


def clear_soft_mute_for_session(session: Any) -> None:
    """Clear session-local soft mute and cancel any pending timed clear."""
    _cancel_soft_mute_resume(session)
    session.soft_muted = False


def _cancel_soft_mute_resume(session: Any) -> None:
    task = getattr(session, "soft_mute_resume_task", None)
    current = asyncio.current_task()
    if task and not task.done() and task is not current:
        task.cancel()
    session.soft_mute_resume_task = None


async def _auto_resume_soft_mute(session: Any, owner_id: str, delay_s: int) -> None:
    try:
        await asyncio.sleep(delay_s)
    except asyncio.CancelledError:
        return

    if getattr(session, "soft_mute_resume_task", None) is not asyncio.current_task():
        return

    clear_soft_mute_for_session(session)
    await _publish_session_state(session)
    try:
        await attention_service.get_state(owner_id)
    except Exception:
        logger.warning("attention expiry refresh failed for timed soft mute", exc_info=True)


async def _publish_session_state(session: Any) -> None:
    connection_id = getattr(session, "connection_id", None)
    if not connection_id:
        return

    from api.websockets.connection import manager, session_state_payload
    from api.websockets.types import WSMessageType

    await manager.send_voice_response(
        connection_id,
        WSMessageType.STATUS,
        {"session": session_state_payload(session)},
    )
