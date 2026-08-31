"""User-shaped setup control plane tools."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.config import settings
from core.context import get_owner_id
from core.decorators import tool
from core.operations.lifecycle_dispatch import (
    delete_with_consent,
    pause_managed_setup,
    resume_managed_setup,
)
from core.operations.projection import (
    ManagedSetup,
    SetupType,
    find_managed_setups,
    resolve_managed_setup,
)
from core.operations.definitions import SetupStatus
from core.plugins.read_evidence import MatchStatus, ReadCoverage, match_status_from_count
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _owner_id() -> str:
    try:
        return get_owner_id()
    except RuntimeError:
        return settings.DEFAULT_USER_ID


def _automation_hold(
    setups: list[ManagedSetup],
    setup_type: SetupType | None,
) -> str | None:
    about_automations = setup_type == "automation" or any(
        row.managed_by == "automations" for row in setups
    )
    if not about_automations:
        return None
    from datetime import datetime, timezone

    from services.automation import automation_service

    return automation_service.pause_observation(datetime.now(timezone.utc))


def _format_candidates(rows: list[ManagedSetup]) -> CapabilityErrorDetail:
    candidates = ", ".join(
        (
            f"{row.name} ({row.resource_ref}; {row.status}; "
            f"{row.trigger_label}; managed by {row.managed_by})"
        )
        for row in rows[:8]
    )
    return _fail(f"Ambiguous setup. Retry with one resource_ref: {candidates}")


def _lifecycle_rows(
    resolved: ManagedSetup | list[ManagedSetup],
    action: str,
) -> list[ManagedSetup] | CapabilityErrorDetail:
    rows = resolved if isinstance(resolved, list) else [resolved]
    unsupported = [row for row in rows if action not in row.supported_actions]
    if unsupported:
        detail = ", ".join(
            f"{row.name} (managed by {row.managed_by})" for row in unsupported
        )
        return _fail(
            f"Cannot {action}: {detail}. "
            "Retry with a resource_ref for a setup that supports that action."
        )
    if len(rows) > 50:
        return _fail(
            f"Matched {len(rows)} setups. Narrow the name or use a resource_ref."
        )
    return rows


class SetupQueryResult(BaseModel):
    setups: list[ManagedSetup] = Field(default_factory=list)
    match_status: MatchStatus
    coverage: ReadCoverage
    hold: str | None = None


class SetupsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="setups",
        version="1.0.0",
        description=(
            "Find and manage configured behavior the user set up: schedules, automations, "
            "habit check-ins, quiet windows, and saved routines."
        ),
        utterances=[
            "what did I set up",
            "what have you set up for me",
            "find the morning lights thing",
            "show my configured routines",
            "pause that routine",
            "turn off that automation for now",
            "delete the old disabled one",
            "remove that scheduled setup",
            "what proactive things are configured",
            "find my bedtime lights schedule",
            "why aren't my automations working",
            "what automations do I have",
            "do I already have a meeting automation",
            "inspect my existing automations",
        ],
    )

    @tool
    async def find(
        self,
        query: str | None = None,
        status: SetupStatus | None = None,
        setup_type: SetupType | None = None,
        limit: int = 20,
    ) -> SetupQueryResult:
        """
        Find configured behavior by name or keywords across schedules, automations,
        habit check-ins, quiet windows, protocols, and pending one-off scheduled work.
        Use this to inspect existing rules and automations; do not use
        automations.list_available_triggers or search.web for that inventory.
        Results include the managing domain, supported actions, exact downstream IDs,
        and edit_tool. Named edit_tool capabilities are offered on the next iteration.
        Pause and resume accept the same query; use resource_ref for delete.
        """
        limit = max(1, min(limit, 50))
        owner_id = _owner_id()
        rows = await find_managed_setups(
            owner_id,
            query=query,
            status=status,
            setup_type=setup_type,
        )
        truncated = len(rows) > limit
        setups = rows[:limit]
        return SetupQueryResult(
            setups=setups,
            match_status=match_status_from_count(len(setups)),
            coverage=ReadCoverage.PARTIAL if truncated else ReadCoverage.COMPLETE,
            hold=_automation_hold(setups, setup_type),
        )

    @tool
    async def get(self, target: str) -> ManagedSetup | CapabilityErrorDetail:
        """
        Resolve one configured item by resource_ref or natural query.
        Use before delete when the user names one thing. Pause and resume
        accept the same target directly.
        """
        resolved = await resolve_managed_setup(_owner_id(), target)
        if resolved is None:
            return _fail(
                f"No setup found for {target!r}. "
                "Try setups.find(query=...) with a shorter name or keyword."
            )
        if isinstance(resolved, list):
            return _format_candidates(resolved)
        return resolved

    @tool
    async def pause(self, target: str, until: str | None = None) -> str | CapabilityErrorDetail:
        """
        Pause matching configured setups until resumed. Accepts resource_ref or natural query.
        A query that matches several pause-capable setups pauses all of them.
        Omit until for indefinite; status stays paused, not disabled.
        For upcoming reminders/alarms without a durable definition, use scheduler tools.
        """
        owner_id = _owner_id()
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        rows = _lifecycle_rows(resolved, "pause")
        if isinstance(rows, CapabilityErrorDetail):
            return rows
        paused_until = None
        if until:
            from core.scheduling import parse_schedule_time
            from core.context import get_timezone

            try:
                paused_until = parse_schedule_time(until, timezone_name=get_timezone())
            except ValueError as exc:
                return _fail(f"Could not parse until={until!r}. {exc}")
        try:
            messages = [
                await pause_managed_setup(owner_id, row, until=paused_until)
                for row in rows
            ]
        except ValueError as exc:
            return _fail(f"{exc}")
        if len(messages) == 1:
            return messages[0]
        names = ", ".join(row.name for row in rows)
        return f"Paused {names}."

    @tool
    async def resume(self, target: str) -> str | CapabilityErrorDetail:
        """Resume matching paused setups. Accepts resource_ref or natural query."""
        owner_id = _owner_id()
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        rows = _lifecycle_rows(resolved, "resume")
        if isinstance(rows, CapabilityErrorDetail):
            return rows
        try:
            messages = [await resume_managed_setup(owner_id, row) for row in rows]
        except ValueError as exc:
            return _fail(f"{exc}")
        if len(messages) == 1:
            return messages[0]
        names = ", ".join(row.name for row in rows)
        return f"Resumed {names}."

    @tool
    async def delete(self, target: str) -> str | CapabilityErrorDetail:
        """
        Permanently delete one configured setup. Resolve first; performs no write when ambiguous.
        """
        owner_id = _owner_id()
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        if isinstance(resolved, list):
            return _format_candidates(resolved)
        try:
            return await delete_with_consent(owner_id, resolved)
        except ValueError as exc:
            return _fail(f"{exc}")
