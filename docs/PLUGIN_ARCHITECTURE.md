# Plugin and Tool Architecture

This is the canonical contract for JARV1S plugins and the capability runtime
around them. It defines the boundary; authoring details remain in
`.cursor/rules/plugin-tool-conventions.mdc`.

This document describes both the boundary JARV1S preserves and the target runtime
needed to enforce it. The production loop emits structured capability calls
through one dispatcher and returns JSON-safe observations to the model.
Process isolation remains a later target listed in **Build Next**. See
[`ARCHITECTURE.md`](./ARCHITECTURE.md) for the turn pipeline.

## Decision

JARV1S uses **structured capability calls as its model-facing action language**.
The model emits named JSON arguments against typed capabilities. Adapters keep
provider events distinct; a wire tool call becomes a `CapabilityCall` only after
registry name resolution.

A plugin is the typed domain-capability boundary; cross-cutting execution policy
belongs to the harness. MCP tools and first-party plugins must enter the same
capability contract. MCP is an integration protocol, not a second agent
architecture.

Target runtime:
```text
Model
  -> provider-neutral model events
  -> CapabilityCall
  -> isolated execution runtime
  -> capability dispatcher
       schema validation, identity, permission, consent, tracing, budgets
  -> first-party plugin or stateless MCP adapter
  -> typed observation and verified receipts
```

Current runtime:
```text
Model -> LiteLLM tools= -> complete wire tool calls
      -> registry name resolution -> CapabilityCall
      -> CapabilityDispatcher (validate, invoke, ledger)
      -> plugin / MCP adapter
      -> JSON-safe observation + harness invocation ledger in turn/task traces
```

UI widget actions call the same dispatcher for validation, but do not open a
turn ledger; turn/task tracing remains owned by the agent loop.

The agent loop stays simple: generate, execute, observe, and repeat. Complexity
belongs in capability contracts, context management, permissions, and
verification—not orchestration heuristics.

## What a Plugin Is

A plugin exposes one coherent user capability, such as calendar, smart home, or
Gmail. It is not a mirror of a provider API and not a hard-coded workflow.

A plugin owns:

- **User-shaped operations.** Tool names and parameters reflect how people ask
  for the capability. Common intent should be expressible in one correct call.
- **Domain interpretation.** Normalize ordinary dates, units, aliases, relative
  intent, and other concepts with a clear domain mapping.
- **Domain truth.** Valid actions, fields, identifiers, mutation scope, provider
  capabilities, and persistence invariants live with the plugin.
- **Target resolution.** Lookups return plausible concrete targets with the
  exact stable identifiers accepted by downstream mutation tools.
- **Honest evidence.** Results expose the scope actually searched, ambiguity,
  truncation, and partial provider failure whenever those facts affect whether
  an absence claim is valid.
- **Safe mutation.** Validate the complete intended state before writing,
  resolve risky targets before consent, and return an explicit confirmed
  outcome. Retryable writes need domain-owned idempotency or duplicate guards.
- **Provider adaptation.** Authentication, payloads, pagination, retries, and
  provider errors stay in integration clients behind the public tool.

A plugin does not own:

- conversation history, model routing, prompt assembly, or context compaction;
- cross-plugin execution policy, generic permissions, or tracing;
- delivery channels or frontend transport;
- generated plans for rare multi-tool tasks—the agent loop chains ordinary
  capability calls;
- generic abstractions added only because several plugins happen to use MongoDB
  or return lists.

Canonical entry points reduce overlapping tool choice:

- `scheduler` owns concrete reminders, timers, alarms, and timed deferred work,
  including instance-versus-series edits.
- `automations` owns rules triggered by external domain events; `rules.create`
  is the user-shaped facade for conditional trigger/action plans.
- `setups` inventories and delegates lifecycle for managed definitions; it does
  not replace domain creation or editing tools.

Public tools are user-shaped jobs, not provider endpoints. MCP and Composio
are providers until a bespoke plugin of the same name wins. UI is a side
channel of the domain tool: reads and mutations that have a widget attach it
themselves; do not add a second `render_*` decision for the same data.

The current cross-plugin conformance audit and prioritized gaps live in
[`PLUGIN_CONFORMANCE.md`](./PLUGIN_CONFORMANCE.md).

## Tool Contract

Every public first-party tool is an `@tool` method with a typed visible
signature. Dynamic adapters such as MCP must expose equivalent capability
metadata even when their callables are generated rather than decorated.

The mounted callable, provider `tools=` schema, dispatcher validation, and
trace identity must derive from one capability definition. An advertised tool
must be callable under the same name and signature. Unknown tools, invalid
arguments, or stale call shapes must produce a recoverable error containing the
current callable name and parameter contract; they must not disappear into an
opaque executor exception.

