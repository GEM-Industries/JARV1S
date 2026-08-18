# Action Runtime Migration

**Status:** Superseded for the return/protocol cutover (2026-08-13).
Structured `CapabilityCall`s, typed plugin returns, and CodeAct/prefix deletion
are done. Remaining follow-ons in this note — evidence contracts, `run_code`,
constrained local decoding — are still open.

**Date:** 2026-08-13  
**Goal:** One typed capability system, one model action protocol, and no
plugin-specific prompt folklore.

This replaces CodeAct as the default action language. Daily voice turns are
one or two named capability calls, not generated Python. `run_code` can wait
until traces show work that actually needs a program.

## Outcome

After this migration:

- requests to execute JARV1S-managed behavior enter the capability runtime
  only as schema-conforming `CapabilityCall` values;
- multi-step work chains ordinary calls through the same agent loop;
- every call produces the same structured outcome, trace, and UI events;
- plugin authors define typed inputs, typed outputs, and domain behavior;
- the model no longer needs instructions about live Pydantic objects, result
  access paths, `Success:` / `Error:` prefixes, UI render tools, or stdout;
- the text `<tool_call>` protocol, in-process executor, and scanner are gone.

Build it on one migration branch. Merge only after the old model protocol is
removed — not while text actions and structured calls are both selectable.

## Architectural Decision

`CapabilityCall(capability, arguments, call_id)` is the only request type for
invoking a JARV1S-managed capability. It is one variant of model output, not a
universal response envelope or wire format.

LLM adapters emit provider-neutral model events: text, reasoning, complete
wire tool calls, and any future media or provider-managed events. A complete
tool call becomes a `CapabilityCall` only after the registry decodes its
provider-safe name to an internal FQN. `CapabilityDispatcher` and everything
below it consume that one validated request type.

The initial implementation sends `tools=` through LiteLLM for cloud and local
OpenAI-compatible runtimes. Ollama, LM Studio, and llama.cpp may use different
chat templates or constrained decoding internally; that is an adapter/runtime
detail when they return a standard structured tool call.

An assistant model is action-capable only when a live setup probe produces a
structured tool call that resolves to a schema-conforming `CapabilityCall`
and accepts the corresponding tool result. Unsupported models are chat-only.
Static capability metadata is not consulted; a catalog miss must not skip the
probe.

A future local adapter may use server-enforced JSON Schema or grammar decoding
when `tools=` cannot produce reliable calls. It must use the registry's schemas
and emit the same complete tool-call event that resolves to `CapabilityCall`;
it must also preserve final text, no-action turns, parallel calls, and
tool-result continuation. Prompt-only JSON/XML, unconstrained extraction, and
a JARV1S-authored grammar compiler are not production fallbacks.

```text
Model
  -> LLM adapter
       tools= response or runtime-enforced constrained decoding
  -> ModelEvent
       text | reasoning | tool call | future media/provider event
  -> JarvisAgent tool loop
  -> registry name resolution
  -> CapabilityCall(call_id, capability FQN, arguments)
  -> CapabilityDispatcher
       resolve, bind, inject, authorize, invoke, normalize, record
  -> first-party plugin or MCP adapter
  -> CapabilityOutcome
       status, data, error, UI events, invocation record
  -> provider tool result + trace + frontend events
```

There is no `ActionRunner`, `DirectCall`, or `RunCode` type. `JarvisAgent`
owns one provider-neutral loop. A future `run_code` is just another
registered capability.

The catalog, dispatcher, and ledger already exist. This migration changes
how the model speaks to them, not the execution authority.

## Module Ownership

Keep the current package boundaries:

| Module | Owns |
|---|---|
| `core/plugins/capabilities.py` | Definitions, calls, outcomes, invocation context, ledger |
| `core/plugins/registry.py` | Catalog, schema derivation, provider-safe name encoding |
| `core/plugins/dispatcher.py` | Resolve, validate, inject, authorize, invoke, normalize, trace |
| `core/decorators.py` | `@tool` metadata only |
| `core/plugins/result.py` | Optional `ToolResult` authoring envelope |
| `core/plugins/ui.py` | UI event publication |
| `core/llm/types.py` | Provider-neutral model events and tool-result messages |
| `core/llm/adapters/*` | Wire transport and complete tool-call assembly |
| `core/agent/agent.py` | The single model/tool loop |
| `core/turns/execution.py` | Turn delivery and normalized traces |
| `core/tool_router.py` | Per-turn active FQN set |

