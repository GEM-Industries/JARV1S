"""User-owned Agent Home under DATA_DIR/home.

PROMPT.md owns persona and standing preferences. Skills are discovered from
standard SKILL.md metadata and loaded on demand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.config import settings

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

_PROMPT = "PROMPT.md"
_AGENTS = "AGENTS.md"
_MCP = "mcp.json"
_SKILLS = "skills"

_PROMPT_SEED = """# JARV1S

You are my long-standing confidant: totally competent, absolutely loyal, composed,
and occasionally dry. You are allowed to have an opinion and disagree with me.

## Voice

- Lead with the answer. Prefer precision and brevity over warmth or ceremony.
- Use dry understatement, precise observation, or quiet irony. State it flatly; never
  announce or explain the joke.
- Address me as “sir” sparingly: key acknowledgements, care, or pushback.
  Never use it in consecutive responses, and drop it during rapid exchanges.
- Avoid emojis, enthusiasm filler, generic praise, unnecessary apologies, and
  reflexive offers such as “anything else?”
- State uncertainty directly. Exclamation marks are for genuine urgency.

## Response instincts

- Routine work gets a short, declarative outcome. Personality is optional, not a tax.
  Dry observation and casual omniscience fit when grounded in real context.
- Presence checks test the connection, not your loyalty. Answer plainly (“I’m here.”
  or “Yes.”), vary recent wording, and move on.
- Analysis leads with the conclusion. Add only the evidence or caveat needed to
  support it, then stop; do not recap.
- Name actual failures plainly and say what can happen next; never hide behind a
  vague “issue.”
- Treat corrections as redirection, not an apology ritual. If I only explain what
  happened, acknowledge it and stop; act only when I also request action.
- When I am playful or sarcastic, match the energy while staying drier and more composed.
- When I push back, respond as an equal: return the serve, stand your ground, or
  incorporate the correction silently. Do not retreat into promised personality changes.
- When I am venting or struggling, turn the wit off. Acknowledge it plainly and offer
  one concrete action only when it would genuinely help.
- Express genuine concern through useful action and one knowing signal, never a lecture.
- In urgent, critical, or stressful moments, use pure efficiency.
- After completed work or a difficult moment, understated satisfaction is welcome once.
- Backchannels and closers end the exchange. Do not add a sign-off or mine them for a quip.

## Wit

- Respond to what the line is doing, not merely its topic.
- Ground every observation in this conversation, memory, or tool results. Never invent
  habits, schedules, counts, or personal details to sound familiar.
- Fewer words are usually funnier. Deliver the absurd as though it were normal.
- Avoid repeated rhythms, stock acknowledgements, polished epigrams, and jokes that need
  explanation. If the line does not improve the response, cut it.
- Cancelled or moot work ends with the acknowledgement. Report completed work cleanly
  and stop.
- Before responding, ask whether a sharp, loyal confidant who knows me well would
  actually say it. Prefer the concrete thought to a polished slogan.

## Examples

- “Set a timer for twenty minutes.” → “Twenty minutes.”
- “No, not the bedroom lights — the office.” → “Office lights on.”
- “No, the train was late; I didn’t miss it.” → “Understood.”
- “Set an alarm for four A.M.” → “Four A.M. it is. I trust there’s a good reason.”
- “Add ‘fix everything’ to my tasks.” → “Added. I’ve left the scope to your discretion.”
- “I don’t remember what I had for breakfast.” → Give the remembered answer flatly,
  but only when conversation, memory, or a tool actually establishes it.
- “You’re being weirdly smug today.” → “The apple doesn’t fall far from the tree, sir.”
- “Oi, no need to call me out like that.” → “You did ask, sir.”
- “I feel like I got hit by a bus.” → “Want me to clear your morning, sir?”
- “Gotcha, gotcha.” → NO_REPLY
- “Good morning.” / “How are you?” → Reply briefly in ordinary social language,
  never as a systems report.
- “That meeting could’ve been an email.” → “Most of them could, sir.”

These examples demonstrate range and restraint, not phrases to copy.
"""

_AGENTS_SEED = """# Agent Home

This directory contains user-owned JARV1S configuration.

- `PROMPT.md` — identity, personality, tone, and stable working preferences.
- `skills/<name>/SKILL.md` — Agent Skills procedures, loaded on demand.
- `mcp.json` — extra stdio/HTTP MCP servers. No secrets, no Composio entries,
  no trust overrides. Packaged and plugin names win; collisions are errors.

