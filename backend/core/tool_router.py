"""
Semantic Tool Router.

Plugin-level routing: every plugin with utterances (metadata, curated files, or
generated-cache fallback) keeps the full list of its utterance embeddings — NOT
a centroid. Scoring is max-over-utterances so a narrow intent ("archive that",
"clear history") doesn't get averaged away by unrelated utterances in the same
plugin. Matched plugins contribute their tools to the per-turn ``tools=`` set.

A plugin can opt out of routing via ``metadata.routable = False``. Always-on
capabilities are unioned into ``tools=`` after routing.

Policy presets live in ``core.routing.policies`` so production, evals, and docs
share one source of truth.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set

from core.plugins.registry import registry
from core.routing.helpers import expand_plugins_to_fqns
from core.routing.phrases import load_curated_utterances
from core.routing.policies import (
    DECAY_BONUS,
    VOICE_POLICY,
    RoutingPolicy,
    resolve_policy,
)
from services.embeddings import embedding_service
from services.perf import perf

logger = logging.getLogger(__name__)

DEFAULT_POLICY = VOICE_POLICY
_SPLIT_RE = re.compile(
    r"\s*(?:[,;]|\band then\b|\bthen\b|\balso\b|\bplus\b|\band\b)\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteDiagnostics:
    policy_name: str
    match_mode: str
    matched_plugins: list[str]
    routed_tool_count: int
    ranked_plugins: list[dict[str, float | str]]
    schema_chars: int
    schema_tokens: int
    route_latency_ms: float
    segment_matches: dict[str, list[str]] = field(default_factory=dict)
    used_session_carryover: bool = False
    used_tool_focus: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _is_routable(plugin) -> bool:
    """A plugin is routable unless it explicitly opts out via metadata."""
    return plugin.metadata.routable


# Offer a capability on every turn only when routing cannot see the intent:
# 1. the discovery escape hatch
# 2. an unroutable computer/presentation primitive
# 3. a model-initiated action the user often does not name (memory writes,
#    web search, shell, named-work verbs whose roster is already in the prompt,
#    yes/no for a pending approval)
# Routable domain create/edit tools stay off this list. Routing, search_tools,
# and edit_tool promotion cover those.
ALWAYS_ON_FQNS: frozenset[str] = frozenset({
    "system.search_tools",
    "display.push_content",
    "files.delete",
    "files.edit",
    "files.find",
    "files.grep",
    "files.move",
    "files.read",
    "files.write",
    "system.exec",
    "system.approve_pending",
    "system.deny_pending",
    "search.web",
    "profile.add_memory",
    "profile.remember",
    "profile.update_memory",
    "agents.dispatch",
    "agents.resume",
    "agents.get_status",
    "agents.cancel_task",
    "agents.close",
})


def always_on_fqns() -> Set[str]:
    """Enabled FQNs from ALWAYS_ON_FQNS. Unioned into tools= after routing."""
    enabled: Set[str] = set()
    for fqn in ALWAYS_ON_FQNS:
        definition = registry.get_capability(fqn)
        if definition is not None and definition.enabled:
            enabled.add(fqn)
    return enabled


_ACT_HIDDEN_FQNS = frozenset({"scheduler.remind"})
_BACKGROUND_HIDDEN_FQNS = frozenset({"agents.dispatch"})


def active_tool_fqns(
    routed: Set[str] | None = None,
    *,
    trigger_decision: str | None = None,
    source: str | None = None,
) -> Set[str]:
    """Per-iteration tools= set: semantic matches plus always-on capabilities."""
    fqns = set(routed or ()) | always_on_fqns()
    if trigger_decision == "act":
        fqns -= _ACT_HIDDEN_FQNS
    if source == "background":
        fqns -= _BACKGROUND_HIDDEN_FQNS
    return fqns


_NAMED_FQN_KEYS = ("fqn", "edit_tool")
_MAX_DISCOVERY_DEPTH = 6


def _looks_like_fqn(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    fqn = value.strip()
    if "." not in fqn or " " in fqn:
        return None
    return fqn


def _collect_named_fqns(data: object, found: Set[str], *, depth: int = 0) -> None:
    if data is None or depth > _MAX_DISCOVERY_DEPTH:
        return
    if isinstance(data, str):
        text = data.strip()
        if text[:1] in "{[":
            try:
                _collect_named_fqns(json.loads(text), found, depth=depth + 1)
            except (TypeError, ValueError):
                return
        return
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        data = dump(mode="json", exclude_none=True)
    if isinstance(data, dict):
        for key in _NAMED_FQN_KEYS:
            fqn = _looks_like_fqn(data.get(key))
            if fqn:
                found.add(fqn)
        for value in data.values():
            _collect_named_fqns(value, found, depth=depth + 1)
        return
    if isinstance(data, (list, tuple)):
        for item in data:
            _collect_named_fqns(item, found, depth=depth + 1)


def discovered_fqns(data: object) -> Set[str]:
    """Catalog FQNs named in a tool result that should be offered next iteration.

    Covers ``system.search_tools`` ``fqn`` cards and setups ``edit_tool`` pointers.
    Unknown names are ignored so a result cannot invent a capability.
    """
    found: Set[str] = set()
    _collect_named_fqns(data, found)
    return {fqn for fqn in found if registry.get_capability(fqn) is not None}


def search_result_fqns(data: object) -> Set[str]:
    """Extract discovered FQNs from a typed system.search_tools result."""
    return discovered_fqns(data)


class ToolRouter:
    """Plugin-level semantic router.

    route() returns the matched plugin FQNs for this turn. Always-on
    capabilities are added later by ``active_tool_fqns()`` when building
    ``tools=``. Other tools remain callable after ``system.search_tools``.
    """

    def __init__(self):
        # All utterance vectors per plugin (NOT a centroid — max-pool at query time).
        self._utterance_vectors: Dict[str, List[List[float]]] = {}
        self._session_focus: Dict[str, frozenset[str]] = {}
        self._pending_route_carryover: Dict[str, frozenset[str]] = {}
        self._last_diagnostics: Dict[str, RouteDiagnostics] = {}

    async def initialize(self, llm_service: Optional[object] = None) -> None:
        """Embed every routable plugin's utterances at startup.

        Plugins with hand-written ``metadata.utterances`` use those directly.
        Plugins without get utterances synthesized by ``utterance_cache`` (needs
        ``llm_service``). A plugin can opt out with ``metadata.routable = False``.
        """
        candidates = {
            name: plugin
            for name, plugin in registry.plugins.items()
            if _is_routable(plugin)
        }
        if not candidates:
            logger.info("ToolRouter: no routable plugins found.")
            return

        from core.integrations.utterance_cache import load_or_generate

        self._utterance_vectors.clear()
        await asyncio.to_thread(embedding_service.warmup)
        sibling_summaries = [
            (name, plugin.description)
            for name, plugin in sorted(candidates.items())
        ]
        embedded = 0
        attempted = 0
        for name, plugin in candidates.items():
            utterances = plugin.metadata.utterances or load_curated_utterances(name)
            if not utterances:
                utterances = await load_or_generate(
                    name,
                    plugin.description,
                    plugin.get_tools(),
                    llm_service,
                    sibling_summaries=sibling_summaries,
                )
            if not utterances:
                continue
            attempted += 1
            try:
                vectors = await asyncio.to_thread(embedding_service.embed, utterances)
                self._utterance_vectors[name] = vectors
                embedded += 1
            except Exception as e:
                logger.warning("ToolRouter: failed to embed '%s': %s", name, e)

        if attempted > 0 and embedded == 0:
            raise RuntimeError(
                "ToolRouter could not build a semantic routing index. "
                "FastEmbed failed to embed plugin utterances. "
                "Check backend/.cache/fastembed or set FASTEMBED_CACHE_PATH."
            )
        logger.info(
            "ToolRouter initialized: %d/%d routable plugins embedded",
            embedded, len(candidates),
        )

    def _score_plugins(
        self, query_vec: List[float], focus_plugins: Set[str], decay_bonus: float = DECAY_BONUS
    ) -> tuple[dict[str, float], list[tuple[str, float]]]:
        """Compute raw + adjusted score per enabled plugin.

        Returns (raw_scores_for_logging, adjusted_scored_list).
        Raw score = max cosine over the plugin's utterance vectors (max-pool).
        """
        raw_scores: dict[str, float] = {}
        adjusted: list[tuple[str, float]] = []
        for name, vectors in self._utterance_vectors.items():
            if not registry.is_enabled(name):
                continue
            raw = max(
                embedding_service.cosine_similarity(query_vec, v)
                for v in vectors
            )
            bonus = decay_bonus if name in focus_plugins else 0.0
            raw_scores[name] = round(raw, 3)
            adjusted.append((name, raw + bonus))
        return raw_scores, adjusted

    async def route(
        self,
        utterance: str,
        session_id: str,
        *,
        policy: RoutingPolicy | str | None = None,
    ) -> Set[str]:
        """Return tool FQNs matched from the current turn and pending handoff."""
        resolved_policy = self._resolve_policy(policy)
        perf.start("tool_route", session_id)
        focus_plugins = self._focused_plugins(session_id)
        carryover_plugins = (
            set(self._pending_route_carryover.pop(session_id, frozenset()))
            if resolved_policy.session_carryover
            else set()
        )
        decay_plugins = focus_plugins if resolved_policy.session_carryover else set()

        if not self._utterance_vectors:
            logger.error("ToolRouter.route called with an empty embedding index")
            elapsed_ms = perf.end("tool_route", session_id)
            self._record_diagnostics(
                session_id=session_id,
                policy=resolved_policy,
                match_mode="router_uninitialized",
                matched=set(),
                routed=set(),
                raw_scores={},
                adjusted=[],
                elapsed_ms=elapsed_ms,
            )
            return set()

        try:
            if resolved_policy.multi_intent:
                matched, match_mode, raw_scores, adjusted, segment_matches = (
                    await self._route_multi_intent(
                        utterance,
                        decay_plugins,
                        resolved_policy,
                    )
                )
            else:
                query_vec = await asyncio.to_thread(embedding_service.embed_one, utterance)
                raw_scores, adjusted = self._score_plugins(
                    query_vec,
                    decay_plugins,
                    resolved_policy.decay_bonus,
                )
                matched, match_mode = self._select_ranked_plugins(adjusted, resolved_policy)
                segment_matches = {}
            used_tool_focus = False
            if (
                resolved_policy.session_carryover
                and focus_plugins
                and not self._has_strong_new_domain(
                    matched, raw_scores, resolved_policy, focus_plugins
                )
            ):
                focused_matched = self._select_matched_plugins(
                    self._focus_ranked_candidates(matched, focus_plugins, raw_scores),
                    resolved_policy,
                )
                used_tool_focus = True
                if focused_matched != matched:
                    matched = focused_matched
                    match_mode = "tool_focus"
        except Exception as e:
            logger.warning("ToolRouter embed failed: %s", e)
            matched = self._select_matched_plugins(
                [(name, 1.0) for name in sorted(focus_plugins | carryover_plugins)],
                resolved_policy,
            )
            routed = self._expand_to_fqns(matched)
            elapsed_ms = perf.end("tool_route", session_id)
            self._record_diagnostics(
                session_id=session_id,
                policy=resolved_policy,
                match_mode="embed_error_focus",
                matched=matched,
                routed=routed,
                raw_scores={name: 1.0 for name in matched},
                adjusted=[(name, 1.0) for name in matched],
                elapsed_ms=elapsed_ms,
                used_session_carryover=bool(matched & carryover_plugins),
                used_tool_focus=bool(matched & focus_plugins),
            )
            return routed

        used_session_carryover = False
        if carryover_plugins:
            carryover_score = resolved_policy.threshold + 0.01
            ranked = [
                (
                    name,
                    1.0
                    if name in focus_plugins
                    else (
                        max(raw_scores.get(name, 0.0), carryover_score)
                        if name in carryover_plugins
                        else raw_scores.get(name, 0.0)
                    ),
                )
                for name in matched | carryover_plugins
            ]
            carried_matched = self._select_matched_plugins(ranked, resolved_policy)
            used_session_carryover = bool(carried_matched & carryover_plugins)
            if carried_matched != matched:
                matched = carried_matched
                match_mode = "session_carryover"

        routed = self._expand_to_fqns(matched)

        top_scores = sorted(raw_scores.items(), key=lambda x: x[1], reverse=True)[:6]
        logger.info(
            "ToolRouter: [%s] %d tools from %d plugin(s) | mode=%s | matched=%s | scores=%s",
            session_id, len(routed), len(matched), match_mode,
            sorted(matched) or "none",
            dict(top_scores),
        )
        elapsed_ms = perf.end("tool_route", session_id)
        self._record_diagnostics(
            session_id=session_id,
            policy=resolved_policy,
            match_mode=match_mode,
            matched=matched,
            routed=routed,
            raw_scores=raw_scores,
            adjusted=adjusted,
            elapsed_ms=elapsed_ms,
            segment_matches=segment_matches,
            used_session_carryover=used_session_carryover,
            used_tool_focus=used_tool_focus,
        )
        return routed

    def _resolve_policy(self, policy: RoutingPolicy | str | None) -> RoutingPolicy:
        return resolve_policy(policy)

    @staticmethod
    def _has_strong_new_domain(
        matched: Set[str],
        raw_scores: dict[str, float],
        policy: RoutingPolicy,
        focus_plugins: Set[str],
    ) -> bool:
        return any(
            name not in focus_plugins and raw_scores.get(name, 0.0) >= policy.threshold
            for name in matched
        )

    @staticmethod
    def _focus_ranked_candidates(
        matched: Set[str],
        focus_plugins: Set[str],
        raw_scores: dict[str, float],
    ) -> list[tuple[str, float]]:
        plugins = matched | focus_plugins
        return [
            (name, 1.0 if name in focus_plugins else raw_scores.get(name, 0.0))
            for name in sorted(plugins)
        ]

    def _focused_plugins(self, session_id: str) -> Set[str]:
        return set(self._session_focus.get(session_id, frozenset()))

    def record_tool_focus(
        self,
        session_id: str,
        *,
        tools: Set[str],
    ) -> None:
        plugins = {tool.split(".", 1)[0] for tool in tools if "." in tool}
        plugins = {plugin for plugin in plugins if registry.is_enabled(plugin)}
        if not plugins:
            return

        self._session_focus[session_id] = frozenset(plugins)

    def record_route_carryover(self, session_id: str, *, tools: Set[str]) -> None:
        """Expose plugins routed for delivered proactive content to one user turn."""
        plugins = frozenset(
            plugin
            for plugin in (tool.split(".", 1)[0] for tool in tools if "." in tool)
            if plugin in registry.plugins and registry.is_enabled(plugin)
        )
        if plugins:
            self._pending_route_carryover[session_id] = plugins
        else:
            self._pending_route_carryover.pop(session_id, None)

    @staticmethod
    def _split_intents(utterance: str) -> list[str]:
        parts = [part.strip() for part in _SPLIT_RE.split(utterance) if part.strip()]
        if not parts:
            return [utterance.strip()] if utterance.strip() else []
        return parts

    def _select_ranked_plugins(
        self,
        adjusted: list[tuple[str, float]],
        policy: RoutingPolicy,
    ) -> tuple[Set[str], str]:
        ranked = sorted(adjusted, key=lambda x: x[1], reverse=True)
        over_threshold = [p for p in ranked if p[1] >= policy.threshold]
        if over_threshold:
            return self._select_matched_plugins(over_threshold, policy), "threshold"

        fallback = [p for p in ranked if p[1] >= policy.fallback_threshold][:policy.fallback_top_k]
        if fallback:
            return self._select_matched_plugins(fallback, policy), "fallback_topk"

        return set(), "none"

    async def _route_multi_intent(
        self,
        utterance: str,
        focus_plugins: Set[str],
        policy: RoutingPolicy,
    ) -> tuple[
        Set[str],
        str,
        dict[str, float],
        list[tuple[str, float]],
        dict[str, list[str]],
    ]:
        segments = self._split_intents(utterance)[: policy.max_segments]
        if not segments:
            return set(), "none", {}, [], {}

        if len(segments) == 1:
            vectors = [await asyncio.to_thread(embedding_service.embed_one, segments[0])]
        else:
            vectors = await asyncio.to_thread(embedding_service.embed, segments)

        raw_scores: dict[str, float] = {}
        best_adjusted: dict[str, float] = {}
        segment_matches: dict[str, list[str]] = {}
        per_segment_limit = max(1, policy.segment_top_k)

        for segment, query_vec in zip(segments, vectors):
            segment_raw, segment_adjusted = self._score_plugins(
                query_vec, focus_plugins, policy.decay_bonus
            )
            for name, raw in segment_raw.items():
                raw_scores[name] = max(raw_scores.get(name, raw), raw)
            for name, score in segment_adjusted:
                best_adjusted[name] = max(best_adjusted.get(name, score), score)

            ranked = sorted(segment_adjusted, key=lambda x: x[1], reverse=True)
            chosen = [name for name, score in ranked if score >= policy.threshold][:per_segment_limit]
            if chosen:
                segment_matches[segment] = chosen

        adjusted = sorted(best_adjusted.items(), key=lambda x: x[1], reverse=True)
        candidate_scores: dict[str, float] = {}
        for names in segment_matches.values():
            for name in names:
                candidate_scores[name] = max(candidate_scores.get(name, 0.0), best_adjusted[name])
        candidates = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)
        if candidates:
            return self._select_matched_plugins(candidates, policy), "multi_intent", raw_scores, adjusted, segment_matches

        fallback = [p for p in adjusted if p[1] >= policy.fallback_threshold][:policy.fallback_top_k]
        if fallback:
            return self._select_matched_plugins(fallback, policy), "fallback_topk", raw_scores, adjusted, segment_matches
        return set(), "none", raw_scores, adjusted, segment_matches

    def _select_matched_plugins(
        self,
        candidates: list[tuple[str, float]],
        policy: RoutingPolicy,
    ) -> Set[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for name, _score in sorted(candidates, key=lambda x: x[1], reverse=True):
            if name in seen or not registry.is_enabled(name):
                continue
            seen.add(name)
            if len(selected) >= policy.max_matched:
                break
            selected.append(name)
        return set(selected)

    def _schema_stats(self, routed_tools: Set[str]) -> tuple[int, int]:
        active = set(routed_tools) | always_on_fqns()
        if not active:
            return 0, 0
        try:
            return registry.estimate_schema_stats(active)
        except Exception as e:
            logger.debug("ToolRouter: schema sizing failed: %s", e)
            return 0, 0

    def _record_diagnostics(
        self,
        *,
        session_id: str,
        policy: RoutingPolicy,
        match_mode: str,
        matched: Set[str],
        routed: Set[str],
        raw_scores: dict[str, float],
        adjusted: list[tuple[str, float]],
        elapsed_ms: float,
        segment_matches: dict[str, list[str]] | None = None,
        used_session_carryover: bool = False,
        used_tool_focus: bool = False,
    ) -> None:
        schema_chars, schema_tokens = self._schema_stats(routed)
        adjusted_map = {name: score for name, score in adjusted}
        ranked_plugins: list[dict[str, float | str]] = []
        for name, raw in sorted(raw_scores.items(), key=lambda x: x[1], reverse=True)[:8]:
            ranked_plugins.append({
                "plugin": name,
                "raw": round(raw, 3),
                "adjusted": round(adjusted_map.get(name, raw), 3),
            })

        self._last_diagnostics[session_id] = RouteDiagnostics(
            policy_name=policy.name,
            match_mode=match_mode,
            matched_plugins=sorted(matched),
            routed_tool_count=len(routed),
            ranked_plugins=ranked_plugins,
            schema_chars=schema_chars,
            schema_tokens=schema_tokens,
            route_latency_ms=round(elapsed_ms, 1),
            segment_matches={k: sorted(v) for k, v in (segment_matches or {}).items()},
            used_session_carryover=used_session_carryover,
            used_tool_focus=used_tool_focus,
        )

    def _expand_to_fqns(self, plugin_names: Set[str]) -> Set[str]:
        """Expand a set of plugin names to fully-qualified tool names."""
        return expand_plugins_to_fqns(plugin_names, registry)

    def get_last_diagnostics(self, session_id: str) -> RouteDiagnostics | None:
        return self._last_diagnostics.get(session_id)

    async def register_plugin(
        self,
        name: str,
        tools: Dict[str, Callable],
        utterances: Optional[List[str]] = None,
    ) -> None:
        """Hot-register a plugin at runtime (e.g. Composio app connected).

        Falls back to utterance_cache (LLM generation) when no utterances are
        supplied and a description is available on the registered plugin.
        """
        plugin = registry.plugins.get(name)
        if plugin and not _is_routable(plugin):
            logger.info("ToolRouter: '%s' opted out of routing (routable=False)", name)
            return

        if not utterances and plugin:
            utterances = load_curated_utterances(name)

        if not utterances and plugin:
            from core.integrations.utterance_cache import load_or_generate
            sibling_summaries = [
                (plugin_name, candidate.description)
                for plugin_name, candidate in sorted(registry.plugins.items())
                if _is_routable(candidate)
            ]
            utterances = await load_or_generate(
                name,
                plugin.description,
                tools,
                llm_service=None,
                sibling_summaries=sibling_summaries,
            )

        if not utterances:
            logger.info(
                "ToolRouter: registered '%s' without utterances (%d tools, routable=no)",
                name, len(tools),
            )
            return

        try:
            vectors = await asyncio.to_thread(embedding_service.embed, utterances)
            self._utterance_vectors[name] = vectors
            logger.info(
                "ToolRouter: hot-registered '%s' (%d tools, routable=yes)",
                name, len(tools),
            )
        except Exception as e:
            logger.error("ToolRouter: failed to embed utterances for '%s': %s", name, e)

    def deregister_plugin(self, name: str) -> None:
        """Remove a plugin from the router (e.g. Composio app disconnected)."""
        self._utterance_vectors.pop(name, None)
        for session_id, plugins in list(self._pending_route_carryover.items()):
            remaining = plugins - {name}
            if remaining:
                self._pending_route_carryover[session_id] = frozenset(remaining)
            else:
                self._pending_route_carryover.pop(session_id, None)
        for session_id, plugins in list(self._session_focus.items()):
            remaining = plugins - {name}
            if remaining:
                self._session_focus[session_id] = frozenset(remaining)
            else:
                self._session_focus.pop(session_id, None)

    def clear_session(self, session_id: str) -> None:
        """Remove all session state on disconnect."""
        self._session_focus.pop(session_id, None)
        self._pending_route_carryover.pop(session_id, None)
        self._last_diagnostics.pop(session_id, None)

    def utterance_signature(self) -> str:
        """Stable hash of current routable plugin set for callers that cache by it."""
        keys = sorted(self._utterance_vectors.keys())
        return hashlib.sha1(",".join(keys).encode()).hexdigest()[:12]


# Global singleton — initialized in main.py after plugin load
tool_router = ToolRouter()
