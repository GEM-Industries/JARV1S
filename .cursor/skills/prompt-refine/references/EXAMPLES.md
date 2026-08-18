# Prompt Refine — Extended Examples & Reference

Extended material for the `prompt-refine` skill. Consult when the base SKILL.md doesn't cover the situation at hand.

---

## Extra Review Examples

### Example 3 · Reviewing a SKILL.md / Cursor rule

User input:
```
You are an expert reviewer of React components. Always be thorough.
When reviewing, look for:
- Bugs
- Performance issues
- Accessibility problems
- Code smells

Give detailed feedback.
```

**Review output:**

## Overall Verdict
Vague scope + non-deterministic wording. Works as a brainstorm prompt, not as a reliable review skill.
Layer pass/fail: L1 ⚠️ · L2 ❌ · L3 ❌ · L4 ❌ · L5 N/A

## Top Issues

### [P1] No altitude — the "how" is missing
**Location:** `look for: Bugs / Performance / Accessibility / Code smells`
**Problem:** These are categories, not criteria. The model will produce generic observations (too vague) or invent issues (over-eager) depending on the day.
**Rewrite:**
```
When reviewing a React component, run these checks in order:

1. Bugs: trace state flow and effect dependencies. Flag any effect that
   reads state it doesn't list in deps, and any setState called during render.
2. Performance: flag components that re-render a large subtree from a
   frequently-changing prop. Suggest memoization only when the subtree
   cost > render cost.
3. Accessibility: keyboard-navigable? ARIA roles correct? Color-only cues?
4. Code smells: components > 200 lines, prop drilling > 3 levels,
   repeated conditional JSX blocks.

For each finding, cite the exact lines and propose a concrete fix.
```

### [P1] "Always be thorough" + "Give detailed feedback" invites verbose output
**Problem:** No length budget. Model will pad with restated code, hedging, and filler.
**Rewrite:**
```
Respond with a numbered list of findings. Each finding MUST include:
(a) severity P0-P3, (b) file/line reference, (c) one-sentence problem
description, (d) a code-diff rewrite. No preamble, no summary of the code.
```

### [P2] No examples demonstrating the expected output shape
**Rewrite:** add 1-2 `<example>` blocks showing the numbered-finding format.

---

### Example 4 · Reviewing a tool description (MCP-style)

User input:
```json
{
  "name": "search",
  "description": "Search for items.",
  "parameters": {
    "query": { "type": "string" },
    "limit": { "type": "number" }
  }
}
```

**Review output:**

## Overall Verdict
Severely under-specified. The model cannot reliably decide when to call this tool or what to pass in.
Layer pass/fail: L1 N/A · L2 ❌ · L3 N/A · L4 N/A · L5 N/A (tool-description layer dominates)

## Top Issues

### [P0] Description fails the 5-part test
**Location:** `"description": "Search for items."`
**Problem:** Anthropic's own rule: *"Even small refinements to tool descriptions can yield dramatic improvements."* The current description covers (1) what, and nothing else. Missing: when-to-use, when-NOT-to-use, parameter semantics, caveats.
**Rewrite:**
```
Search the product catalog for items matching a query string. Returns
up to `limit` items, ranked by relevance.

When to use this tool:
- User asks about product availability, pricing, or specs
- User mentions a product name or category

When NOT to use this tool:
- If the user provides a specific product ID, use `get_product` instead
- If the user wants a listing of ALL products in a category, use `list_category`

Parameters:
- query: free-text search string (e.g., "red running shoes size 10")
- limit: max results, 1-50, default 10

Caveats:
- Search is fuzzy; typos tolerated within edit distance 2
- Returns [] when no match; never throws on empty result
```

### [P1] Ambiguous parameter name
**Location:** `"query"`
**Problem:** "query" is borderline — it works for search but could be improved to `search_text` for unambiguity.
**Rewrite:** optional rename to `search_text`, or add description `"The free-text search phrase"`.

### [P1] No return-shape documentation
**Rewrite:** add to description:
```
Returns: array of { product_id, name, price, in_stock }. Do NOT assume
the array is non-empty — always check length.
```

---

