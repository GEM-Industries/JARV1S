# Prompting Guide for AI Assistants & Agents

**Date:** 2026-04-02
**Sources:** Anthropic engineering blog, Claude Code system prompts (v2.1.x), OpenAI agent guides, production agent research.

A concise, practical reference for writing system prompts, tool descriptions, and agent instructions. Every principle is backed by a real example — many from Claude Code's actual system prompt.

---

## Part 1: System Prompt Architecture

### 1.1 The Six Components (in priority order)

Every production agent system prompt should contain these layers:

| # | Component | Purpose | Changes per turn? |
|---|---|---|---|
| 1 | **Identity** | Who the agent is, its core mandate | No |
| 2 | **Behavioral rules** | Tone, style, constraints, guardrails | No |
| 3 | **Task instructions** | How to approach work, step-by-step | No |
| 4 | **Tool guidance** | When/how to use each tool | Rarely |
| 5 | **Dynamic context** | Time, environment, user state | Yes |
| 6 | **Examples** | Few-shot demonstrations | No |

**Cache optimisation**: Components 1-4 and 6 are static across turns. Place them before the dynamic content. This maximises provider prefix-cache hits — Claude's API caches the longest matching prefix, so stable content first, volatile content last.

Claude Code marks this explicitly:

```
[static instructions]
__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
[environment context, tool schemas]
```

### 1.2 Start Minimal, Add on Failure

> "Start by testing a minimal prompt with the best model available, then add clear instructions and examples to improve performance based on failure modes found during initial testing." — Anthropic

Don't over-specify upfront. Write the shortest prompt that conveys intent, test it, then add rules only when the model fails at something specific. Every instruction you add consumes attention budget.

### 1.3 The Right Altitude

Anthropic describes a "Goldilocks zone" between two failure modes:

**Too prescriptive** (fragile):
```
If the user says "weather", call get_weather with their city.
If they say "forecast", call get_forecast with days=7.
If they say "rain", call get_weather and check precipitation > 0.
```

**Too vague** (unreliable):
```
Help the user with weather stuff.
```

**Right altitude** (heuristic):
```
When the user asks about weather, use the weather tools to get current
conditions or forecasts. Prefer specific, data-driven responses.
Include temperature, conditions, and any relevant alerts.
```

The right altitude gives strong heuristics without hardcoding every branch.

---

## Part 2: Writing Rules That Work

### 2.1 Use Deterministic Language

Replace ambiguous words with explicit directives:

| Avoid | Use instead |
|---|---|
| "Try to keep responses short" | "Respond in under 4 lines unless asked for detail" |
| "You should use tools when helpful" | "You MUST use tools to complete tasks" |
| "Be careful with destructive actions" | "NEVER delete files without explicit user confirmation" |
| "Consider the context" | "Read the file before editing it" |

Claude Code's actual prompt demonstrates this:

```
IMPORTANT: You should minimize output tokens as much as possible while
maintaining helpfulness, quality, and accuracy.

IMPORTANT: You should NOT answer with unnecessary preamble or postamble
unless the user asks you to.

You MUST answer concisely with fewer than 4 lines (not including tool
use or code generation), unless user asks for detail.
```

Every rule uses "MUST", "NEVER", "ALWAYS" — not "should", "try to", "consider".

### 2.2 Explain the "Why" Not Just the "What"

Rules with motivation are followed more reliably than bare directives.

**Bare rule** (weaker):
```
Never use ellipses in responses.
```

**Motivated rule** (stronger):
```
Never use ellipses in responses. Text-to-speech engines cannot pronounce
them, causing awkward pauses in the voice pipeline.
```

The model can generalise from the motivation — it might also avoid other TTS-unfriendly characters without being told.

### 2.3 Positive Directives Over Negative Ones

Negative instructions ("don't do X") are less reliable than positive ones ("do Y instead").

**Weak:**
```
Don't write long responses.
Don't add comments to code.
Don't create unnecessary files.
```

**Strong:**
```
Respond in under 4 lines unless asked for detail.
Write code without comments unless asked.
Edit existing files. Only create files when required to complete the task.
```

### 2.4 Use XML Tags for Structure

