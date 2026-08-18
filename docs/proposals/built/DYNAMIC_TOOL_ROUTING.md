# Tool Routing Architecture

**Status:** Built — current architecture after the Phase 8.5 cleanup.
**Original proposal date:** 2026-02-08
**Updated:** 2026-07 (budget-aware multi-intent routing, tool focus, delivered proactive handoff, two-density manifest)

---

## Problem

Three bottlenecks collide when scaling to 100+ tools:

1. **Context Budget** — Tool definitions are ~100-200 tokens each. 100 tools = 15-20K tokens of prompt. LLM accuracy degrades and time-to-first-token scales linearly with input size.
2. **Routing accuracy** — Generic tool descriptions sit in a different semantic region of vector space than natural user speech ("semantic gap problem"). Tool2Vec research shows utterance-based embeddings achieve ~39% better recall than description-based approaches.
3. **Latency vs Precision** — Loading all tools is fast but inaccurate. LLM-based meta-tool routing is accurate but adds 2-4s.

---

## Implemented Solution: Two-Density Manifest + Plugin-Level Routing

The router is **candidate generation for the tool manifest**. It does not choose
the model, decide whether a tool should be called, interpret dialogue, or route a
notification to a physical endpoint. Those boundaries belong to delivery/session
routing, not tool selection.
the main agent with conversation history, and the trigger delivery pipeline
respectively.

### Two-Density Manifest

Every enabled plugin appears in the manifest every turn. Density depends on whether the plugin matched this turn:

| Density | Where | Format | Assigned to |
|---|---|---|---|
| **Namespace** | Stable prefix | One plugin line: `jarvis.<plugin>: <description>` | Every enabled plugin |
| **Always full** | Stable prefix | Signature + full docstring + Pydantic return schema hint | Tools opting in via `@tool(manifest="full")` |
| **Routed full** | Per-turn tail | Signature + full docstring + Pydantic return schema hint | All tools from routed plugins, except always-full duplicates |

The stable prefix is turn-invariant and cached by `_enabled_signature()` so it sits in the cacheable half of the system prompt. It does **not** list every tool signature. Non-routed tools are visible only as part of their plugin namespace unless they opt into `@tool(manifest="full")` or are discovered through `jarvis.system.search_tools()`. The routed tail is per-turn and lives in the dynamic half.

`jarvis.system.search_tools()` is the escape hatch for mounted tools whose plugin was not routed. `jarvis.composio.search_catalog()` remains the slower remote fallback for tools not mounted locally.

### Plugin-Level Routing

One embedding source, one scoring pass. Each routable plugin owns a list of utterance vectors from hand-written `metadata["utterances"]`, source-controlled curated phrases under `core/routing/utterances/`, or generated fallback from `core/integrations/utterance_cache.py`. Per-tool description embeddings were dropped in Phase 8.5 — the signal-to-noise was poor on MCP/Composio descriptions and the tail-bloat caused weak fast-path models to stall.

```
route(utterance, session_id):
  1. Split obvious compound utterances on cheap task boundaries ("and", "then", commas).
  2. Embed one segment for simple turns, or the segment batch for compound turns.
  3. For each enabled routable plugin: score = max(cosine(query, v) for v in plugin_utterances).
  4. Add DECAY_BONUS (+0.10) only for plugins from the latest successful tool-call focus frame.
  5. For each segment, keep the top few plugins above the selected policy threshold (voice default: 0.74, top 2 per segment).
  6. Merge candidates by adjusted score until the routed manifest tail reaches the budget.
  7. Fallback: if no primary match, promote only the best plugin when it clears the selected policy fallback threshold (voice default: 0.70).
  8. If there is recent successful tool focus and the current route has no strong new-domain match, add focused plugins back into the budgeted candidate set.
  9. On user policies only, merge any one-shot plugins retained from the latest
     successfully delivered proactive turn, subject to the same plugin and tail budgets.
  10. Promote ALL tools from selected plugins to the full-density tail.
  11. Persist matched set for diagnostics/read-only last-routed views.
  returns: set[str] of "plugin.tool_name" FQNs
```