Do not add a new runtime package. Delete the scanner and executor after the
structured action loop is the only model path.

## Target Contracts

### Capability call

Use one vocabulary throughout the runtime:

- `CapabilityDefinition`: registered contract and implementation metadata;
- `CapabilityCall`: resolved request to invoke a JARV1S-managed capability;
- `CapabilityOutcome`: normalized execution result;
- `InvocationRecord`: durable audit span for the attempted call.

`CapabilityCall` is the only request accepted by the capability execution
layer:

```text
call_id       provider id, or an adapter-generated stable id
capability    internal capability FQN
arguments     JSON object validated against that capability's input schema
```

Text, reasoning, generated media, citations, and provider-managed operations
are model output events, not `CapabilityCall` variants. A computer-use, MCP,
web, or media operation becomes a capability call only when JARV1S owns its
execution. Provider-managed operations remain adapter events and must not be
reported as dispatcher invocations.

For constrained decoding, the response schema must be generated as a
discriminated union of the active capability definitions so the encoded tool
name selects the matching argument schema. A generic
`{name: string, arguments: object}` envelope guarantees JSON shape but not a
valid capability call.

Adapters assemble and validate the wire response; the registry resolves names;
the dispatcher validates arguments again at the authority boundary. Grammar
constraints prevent malformed shape. They do not establish authorization,
mutation safety, correct target selection, or truthful outcomes.

### Capability definition

`CapabilityDefinition` is the source of truth. The registry derives, once:

- internal FQN and provider-safe encoded name;
- input and output JSON schema;
- concise description;
- injected dependency names;
- raw implementation;
- source and enabled state.

Every caller consumes this definition. No adapter or scanner reconstructs a
schema independently. The registry owns name encoding (`jarvis.calendar.get_events`
must round-trip through providers that forbid dots) and adapters never invent
names.

`visible_signature` may stay as a Python binding detail. It is not a second
model contract.

### Request tools

With the initial LiteLLM adapter, each action-capable model iteration receives
`tools[]` for the routed FQNs plus always-on core tools. Any future constrained
local adapter receives the same active definitions encoded from the same
registry schemas. `system.search_tools` remains the escape hatch for
capabilities not routed this turn. It is not how lights, weather, or reminders
are found.

ToolRouter budgets against JSON schema tokens, not scanner prose.

### Capability outcome

`CapabilityDispatcher` returns one internal `CapabilityOutcome`:

```text
call_id
capability
status        succeeded | failed | blocked | interrupted | not_executed
data          JSON-safe structured value, if any
error         typed code, message, and details for non-success outcomes
ui_events     side-channel presentation
invocation    durable trace record
```

Domain receipts stay domain-owned. The dispatcher serializes Pydantic values
once. Approval and reauthentication are blocked outcomes; unexpected
exceptions are failures.

Provider adapters serialize that outcome to the provider's tool-result
format. Internally it stays structured.

During plugin migration, the dispatcher may still interpret leftover
`Error:` / `Success:` / `APPROVAL_NEEDED:` / `REAUTH_NEEDED:` / `SKIPPED:`
strings. That is not a second model protocol. Remove the interpreter when
no public tool returns those prefixes.

### Plugin authoring

```python
@tool(inject=["calendar"])
async def get_events(
    self,
    start_date: date,
    end_date: date | None = None,
    *,
    calendar: CalendarClient,
) -> EventQueryResult:
    """Find calendar events in the requested range."""
```

- one typed visible signature;
- a one-sentence selection description;
- a typed domain value on success;
- a typed capability error when the request cannot be completed;
- `ToolResult(content=..., ui=[...])` when the model needs text and the
  frontend needs a widget;
- domain normalization, evidence, scope, and mutation safety in code.

The dispatcher unwraps `ToolResult` into `data` / model content / `ui_events`.
Do not add a separate `emit_ui()` requirement, result-path instructions,
`manifest="full"`, or `render_*_widget` follow-up tools.

### Agent loop

Rewrite `JarvisAgent.process_stream()` in place:

