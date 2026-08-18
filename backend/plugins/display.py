from typing import Any, Dict, List

from core.decorators import tool
from core.id import generate_id
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.ui import delete_ui
from core.plugins.ui import push_content as _push_content
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _normalize_sections(sections: Any) -> list[dict[str, Any]]:
    """Ensure ContentWidget receives a list of typed sections."""
    if isinstance(sections, dict):
        if "type" in sections:
            return [sections]
        return [{"type": "kv", "pairs": {str(k): str(v) for k, v in sections.items()}}]

    if not isinstance(sections, list):
        raise ValueError("sections must be a list of typed section dicts.")

    is_title_content_list = all(
        isinstance(section, dict)
        and "type" not in section
        and "title" in section
        and "content" in section
        for section in sections
    )
    if is_title_content_list:
        return [{"type": "kv", "pairs": {str(s["title"]): str(s["content"]) for s in sections}}]

    if any(not isinstance(section, dict) or "type" not in section for section in sections):
        raise ValueError(
            'each section must include a "type". For simple facts, use {"type": "kv", "pairs": {...}}.'
        )

    return sections


class DisplayPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="display",
        version="1.0.0",
        description="Push structured content widgets to the screen.",
        routable=False,
    )

    @tool
    async def push_content(
        self,
        title: str,
        sections: List[Dict[str, Any]],
        pinned: bool = False,
        widget_id: str | None = None,
    ) -> str | CapabilityErrorDetail:
        """
        Display structured content on screen. Use for dense generic output that has no domain widget; speak only a one-sentence summary.
        Do not call this after a tool that already displayed its artifact.

        When the same on-screen display will be updated again, pass a stable widget_id on every
        push so the UI updates in place instead of creating a new card. Use a short semantic slug
        you can re-derive from context (e.g. tracker-budget, score-finals-game4). Omit for one-off
        displays. The return string includes widget_id= for recovery across turns. When done,
        call delete_widget(widget_id) to remove it.

        Choose the simplest section type:
        - Facts, scores, statuses, small summaries -> one kv section:
          title="Lakers Score", widget_id="score-lakers-thunder",
          sections=[{"type": "kv", "pairs": {
              "Result": "Thunder 115, Lakers 110",
              "Date": "May 19, 2026",
              "Context": "Western Conference Semifinals, Game 4",
          }}]
          Reuse the same widget_id on later updates to the same display.
        - Tables -> {"type": "table", "headers": ["Col A", "Col B"], "rows": [["v1", "v2"]]}.
        - Bullets -> {"type": "list", "items": ["Item one", "Item two"], "ordered": False}.
        - Paragraphs or mixed notes -> {"type": "markdown", "content": "## Heading\\nBody..."}.
        - Code -> {"type": "code", "language": "python", "content": "print('hello')"}.
        - Metrics -> {"type": "metric", "items": [{"label": "CPU", "value": "27%", "percent": 27,
          "status": "good", "sublabel": "8 cores"}]}; status is good | warning | critical.
        Every section must include a "type". Never pass a plain dict of fields as sections.

        Args:
            title: Short widget title.
            sections: List of typed section dicts. For simple facts use [{"type": "kv", "pairs": {...}}].
            pinned: True only when the user asked to keep the display visible or it should remain
                useful after the current response. Do not pin every live update automatically.
            widget_id: Stable ID for displays that will be updated again. Omit for one-off content.

        pinned=True keeps the widget visible after the response ends.
        """
        try:
            normalized_sections = _normalize_sections(sections)
        except ValueError as exc:
            return _fail(f"Display content not shown: {exc}")

        resolved_id = widget_id or generate_id("content-")
        _push_content(
            title=title,
            sections=normalized_sections,
            pinned=pinned,
            widget_id=resolved_id,
        )
        n = len(normalized_sections)
        return f"Displayed '{title}' ({n} section{'s' if n != 1 else ''}). widget_id={resolved_id}"

    @tool
    async def delete_widget(self, widget_id: str) -> str:
        """Remove a widget from the screen by its widget_id."""
        delete_ui(widget_id)
        return f"Removed widget '{widget_id}'."
