"""Search Plugin for JARV1S."""

from core.decorators import tool
from core.plugins.types import JarvisPlugin, PluginMetadata
from plugins.search.client import (
    DEFAULT_MAX_RESULTS,
    FallbackSearchClient,
    SearchClient,
    SearchResult,
    SearchType,
    create_search_client,
)

__all__ = [
    "SearchPlugin",
    "SearchResult",
    "FallbackSearchClient",
    "SearchClient",
    "create_search_client",
]


class SearchPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="search",
        version="2.0.0",
        description="Search the web for real-time information.",
        dependencies=["httpx"],
        utterances=[
            "search the web for recent news",
            "have a look online for information",
            "search online",
            "look that up online",
            "look up the latest on AI",
            "google something for me",
            "find information about Tokyo flights",
            "what happened in the world today",
            "search the web for current pricing",
            "look up current pricing online",
            "research recent documentation online",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations

        integrations.register("search", create_search_client, config_keys=[])

    @tool(inject=["search"])
    async def web(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        search_type: SearchType = "auto",
        search: SearchClient = None,
    ) -> list[SearchResult]:
        """
        Search the web for real-time information.
        Use when user asks about current events, factual questions you can't answer from memory,
        or needs up-to-date data (prices, schedules, results). Increase max_results up to 10
        when broader coverage is useful. Use search_type="deep" for Exa-backed research that needs
        more thorough source discovery; otherwise keep search_type="auto".
        NEVER read the 'url' field aloud.
        """
        return await search.search(
            query=query,
            max_results=max_results,
            search_type=search_type,
        )