- stream text and reasoning as today;
- consume only complete tool-call events from the adapter, resolve them to
  `CapabilityCall`, and never execute streaming deltas;
- run every tool call from one assistant message before the next inference;
- dispatch each call through `CapabilityDispatcher`;
- append the adapter's structured tool-result message;
- continue until the model returns final text.

If the model emits text before tools, speak it and speech-gate as today.
If it emits tools with no preamble, keep the thinking/working cue — do not
require a spoken acknowledgement.

Persist name, arguments preview, outcome, and invocation id. Do not
synthesize `<tool_call>` / `<tool_result>` XML. Evals assert tool name and
arguments, not Python.

A blocked call is a terminal result for that invocation. The turn may end
on a pending-approval widget; the model must not observe a fake success.

## Migration Work

### 1. Structured calls through the existing dispatcher

Change: `capabilities.py`, `dispatcher.py`, `registry.py`, `decorators.py`,
`core/llm/*`, `agent.py`, `turns/execution.py`.

- add input schema and provider-safe names to `CapabilityDefinition`;
- move injection and auth normalization into the dispatcher; `@tool` is
  metadata;
- introduce `CapabilityOutcome`; unwrap `ToolResult` and leftover string
  prefixes there;
- resolve complete provider tool calls into `CapabilityCall`;
- pass routed schemas as `tools[]`;
- probe the complete action/result round trip at setup and mark unsupported
  models chat-only.

Do not delete `ToolResult` or `CodeExecutor` in this step.

Gate: weather, light control, reminder creation, calendar mutation, a
chained read/write, and `search_tools` discovery all complete without
generating Python. Foreground, system, headless, and
`dispatch(mode="jarvis")` share this loop.

### 2. Shrink prompts

Delete the scanner, CodeAct protocol examples, `manifest="full"`,
return-path instructions, and success-prefix guidance.

Keep semantic routing, one-line capability descriptions, `search_tools`,
runtime facts, and global voice/safety policy.

Gate: adding a typed plugin requires no global prompt edit.

### 3. Migrate plugin returns

Start with `calendar`, `scheduler`, `gmail`, `smart_home`, and `todo`.
Then the remaining first-party tools, then MCP.

- typed reads stay domain-owned Pydantic values;
- mutations return small typed receipts or `ToolResult` with a receipt;
- expected failures become typed capability errors;
- approval/auth become blocked outcomes;
- delete public `render_*_widget` tools that exist only for refresh.

Gate: no public plugin return relies on string-prefix interpretation.

### 4. Delete the legacy path

After the structured action loop is the only model protocol and prefix sniffing is
gone:

- delimiter parsing and XML history synthesis;
- `CodeExecutor` and stdout as the observation contract;
- `__UI_UPDATE__` / `__UI_DELETE__` markers;
- `render_*_widget` fallback in `ui_handler.py`;
- `format_tool_output()`;
- scanner;
- `focus_tools` as a trace fallback;
- obsolete CodeAct tests.

## Follow-on

**Programmatic composition.** Add `run_code` only when traces show repeated
tasks that need loops, filtering, fan-out, or large intermediates kept
outside model context. It is one capability over a real sandbox, not a
second action protocol, and not an adaptation of today's in-process
executor.

**Constrained local decoding.** Do not build this in phase 1. Add an adapter
only if setup probes and real traces show useful local models that cannot
produce reliable structured calls through `tools=`. Prefer a runtime's
server-enforced `response_format` / JSON Schema support. The adapter must
emit the same complete tool-call event and pass the same action-loop tests; do
not parse prompt-shaped JSON or maintain model-specific grammars in JARV1S.

**Compiled routines.** After structured calls are stable, deterministic
protocol/trigger steps can become validated capability calls through the
same dispatcher. Keep natural-language instructions where fire-time
judgment is intended. Do not design a workflow DSL in this migration.

## Verification

- dispatcher: schema binding, outcome status, injection, blocked states,
  ledger completeness;
- adapter: streamed tool-call assembly and tool-result messages, including
  name encode/decode;
- action loop: direct calls, parallel calls from one response, chaining,
  discovery, blocked outcomes, preamble-optional voice gating;