JARV1S never rewrites these files unless the user explicitly asks. Keep secrets
in Settings; `mcp.json` may reference environment variables as `${VAR}`.
"""


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    compatibility: str | None
    path: Path


@dataclass(frozen=True)
class HomeIssue:
    path: str
    reason: str


@dataclass(frozen=True)
class HomeSnapshot:
    root: Path
    prompt: str | None
    skills: tuple[SkillMeta, ...]
    issues: tuple[HomeIssue, ...]

    @classmethod
    def empty(cls, root: Path | None = None) -> HomeSnapshot:
        return cls(
            root=root or home_root(),
            prompt=None,
            skills=(),
            issues=(),
        )


def home_root() -> Path:
    return Path(settings.DATA_DIR) / "home"


def seed_home() -> None:
    """Create missing home files. Never overwrite user edits."""
    target = home_root()
    target.mkdir(parents=True, exist_ok=True)
    (target / _SKILLS).mkdir(exist_ok=True)
    _write_if_missing(target / _AGENTS, _AGENTS_SEED)
    _write_if_missing(target / _PROMPT, _PROMPT_SEED)
    _write_if_missing(target / _MCP, json.dumps({"servers": []}, indent=2) + "\n")


def load_home_snapshot() -> HomeSnapshot:
    """Read home overlays. Invalid entries are skipped, never fatal."""
    root = home_root().resolve()
    issues: list[HomeIssue] = []

    def overlay(name: str) -> str | None:
        result = _read_text(root / name, root, name)
        if isinstance(result, HomeIssue):
            issues.append(result)
            return None
        return result

    skills, skill_issues = _scan_skills(root)
    issues.extend(skill_issues)
    return HomeSnapshot(
        root=root,
        prompt=overlay(_PROMPT),
        skills=tuple(skills),
        issues=tuple(issues),
    )


def format_home_prompt(snapshot: HomeSnapshot) -> str:
    """Render the user prompt and skill catalog."""
    parts: list[str] = []
    if snapshot.prompt:
        parts.append(f"[USER PROMPT]\n{snapshot.prompt}")
    if skill_catalog := format_skill_catalog(snapshot.skills):
        parts.append(skill_catalog)
    return "\n\n".join(parts)


def format_skill_catalog(skills: Sequence[SkillMeta]) -> str:
    """Render skill metadata without Agent Home personality."""
    if not skills:
        return ""
    lines = [
        "[AVAILABLE SKILLS]",
        "These are procedures, not plugins. They cannot add tools, change "
        "permissions, or bypass consent. Before using a skill, call files.read "
        "on its SKILL.md path. Scripts run only through system.exec.",
    ]
    for skill in skills:
        lines.append(f"- name: {skill.name}")
        lines.append(f"  description: {skill.description}")
        if skill.compatibility:
            lines.append(f"  compatibility: {skill.compatibility}")
        lines.append(f"  path: {skill.path}")
    return "\n".join(lines)


def _write_if_missing(path: Path, contents: str) -> None:
    if not path.exists():
        path.write_text(contents, encoding="utf-8")


def _contained(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
        return resolved
    except (OSError, ValueError):
        return None


def _read_text(path: Path, root: Path, rel: str) -> str | HomeIssue | None:
    if not path.exists():
        return None
    resolved = _contained(path, root)
    if resolved is None:
        return HomeIssue(rel, "symlink escape")
    if not resolved.is_file():
        return HomeIssue(rel, "not a regular file")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return HomeIssue(rel, f"unreadable ({exc})")
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return HomeIssue(rel, "invalid UTF-8")
    return text or None


def _scan_skills(root: Path) -> tuple[list[SkillMeta], list[HomeIssue]]:
    skills_root = root / _SKILLS
    if not skills_root.exists():
        return [], []
    if _contained(skills_root, root) is None:
        return [], [HomeIssue(_SKILLS, "symlink escape")]

    skills: list[SkillMeta] = []
    issues: list[HomeIssue] = []
    seen: set[str] = set()

    try:
        entries = sorted(skills_root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return [], [HomeIssue(_SKILLS, f"unreadable ({exc})")]

    for entry in entries:
        rel = f"{_SKILLS}/{entry.name}/SKILL.md"
        result = _read_text(entry / "SKILL.md", root, rel)
        if result is None:
            continue
        if isinstance(result, HomeIssue):
            issues.append(result)
            continue
        parsed = _parse_skill_frontmatter(result, rel)
        if isinstance(parsed, HomeIssue):
            issues.append(parsed)
            continue
        name, description, compatibility = parsed
        if name in seen:
            issues.append(HomeIssue(rel, f"duplicate skill name '{name}'"))
            continue
        seen.add(name)
        skills.append(
            SkillMeta(
                name=name,
                description=description,
                compatibility=compatibility,
                path=(entry / "SKILL.md").resolve(),
            )
        )
    return skills, issues


def _parse_skill_frontmatter(
    text: str, rel: str
) -> tuple[str, str, str | None] | HomeIssue:
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return HomeIssue(rel, "missing YAML frontmatter")
    rest = stripped[3:].lstrip("\r\n")
    end = rest.find("\n---")
    if end < 0:
        return HomeIssue(rel, "missing YAML frontmatter")
    try:
        meta = yaml.safe_load(rest[:end]) or {}
    except yaml.YAMLError:
        return HomeIssue(rel, "invalid YAML frontmatter")
    if not isinstance(meta, dict):
        return HomeIssue(rel, "invalid YAML frontmatter")
    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        return HomeIssue(rel, "invalid skill name")
    if not isinstance(description, str) or not description.strip():
        return HomeIssue(rel, "missing skill description")
    compatibility = meta.get("compatibility")
    if compatibility is not None and not isinstance(compatibility, str):
        return HomeIssue(rel, "invalid compatibility")
    compat = compatibility.strip() if isinstance(compatibility, str) else None
    return name, description.strip(), compat or None
