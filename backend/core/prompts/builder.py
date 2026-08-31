"""Build the JARV1S system prompt and dynamic turn context."""

import logging
import os
import platform as _platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any, Protocol

from core.context import allows_home_location_fallback, fresh_geo_position
from core.home import format_home_prompt, load_home_snapshot

logger = logging.getLogger(__name__)


@dataclass
class SystemPrompt:
    """Split prompt enabling LLM provider caching of the static prefix.

    Providers cache the longest matching prefix between requests. Product
    instructions are turn-invariant and live in ``static``. User-owned and
    per-turn facts live in
    ``dynamic``. Offered tool schemas are sent separately via ``tools=``.
    """

    static: str
    dynamic: str

    def __str__(self) -> str:
        parts = [self.static]
        if self.dynamic:
            parts.append(self.dynamic)
        return "\n\n".join(parts)


class PromptBuilderLike(Protocol):
    def build(
        self,
        runtime_context: Optional[Dict[str, Any]] = None,
        user_profile: Optional[str] = None,
    ) -> SystemPrompt: ...


def format_runtime_context(context: Dict[str, Any]) -> str:
    """Render model-visible facts and current-turn reminders."""
    blocks: list[str] = []
    lines = ["[RUNTIME CONTEXT]"]

    if "local_time" in context:
        local_time = str(context["local_time"])
        clock = context.get("local_time_clock")
        local_iso = context.get("local_time_iso")
        if clock and local_iso:
            lines.append(f"Authoritative Current Local Time: {local_time} ({clock}; {local_iso})")
        elif clock:
            lines.append(f"Authoritative Current Local Time: {local_time} ({clock})")
        else:
            lines.append(f"Authoritative Current Local Time: {local_time}")
    if "utc_time" in context:
        lines.append(f"Current UTC Time: {context['utc_time']}")
    if "today_date" in context:
        lines.append(f"Today's Date: {context['today_date']}")
    if "tomorrow_date" in context:
        lines.append(f"Tomorrow's Date: {context['tomorrow_date']}")
    if "timezone" in context:
        lines.append(f"User Timezone: {context['timezone']}")
    if "modality" in context:
        lines.append(f"Input Modality: {context['modality']}")
    if "week_dates" in context:
        lines.append(f"Week Dates (use these for day-name → ISO resolution): {context['week_dates']}")

    location = context.get("location")
    if fresh_geo_position(location) is not None:
        lines.append(
            "Geographic Position Available: yes (source=device; "
            "location-aware tools resolve omitted location inputs automatically)"
        )
    elif allows_home_location_fallback(context):
        lines.append(
            "Geographic Position Available: room/home fallback eligible "
            "(location-aware tools resolve it automatically when configured)"
        )
    else:
        lines.append(
            "Geographic Position Available: no "
            "(ask for a concrete place name or address when location is required)"
        )

    location_ref = context.get("location_ref")
    if isinstance(location_ref, dict):
        room = (
            location_ref.get("room_name")
            or location_ref.get("room_id")
            or location_ref.get("ha_area_id")
        )
        if room:
            provider = location_ref.get("provider") or "unknown"
            lines.append(f"Speaking From Room: {room} (provider={provider})")

    if context.get("node_id"):
        lines.append(f"Node Id: {context['node_id']}")
    if context.get("node_label"):
        lines.append(f"Node Label: {context['node_label']}")

    skip = {
        "local_time",
        "local_time_iso",
        "local_time_clock",
        "utc_time",
        "today_date",
        "tomorrow_date",
        "timezone",
        "source",
        "modality",
        "week_dates",
        "has_history",
        "trigger_decision",
        "location",
        "location_ref",
        "node_id",
        "node_label",
        "node_capabilities",
        "device_kind",
        "owner_id",
        "connection_id",
        "speaker_id",
        "user_profile",
        "cwd",
        "open_work_block",
    }
    for key, value in context.items():
        if key not in skip and value is not None:
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    blocks.append("\n".join(lines))

    if context.get("open_work_block"):
        blocks.append(str(context["open_work_block"]))

    if context.get("has_history") and context.get("source") not in ("system", "background"):
        blocks.append(
            "[CONVERSATION] The messages that follow are your conversation history. "
            "Use them directly; use recall only for older topics not shown."
        )

    if context.get("source") not in ("system", "background") and "local_time" in context:
        blocks.append(
            "[CURRENT TIME] For direct questions about the current time, date, "
            "day, or timezone, use this turn's Authoritative Current Local Time "
            "and User Timezone. Prior assistant answers about the time are stale "
            "immediately. Treat message timestamps as history metadata, not the "
            "current local time."
        )

    if context.get("modality") == "voice" and context.get("source") == "user":
        blocks.append(
            "[CURRENT VOICE TURN] Act on this request. This text is a speech "
            "transcript: resolve likely substitutions from this turn's context "
            "and do not comment on them. A new state change, repeated request, "
            "failed requested state, lookup, or store needs a confirming tool "
            "result from this response."
        )

    return "\n\n".join(blocks)