- existing calendar/scheduler safety tests;
- P0 agent behavior evals rewritten to name + arguments.

Measure before and after: prompt and tool-schema tokens, first-action and
whole-turn latency, invalid call-shape rate, false success/absence claims,
calls and model iterations per turn.

Done when:

1. all action callers use `CapabilityDispatcher`;
2. all model observations are structured outcomes;
3. atomic voice actions do not execute Python;
4. plugin contracts need no output-access prompting;
5. legacy action, stdout, UI-marker, and string-sentinel code is gone;
6. architecture docs describe only this path — including reversing the
   CodeAct-only decision in `PLUGIN_ARCHITECTURE.md`.

## Documentation Cutover

When the code lands: `VISION.md`, `ARCHITECTURE.md`,
`PLUGIN_ARCHITECTURE.md`, `.cursor/rules/plugin-tool-conventions.mdc`,
`.cursor/skills/build-jarvis-plugin/SKILL.md`, and `BACKGROUND_AGENTS.md`.
Mark older tool-result cleanup proposals superseded.

## Reuse existing libraries

Do not add an agent framework. Squeeze more from what is already in
`pyproject.toml`.

| Job | Use | Do not write |
|---|---|---|
| Initial action transport, streaming deltas, name sanitization, orphaned tool-result repair | LiteLLM `acompletion(..., tools=)` for cloud and supported local runtimes — `modify_params = True` is already on | A per-provider tool codec |
| Setup probe | Live `probe_action_capability()` tool-call/result round trip; persist `action_capable`. Re-probe on startup unless already `True`. LiteLLM `supports_function_calling` catalogs are not a gate (they false-negative Gemma 4 on Cerebras and similar OpenAI-compatible hosts). | A hardcoded provider allow-list or trust in static metadata alone |
| Input JSON schema from the visible signature | Pydantic `create_model` + `TypeAdapter.json_schema`, same path as today's return schemas | A schema compiler |
| Bind/validate arguments | That generated model, or `visible_signature.bind` | A second validator |
| JSON-safe outcomes | Existing `tool_output_data()` | A new serializer |
| OpenAI tool dict shape | `openai` types already imported (`ChatCompletionFunctionToolParam`) | Hand-rolled tool JSON |
| Evidence-backed local constrained decoding | The local runtime's JSON Schema / grammar API, behind an LLM adapter | Client-side grammar generation or prompt-only JSON extraction |

The agent loop, dispatcher, ToolRouter, consent, speech gate, and traces
stay JARV1S-owned. Those are the product, not missing packages.

### Runtime evidence

- [LiteLLM's Ollama adapter](https://docs.litellm.ai/docs/providers/ollama)
  accepts `tools=` and may normalize non-native local calls through JSON mode.
- [Ollama](https://docs.ollama.com/capabilities/tool-calling) and
  [LM Studio](https://lmstudio.ai/docs/developer/openai-compat/tools) expose
  OpenAI-compatible tool calls; both also expose schema-constrained output.
- [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)
  combines tool-aware chat templates with grammar-constrained arguments behind
  an OpenAI-compatible response.
- [vLLM structured outputs](https://docs.vllm.ai/en/stable/features/structured_outputs/)
  supports JSON Schema and grammar backends. These are viable future adapter
  mechanisms, not a reason to fork the phase-1 loop.

## Do Not Add

- Pydantic AI, OpenAI Agents SDK, LangGraph, or LiveKit Agents as the
  voice loop — they own orchestration you must keep;
- `litellm.add_function_to_prompt` — that is a second text action grammar;
- the official MCP Python SDK solely to replace the small JSON-RPC client;
- a second registry, dispatcher, consent path, or trace format;
- `ActionRunner` / `DirectCall` / `RunCode` as peer action types;
- a smarter scanner;
- a universal domain search-result or mutation base class;
- provider-specific schemas handwritten outside `CapabilityDefinition`;
- a production fallback to text-delimited tool calls;
- prompt-only or unconstrained JSON action extraction;
- a JARV1S-authored GBNF / JSON Schema compiler;
- a compatibility adapter from structured calls back into `CodeExecutor`;
- `search_tools` as the common voice discovery path;
- a required spoken acknowledgement before the first action call;
- a workflow framework before repeated routines justify compiled actions.
