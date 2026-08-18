"""Activity feed tools — operational visibility for headless and background work."""

from typing import Literal

from core.activity import ActivityItem, recent_activity
from core.config import settings
from core.context import get_owner_id
from core.decorators import tool
from core.operations.definitions import SetupExplain, explain_setup
from core.operations.projection import resolve_managed_setup
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


class ActivityPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="activity",
        version="1.0.0",
        description="Inspect recent background activity and explain why a configured setup last fired.",
        utterances=[
            "why did that fire",
            "why did you tell me that",
            "what have you been doing",
            "what did you do while I was away",
            "did anything run while I was out",
            "have any automations failed",
            "what did you do quietly",
            "anything waiting to tell me",
            "show me recent background activity",
            "what happened in the background",
        ],
    )

    @tool
    async def recent(
        self,
        kind: Literal["headless", "task", "trigger", "automation"] | None = None,
        limit: int = 20,
    ) -> list[ActivityItem]:
        """
        Recent operational activity: silent/suppressed turns, automation failures,
        triggers awaiting delivery, and background task outcomes.

        limit must be 1-200. Use small limits for voice summaries and larger
        limits only when auditing history.

        Rows are pointers — use follow-up tools to act:
        kind=headless -> recent silent/evaluate turn audit rows; id is the turn_id audit key;
        kind=task -> jarvis.agents.get_task(id) or resume(id);
        kind=automation -> setups.find(query=...) / jarvis.automations.update_rule;
        kind=trigger -> inspect awaiting/failed trigger instance state by id.
        For configured inventory or deletion, use setups.find/get/delete.
        """
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        return await recent_activity(owner_id, limit=limit, kind=kind)

    @tool
    async def why_last_fire(self, name_or_id: str) -> SetupExplain | CapabilityErrorDetail:
        """
        Explain the latest fire for a setup by resolving its latest TriggerInstance.
        Use when the user asks why Jarvis spoke, stayed quiet, suppressed, or failed.
        For finding the setup first, use setups.find/get.
        """
        try:
            owner_id = get_owner_id()
        except RuntimeError:
            owner_id = settings.DEFAULT_USER_ID
        resolved = await resolve_managed_setup(owner_id, name_or_id)
        if resolved is None:
            return _fail(
                f"No setup found for {name_or_id!r}. "
                "Use setups.find(query=...) first, or scheduler.get_alerts for one-off upcoming work."
            )
        if isinstance(resolved, list):
            candidates = ", ".join(
                f"{item.name} ({item.resource_ref}; {item.trigger_label})"
                for item in resolved[:8]
            )
            return _fail(f"Ambiguous setup {name_or_id!r}. Candidates: {candidates}")
        rule_id = resolved.rule_id or resolved.series_id
        if not rule_id:
            return _fail(
                f"{resolved.name!r} has no trigger-rule history to explain. "
                "Use activity.recent to inspect recent runs."
            )
        result = await explain_setup(owner_id, f"rule:{rule_id}")
        if result is None or isinstance(result, list):
            return _fail(f"No run history found for {resolved.name!r}.")
        return result
