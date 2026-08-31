---
name: prompt-refine
description: Review and refine JARV1S prompts, agent instructions, tool docstrings, Cursor rules, and skills. Use when a prompt change is requested, an LLM behavior failure needs diagnosis, or work touches backend/core/prompts, PromptBuilder, system turn messages, tool descriptions, or SKILL.md files. Focuses on root-cause framing, eval/regression thinking, and avoiding hard-coded counterexamples. Do not use for product architecture or implementation plans; use shape-feature.
license: MIT
metadata:
  author: roy-songzhe-li
  version: "2.1.0"
  updated: "2026-06-08"
  based-on: Anthropic eval guidance, OpenAI prompt engineering/eval guidance, Cursor Skills guidance, JARV1S prompt architecture
---

# Prompt Refine

Use this skill to improve existing JARV1S prompts without overfitting to one bad transcript. Product jobs, new modules, and implementation plans belong in `shape-feature`, not here.

## Core Rule

Never "fix" a prompt failure by pasting the failed case, a narrow counterexample, or a reusable catchphrase into the prompt. Treat the failure as one data point. First decide whether the behavior came from instruction ambiguity, missing dynamic context, the wrong prompt surface, a tool/docstring issue, conflicting examples, model nondeterminism, or missing eval coverage.

Every prompt edit must be justified by a broader invariant:

```text
When [general condition], JARV1S should [desired behavior] because [reason].
```

If you cannot state the invariant, do not edit the prompt yet. Ask for the transcript, add an eval, inspect the assembled prompt, or recommend instrumentation instead.

## JARV1S Prompt Map

Read the relevant files before editing. The current prompt architecture is split across these surfaces:

- `backend/core/prompts/SYSTEM.md`: consolidated product-owned grounding, tool-use, reliability, and delivery contract.
- `backend/core/prompts/BACKGROUND.md` and `background.py`: focused in-process worker contract and operational context; no Home personality or interactive delivery rules.
- `backend/core/home.py` and `$JARVIS_DATA_DIR/home/PROMPT.md`: user-owned identity, personality, tone, and stable working preferences.
- `backend/core/prompts/builder.py`: `PromptBuilder` assembles the cacheable product prefix plus Home, profile, skills, and runtime context. It also owns `build_subprocess_prompt()`.
- `backend/core/prompts/system_turn_context.py`: user-message builder for system turns, automations, protocol runs, alerts, and task-result delivery.
- `backend/core/prompts/protocol_context.py`: protocol-specific run context.
- `backend/plugins/**/*.py`: tool docstrings are part of the prompt surface. For tool selection, arguments, return handling, and policy local to one tool, edit the tool docstring before adding global prompt rules.

The static product prompt must stay stable where possible. Dynamic facts such as time, timezone, source, modality, or user/session data belong in runtime context or system-turn messages, not `SYSTEM.md`. Offered tool names and argument schemas are sent separately via provider `tools=`.

For current-turn voice pacing, prompt placement matters as much as wording. If prior turns are bleeding into the current response, prefer a concise runtime-context rule near the end of the dynamic prompt over adding more static persona prose or tool-docstring policy.

## Refinement Workflow

### 1. Gather Evidence

Before proposing or making changes:

- Read the entire relevant prompt section and adjacent sections that may conflict with it.
- Identify the execution path: direct voice/text, in-process background, subprocess coding agent, system turn, protocol run, or tool docstring.
- If available, inspect the failure transcript, assembled prompt, tool results, and expected behavior.
- If the only evidence is "the model once did X", treat nondeterminism as a live possibility. Prefer a small eval or multiple trial checks before a broad prompt change.
- For subjective prompt tuning, use `tier: probe` cases as measurement, not regression gates. Run a baseline first, then compare after edits.

### 2. Classify The Failure

Pick the primary cause before editing:

- `prompt_gap`: the prompt lacks a general rule or motivation.
- `prompt_conflict`: two sections or examples imply different behavior.
- `context_gap`: the model lacked needed runtime/user/history/tool context.
- `wrong_surface`: the behavior belongs in a tool docstring, router utterance, runtime context, eval, or code path rather than a global prompt.
- `example_collision`: examples create the wrong recency, tone, or trigger bias.
- `nondeterminism`: the prompt is probably adequate but the model failed once or inconsistently.
- `eval_gap`: no regression coverage exists, so future prompt changes will remain guesswork.

