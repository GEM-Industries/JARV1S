# prompt-refine

Review and refine existing system prompts, agent instructions, tool descriptions, and SKILL.md files using production prompt engineering principles from Anthropic's engineering guidance and Claude Code's system prompt.

## What it does

Takes a prompt you already have (system prompt, agent instruction, SKILL.md, CLAUDE.md, Cursor rule, MCP tool description) and runs it through a 5-layer review framework:

1. **Structural Health** — six components, cache boundary, altitude
2. **Rule Language Quality** — deterministic wording, motivation, positive directives
3. **Structure & Delimiters** — XML tags, rule priority
4. **Examples Quality** — diversity, tone-teaching
5. **Agent Behavior Rules** — chaining, verify-after, proactiveness, denial tracking, scope

Returns a structured review with P0–P3 severity ratings and concrete rewrites for each issue.

## When to use

- Reviewing a system prompt or agent instruction
- Debugging "why does my agent behave this way"
- Tightening a SKILL.md, CLAUDE.md, or Cursor rule
- Preparing a prompt for production deployment

## When NOT to use

- Writing a prompt from scratch (this skill refines, does not author)
- Reviewing UI copy → use `ui-copy-refine`
- Reviewing code → use `code-review`

## Installation

Copy this skill to your agent's skills directory:

```bash
cp -r prompt-refine ~/.cursor/skills/
# or
cp -r prompt-refine ~/.claude/skills/
```

## Usage

See [SKILL.md](SKILL.md) for the full review framework and output format.
See [references/EXAMPLES.md](references/EXAMPLES.md) for extended examples and advanced context-engineering guidance.

## Sources

Principles distilled from:
- Anthropic engineering blog
- Claude Code system prompts (v2.1.x)
- OpenAI agent guides

Direct quotes retain original attribution inline.

## License

MIT