### Reads

Return a small Pydantic model or list of models containing only fields useful to
the likely next action. Include human labels, stable downstream identifiers,
scope, and status.

When a lookup also needs evidence, return a domain-owned result with a named
item list (`events`, `emails`, `alerts`) plus `match_status` / `coverage`.
The model observes that object as a JSON-safe structured result and reads the
named field (`result.events`). Do not override iteration or indexing so the
envelope behaves like its item list. A plain `list[Model]` is correct only when
absence needs no coverage evidence.

Lookup tools should accept targeted user-shaped filters rather than requiring
the model to fetch a mixed inventory and invent domain classification. When a
negative answer matters, the return must distinguish:

- no matching object in a complete search;
- ambiguous candidates;
- incomplete coverage, truncation, or provider failure.

Use the shared two-axis vocabulary from
`core.plugins.read_evidence`:

- `match_status` (`none` | `single` | `multiple`) — returned candidate
  cardinality;
- `coverage` (`complete` | `partial`) — whether absence claims are
  authoritative.

Truncation, failed providers, and failed message fetches are domain-owned
reasons for `coverage=partial`, not additional coverage values. Only claim
absence when `coverage=complete` and `match_status=none`. A successful partial
read remains an invocation success; total call failure still raises or returns
an actionable `CapabilityErrorDetail`.

An unconfigured integration, reauthorization requirement, timeout, or provider
failure is not an empty result. Provider adapters own bounded retries and
response validation; the plugin preserves any partial-coverage status needed to
interpret the result honestly.

The concrete result model remains domain-owned; reuse the shared enums and
`match_status_from_count()` helper rather than inventing a universal
search-result envelope or base class.

### Mutations

Mutations treat model-generated arguments as untrusted input:

1. Resolve or fetch the target.
2. Validate scope, expected guards, and the full intended state.
3. Apply consent at the domain edge when required.
4. Perform the side effect.
5. Return a domain confirmation or prefix-free `ToolResult` only from provider
   or durable-store confirmation; otherwise return `CapabilityErrorDetail` or
   raise an unexpected failure.
6. Return the resulting stable identifier and relevant state when a follow-up
   may need them.

UI is a side channel, not evidence that an operation succeeded. Detailed return
shapes, docstring style, normalization, persistence, and consent recipes belong
in [`.cursor/rules/plugin-tool-conventions.mdc`](../.cursor/rules/plugin-tool-conventions.mdc).

## Observation Contract

The model observes JSON-safe `CapabilityOutcome` values, not generated Python
or stdout. Task-level composition happens by chaining ordinary capability
calls through the same agent loop:

- selecting among returned candidates;
- joining and filtering typed data;
- parallel or repeated reads from one assistant message;
- branching on explicit result state;
- sequencing verified operations.

Domain receipts stay domain-owned. The dispatcher serializes Pydantic values
once. Approval and reauthentication are blocked outcomes; unexpected
exceptions are failures.

The model must not own:

- access control or consent;
- provider credentials or raw networking;
- the meaning of domain-specific relative intent;
- authoritative absence from a bare empty list;
- whether an unobserved side effect succeeded.

Every `jarvis.*` invocation passes through one dispatcher. A structured call
produces both its observation and a harness-owned invocation ledger containing
tool identity, bounded redacted arguments, terminal status, duration, error
type, and consent linkage. Every attempted invocation closes as succeeded,
failed, blocked, interrupted, or not executed. The ledger is persisted inside
existing turn and task `tool_result` trace metadata.

Final responses remain model-generated, but the harness must make unsupported
claims difficult: mutation receipts and search-coverage evidence are injected
as structured observations.

## Harness Responsibilities

### Implemented foundations

The current runtime provides:

- a stable `jarvis.*` namespace with plugin-level semantic routing and explicit
  tool discovery;
- a registry-owned **capability catalog** (`CapabilityDefinition` per mounted
  tool: FQN, visible signature, docs, implementation, source, input schema).
  Provider `tools=` payloads, proxies, validation, and traces all read this
  catalog — it is not a second plugin system;
- a shared **`CapabilityDispatcher`** gate for first-party, MCP, eval, and UI
  calls (resolve, bind args, invoke, record);
- an in-memory per-run invocation ledger persisted through turn/task traces
  (`tcall-` / `inv-` IDs, status, redacted args, consent linkage);
- typed first-party tool metadata, integration injection, and domain-owned
  consent primitives;
- turn and task identity, durable approvals and task state, bounded capability
  iterations, call timeout, and token-aware history trimming;
- turn-level traces and persisted capability outcomes.

