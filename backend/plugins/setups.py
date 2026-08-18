"""User-shaped setup control plane tools."""

from __future__ import annotations

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
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _format_candidates(rows: list[ManagedSetup]) -> CapabilityErrorDetail:
    candidates = ", ".join(
        (
            f"{row.name} ({row.resource_ref}; {row.status}; "
            f"{row.trigger_label}; managed by {row.managed_by})"
        )
        for row in rows[:8]
    )
    return _fail(f"Ambiguous setup. Retry with one resource_ref: {candidates}")


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
        ],
    )

    @tool
    async def find(
        self,
        query: str | None = None,
        status: SetupStatus | None = None,
        setup_type: SetupType | None = None,
        limit: int = 20,
    ) -> list[ManagedSetup]:
        """
        Find configured behavior by name or keywords across schedules, automations,
        habit check-ins, quiet windows, protocols, and pending one-off scheduled work.
        Results include the managing domain, supported actions, exact downstream IDs,
        and edit_tool. Use resource_ref for setups pause, resume, or delete.
        """
        limit = max(1, min(limit, 50))
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        return (await find_managed_setups(
            owner_id,
            query=query,
            status=status,
            setup_type=setup_type,
        ))[:limit]

    @tool
    async def get(self, target: str) -> ManagedSetup | CapabilityErrorDetail:
        """
        Resolve one configured item by resource_ref or natural query.
        Use before pause, resume, or delete when the user names one thing.
        """
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        resolved = await resolve_managed_setup(owner_id, target)
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
        Pause one configured setup until resumed. Accepts resource_ref or natural query.
        For upcoming reminders/alarms without a durable definition, use scheduler tools.
        """
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        if isinstance(resolved, list):
            return _format_candidates(resolved)
        paused_until = None
        if until:
            from core.scheduling import parse_schedule_time
            from core.context import get_timezone

            try:
                paused_until = parse_schedule_time(until, timezone_name=get_timezone())
            except ValueError as exc:
                return _fail(f"Could not parse until={until!r}. {exc}")
        try:
            return await pause_managed_setup(owner_id, resolved, until=paused_until)
        except ValueError as exc:
            return _fail(f"{exc}")

    @tool
    async def resume(self, target: str) -> str | CapabilityErrorDetail:
        """Resume one paused setup. Accepts resource_ref or natural query."""
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        if isinstance(resolved, list):
            return _format_candidates(resolved)
        try:
            return await resume_managed_setup(owner_id, resolved)
        except ValueError as exc:
            return _fail(f"{exc}")

    @tool
    async def delete(self, target: str) -> str | CapabilityErrorDetail:
        """
        Permanently delete one configured setup. Resolve first; performs no write when ambiguous.
        """
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        resolved = await resolve_managed_setup(owner_id, target)
        if resolved is None:
            return _fail(f"No setup found for {target!r}.")
        if isinstance(resolved, list):
            return _format_candidates(resolved)
        try:
            return await delete_with_consent(owner_id, resolved)
        except ValueError as exc:
            return _fail(f"{exc}")