**Why max-pool (not centroid).** Max-over-utterances preserves narrow intents — a single utterance like "archive that" doesn't get averaged away by unrelated utterances in the same plugin. The canonical `voice_default` policy uses `threshold=0.74`, `fallback_threshold=0.70`, max 3 plugins total, top 2 per segment, and a 16K-character rendered-tail budget. The fallback path is deliberately narrow: at most one plausible namespace clears the fallback floor; casual or ambiguous utterances below that floor promote nothing.

### Conversation continuity

There are two distinct continuity mechanisms:

| Mechanism | Seeded by | Lifetime | Purpose |
|---|---|---|---|
| Tool focus | Successful tool output | Latest focus remains until replaced or the connection closes | Keep the plugin used for an actual operation available for underspecified follow-ups. |
| Delivered-route handoff | A live-routed proactive `tell` or spoken `offer` that settles as delivered | Consumed by the next user route; restored from the latest eligible same-node assistant row after reconnect | Keep capabilities already selected for a question or alert available when the user replies elliptically. |

Neither mechanism classifies the reply or forces a call. The current user
transcript still routes normally; selected plugins are merged under the active
manifest budget, and the main agent uses conversation history to decide what the
reply means. The router does not re-embed rendered assistant text, trigger
context, or acknowledgement phrases.

The delivered-route handoff stores plugin identity, not prompt provenance. Its
live fast path is keyed by `connection_id`; the settled conversation row's
existing `routed_tools` can restore it for the first reply after a same-node
reconnect. A reply on another node does not inherit it. Silent `act` turns,
suppressed/deferred offers, no-audio or TTS-failed delivery, local fixed
responses, and prefetched protocol playback do not create it. Trigger settlement
is authoritative: carryover is recorded only when `mark_delivered()` actually
transitions the instance. System turns do not consume a pending handoff; the
next routed voice/text user turn does.

**Tool focus, not phrase matching.** The router does not maintain regexes or verb
lists for referential follow-ups. Successful tool calls are recorded by the
orchestrator as the latest connection-keyed plugin set. Focused plugins receive
the standard decay bonus and can be re-added unless the current utterance
strongly routes to a different domain. Tool outputs and object identifiers are
not parsed or stored by the router.

**Startup contract.** `ToolRouter.initialize()` warms the local FastEmbed ONNX model, embeds routable plugin utterances, and fails startup if no routing index can be built. The runtime must not mark itself ready with an empty embedding index.

**Recall over no-tool precision.** The router is a semantic tool surfacer, not a final "should a tool be called?" classifier. A no-op turn like "thanks" after a successful tool call may still include the prior tool's plugin in the manifest tail. The model can choose not to call it, and destructive/context-bound tools must validate identifiers or resolvable focus before executing. This avoids an infinite regression of phrase classifiers while preserving follow-up recall.

### Turn and delivery boundaries

`ToolRouter` accepts an utterance and a `RoutingPolicy`; it does not inspect
delivery state. The orchestrator owns the mapping:

| Turn path | Router input | Policy | Session effects |
|---|---|---|---|
| Voice user | Final transcript | `voice_default` | Consumes pending delivered-route handoff; records last route. |
| Text or attachment user | User text, when present | `text_default` | Same handoff behavior with a wider plugin cap. |
| Trigger system / headless | Bounded `routing_hint` combining `message`, `instructions`, and protocol context | `system_hint` | Policy disables session carryover; does not consume user handoff or create tool focus. |
| System turn without a hint | No semantic route | none | Relies on always-full tools, protocol context, or explicit discovery. |
| Delegated background task | Dispatch prompt, keyed by task ID | default bounded policy | Task-scoped session id; isolated from live connection continuity. |

For triggers, `reply_grounding`, `source_event`, `CURRENT_STATE`, and rendered
assistant output are prompt inputs, not router inputs. The explicit routing
surface combines only action message, instructions, and protocol steps, so
generic delivery policy cannot hide the domain-bearing message. A successful
delivery may retain that route result afterward, but the router never infers a
domain from unrelated prompt fields.

### Latency Budget

| Step | Latency |
|------|---------|
| Embed query (fastembed, thread pool) | ~20-50ms |
| Cosine similarity scan (~15 plugins, ~10 utterances each) | <5ms |
| **Total routing overhead** | **~25-55ms** |

LLM-based meta-tool routing = 2-4s. Semantic routing is ~40-80x faster.

---

## ToolRouter Implementation (`core/tool_router.py`)