class PromptBuilder:
    """Build the product prompt followed by user-owned and per-turn context."""

    def __init__(self, prompts_dir: Optional[Path] = None):
        self.prompts_dir = prompts_dir or Path(__file__).parent
        self._system_prompt = (self.prompts_dir / "SYSTEM.md").read_text(
            encoding="utf-8"
        ).strip()

    def build(
        self,
        runtime_context: Optional[Dict[str, Any]] = None,
        user_profile: Optional[str] = None,
    ) -> SystemPrompt:
        """Build the system prompt split into static (cacheable) and dynamic (per-turn) parts.

        Offered capability names, descriptions, and argument schemas are sent
        separately via provider ``tools=``. This prompt carries only global
        behavior and per-turn runtime facts.
        """
        try:
            home_block = format_home_prompt(load_home_snapshot())
        except Exception as exc:
            logger.warning("Failed to load Agent Home overlays: %s", exc)
            home_block = ""

        dynamic_parts: list[str] = []
        if home_block:
            dynamic_parts.append(home_block)
        if user_profile:
            dynamic_parts.append(user_profile)
        if runtime_context:
            dynamic_parts.append(format_runtime_context(runtime_context))
        dynamic_prompt = '\n\n'.join(dynamic_parts)

        return SystemPrompt(static=self._system_prompt, dynamic=dynamic_prompt)

    # ------------------------------------------------------------------
    # Subprocess prompt (mode="code") — XML-structured for SDK agents
    # ------------------------------------------------------------------

    async def build_subprocess_prompt(
        self,
        owner_id: str,
        cwd: str = "",
        conversation_context: str = "",
    ) -> str:
        """Build an XML-structured system prompt for background SDK subprocesses.

        This is intentionally separate from build() because subprocess agents
        run a different runtime (Claude/OpenCode SDK) with their own tools.
        Shared data (memories, env) is fetched here; the identity and rules
        are specific to the subprocess execution mode.
        """
        from plugins.profile import get_profile_block, get_recent_events_block

        home_dir = os.path.expanduser("~")
        platform_name = _platform.system().lower()
        today = date.today().isoformat()

        profile_block: str | None = None
        events_block: str | None = None
        try:
            profile_block = await get_profile_block(owner_id)
        except Exception as e:
            logger.warning("Failed to fetch profile for subprocess prompt: %s", e)
        try:
            events_block = await get_recent_events_block(owner_id)
        except Exception as e:
            logger.warning("Failed to fetch events for subprocess prompt: %s", e)

        parts: list[str] = [
            (
                "<identity>\n"
                "You are a background coding agent for JARV1S, a personal AI voice assistant. "
                "You were dispatched to complete a task autonomously. "
                "Your final text response will be spoken aloud via text-to-speech — "
                "keep it to 1–3 plain sentences summarising what you did.\n"
                "</identity>"
            ),
            (
                "<rules>\n"
                "- Work efficiently. Verify output once at the end, not after every step.\n"
                "- Keep your final response to 1–3 sentences. "
                "No markdown, no bullet lists, no code blocks — it will be read aloud.\n"
                "- NEVER make unrequested changes (extra comments, refactors, documentation, cleanup).\n"
                "- ALWAYS use absolute paths — never use ~ or relative paths.\n"
                "- If an approach fails twice, stop and report the failure clearly instead of looping.\n"
                "</rules>"
            ),
            (
                f"<env>\n"
                f"owner: {owner_id}\n"
                f"home: {home_dir}\n"
                f"cwd: {cwd or home_dir}\n"
                f"platform: {platform_name}\n"
                f"date: {today}\n"
                f"</env>"
            ),
        ]

        if conversation_context:
            parts.append(f"<conversation-context>\n{conversation_context}\n</conversation-context>")

        if profile_block:
            parts.append(f"<user-context>\n{profile_block}\n</user-context>")

        if events_block:
            parts.append(f"<user-preferences>\n{events_block}\n</user-preferences>")

        return "\n\n".join(parts)

    @staticmethod
    async def build_conversation_context(owner_id: str, max_messages: int = 6) -> str:
        """Extract recent conversation turns for background agent context.

        Tool results are truncated to avoid flooding the system prompt.
        """
        from services.database.mongodb import mongodb

        try:
            history = await mongodb.get_history(owner_id, limit=max_messages)
            if not history:
                return ""
            lines: list[str] = []
            for msg in history[-max_messages:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                content = str(content).strip()
                if role == "tool" or "<tool_result>" in content:
                    content = content[:300] + ("…" if len(content) > 300 else "")
                elif len(content) > 400:
                    content = content[:400] + "…"
                lines.append(f"[{role}]: {content}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Failed to build conversation context: %s", e)
            return ""

