# Agent Home

JARV1S keeps product behavior in packaged files and user-owned overlays in one
directory: `$JARVIS_DATA_DIR/home` (`.data/home` in development; Application
Support in the packaged app).

## Ownership

| Layer | Owner | Role |
| :--- | :--- | :--- |
| Packaged `SYSTEM.md` | Product | Cross-cutting reliability, tool-use, and delivery contract |
| Agent Home files | User | Persona, standing preferences, skill procedures, extra MCP servers |
| MongoDB | Runtime | Memory and state: profile facts, archival recall, conversations, triggers |
| Plugins / MCP | Runtime | Capabilities. Consent, trust, and tool availability stay here |
| Protocols | Runtime | Durable tracked workflows |

`PROMPT.md` controls personality and working style. Runtime context, available
tools, consent state, and tool results remain authoritative.

## Layout

```text
$JARVIS_DATA_DIR/home/
  AGENTS.md                 # maintenance notes for external agents; not injected
  PROMPT.md                 # identity, personality, tone, and preferences
  skills/<name>/SKILL.md    # Agent Skills standard
  skills/<name>/{scripts,references}/...
  mcp.json                  # extra stdio/HTTP MCP servers; no secrets
```

Missing files are harmless. Startup seeds any that are absent and never
overwrites user edits. JARV1S writes home files only after an explicit user
request, using the existing `files.*` tools and receipts.

## Prompt layering

For direct JARV1S turns, packaged `SYSTEM.md` is the deterministic static
prefix. The dynamic prompt adds `PROMPT.md`, a compact skill catalog, Mongo
profile facts, and runtime facts last. Tool schemas are sent separately through
the provider API.

In-process background workers use the focused packaged `BACKGROUND.md` plus
the Home skill catalog, operational profile facts, and runtime facts. They do
not receive `PROMPT.md`; skills remain reusable procedures independent of
assistant personality.

`mode="code"` subprocess prompts do not receive home overlays. Project-native
agent files govern coding workers.

## Skills

JARV1S scans exactly `skills/*/SKILL.md`, parses YAML frontmatter (`name`,
`description`, optional `compatibility`), and lists metadata plus the absolute
`SKILL.md` path. The model must call `files.read` before following a skill.
Skill bodies are not injected globally. There is no skill executor: scripts run
through `system.exec` and its approval rules. Skills are not plugins.

Invalid UTF-8, duplicate names, invalid Agent Skills names, and symlink escapes
are skipped per file. Valid files still load.

## MCP

Packaged `backend/mcp_servers.json` and `home/mcp.json` share one
`{ "servers": [...] }` JSON shape. Home may add stdio or HTTP servers only —
not Composio entries or trust overrides. Packaged and plugin names win;
collisions are validation errors.

Reload is explicit through the existing integrations refresh API/tool. Invalid
refresh keeps the live servers. Removed servers are torn down before the new
set is registered. Stdio children inherit a safe base environment plus
explicitly configured values; inline secret-looking env values are rejected.
Use `${VAR}` references. Resolved secrets are not returned in status or logs.

## Memory

Mongo profile facts and archival `recall()` remain canonical. This slice does
not add `MEMORY.md` or a file/Mongo sync layer.
