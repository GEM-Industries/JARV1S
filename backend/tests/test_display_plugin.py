"""Tests for the display plugin push_content widget_id contract."""

from unittest.mock import patch

import pytest

from plugins.display import DisplayPlugin


@pytest.fixture
def plugin() -> DisplayPlugin:
    return DisplayPlugin()


@pytest.mark.asyncio
async def test_push_content_passes_explicit_widget_id(plugin: DisplayPlugin):
    sections = [{"type": "kv", "pairs": {"Score": "110-115"}}]
    pushed: list = []

    with patch("plugins.display._push_content", side_effect=lambda **kw: pushed.append(kw)):
        result = await plugin.push_content(
            title="Lakers Score",
            sections=sections,
            widget_id="score-lakers-thunder",
        )

    assert pushed[0]["widget_id"] == "score-lakers-thunder"
    assert "widget_id=score-lakers-thunder" in result


@pytest.mark.asyncio
async def test_push_content_generates_widget_id_when_omitted(plugin: DisplayPlugin):
    sections = [{"type": "kv", "pairs": {"Note": "One-off"}}]
    pushed: list = []

    with patch("plugins.display._push_content", side_effect=lambda **kw: pushed.append(kw)):
        result = await plugin.push_content(title="Note", sections=sections)

    assert pushed[0]["widget_id"].startswith("content-")
    assert f"widget_id={pushed[0]['widget_id']}" in result


@pytest.mark.asyncio
async def test_push_content_reuses_same_widget_id_on_updates(plugin: DisplayPlugin):
    sections = [{"type": "kv", "pairs": {"Score": "110-115"}}]
    pushed: list = []

    with patch("plugins.display._push_content", side_effect=lambda **kw: pushed.append(kw)):
        await plugin.push_content(
            title="Game Score",
            sections=sections,
            widget_id="score-game4",
        )
        await plugin.push_content(
            title="Game Score",
            sections=[{"type": "kv", "pairs": {"Score": "112-115"}}],
            widget_id="score-game4",
        )

    assert pushed[0]["widget_id"] == "score-game4"
    assert pushed[1]["widget_id"] == "score-game4"
