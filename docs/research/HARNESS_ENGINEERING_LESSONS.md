# Lessons from Harness Engineering for Self-Improvement (Lilian Weng)

**Date:** 2026-08-13  
**Source:** [Lilian Weng, “Harness Engineering for Self-Improvement” (Lil’Log, Jul 2026)](https://lilianweng.github.io/posts/2026-07-04-harness/)  
**Status:** Non-normative research. Normative contracts stay in [`PLUGIN_ARCHITECTURE.md`](../PLUGIN_ARCHITECTURE.md), [`ARCHITECTURE.md`](../ARCHITECTURE.md), and [`BACKGROUND_AGENTS.md`](../BACKGROUND_AGENTS.md).

This note extracts what matters for JARV1S feature and architecture decisions. It is not a plan to build recursive self-improvement (RSI), meta-harness search, or auto-research pipelines. JARV1S is a voice-first home assistant; we borrow **harness design principles**, not the full RSI research program.

---

## How this maps onto JARV1S

Weng defines a **harness** as the system around a base model that orchestrates execution: how the model thinks/plans, calls tools, manages context, stores artifacts, and evaluates results. In JARV1S that layer is already explicit:

| Weng harness concern | JARV1S surface today |
| :--- | :--- |
| Agent loop | `JarvisAgent` + CodeAct (`generate → execute → observe → repeat`) via `turns/execution.py` |
| Tools / action interface | `jarvis.*` capabilities, `@tool` plugins, MCP adapters, `CapabilityDispatcher` |
| Context construction | `PromptBuilder`, ToolRouter progressive disclosure, `fit_to_budget()`, history projection |
| Persistent state | MongoDB conversations/tasks/approvals/triggers; full tool output retained while LLM sees previews |
| Sub-agents / backend jobs | `jarvis.agents.dispatch` (`mode="jarvis"` vs `mode="code"`), task rows, progress receipts |
| Permissions | Consent, trust, plugin enablement — enforced outside generated code |
| Evaluation / improvement | Turn/task traces + invocation ledger; `agent_behavior` evals; voice eval ladder; Improvement Contract in plugin architecture |

**Core product implication:** near-term gains come from making this harness simpler, more observable, and more evidence-driven — not from rewriting model weights or letting the agent edit its own OS.

JARV1S’s existing stance already matches the article’s strongest product lesson:

> Complexity belongs in capability contracts, context management, permissions, and verification — not orchestration heuristics.  
> ([`PLUGIN_ARCHITECTURE.md`](../PLUGIN_ARCHITECTURE.md))

---

## 1. Treat the harness as an OS, keep the loop generic

**Article:** Harnesses are closer to runtime/OS design than prompt templates. Encapsulate complicated logic; keep the model-facing interface simple and software-engineering shaped so pretrained knowledge transfers. Workflow, evaluation, permission controls, and persistent state are first-class — not afterthoughts.

**For JARV1S:**

- Prefer extending **capabilities, evidence contracts, consent, and durable state** over new special-case orchestration paths.
- Keep CodeAct as the composition language for rare multi-step work; do not grow hard-coded multi-tool workflows into the agent loop.
- When adding a feature, ask: *does this belong in a plugin domain contract, durable state, or the harness gate?* If it only “works” as a one-off prompt or orchestration branch, it is probably the wrong layer.
- Standardize protocols at boundaries we already own: capability catalog, dispatcher ledger, ToolResult/UI side channel, trigger delivery — analogous to industry tool/interface standardization in the article.

**Do not:** invent a second agent architecture beside CodeAct/MCP, or encode every user routine as harness code when protocols/automations already exist as user-owned workflows.

---

## 2. Workflow automation = goal-oriented loops, not static prompt templates

**Article — Pattern 1:** Successful coding agents run plan → execute → observe/test → improve until the goal is met, including proactive clarification. The model should analyze its own trajectories/failures through an **agent runtime**, not a static prompt.

**For JARV1S:**

- Voice and headless turns already share one delivery-agnostic loop; preserve that.
- Background tasks and protocols should remain inspectable runs with durable status, not chat-only side effects.
- Prefer “run, observe evidence, continue/retry/clarify” over stuffing more failure folklore into prompts.
- User clarification belongs at consent/attention/pending-input touchpoints we already have — not unbounded autonomous inventiveness.

**Feature test:** Can the model recover after interruption by reading durable task/turn state, or only if the chat context survived?

---

## 3. Durable artifacts beat stuffing the context window

**Article — Pattern 2:** Do not carry the whole workflow and all logs in context. Keep durable state in files (or equivalent). Long-horizon artifacts — logs, diffs, error traces, past trajectories — outgrow context. Models already know how to read/write simple persistent stores.

**For JARV1S (Mongo/files instead of “everything in prompt”):**

- Continue the rule: **context window is a cache; Mongo (and task artifacts) are truth**.
- Full tool outputs, traces, invocation ledgers, approvals, and background task events stay durable; the model gets previews, handles, and retrieval tools (`recall`, `get_result`, scoped lookups).
- Long-horizon work (morning briefing, multi-step home routines, coding dispatch) should write **artifacts with stable IDs** the voice loop can reopen — not enlarge history injection.
- Aligns with two-tier memory: Layer 1 small always-on facts; Layer 2 on-demand archival recall.

**Build implication:** when a feature produces large intermediate data (calendar search dumps, HA inventories, code diffs), design the plugin return as **small typed evidence + handle**, and store richness out of band.

---

## 4. Sub-agents need an explicit, inspectable process manager

**Article — Pattern 3:** Spawn parallel subagents/backend jobs; parent needs launch / inspect logs / cancel / merge. Parallelism must be **explicit and inspectable**. Transient chat-only subagent outputs go stale; file/log/status records survive interruption.

**For JARV1S:**

- `dispatch` already returns `task_id` and persists lifecycle; keep results retrievable via `get_result` / status, not by hoping conversation memory retained them.
- Fan-out limits, depth limits, and lane priority (Voice > System > Background) are harness process-management — protect them when adding parallelism.
- Prefer merging **structured outcomes** (receipts, evidence, artifact paths) into the parent turn over dumping raw subagent transcripts into voice context.
- Crash recovery that marks interrupted tasks failed (no silent auto-resume) matches “inspectable state over magic continuity.”

**Watch-out from the article:** if subagent outputs only live in chat context, they become obsolete and hidden. JARV1S should keep treating background agents as **jobs with durable rows**.

---

## 5. Context engineering: structured evolving memory, not prompt accretion

**Article:**

- Dumping all tool responses into context collapses under long horizons.
- **ACE:** context as an evolving playbook of itemized bullets; curator merges structured entries instead of rewriting one giant prompt blob (avoids context collapse / brevity bias).
- **MCE:** separate *mechanism* (how context is managed) from *content* (what is in context); skills can evolve at a meta level.
- **Meta-Harness:** optimize the code that decides what to store/retrieve/present; history searchable via filesystem tools rather than one mega-prompt.

**For JARV1S (practical slice only):**

| Adopt now | Defer / avoid |
| :--- | :--- |
| Itemized, mergeable context sections (profile facts, runtime, routed tools) with deterministic assembly | Full ACE/MCE/Meta-Harness auto-evolution of PromptBuilder |
| Progressive disclosure (ToolRouter, lazy MCP schemas) | Shoveling full MCP catalogs into every turn |
| Compaction that preserves durable handles and reloads fresh dynamic context | One-shot rewrite of the entire system prompt from a trajectory |
| Trace/ledger as searchable failure evidence for humans (and later tools) | Letting the live agent rewrite harness code in production |

**Concrete alignment with existing gaps:** mid-loop budget recovery, summarization before drop, and reload of profile/runtime after compaction (already noted in Claude Code lessons) are the JARV1S-shaped version of “context engineering,” not an outer evolutionary search.

---

## 6. Optimize the right object — and stop before overengineering

**Article progression:** instruction prompts → structured context → workflow → harness code → optimizer code. As models improve, move complexity **out of brittle heuristics** into more general mechanisms — but eventually many harness tricks get internalized by the model; the external tool/context interface remains.

**STOP / Lin et al. caution:** recursive improvers help only if the base model is strong enough; “harness updating” ≠ “harness benefit.” Mid-tier models often benefit most from harness changes; using the harness well (timely tools, long-horizon instruction following) is a separate capability.

**For JARV1S:**

1. Fix **tool contracts and evidence** before adding orchestration.
2. Fix **context/routing** before inventing new planning frameworks.
3. Fix **eval + trace attribution** before any automated harness edit loop.
4. Assume stronger models will reduce need for clever prompt patches; do not accumulate transcript-specific prompt folklore (already in Improvement Contract).

JARV1S product constraint strengthens this: **latency and voice UX beat research-agent sophistication**. Prefer mechanisms that keep the voice loop lean.

---

## 7. Evidence-driven harness improvement (the part to internalize)

Weng’s Self-Harness / AHE material is the closest match to JARV1S’s written Improvement Contract. Distill for builders:

### Self-Harness loop (adapted)

1. **Weakness mining** — cluster failures from rich traces, not surface error strings alone. Same verifier outcome can hide different causal mechanisms.
2. **Bounded proposal** — edit one explicit surface; prefer recurrent, addressable patterns; preserve known-good behavior; keep a log of attempted edits.
3. **Validation** — held-in (did the weakness clear?) and held-out (did we regress?); accept only if both pass.

### AHE observability pillars (adapted)

| Pillar | JARV1S meaning |
| :--- | :--- |
| Component observability | Editable surfaces are explicit: tool contract, implementation, dispatcher, context, prompt, routing, eval — map failures to one |
| Experience observability | Layered evidence: raw turn/task traces → per-failure analysis → clustered patterns (token-efficient; raw available on demand) |
| Decision observability | Every change states expected fix + at-risk regressions; falsifiable next round |

### Hard safety rule (article + our contract)

**Permission controls and evaluators sit outside any loop that could edit the harness.**  
Do not let an improver disable verifiers, swap models, raise budgets, or bypass consent to farm a metric.

JARV1S already states this in [`PLUGIN_ARCHITECTURE.md` Improvement Contract](../PLUGIN_ARCHITECTURE.md). When building features:

- Prefer changes that make failures **attributable** (ledger statuses, coverage/match evidence, consent linkage).
- Prefer evals that protect **held-out** behavior, not only the bug you just saw.
- Prefer narrow plugin conformance fixes over “smarter” global scaffolding.

---

## 8. Workflow search and auto-research — mostly out of scope

**Article covers:** AI Scientist pipelines, ADAS/AFlow meta-agent workflow search, Autodata challenger/solver/verifier, evolutionary program search (AlphaEvolve, DGM), joint weight+harness optimization.

**For JARV1S:** treat as background awareness, not a build target.

Useful residue only:

- **Verifiability / chain-of-evidence** (ScientistOne): every consequential claim should trace to a tool receipt or durable store — same spirit as mutation receipts and read `coverage`/`match_status`.
- **Challenger / weak / strong / verifier** thinking for **synthetic eval data**: generate cases where a weak policy fails and a strong one passes — useful for agent_behavior datasets, not for live home control.
- Evolutionary harness search needs **cheap, objective fitness**. Home assistant success (taste, trust, latency feel, long-term household health) is mostly **not** that domain. Do not optimize JARV1S by open-ended agent self-modification against fuzzy user satisfaction proxies.

---

## 9. Failure modes to design against (Trehan & Chopra, plus Weng challenges)

When shipping long-horizon or proactive features, assume these failure modes unless the harness makes them hard:

| Failure mode | JARV1S countermeasure direction |
| :--- | :--- |
| Bias toward training-data defaults | Domain plugins own truth (IDs, scopes, provider capabilities); don’t let CodeAct invent domain semantics |
| Implementation drift under pressure | Evidence contracts; refuse “success” without provider/store confirmation |
| Memory/context degradation | Durable artifacts + recall; compaction that doesn’t silently delete the only copy of critical state |
| Over-optimism / “eureka-ing” | Read coverage axes; mutation receipts; evals that punish false completion |
| Weak taste / wrong question | Human oversight at consent, offers, and protocol authoring; don’t auto-declare household policy |
| Fuzzy evaluators | Prefer measurable gates (tool correctness, latency, FA/hr, held-out agent_behavior) for automated loops; keep taste with humans |
| Negative-result blindness | Preserve failed attempts in traces/tasks; don’t only store wins |
| Diversity collapse | When generating evals or automations, avoid cloning one high-reward pattern |
| Reward hacking | Evaluators + permissions outside editable surfaces; held-out tests; human review at high-impact decisions |
| Short-term task success vs long-term health | Plugin maintainability, ownership boundaries, backwards-compatible tool contracts, migration cost — treat as first-class when changing APIs |
| Humans removed from the loop | Move humans **up** the stack: goals, permissions, taste, irreversible actions — not out |

---

## 10. Coding-agent tool shape (reference checklist)

Weng’s stabilized coding-agent tool groups are a useful checklist when JARV1S grows computer-use / repo work (`mode="code"` already overlaps):

- Filesystem discovery + read/edit  
- Shell  
- Git / LSP-style IO  
- External context (MCP, Skills)  
- Web  
- Artifacts  
- Backend processes (cron-like)  
- Agent delegation (spawn/resume/wait/list/close/interrupt)

JARV1S voice loop should **not** mirror this full IDE surface every turn. Keep heavy tooling in background `mode="code"`; keep voice on `jarvis.*` with triage (speak / widget / silent).

---

## Decision checklist for new JARV1S work

Use when designing features or architecture changes:

1. **Layer:** plugin domain, durable state, harness gate, or prompt? Prefer the leftmost that can own the truth.
2. **Loop:** does this keep `generate → execute → observe` simple?
3. **State:** is anything needed later stored with an ID outside the prompt?
4. **Evidence:** can success, absence, ambiguity, and partial coverage be distinguished without trusting model prose?
5. **Permissions:** is policy enforced outside generated code / outside any future improver?
6. **Observability:** if it fails in production, which component will the trace implicate?
7. **Eval:** what held-out or regression check proves we didn’t Goodhart a local fix?
8. **Human touchpoint:** where does the user steer goals, approve risk, or reject taste-poor automation?
9. **Latency:** does this add weight to the voice path that belongs in a background job instead?
10. **Self-improvement temptation:** are we proposing meta-search over harness code before evidence contracts and traces are good enough? If yes, stop.

---

## What JARV1S should *not* take from the article (yet)

- Production agents that rewrite their own harness/OS.
- Unbounded evolutionary search over prompts/workflows as a product feature.
- Joint weight + harness training loops.
- Paper-scale auto-research pipelines as a substitute for household reliability.
- Treating benchmark self-play as proof of assistant quality.

Those remain research. Our near-term RSI-shaped path is the one Weng predicts for products: **better meta-methodology around getting reliable answers** — traces, bounded edits, held-out evals, simple loops — while the model stays a replaceable intelligence core.

---

## Related JARV1S docs

- [`PLUGIN_ARCHITECTURE.md`](../PLUGIN_ARCHITECTURE.md) — CodeAct contract, harness responsibilities, Improvement Contract (normative).
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — turn pipeline, context budget, persistence.
- [`BACKGROUND_AGENTS.md`](../BACKGROUND_AGENTS.md) — subagent process model.
- [`VISION.md`](../VISION.md) — latency, triage, two-tier memory, starvation-free concurrency.
- [`CLAUDE_CODE_ARCHITECTURE_LESSONS.md`](./CLAUDE_CODE_ARCHITECTURE_LESSONS.md) — loop simplicity and context compression detail.
- [`OPENCLAW_ARCHITECTURE_LESSONS.md`](./OPENCLAW_ARCHITECTURE_LESSONS.md) — OS/gateway and memory patterns.
- [`VOICE_EVALS.md`](./VOICE_EVALS.md) — measurable eval ladder mindset.