```
ToolRouter:
  _utterance_vectors: dict[plugin_name, list[vec]]   # one vec per utterance, NOT a centroid
  _session_focus:     dict[session_id, frozenset[plugin_name]]  # latest successful tool-call plugins
  _pending_route_carryover: dict[session_id, frozenset[plugin_name]]  # one user route
  _last_diagnostics:  dict[session_id, RouteDiagnostics]

  initialize(llm_service)        — embed every routable plugin's utterances at startup
  route(utterance, session_id, policy) — budget-aware plugin-level max-pool scoring; returns set[fqn] for the tail
  record_tool_focus(session_id, tools) — record successful tool-call plugins for follow-up recall
  record_route_carryover(session_id, tools) — retain delivered proactive plugins for one user route
  register_plugin(name, tools, utterances=None)   — hot-register at runtime
  deregister_plugin(name)        — remove on disconnect
  get_last_diagnostics(session_id) — public read-only view of the last route decision
  clear_session(session_id)      — called on WebSocket disconnect
  utterance_signature()          — stable hash of routable plugin set (for cache keys)
```

`PluginMetadata(routable=False)` opts a plugin out of routing entirely (it stays in the namespace prefix, never gets promoted to the tail). Hot-registration / deregistration also invalidates the manifest prefix cache via `scanner.invalidate_prefix_cache()`.

Offline policy checks live in `backend/tools/evaluate_tool_routing.py` with labels in `backend/evals/tool_routing.yaml`. The runner imports the same canonical policy objects as production (`baseline`, `voice_default`, `text_default`, `system_hint`) plus optional threshold/cap sweeps. Use `task be:eval-routing` for the production voice check and `--sweep` for voice-path policy tuning.

Route diagnostics are intentionally compact outside active logs: policy name, match mode, matched plugins, routed tool count, tail tokens, route latency, and hard flags such as tool-focus use. Full ranked score lists are not part of the persisted `turn_runs` contract.

---

## What This Replaced (Phase 8.5 cleanup)

The earlier system used a hybrid pass: utterance centroids per plugin, plus per-tool description embeddings (`_tool_vectors`) for MCP/Composio orphan tools, capped at `TOP_TOOLS=12`. Pre-8.5 also used a three-tier framing (Always-On / Routed / Available) where everything else was hidden.

Phase 8.5 collapsed this to plugin-level only:

- Per-tool description embeddings, `TOOL_THRESHOLD`, `TOP_TOOLS`, and orphan scoring all removed from `tool_router.py`.
- Composio's per-tool index moved into `composio_meta.search_tools` (keyed by plugin-set signature) — the only place that ever needed it.
- The "Tier 3 / Available" hidden bucket is gone: every enabled plugin is in the namespace prefix, every matched plugin is full-density in the tail. Mounted tools outside the routed tail are reached via `system.search_tools`; unmounted Composio catalog tools are reached via `composio.search_catalog`.

---

## Plugin Authoring

Plugins declare `utterances` in metadata for best routing accuracy:

```python
class WeatherPlugin(JarvisPlugin):
    @property
    def metadata(self):
        return {
            "name": "weather",
            "description": "Real-time weather and forecasts.",
            "utterances": [
                "what's the weather like today",
                "will it rain tomorrow",
                "how cold is it",
                "weekly forecast",
            ],
        }
```

If `utterances` is omitted, the router first checks source-controlled curated phrases under `core/routing/utterances/`. If none exist, `core/integrations/utterance_cache.py` supplies generated utterances. It checks the disposable disk cache (`backend/.cache/utterances/<plugin>.json`), then runs a cached LLM generation pass when an LLM service is available, and finally falls back to deterministic query-like phrases from tool names/docstrings when offline or when generation fails. The LLM prompt includes nearby plugin names/descriptions so generated positives avoid obvious confusers such as scheduler vs automations or search vs maps. Hand-written and curated utterances still win for voice-critical or high-confusion plugins.

Recommended split:

- Hand-write utterances for high-frequency or easily confused plugins: scheduler, calendar, Gmail, maps, Slack, Spotify, automations.
- Let the generator handle lower-risk internal plugins and long-tail connected integrations.
- Promote generated utterances to hand-written metadata only when the routing eval shows a repeated false negative or confuser.

To opt out of routing entirely (plugin stays namespace-only, never promoted):

```python
metadata = {"name": "myplugin", "routable": False, ...}
```