MCP tools are mounted eagerly today; only their inclusion in the per-turn
`tools=` set is lazy via routing and `search_tools`.
Direct turns and `dispatch(mode="jarvis")` use `jarvis.*`;
`dispatch(mode="code")` is a separate isolated coding runtime.

### Target enforcement

The target runtime must enforce these responsibilities end to end:

- **Isolation.** Generated code runs without ambient filesystem, process,
  network, environment, credential, or import access. Its only capabilities are
  typed proxies granted by the dispatcher.
- **Permissions outside code.** Identity, trust level, plugin enablement,
  consent, rate limits, and side-effect policy are enforced for every nested
  invocation.
- **Progressive disclosure.** Keep plugin-level routing, per-turn `tools=`
  schemas, and explicit tool discovery. Large MCP catalogs remain lazy.
- **Structured observations.** Preserve typed values and per-call outcomes;
  stdout is diagnostic output, not the execution contract.
- **Budgets and recovery.** Enforce turns, time, output, tool calls, fan-out,
  and context budget inside the loop. Compact or offload state before dropping
  it, and retry only bounded transient failures.
- **Explicit durable state.** Conversations, tasks, approvals, artifacts, and
  long-running application state live outside model context. Stateful services
  return explicit handles that later calls must pass back.
- **Traceability.** Correlate each model iteration, code run, nested tool call,
  approval, provider error, and final delivery under the turn or task ID.

Stateless MCP is the preferred remote integration boundary. JARV1S still owns
identity, permissions, context, consent, and durable workflow state. Remote
servers receive only the context required for a call. Stateful services return
explicit resource or task handles that later calls pass back rather than hiding
workflow state in the transport session.

## Improvement Contract

Production traces are evidence, not just logs:

1. Preserve the terminal outcome, causal tool behavior, and harness component
   implicated by each failure. Distinguish tool-selection, call-shape,
   validation, integration/auth, provider, policy, and runtime failures.
2. Cluster repeated failures before changing prompts or tools.
3. Make the smallest edit to one explicit surface: tool contract,
   implementation, dispatcher, context, prompt, routing, or eval.
4. Record the expected improvement and likely regression.
5. Validate against the original failure, adjacent cases, passing behavior that
   must be preserved, and held-out cases.

Prompts and docstrings must not accumulate transcript-specific patches.
Permission controls and evaluators remain outside any future automated harness
improvement loop.

## Preserve

The current architecture already has foundations worth keeping:

- one capability catalog for direct turns and `dispatch(mode="jarvis")`
  background work;
- typed `PluginMetadata`, `@tool` discovery, and Pydantic return schemas;
- plugin-level semantic routing and per-turn `tools=` disclosure;
- domain-owned persistence and validation;
- `ToolResult` for model content plus UI;
- integration injection and MCP adapters in the same capability catalog;
- durable pending approvals and trigger/task state;
- turn-level traces and routing/eval datasets.

## Build Next

These remaining gaps close the distance between the target contract above and
the current in-process dispatcher. Structured capability calls, typed plugin
returns, and the CodeAct/prefix path are done.

1. **Evidence contracts** (active reliability work): domain
   lookups must distinguish complete absence, ambiguity, and incomplete
   coverage via the shared `match_status` / `coverage` vocabulary;
   mutations must resolve a scoped target before write and return
   confirmed receipts. Implement first in plugins that already fail in
   production (e.g. calendar find, scoped alarm/reminder edits)—via plugin
   conformance, not more harness machinery. Calendar, scheduler, and Gmail
   now provide the first domain-owned evidence/scope contracts; the scorecard
   tracks remaining plugin-specific gaps.
2. **Mid-loop context/budget recovery and failure-derived evals**, using the
   ledger-backed traces to validate narrow harness and plugin changes
   without regressions.

Do not add a second universal structured-tool agent loop, a generic plugin DSL,
or broad workflow tools before these gaps are addressed and measured.

## Related Guidance

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — current runtime and turn pipeline.
- [`BACKGROUND_AGENTS.md`](./BACKGROUND_AGENTS.md) — `dispatch()` execution
  modes and task lifecycle.
- [Plugin and Tool Conventions](../.cursor/rules/plugin-tool-conventions.mdc) —
  authoring recipes and return shapes.
- [Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/) —
  evidence-driven, bounded harness improvement.
- [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
  and [Writing Effective Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents) —
  simple loops, agent-computer interfaces, and tool evals.
- [Executable Code Actions Elicit Better LLM Agents](https://arxiv.org/abs/2402.01030) —
  CodeAct composition, iterative observations, and sandbox tradeoffs.
- [MCP Architecture](https://modelcontextprotocol.io/specification/draft/architecture) —
  host-owned orchestration, scoped server context, and stateless capability
  exchange.

