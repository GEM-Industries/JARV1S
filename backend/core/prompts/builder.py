"""Prompt builder that assembles system prompts from cached YAML sections.

Supports multiple PromptModes following the OpenClaw pattern:
  FULL       — main voice/text turns (all sections)
  BACKGROUND — in-process background agents (skip voice/style/examples)
Subprocess agents (mode="code") use build_subprocess_prompt() which
generates a separate XML-structured prompt for the SDK.
"""

import logging
import os
import platform as _platform
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import yaml

from core.context import allows_home_location_fallback, fresh_geo_position

logger = logging.getLogger(__name__)


class PromptMode(str, Enum):
    """Controls which persona sections are included in the system prompt.

    Follows the progressive-disclosure principle from OpenClaw's PromptMode:
    subagents receive only the sections they need, saving tokens and
    preventing context pollution from voice-specific rules.
    """
    FULL = "full"
    BACKGROUND = "background"


# Each section is tagged with the set of modes that include it.
# Voice, style, and examples are voice-turn-only.
PERSONA_SECTIONS: list[tuple[str, set[PromptMode]]] = [
    ('persona/identity.yaml',         {PromptMode.FULL, PromptMode.BACKGROUND}),
    ('persona/style.yaml',            {PromptMode.FULL}),
    ('persona/protocols.yaml',        {PromptMode.FULL}),
    ('persona/background.yaml',       {PromptMode.BACKGROUND}),
    ('persona/voice.yaml',            {PromptMode.FULL}),
    ('persona/examples.yaml',         {PromptMode.FULL}),
    ('capabilities/reasoning.yaml',   {PromptMode.FULL}),
    ('capabilities/reasoning_background.yaml', {PromptMode.BACKGROUND}),
]


@dataclass
class SystemPrompt:
    """Split prompt enabling LLM provider caching of the static prefix.

    Providers cache the longest matching prefix between requests. Persona YAML
    is turn-invariant and lives in ``static``. Per-turn runtime facts live in
    ``dynamic``. Offered tool schemas are sent separately via ``tools=``.
    """

    static: str
    dynamic: str

    def __str__(self) -> str:
        parts = [self.static]
        if self.dynamic:
            parts.append(self.dynamic)
        return "\n\n".join(parts)


