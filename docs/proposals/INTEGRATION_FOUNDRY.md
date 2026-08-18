# Integration Foundry

**Status:** Proposed
**Date:** 2026-06-11
**Related:** `docs/proposals/built/INTEGRATION_SETUP.md`, `docs/proposals/GUIDED_CREDENTIAL_ACQUISITION.md`, `.cursor/rules/plugin-tool-conventions.mdc`, `backend/plugins/agents/`

---

## Problem

JARV1S has two integration qualities and no path between them at runtime:

- **Bespoke plugins** (Gmail, Calendar, Spotify, Smart Home): hand-crafted docstrings, VOICE policy, Pydantic returns, curated utterances. Local models use these well. Cost: developer hours per integration, only the maintainer can add them.
- **Auto-bridged Composio/MCP tools**: 30 seconds to connect, but tool names like `GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER`, schema-derived docstrings with no behavioral guidance, raw JSON envelopes, and synthetic routing utterances. Exactly the surface that weak/local models stumble on — Phase 8.5 dropped per-tool description embeddings because this noise stalled the fast path.

And the auto-bridge path carries the architectural conflict: Composio brokers auth and proxies tool execution through its servers, which breaches data custody for a local-first product, requires an operator platform account before any end-user connect works (friction log #15), and needs public webhook reachability for triggers.

Meanwhile the ecosystem has demonstrated the missing primitive: agents now generate working API integrations from documentation (mcpgen, agentify, build-mcp, MCP-Server-Foundry all shipped 2025–26). They all emit **MCP servers** — generic schema surfaces. JARV1S is uniquely positioned to do better: it already has the target format that local models *prefer* (the bespoke `JarvisPlugin`), a written spec for it (`plugin-tool-conventions` rule), a code-capable background agent runtime (`agents.dispatch(mode="code")`), hot plugin registration (`registry.register()` + `tool_router.register_plugin()`), a consent system, and a routing eval harness.

**The proposal: make "build me a bespoke integration" a product feature.** The foundry is an agent pipeline that turns API docs into a reviewed, tested, hot-mounted `JarvisPlugin` — collapsing the bespoke/auto-bridged quality gap and providing the Composio exit ramp.

---

## Design

### User-facing shape

```
User: "Connect Todoist" (no bespoke plugin, no Composio… or user prefers local)
Jarvis: "I don't have a Todoist integration. I can build one — it takes a few
         minutes and runs entirely on this machine. Todoist uses an API token
         (free). Want me to start?"
  → foundry dispatch (background progress receipt in the review rail; detail opens on demand)
  → credential lane (Guided Credential Acquisition playbook, generated if absent)
  → review gate: tool list + docstrings + the API calls each will make
  → user approves → hot-mount → "Todoist is ready. Want to try 'add milk to
    my shopping list'?"
```

### Pipeline

One foundry run = one background task on the existing dispatch infrastructure:

```
1. RESEARCH    Fetch API docs (OpenAPI URL if available; else doc pages).
               Identify: auth scheme, base URL, the 5–15 endpoints worth
               wrapping, rate-limit/pagination patterns.
2. DESIGN      Select tools by *user intent*, not endpoint coverage —
               curated-wrapper philosophy from INTEGRATION_PLAN.md. Draft
               tool signatures, Pydantic return models, VOICE policies,
               utterances, consent classification (destructive ops).
3. GENERATE    Emit plugins/<name>/ package: client.py (httpx, same factory
               pattern as weather/), __init__.py with @tool methods,
               playbook YAML for the credential (if API-key auth),
               eval fixtures (routing utterances + 3–5 smoke prompts).
4. VERIFY      Static: import, scanner contract (signatures/docstrings),
               docstring lint (Args + VOICE sections present), consent
               gates on destructive tools, no banned imports.
               Live (with credential, sandboxed): read-only smoke calls
               only; network egress restricted to the API's domains.
               Routing: regenerate utterance vectors, run the confuser
               check against existing plugins (scheduler/automations-style
               collisions are the known failure mode).
5. REVIEW      Consent gate (durable pending_inputs row): show tool list,
               each docstring, and a plain-language line per tool — "this
               calls api.todoist.com to read your tasks". Destructive
               tools highlighted. Approve / revise ("drop the delete
               tool") / reject.
6. MOUNT       Write under plugins/community/<name>/, registry.register(),
               tool_router.register_plugin(), invalidate manifest prefix.
               No restart — same hot path the Composio callback uses.
```

The generating agent runs as `mode="code"` (subprocess, SDK-isolated) with the `plugin-tool-conventions` rule and 2–3 exemplar plugins (weather, search) in its context as the spec. The generated code is plain reviewable Python in the repo working tree — no runtime metaprogramming, no opaque registry blobs. Promotion to "official" is just moving the directory and a PR.

### Trust model

Generated code executes in-process eventually, so the gates are the design's center of gravity:

- **Generation is sandboxed** (existing SDK subprocess isolation); the foundry agent has no live plugin access.
- **Live verification runs read-only calls only**, under an httpx transport that hard-allowlists the integration's domains.
- **Nothing mounts without explicit user approval** through the standard consent flow. The review payload is honest about what each tool can do.
- **Provenance manifest** per generated plugin (source doc URLs, generation date, model, verification results) stored alongside the code — auditable, and the input for regeneration when the upstream API changes.
- **Community plugins are namespaced** (`plugins/community/`) and individually toggleable via the existing `set_plugin_enabled()` path.

This is deliberately the opposite of "the LLM writes code into its own runtime": the foundry is a *build step* with review, not self-modification.

### Auth strategy per integration (the Composio exit ramp)

The foundry's DESIGN step classifies auth and picks the local-custody path:

| Auth scheme | Path |
|---|---|
| API key / personal token | Credential playbook (Tier 0 of Guided Credential Acquisition) — generated as part of the foundry run |
| Google / Microsoft OAuth | Reuse the existing ecosystem apps via `AuthManager` + `register_aux_provider_scopes()` — new scopes, not new OAuth apps |
| Other OAuth, device-code supported | Device authorization grant through `AuthManager` (pattern already proven by the MSAL flow) |
| Other OAuth, no device flow | Honest fallback: Composio (if operator configured it) **or** the Tier-1/2 assisted OAuth-app-creation flow — user's choice, tradeoffs stated |

Net effect over time: Composio shrinks from *the* long-tail mechanism to one optional auth backend for the shrinking set of providers with hostile OAuth postures. The `INTEGRATION_SETUP.md` mitigation ("swap the backend, keep the tools") becomes real instead of aspirational.

### Relationship to the MCP auto-bridge

The bridge stays — it remains the right answer for *instant* connection and for genuinely good third-party MCP servers. The foundry adds the promotion path that `INTEGRATION_SETUP.md` already names ("start auto-generated, promote to bespoke when important") but makes it a runtime feature instead of developer homework. Bespoke-wins shadowing means a foundry-built plugin cleanly replaces a bridged one with zero config.

### Community flywheel

Every foundry output is a shareable artifact. A lightweight index (a git repo of vetted `plugins/community/` packages + their provenance manifests) lets users install a neighbor's reviewed Todoist plugin instead of regenerating it — generation becomes the fallback, not the default, as coverage grows. This is the open-source answer to Composio's catalog: the catalog is the community, and the tools meet bespoke quality bars because the foundry's gates enforced them.

---

## What NOT to build

| Idea | Why skip |
|---|---|
| Auto-mount without review | The whole trust model collapses; review is the product, not friction |
| Full-coverage endpoint mapping | One tool per endpoint is the MCP-generator trap that produces 147-tool Slack surfaces; intent-curated 5–15 tools is the bespoke quality bar |
| In-conversation code generation (foreground turn) | Foundry runs are minutes-long; background dispatch + widget is the existing right-shaped surface |
| A DSL/IR between docs and plugin | The `JarvisPlugin` + conventions rule *is* the IR; an intermediate format doubles maintenance for zero gain at this scale |
| Foundry-generated push/webhook adapters (v1) | Watchers/push adapters touch the automation engine's invariants; keep generated scope to pull tools until the pattern is proven |
| Sandboxed-forever execution (subprocess per tool call) | Latency-hostile for the voice loop; the review gate + read-only verification + allowlisted egress is the proportionate control for personal-assistant scale |

---

## Phases

**Phase 1 — Foundry pipeline, API-key integrations only**
- Foundry dispatch flow + prompt pack (conventions rule + exemplars)
- Static verification suite (scanner contract, docstring lint, consent check)
- Review consent gate + hot-mount; `plugins/community/` namespace + provenance manifest
- Target validation: Todoist, Notion, Linear — three real runs end-to-end

**Phase 2 — Quality gates that bite**
- Domain-allowlisted live smoke verification
- Routing confuser eval integration; auto-revision loop on gate failure (bounded retries)
- Regeneration from provenance when an API version changes (re-run → diff → re-approve)

**Phase 3 — Auth breadth + community**
- Ecosystem-OAuth scope extension and device-code generation paths
- Shared community index with provenance-verified installs
- Composio default flipped: bridge only when the user picks it

---

## Tradeoffs

- **Generated ≠ maintainer quality, initially.** The gates and exemplars get it to "good"; real-world use gets it to "great" via the same eval-loop guardrail the project already applies to hand-written tools. The honest claim is parity of *shape* (docstrings, types, consent, routing), not instant parity of judgment.
- **Verification needs credentials.** Live smoke tests run only after the user completes the credential lane — the pipeline must tolerate a pause mid-run (the background-task `pending_input` machinery already models this).
- **Docs quality varies wildly.** OpenAPI-first providers will work great; doc-page-only providers will need the research step to be genuinely agentic and will fail sometimes. Failing with "I couldn't build this reliably — here's the Composio option" is an acceptable outcome and must be a first-class result, not an error.
- **Security review burden shifts to users.** Mitigated by plain-language review payloads, read-only verification, egress allowlists, and community provenance — but a user can still approve a bad plugin. This is the same trust position as installing any community extension, made *more* legible than the ecosystem norm.
