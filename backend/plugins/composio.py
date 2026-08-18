"""
Composio Meta Plugin for JARV1S.

Remote Composio catalog and execution fallback for tools not mounted locally.
Use jarvis.system.search_tools() first for mounted jarvis.* tools.
"""

import json
import logging
from typing import Any

from core.decorators import tool
from core.plugins.types import JarvisPlugin, PluginMetadata

logger = logging.getLogger(__name__)


class ComposioPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="composio",
        version="1.0.0",
        description="Search and execute tools not mounted locally.",
        routable=False,
        hidden=True,
    )

    @tool
    async def search_catalog(self, query: str) -> str:
        """Search Composio's full toolkit catalog for tools not mounted locally.
        Only use as a fallback when jarvis.system.search_tools() returns no useful results.
        Results are from Composio's remote API — use execute_tool() to call them.
        """
        from core.integrations.composio_gateway import get_composio_gateway

        gateway = get_composio_gateway()
        if not gateway:
            return json.dumps({"error": "Composio is not configured."})

        try:
            results = await gateway.search_composio_tools(query)
        except Exception as e:
            logger.error("search_catalog failed: %s", e)
            return json.dumps({"error": str(e)})

        return json.dumps([
            {
                "name": r["name"],
                "description": r.get("description", ""),
                "app": r.get("app", ""),
                "call_as": f"jarvis.composio.execute_tool('{r['name']}')",
            }
            for r in results[:10]
        ], indent=2)

    @tool
    async def execute_tool(self, tool_name: str, **params: Any) -> str:
        """Execute a tool by exact name with the given params.
        Prefer calling jarvis.<app>.<TOOL_NAME>() directly — it is faster.
        Only use this when the tool is not mounted on the jarvis.* namespace.
        """
        from core.integrations.composio_gateway import get_composio_gateway

        gateway = get_composio_gateway()
        if not gateway:
            return json.dumps({"error": "Composio is not configured."})

        try:
            data = await gateway.execute_composio_tool(tool_name, params)
            if "error" in data:
                return json.dumps(data)
            result = data.get("response", data.get("result", data))
            return json.dumps(result, indent=2) if not isinstance(result, str) else result
        except Exception as e:
            logger.error("composio.execute_tool('%s') failed: %s", tool_name, e)
            return json.dumps({"error": str(e)})