If classification is uncertain, state the uncertainty and gather more evidence instead of adding instructions.

### 3. Choose The Smallest Correct Surface

Use this routing guide:

- Tone, identity, "sir", wit, warmth: Agent Home `PROMPT.md`.
- Cross-cutting direct-assistant grounding, tool use, and voice/text delivery: `SYSTEM.md`.
- In-process delegated-worker behavior: `BACKGROUND.md`.
- Current-turn modality, time, history, location, or source reminders: `builder.py`.
- Background coding-worker behavior: `build_subprocess_prompt()`.
- System-trigger behavior, `NO_REPLY`, event classification, task-result delivery: `system_turn_context.py` or `protocol_context.py`.
- Tool choice, parameters, output shape, tool-local policy: the plugin tool docstring or return schema.
- Prompt assembly, static/dynamic placement, mode inclusion, and current-turn runtime reminders: `builder.py`.

Do not add a global rule when a narrower prompt surface can solve the invariant.

Product/architecture plans, new modules, collections, or coordinators: `shape-feature`, not this skill.

### 4. Design The Change

Good prompt changes generalize. They usually:

- Replace a brittle branch with a principle plus motivation.
- Remove or rewrite conflicting instructions before adding new text.
- Prefer positive directives over negative-only prohibitions.
- Make examples diverse and canonical, not a list of near-duplicate edge cases.
- Preserve code-like tokens exactly: template variables, enum strings, XML tags, tool names, YAML keys, regexes, and API contract values.
- Keep wording concise. Every added token competes with tool schemas, history, and runtime context.
- When changing pacing, encode the category boundary, not the phrase. In-flight wait speech is delivery-owned; the model calls the tool and speaks only the outcome.

Avoid these anti-patterns:

- "Never say `<exact bad output>`" unless that exact token is a protocol violation.
- Adding the failed user phrase as a special case.
- Adding a catchphrase the model may repeat every time.
- Adding `ALWAYS` rules that silently affect voice, text, background, and system turns differently.
- Moving dynamic facts into `SYSTEM.md`.
- Adding examples that teach wording rather than behavior.

### 5. Check Adjacent Regressions

Before editing, write a small regression matrix in your own notes:

- Original failure: the case that motivated the change.
- Adjacent positive: a different case where the new invariant should also apply.
- Adjacent negative: a similar case where the behavior must not trigger.
- Mode check: whether voice, text, background, system, or protocol turns should differ.
- Example risk: whether new wording creates repetition, over-triggering, or tone drift.
- History risk: whether a previous turn with the opposite pacing could bias the next turn.

For important behavior, add or update the smallest useful test or eval. Balanced evals matter: include cases where the behavior should happen and cases where it should not. One-sided prompt examples and one-sided evals both create over-triggering.
For history-sensitive behavior, add paired probes: previous-chatty → current-silent and previous-silent → current-chatty. Keep fixture outputs clear enough that the probe measures the prompt behavior, not retry/error handling.

### 6. Verify The Result

After editing:

- Re-read the assembled prompt order for the affected mode.
- Run the smallest relevant tests or evals when they exist.
- For nondeterministic failures, prefer multiple trials or an eval over claiming the prompt is fixed from one manual run.
- Report verification honestly. If no test/eval was run, say so.

## Output Expectations

When asked to edit prompts, make the focused edit directly. In the final response, include only:

- The prompt surface changed.
- The invariant the edit now encodes.
- Verification performed, or a clear note that none was run.

When asked for analysis or review, use this format:

```markdown
## Verdict
<1-2 sentence assessment>

## Root Cause
Classification: <prompt_gap | prompt_conflict | context_gap | wrong_surface | example_collision | nondeterminism | eval_gap>
<brief evidence>

## Proposed Change
Surface: `<file or prompt section>`
Invariant: <general behavior rule>
Rewrite: <exact proposed wording or summary>

## Regression Matrix
- Original failure: <case>
- Adjacent positive: <case>
- Adjacent negative: <case>
- Mode check: <affected modes>

## Verification
<tests/evals/manual checks>
```