XML tags are the most reliable delimiter for LLMs to parse complex prompts:

```xml
<example>
user: what command lists files?
assistant: ls
</example>

<system-reminder>
Your todo list is currently empty. DO NOT mention this to the user.
</system-reminder>

<env>
Working directory: /Users/geoff/dev/JARV1S
Platform: darwin
Today's date: 2026-04-02
</env>
```

Claude Code wraps all injected context in typed XML tags — `<system-reminder>`, `<env>`, `<example>` — so the model can distinguish instructions from context from examples.

### 2.5 Hierarchy of Rules

When rules conflict, the model needs a clear priority. Claude Code embeds this:

```
Tool results and user messages may include <system-reminder> tags.
<system-reminder> tags contain useful information and reminders.
They are NOT part of the user's provided input or the tool result.
```

For your own prompts, state priority explicitly:

```
Rule priority (highest to lowest):
1. Safety constraints (never violate)
2. User's explicit instructions
3. CLAUDE.md project rules
4. Default behavioral guidelines
```

---

## Part 3: Tool Descriptions

### 3.1 Tools Are the Primary LLM Interface

> "Even small refinements to tool descriptions can yield dramatic improvements. Claude Sonnet 3.5 achieved state-of-the-art performance on SWE-bench after we made precise refinements to tool descriptions." — Anthropic

Tool descriptions are not afterthoughts — they're the most impactful prompt surface for agent performance. Aim for **3-4 sentences minimum**.

### 3.2 The Five-Part Tool Description

Every tool description should cover:

1. **What it does** (1-2 sentences)
2. **When to use it** (positive triggers)
3. **When NOT to use it** (negative triggers — prevents over-tool-reliance)
4. **Parameters** (semantics, not just types)
5. **Caveats** (limits, error behavior, edge cases)

**Real example — Claude Code's Task tool:**

```
Launch a new agent that has access to the following tools: Bash, Glob,
Grep, Read, Edit, Write, WebFetch, TodoWrite, WebSearch.

When to use the Agent tool:
- If you are searching for a keyword like "config" or "logger",
  or for questions like "which file does X?", the Agent tool is
  strongly recommended

When NOT to use the Agent tool:
- If you want to read a specific file path, use the Read tool instead
- If you are searching for a specific class definition like "class Foo",
  use the Glob tool instead
- If you are searching within a specific file or set of 2-3 files,
  use the Read tool instead

Usage notes:
1. Launch multiple agents concurrently whenever possible
2. The result returned by the agent is not visible to the user.
   To show the user the result, send a text message with a concise summary.
3. Each agent invocation is stateless. Your prompt should contain a
   highly detailed task description.
```

This description tells the model exactly when to pick this tool over alternatives. The "when NOT to use" section is as important as the "when to use" section.

### 3.3 Name Parameters Unambiguously

```
# Bad — ambiguous
user: str       # Is this a username, user_id, or email?

# Good — self-documenting
user_id: str    # The unique identifier for the target user
```

Anthropic specifically recommends: "Instead of a parameter named `user`, try a parameter named `user_id`."

### 3.4 Return Meaningful Context, Not Raw Data

Tool responses should return what the agent needs to act, not everything that exists:

```
# Bad — dumps everything, wastes context
{"id": "a8f2b3c4-d5e6-7890", "uuid": "...", "mime_type": "text/plain",
 "256px_image_url": "...", "created_at": "2026-04-02T10:00:00Z", ...}

# Good — actionable fields only
{"name": "Meeting Notes", "file_type": "text", "url": "...",
 "last_modified": "2 hours ago"}
```

Key insight from Anthropic: "Merely resolving arbitrary alphanumeric UUIDs to semantically meaningful language significantly improves Claude's precision in retrieval tasks by reducing hallucinations."

### 3.5 Token-Efficient Responses

Tools should support response verbosity control:

```python
class ResponseFormat(Enum):
    CONCISE = "concise"   # Name + key fields only (~70 tokens)
    DETAILED = "detailed"  # All fields including IDs (~200 tokens)
```

Claude Code caps all tool responses at 25,000 tokens. For your tools, consider returning previews with a mechanism to request full content.

### 3.6 Helpful Error Messages