class PromptBuilder:
    """Builds system prompts from modular YAML files with dynamic content injection."""

    def __init__(self, prompts_dir: Optional[Path] = None):
        self.prompts_dir = prompts_dir or Path(__file__).parent
        self._section_cache: Dict[str, str] = {}
        self._preload_sections()

    def _preload_sections(self) -> None:
        """Load and cache all persona YAML files once at init."""
        for file, _modes in PERSONA_SECTIONS:
            section = self._load_section(file)
            if section:
                self._section_cache[file] = section

    def build(
        self,
        runtime_context: Optional[Dict[str, Any]] = None,
        user_profile: Optional[str] = None,
        mode: PromptMode = PromptMode.FULL,
        action_capable: bool = True,
    ) -> SystemPrompt:
        """Build the system prompt split into static (cacheable) and dynamic (per-turn) parts.

        Offered capability names, descriptions, and argument schemas are sent
        separately via provider ``tools=``. This prompt carries only global
        behavior and per-turn runtime facts.
        """
        action_files = {
            "persona/protocols.yaml",
            "persona/background.yaml",
            "capabilities/reasoning.yaml",
            "capabilities/reasoning_background.yaml",
        }
        sections = []
        for file, modes in PERSONA_SECTIONS:
            if mode not in modes:
                continue
            if not action_capable and file in action_files:
                continue
            section = self._section_cache.get(file)
            if section:
                sections.append(section)
                if file == 'persona/identity.yaml' and user_profile:
                    sections.append(user_profile)

        static_prompt = '\n\n'.join(sections)

        dynamic_parts: list[str] = []
        if runtime_context:
            dynamic_parts.append(self._format_runtime_context(runtime_context))
        dynamic_prompt = '\n\n'.join(dynamic_parts)

        return SystemPrompt(static=static_prompt, dynamic=dynamic_prompt)

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

    # ------------------------------------------------------------------
    # Shared context utilities
    # ------------------------------------------------------------------

    @staticmethod
    async def build_background_context(owner_id: str, cwd: str = "") -> dict[str, Any]:
        """Build the runtime context dict for in-process background agents.

        Equivalent to what process_turn injects for voice turns so background
        agents receive the same profile and timezone context.
        """
        from core.time import build_turn_time_context

        ctx: dict[str, Any] = {
            "source": "background",
            "cwd": cwd,
            "owner_id": owner_id,
            "connection_id": owner_id,
            "speaker_id": None,
        }
        try:
            from plugins.profile import get_profile_block
            ctx["user_profile"] = await get_profile_block(owner_id)
        except Exception:
            pass
        tz_str = "UTC"
        try:
            from services.database.mongodb import mongodb as _mdb
            session_doc = await _mdb.db["sessions"].find_one({"session_id": owner_id})
            tz_str = (session_doc or {}).get("timezone", "UTC")
        except Exception:
            pass
        try:
            ctx.update(build_turn_time_context(tz_str))
        except Exception:
            ctx.update(build_turn_time_context("UTC"))
        return ctx

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_runtime_context(self, context: Dict[str, Any]) -> str:
        blocks = []

        if context.get("source") == "system":
            decision = context.get("trigger_decision", "tell")
            if decision == "offer":
                blocks.append(
                    "[DECISION EVALUATION] This is a proactive offer evaluation. "
                    "Use CURRENT_STATE and INSTRUCTIONS in the system event to decide "
                    "whether to speak now, defer, or stay silent. Do not announce preemptively."
                )
            elif decision == "act":
                blocks.append(
                    "[SILENT EXECUTION] Execute the system event without speaking to the user."
                )
            else:
                blocks.append(
                    "[IMPORTANT] This is a PROACTIVE ALERT. Follow the VOICE PROTOCOLS. "
                    "You are the messenger. Use the Imperative Mood. "
                    "Direct the message to the user immediately."
                )

        if context.get("source") == "background":
            blocks.append(
                "[EXECUTION MODE] You are a background agent. "
                "Execute the task directly using available tools "
                "(e.g. jarvis.slack.*, jarvis.gmail.*, jarvis.calendar.*). "
                "External API cooldowns outlast a single turn — never retry a "
                "rate-limited call. "
                "Your connected service identities (Slack, GitHub, etc.) are in "
                "[USER CONTEXT] — use them directly when searching for your own "
                "messages, assignments, or activity instead of looking up your account."
            )

        if context.get("modality") == "text":
            blocks.append(
                "[OUTPUT FORMAT] This is a text session. Use Markdown formatting, "
                "include precise data, and structure responses for readability. "
                "Ignore voice/TTS optimization rules."
            )

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
        }
        for key, value in context.items():
            if key not in skip and value is not None:
                lines.append(f"{key.replace('_', ' ').title()}: {value}")

        blocks.append("\n".join(lines))

        # Prompt repetition: re-state conversation awareness at the system/history
        # boundary so non-reasoning models attend to it near the actual messages.
        if context.get("has_history") and context.get("source") not in ("system", "background"):
            blocks.append(
                "[CONVERSATION] The messages that follow are your conversation history. "
                "You can see and quote them directly. NEVER say \"I don't have that information\" "
                "when the answer is in the messages below. "
                "For older topics not visible here, use recall() to search past sessions."
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
                "[CURRENT VOICE TURN] Act from this current user request, not from matching "
                "the previous assistant message. Spoken claims are not tool results. Lookups "
                "may reuse a complete current tool result in this conversation. A new state "
                "change, repeat, or contradiction still needs a tool call in this response. "
                "Never output bracketed example text."
            )

        return "\n\n".join(blocks)

    def _load_section(self, relative_path: str) -> Optional[str]:
        """Load a single YAML section from disk."""
        path = self.prompts_dir / relative_path
        if not path.exists():
            return None

        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if 'content' in data:
            return data['content'].strip()
        elif 'examples' in data:
            return self._format_examples(data['examples'])
        return None

    @staticmethod
    def _format_examples(examples: list) -> str:
        """Format examples list into prompt text."""
        lines = [
            'STYLE EXAMPLES:',
            'These demonstrate tone and brevity, not exact wording. Adapt to actual context.',
            ''
        ]
        for ex in examples:
            lines.append(f"User: {ex['user']}")
            lines.append(f"JARVIS: {ex['jarvis']}")
            if 'note' in ex:
                lines.append(f"(Style note: {ex['note']})")
            lines.append('')
        return '\n'.join(lines)
