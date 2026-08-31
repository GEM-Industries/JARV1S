# Tool Routing

**Status:** Built. Canonical for how plugins enter the per-turn `tools=` set.

The router is **candidate generation**. It does not choose the model, decide
whether a tool should be called, interpret dialogue, or pick a speaker
endpoint. Those belong to the agent, delivery, and presence.

## What is offered

Offered capabilities are provider `tools=` JSON schemas. The system prompt
does not list tool signatures.

| Set | Contents |
| :--- | :--- |
| **Always-on** | Explicit FQNs in `tool_router.ALWAYS_ON_FQNS`: `system.search_tools`, all `files.*`, `display.push_content`, `system.exec`, `search.web`, `profile.add_memory` / `remember` / `update_memory`, `agents.dispatch` / `resume` / `get_status` / `cancel_task` / `close` |
| **Routed** | Every tool from plugins that cleared the active policy |
| **Discovered next iteration** | `system.search_tools` hits and named `edit_tool` / `fqn` values from a successful result |

Disabled plugins are omitted. `PluginMetadata(routable=False)` stays out of
semantic matching; those tools are always-on or reached via `search_tools`.

`schema_tokens` on route diagnostics is measurement. The router does **not**
drop a match to shrink schema size. Plugin count is capped by `max_matched`.

## Plugin-level scoring

One embedding source, one scoring pass. Each routable plugin owns utterance
vectors from hand-written `metadata.utterances`, curated files under
`core/routing/utterances/`, or generated fallback from
`core/integrations/utterance_cache.py`.

```
route(utterance, session_id, policy):
  1. Split compound utterances on cheap task boundaries ("and", "then", commas).
  2. Embed one segment, or the segment batch for compound turns.
  3. Score each enabled routable plugin: max cosine over its utterance vectors.
  4. Add DECAY_BONUS (+0.10) only for plugins from the latest successful tool-call focus.
  5. Keep plugins above the policy threshold (voice: 0.74, top 2 per segment).
  6. Merge candidates, keeping up to max_matched (voice: 3).
  7. Fallback: if nothing matched, promote at most one plugin that clears the
     fallback threshold (voice: 0.70).
  8. If there is recent tool focus and no strong new-domain match, re-add focused plugins.
  9. On user policies, merge one-shot plugins retained from the latest delivered
     proactive turn, still under max_matched.
  10. Expand matched plugins to all of their enabled tool FQNs.
```

Max-pool (not centroid) keeps a narrow utterance like "archive that" from
being averaged away. Production policies live in `core/routing/policies.py`:
`voice_default`, `text_default`, `system_hint`. The eval runner imports the same
objects (`task be:eval-routing`).

## Continuity

| Mechanism | Seeded by | Lifetime | Purpose |
| :--- | :--- | :--- | :--- |
| Tool focus | Successful tool output | Until replaced or the connection closes | Keep the plugin used for an actual operation available for underspecified follow-ups |
| Delivered-route handoff | A live-routed `tell` or spoken `offer` that `mark_delivered()` | Next user route; restored from conversation `routed_tools` after a same-node reconnect | Keep capabilities already selected for a question or alert when the user replies elliptically |

Neither classifies the reply or forces a call. The current transcript still
routes normally. The router does not re-embed rendered assistant text, trigger
context, or acknowledgements. Silent `act` turns, suppressed/deferred offers,
no-audio delivery, and prefetched playback do not create handoff.

## Turn mapping

`ToolRouter` takes an utterance and a `RoutingPolicy`. The orchestrator chooses:

| Turn path | Router input | Policy | Session effects |
| :--- | :--- | :--- | :--- |
| Voice user | Final transcript | `voice_default` | Consumes pending handoff; records last route |
| Text or attachment | User text | `text_default` | Same handoff, wider plugin cap |
| Trigger / headless | Bounded hint: `message` + `instructions` + protocol context | `system_hint` | No session carryover |
| System turn without a hint | No semantic route | none | Always-on + discovery |
| Delegated background task | Dispatch prompt, session id = `task_id` | `voice_default` (no policy arg) | Isolated from the live connection |

`reply_grounding`, `source_event`, and `CURRENT_STATE` are prompt inputs, not
router inputs.

## Startup

`ToolRouter.initialize()` warms FastEmbed and embeds routable utterances. Startup
fails if no routing index can be built. Hot-register / deregister updates the
index when integrations connect or disconnect.

## Authoring

Hand-write utterances for high-frequency or easily confused plugins (scheduler,
calendar, Gmail, maps, Slack, Spotify, automations). Let the generator handle
long-tail connected apps. Promote generated phrases into metadata only when the
routing eval shows a repeated miss or confuser.

```python
metadata = PluginMetadata(
    name="weather",
    description="Get real-time weather with context and forecasts.",
    utterances=[
        "what's the weather like today",
        "will it rain tomorrow",
        "how hot is it outside",
        "do I need an umbrella",
        "what's the forecast for this weekend",
    ],
)
```

Opt out of routing with `routable=False`.

## What this replaced

Earlier slices used a prompt-text tool manifest (namespace prefix + full-density
tail) and, before that, per-tool description embeddings plus a `TOP_TOOLS` cap.
Those are gone. The model sees JSON schemas for the always-on set plus matched
plugins. Mounted-but-unrouted tools go through `system.search_tools`; unmounted
Composio catalog tools go through `composio.search_catalog`.