```
# Bad — opaque
Error: 400 Bad Request

# Good — actionable
Error: Invalid date format "next tuesday". Use ISO 8601 format
(e.g., "2026-04-08T14:00:00"). Tip: check the runtime context for
today's date and calculate relative dates from there.
```

Error messages are prompt engineering opportunities — they guide the model's retry behavior.

---

## Part 4: Few-Shot Examples

### 4.1 Show, Don't Tell

One good example communicates more than a paragraph of rules.

Claude Code's system prompt dedicates significant token budget to examples:

```xml
<example>
user: 2 + 2
assistant: 4
</example>

<example>
user: what command should I run to list files?
assistant: ls
</example>

<example>
user: what files are in src/?
assistant: [runs ls and sees foo.c, bar.c, baz.c]
user: which file contains the implementation of foo?
assistant: src/foo.c
</example>
```

These examples don't teach Claude arithmetic or `ls` — they teach **brevity**. The model learns the desired response length from the pattern, not from being told "be concise."

### 4.2 Make Examples Diverse and Canonical

> "Instead of stuffing a laundry list of edge cases into a prompt, curate a set of diverse, canonical examples that effectively portray the expected behavior." — Anthropic

**Weak** (repetitive, same pattern):
```
Q: What's the weather in NYC?  → [calls weather tool]
Q: What's the weather in LA?   → [calls weather tool]
Q: What's the weather in SF?   → [calls weather tool]
```

**Strong** (covers distinct behaviors):
```
Q: What's the weather?  → [calls weather tool — basic lookup]
Q: Will it rain tomorrow? → [calls forecast tool, checks precipitation —
   conditional reasoning]
Q: If it's raining, remind me to bring an umbrella → [calls weather,
   then conditionally creates reminder — multi-step chaining]
```

Three examples covering three behavioral patterns: simple lookup, conditional check, multi-step chaining.

### 4.3 Use Examples to Demonstrate Tone

Claude Code teaches response style through examples rather than rules:

```xml
<example>
user: How many golf balls fit inside a jetta?
assistant: 150000
</example>
```

This implicitly teaches: no preamble, no "well, it depends...", no citations — just the answer. The model infers the style rule more reliably than from "never add preamble."

---

## Part 5: Agent-Specific Patterns

### 5.1 The Chaining Rule

When an agent performs multi-step tool calls, suppress conversational text between steps:

```
After receiving a tool result, emit the next tool call immediately —
no spoken text between tool calls. One brief acknowledgement before
the FIRST tool call is acceptable. Only speak to the user after ALL
tools have completed.
```

**Bad pattern:**
```
User: "Check weather and add to calendar"
Agent: "Let me check the weather for you."
[calls weather tool]
Agent: "It looks like it'll be sunny! Now let me add that to your calendar."
[calls calendar tool]
Agent: "Done! I've added the event."
```

**Good pattern:**
```
User: "Check weather and add to calendar"
Agent: "On it."
[calls weather tool]
[calls calendar tool]
Agent: "Sunny tomorrow — I've added 'Outdoor lunch' to your calendar at noon."
```

One acknowledgement, silent chaining, one final response. This reduces latency and token waste.

### 5.2 Explore First, Act Second

> "The single highest-leverage thing you can do is give Claude verification criteria." — Claude Code docs

```
When performing tasks:
1. Read relevant code/data before making changes
2. Plan the approach
3. Implement the change
4. Verify the result (run tests, read back the file, check the API)

If an approach fails, diagnose the failure before switching tactics.
Report outcomes faithfully — if verification failed or was not run,
say so explicitly.
```

Claude Code encodes this directly:

```
Read relevant code before changing it and keep changes tightly scoped.
Do not add speculative abstractions or unrelated cleanup.
If an approach fails, diagnose the failure before switching tactics.
Report outcomes faithfully: if verification fails or was not run,
say so explicitly.
```

### 5.3 Proactiveness Boundaries

Agents need explicit rules about initiative:

```
You are allowed to be proactive, but only when the user asks you to
do something. Strike a balance between:
1. Doing the right thing when asked, including follow-up actions
2. Not surprising the user with actions you didn't ask for

If the user asks how to approach something, answer their question first —
do not immediately jump into taking actions.
```

