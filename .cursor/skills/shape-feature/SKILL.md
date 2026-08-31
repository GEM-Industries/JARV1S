---
name: shape-feature
description: >-
  Shape JARV1S product work before implementation: user job, real-use moments,
  existing axes, data owner, and V0 cut. Use when writing or reviewing an
  implementation plan, using Plan mode, designing a new feature or capability,
  adding a product surface, proposing architecture, or introducing a new module,
  collection, coordinator, or noun. Skip bugfixes, prompt-only edits, tests,
  and one-file mechanical changes.
---

# Shape Feature

Plan-time counterpart of prompt-refine. Fill Shape **before** architecture, file lists, or code.

Cursor Plan mode is: clarify → research → plan → you review → build. This skill owns **clarify** and the **first section of the plan**. If already in Agent on new product work, still fill Shape before editing.

## When

Use for new user-facing behavior, new durable state, new modules, or architecture choices.

Skip: a bug in an existing path, prompt-only work (`prompt-refine`), tests, and one-file mechanical edits.

## Core rule

Do not invent a new noun, coordinator, or collection until Shape shows the owner is missing.

If Shape is incomplete, ask **at most three** questions (Job, Moments, Owner, or Cut), then stop. Do not paper over gaps with diagrams.

If two axes share one field, unbraid before building. A new Coordinator that owns batching *and* interruption *and* settlement is three axes in one noun — extend the existing owners instead.

## Shape (first section of the plan)

```markdown
## Shape
- Job: <what the user can do after this ships — no subsystem names>
- Moments: <2–3 real uses, including a failure: where, which device, what they said>
- Axes: <identity / time / delivery / attention / consent / truth — only those touched>
- Record: <object + store; who writes; who reads>
- Owner: <existing module that extends>
- Cut: <V0 is … ; not …>
- Invariant: When [condition], JARV1S should [behavior] because [reason].
```

Write the rest of the plan **inside** Owner and Cut.

### Axes (do not invent a seventh)

| Axis | Meaning |
| :--- | :--- |
| identity | owner, node, connection, speaker, room |
| time | now vs later, freshness, recurrence, inactivity |
| delivery | speak / show / silent; tell / offer / act; origin vs fire-time endpoint |
| attention | may I interrupt (`active` / `quiet` / `paused`); not session mute |
| consent | may I mutate |
| truth | one writer per record; context is a cache |

### Cut

Household, follow-me, load, extra channels, and extra nouns stay out until a walked moment requires them. Do not enumerate hypothetical edge cases.

## Example

```markdown
## Shape
- Job: Ask JARV1S to go do the checkout PR, then pick that work back up by name.
- Moments: "keep going on the checkout PR" after Home forgot; Host lid closed, worker still running.
- Axes: truth (work lineage vs run); time (Home window vs work that lasts).
- Record: `background_tasks.work_id`; `plugins/agents` writes; roster and `resume` read.
- Owner: `plugins/agents`. Not a Job collection.
- Cut: V0 is a stable title + `work_id` over existing tasks. Not a Project, chat session, or running steer.
- Invariant: When the user names unfinished delegated work, JARV1S should resolve the work title, not a dead `task_id`, because resume mints a new run.
```
