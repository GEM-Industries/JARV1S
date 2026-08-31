"""Prompt and runtime context for the in-process background worker."""

import logging
from pathlib import Path
from typing import Any

from core.config import settings
from core.context import get_ctx
from core.home import format_skill_catalog, load_home_snapshot
from core.prompts.builder import SystemPrompt, format_runtime_context

logger = logging.getLogger(__name__)


class BackgroundPromptBuilder:
    """Build the focused prompt used for delegated JARV1S work."""

    def __init__(self, prompts_dir: Path | None = None):
        root = prompts_dir or Path(__file__).parent
        self._system_prompt = (root / "BACKGROUND.md").read_text(encoding="utf-8").strip()

    def build(
        self,
        runtime_context: dict[str, Any] | None = None,
        user_profile: str | None = None,
    ) -> SystemPrompt:
        dynamic_parts: list[str] = []
        try:
            skill_catalog = format_skill_catalog(load_home_snapshot().skills)
        except Exception as exc:
            logger.warning("Failed to load background skills: %s", exc)
            skill_catalog = ""
        if skill_catalog:
            dynamic_parts.append(skill_catalog)
        if user_profile:
            dynamic_parts.append(user_profile)
        if runtime_context:
            dynamic_parts.append(format_runtime_context(runtime_context))
        return SystemPrompt(
            static=self._system_prompt,
            dynamic="\n\n".join(dynamic_parts),
        )


async def build_background_context(owner_id: str, cwd: str = "") -> dict[str, Any]:
    """Build operational context for an in-process background task."""
    from core.time import build_turn_time_context

    context: dict[str, Any] = {
        "source": "background",
        "cwd": cwd,
        "owner_id": owner_id,
        "connection_id": owner_id,
        "speaker_id": None,
    }
    try:
        from plugins.profile import get_profile_block

        context["user_profile"] = await get_profile_block(owner_id)
    except Exception as exc:
        logger.warning("Failed to load background user context: %s", exc)

    timezone_name = get_ctx().get("timezone") or settings.PREFETCH_FALLBACK_TIMEZONE
    try:
        context.update(build_turn_time_context(timezone_name))
    except Exception:
        context.update(build_turn_time_context(settings.PREFETCH_FALLBACK_TIMEZONE))
    return context