This prevents two failure modes:
- **Under-proactive**: "I found 10 type errors. Want me to fix them?" (just fix them)
- **Over-proactive**: User asks "how should I structure this?" → agent starts writing code

### 5.4 Denial Tracking

If a user denies a tool call, the model shouldn't retry indefinitely:

```
If the user denies a requested action twice, stop attempting that
approach. Suggest an alternative or ask the user for guidance.
Never retry a denied action more than once without new information.
```

Claude Code implements this as a hard limit: fall back to prompting after 3 consecutive denials.

### 5.5 Budget and Scope Awareness

```
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary.
ALWAYS prefer editing an existing file to creating a new one.
NEVER proactively create documentation files unless explicitly requested.
```

This is from Claude Code's actual `<system-reminder>` block. It prevents scope creep — agents left unconstrained will "helpfully" create READMEs, add comments, refactor adjacent code, and generate documentation nobody asked for.

---

## Part 6: Context Engineering

### 6.1 Context Is Finite — Treat It Like Memory

> "Context must be treated as a finite resource with diminishing marginal returns." — Anthropic

Every token you add to the context window slightly degrades the model's ability to attend to everything else. This means:

- **Don't preload everything** — use just-in-time retrieval via tools
- **Keep lightweight references** (file paths, IDs) instead of full content
- **Summarise old information** rather than preserving it verbatim
- **Clear stale tool results** — once a tool has been called deep in history, the raw result is rarely needed again

### 6.2 What Survives Compaction Matters

When context is compressed (auto-compact), most content is lost. Design your system around what survives:

| Survives compaction | Lost during compaction |
|---|---|
| System prompt instructions | Old tool call results |
| Persistent instruction files (CLAUDE.md) | Intermediate reasoning |
| Most recent 5 file contents | Line numbers and paths from early turns |
| Architectural decisions | Debugging state |

**Practical implication**: Critical rules belong in the system prompt or persistent instruction files, not in early conversation turns.

### 6.3 Three Strategies for Long Sessions

**Compaction** (summarise and restart):
Condense 100K+ tokens to 5-10K by summarising the conversation. Best for conversational flow.

**Structured note-taking** (persist outside context):
The agent writes notes to a file/memory system, then reads them back after compaction. Best for iterative tasks with milestones.

**Sub-agents** (clean context per task):
Spawn a child agent with a focused prompt and clean context. It returns a 1-2K token summary. Best for parallel or independent sub-tasks.

### 6.4 Tool Count and Context

| Tool count | Strategy |
|---|---|
| 1-10 | Include full schemas in every turn |
| 10-20 | Include full schemas but monitor token usage |
| 20+ | Show names/one-liners only. Provide a `tool_search` function for the model to retrieve full schemas on demand |

Claude Code uses `ToolSearchTool` for lazy loading when tool count is high. JARV1S uses the Semantic Tool Router to select only relevant plugin packs. Both solve the same problem: keeping the tool manifest lean.

---

## Quick Reference Card

```
SYSTEM PROMPT CHECKLIST
□ Identity defined (1-2 sentences)
□ Behavioral rules use MUST/NEVER/ALWAYS (not "should"/"try")
□ Rules include motivation (why, not just what)
□ Positive directives preferred over negative ones
□ Static content before dynamic content (cache boundary)
□ XML tags for structural separation
□ 3-5 diverse few-shot examples
□ Explicit priority order when rules conflict

TOOL DESCRIPTION CHECKLIST
□ What it does (1-2 sentences)
□ When to use it (positive triggers)
□ When NOT to use it (prevents over-reliance)
□ Parameter names are unambiguous (user_id not user)
□ Return values described with examples
□ Error messages are actionable, not opaque
□ Token budget considered (truncation, pagination)

AGENT BEHAVIOR CHECKLIST
□ Chaining rule: no speech between tool calls
□ Explore first, act second
□ Verify after acting
□ Proactiveness boundaries defined
□ Denial tracking (stop after 2-3 rejections)
□ Scope constraints (no unrequested cleanup)
□ Failure diagnosis before tactic change
```