### Example 5 · Reviewing a short Cursor rule

User input:
```
Always use TypeScript.
Never use `any`.
Prefer functional components.
Write tests.
```

**Review output:**

## Overall Verdict
Compact but vague on enforcement and scope. Good bones; needs motivation and specifics.
Layer pass/fail: L1 ✅ · L2 ⚠️ · L3 ⚠️ · L4 ❌ · L5 N/A

## Top Issues

### [P2] No motivation on any rule
**Problem:** "Never use `any`" without a reason allows the model to rationalize exceptions. Add the why:
**Rewrite:**
```
Never use `any`. It disables type checking and silently propagates
errors downstream. Use `unknown` + narrowing, or define a precise type.
```

### [P2] "Write tests" is ambiguous
**Rewrite:**
```
For every new exported function, write a unit test in the sibling
`__tests__` folder using Vitest. Cover the happy path + one failure mode.
```

### [P3] Rule hierarchy unclear
**Problem:** If the file also has a contradicting rule (e.g., a legacy rule allowing `any` in `/legacy/**`), the priority isn't stated.
**Rewrite:** add
```
If a file lives under /legacy/**, the legacy rules override these.
Otherwise these rules are the baseline.
```

---

## Part 6 · Context Engineering (advanced)

Not part of the default 5-layer framework, but relevant when reviewing prompts for **long-running sessions, high tool counts, or multi-agent systems**. Add a "Layer 6" pass when the user mentions any of these symptoms:

- "Agent forgets earlier context"
- "Context window keeps filling up"
- "Cache hit rate is low"
- "Too many tools slow the agent down"

### 6.1 Context is finite

> "Context must be treated as a finite resource with diminishing marginal returns." — Anthropic

Every token added slightly degrades attention to everything else. Check whether the prompt:
- **Preloads everything** (anti-pattern) → should use just-in-time retrieval via tools
- **Keeps lightweight references** (file paths, IDs) instead of full content
- **Summarizes old information** rather than preserving it verbatim

### 6.2 What survives compaction

When context is auto-compacted, most content is lost. Critical rules MUST live in a surface that survives:

| Survives compaction | Lost during compaction |
|---|---|
| System prompt instructions | Old tool call results |
| Persistent instruction files (CLAUDE.md) | Intermediate reasoning |
| Most recent 5 file contents | Line numbers and paths from early turns |
| Architectural decisions | Debugging state |

**Implication:** Any rule buried in mid-conversation will be lost. If a rule matters, it belongs in the system prompt or a persistent instruction file.

### 6.3 Long-session strategies

| Strategy | When to use |
|---|---|
| **Compaction** (summarize + restart) | Best for conversational flow |
| **Structured note-taking** (persist to file) | Best for iterative tasks with milestones |
| **Sub-agents** (clean context per task) | Best for parallel or independent sub-tasks |

### 6.4 Tool count discipline

| Tool count | Strategy |
|---|---|
| 1–10 | Include full schemas in every turn |
| 10–20 | Include full schemas but monitor token usage |
| 20+ | Show names/one-liners only; provide a `tool_search` function for the model to fetch full schemas on demand |

Claude Code uses `ToolSearchTool` for lazy loading at high tool counts. If you're reviewing a prompt that lists 20+ tools inline, flag it.

---

## Source Material

Direct quotes and tables used in the skill come from these public sources:

- Anthropic engineering blog — prompt engineering best practices, tool design, context management
- Claude Code system prompts (v2.1.x, community-documented reference)
- Claude Code docs — verification criteria, proactiveness, scope constraint
- OpenAI agent guides — chaining, denial handling

Where principles conflict between sources, Anthropic guidance takes precedence (most production-validated for Claude-family models).

---

## Suggested Additional Skills

These were scoped out of `prompt-refine` but deserve their own skills if the need arises:

- `prompt-architect` — author a new prompt from scratch using the 6-component architecture
- `tool-description-refine` — specialized version of this skill, focused only on MCP / function-calling tool descriptions
- `agent-behavior-debug` — symptom-first diagnosis ("my agent is too chatty" → targeted fix) rather than full-prompt review
