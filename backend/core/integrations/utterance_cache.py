"""
Disk-backed plugin utterance cache.

Plugins without hand-written or curated utterances get a one-time LLM pass to
synthesize compact example phrases a user would say when they want that plugin.
Results are cached on disk keyed by plugin description, sibling context, tool
names, and first-line docs. The cache is disposable runtime state; curated
voice-critical phrases live under ``core/routing/utterances``.

Hand-written utterances always win — callers short-circuit before reaching this
module.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core.config import settings

logger = logging.getLogger(__name__)

_CACHE_DIR = settings.CACHE_DIR / "utterances"
_GENERATOR_VERSION = "v3"
_TARGET_COUNT = 10
_MIN_ACCEPTABLE = 4
_MAX_WORDS = 14
_DOC_SNIPPET_LINES = 5
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "onto",
    "your", "user", "users", "tool", "tools", "plugin", "jarvis",
}
_GENERIC_CONFIRMATION_RE = re.compile(
    r"\b(?:yes|yep|yeah|ok|okay|go ahead|do it|that one|send it|book it)\b",
    re.IGNORECASE,
)
_MULTI_ACTION_RE = re.compile(
    r"\b(?:and then|then|also)\b",
    re.IGNORECASE,
)


def _cache_path(plugin_name: str) -> Path:
    return _CACHE_DIR / f"{plugin_name}.json"


def _doc_snippet(func: Callable) -> str:
    doc = (func.__doc__ or "").strip()
    if not doc:
        return ""
    lines = [
        line.strip()
        for line in doc.splitlines()
        if line.strip()
    ]
    return " ".join(lines[:_DOC_SNIPPET_LINES])[:420]


def _version_hash(
    pairs: Iterable[tuple[str, str]],
    *,
    plugin_description: str = "",
    sibling_summaries: Sequence[tuple[str, str]] = (),
) -> str:
    sibling_data = "|".join(f"{name}::{desc}" for name, desc in sorted(sibling_summaries))
    tool_data = "|".join(f"{n}::{d}" for n, d in sorted(pairs))
    data = (
        f"{_GENERATOR_VERSION}|"
        f"description::{plugin_description}|"
        f"siblings::{sibling_data}|"
        f"tools::{tool_data}"
    )
    return hashlib.sha1(data.encode()).hexdigest()[:12]


def _tool_pairs(tools: dict[str, Callable]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for name, func in tools.items():
        pairs.append((name, _doc_snippet(func)))
    return pairs


def _read_cache(plugin_name: str, version: str, *, allow_stale: bool = False) -> list[str] | None:
    path = _cache_path(plugin_name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        utterances = payload.get("utterances")
        if not isinstance(utterances, list):
            return None
        if payload.get("version_hash") == version or allow_stale:
            return _validate_utterances([u for u in utterances if isinstance(u, str)])
    except Exception as e:
        logger.debug("utterance cache read failed for %s: %s", plugin_name, e)
    return None


def _write_cache(
    plugin_name: str,
    version: str,
    utterances: list[str],
    *,
    model: str = "",
) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(plugin_name).write_text(
            json.dumps({
                "version_hash": version,
                "generator_version": _GENERATOR_VERSION,
                "utterances": utterances,
                "model": model,
            }, indent=2)
        )
    except Exception as e:
        logger.warning("utterance cache write failed for %s: %s", plugin_name, e)


def _param_names(func: Callable) -> list[str]:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return []
    return [
        p.name for p in sig.parameters.values()
        if p.name != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ][:5]


def _tool_lines(tools: dict[str, Callable]) -> list[str]:
    lines: list[str] = []
    for name, func in tools.items():
        doc = _doc_snippet(func)
        params = ", ".join(_param_names(func))
        suffix = f" args=({params})" if params else ""
        lines.append(f"- {name}{suffix}: {doc}" if doc else f"- {name}{suffix}")
    return lines


def _build_prompt(
    plugin_name: str,
    description: str,
    tools: dict[str, Callable],
    sibling_summaries: Sequence[tuple[str, str]] = (),
) -> str:
    tool_lines = "\n".join(_tool_lines(tools))
    sibling_lines = "\n".join(
        f"- {name}: {desc}" for name, desc in sibling_summaries if name != plugin_name
    ) or "- none"
    return (
        f"Plugin: {plugin_name}\n"
        f"Description: {description}\n"
        f"Tools:\n{tool_lines}\n\n"
        f"Nearby plugin namespaces to avoid confusing with:\n{sibling_lines}\n\n"
        "Generate high-precision routing examples for a low-latency voice assistant.\n"
        f"Return JSON only with keys: positive, notes.\n"
        f"- positive: exactly {_TARGET_COUNT} short, natural voice utterances that should route ONLY to this plugin.\n"
        "- notes: one short sentence describing the routing boundary.\n"
        "Positive mix: include direct commands, questions, capability/access checks, and short follow-ups "
        "only when the follow-up still names this plugin's domain clearly.\n"
        "Rules: keep positives under 14 words, vary verbs and nouns, use natural spoken phrasing, "
        "avoid copying tool names verbatim unless users would naturally say them, avoid generic chatter, "
        "avoid generic confirmations like 'yes go ahead', do not mention nearby plugin names, and do not "
        "include multi-action requests that need a second plugin."
    )


def _parse_json_payload(raw: str) -> tuple[list[str], str] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    positive = payload.get("positive") if isinstance(payload, dict) else None
    notes = payload.get("notes") if isinstance(payload, dict) else ""
    if not isinstance(positive, list):
        return None
    return (
        [x for x in positive if isinstance(x, str)],
        notes if isinstance(notes, str) else "",
    )


def _parse_utterances(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # Strip numbering, bullets, surrounding quotes.
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s)
        s = s.strip(" \t\"'`")
        if s:
            lines.append(s)
    return lines


def _normalize_utterance(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" \t\"'`")
    return text[:1].upper() + text[1:] if text else ""


def _validate_utterances(
    utterances: list[str],
    *,
    forbidden_terms: Sequence[str] = (),
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    forbidden = {
        term.lower()
        for term in forbidden_terms
        if term and len(term) > 2
    }
    for raw in utterances:
        text = _normalize_utterance(raw)
        if not text:
            continue
        words = text.split()
        if len(words) > _MAX_WORDS:
            continue
        lowered = text.lower()
        if _GENERIC_CONFIRMATION_RE.search(lowered):
            continue
        if _MULTI_ACTION_RE.search(lowered):
            continue
        if any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in forbidden):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _sibling_forbidden_terms(
    plugin_name: str,
    sibling_summaries: Sequence[tuple[str, str]],
) -> list[str]:
    terms: list[str] = []
    for name, _desc in sibling_summaries:
        if name == plugin_name:
            continue
        terms.append(name.replace("_", " "))
        terms.extend(_words_from_identifier(name))
    return terms


def _words_from_identifier(text: str) -> list[str]:
    text = re.sub(r"[_\-]", " ", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return [
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", text)
        if len(word) > 2 and word.lower() not in _STOPWORDS
    ]


def _heuristic_utterances(
    plugin_name: str,
    description: str,
    tools: dict[str, Callable],
) -> list[str]:
    candidates: list[str] = []
    plugin_words = " ".join(_words_from_identifier(plugin_name))
    if plugin_words:
        candidates.append(plugin_words)

    desc_words = " ".join(_words_from_identifier(description)[:5])
    if desc_words:
        candidates.append(desc_words)

    for tool_name, func in tools.items():
        tool_words = " ".join(_words_from_identifier(tool_name))
        if tool_words:
            candidates.append(tool_words)
        doc = (func.__doc__ or "").strip()
        first_line = doc.split("\n", 1)[0].strip() if doc else ""
        if first_line:
            candidates.append(first_line)

    return _validate_utterances(candidates)[:_TARGET_COUNT]


async def load_or_generate(
    plugin_name: str,
    description: str,
    tools: dict[str, Callable],
    llm_service: Any | None,
    sibling_summaries: Sequence[tuple[str, str]] = (),
    *,
    allow_stale_cache: bool = False,
) -> list[str]:
    """Return cached utterances for this plugin, or generate + cache via the LLM.

    LLM failures fall back to deterministic heuristic phrases so a configured
    but flaky generation model does not silently remove a plugin from routing.
    """
    pairs = _tool_pairs(tools)
    version = _version_hash(
        pairs,
        plugin_description=description,
        sibling_summaries=sibling_summaries,
    )

    cached = _read_cache(plugin_name, version, allow_stale=allow_stale_cache)
    if cached:
        return cached

    if llm_service is None:
        return _heuristic_utterances(plugin_name, description, tools)

    prompt = _build_prompt(plugin_name, description, tools, sibling_summaries)
    try:
        raw = await llm_service.chat(
            user_message=prompt,
            system_prompt=(
                "You generate compact, high-precision routing examples for a voice assistant. "
                "You must return valid JSON only."
            ),
            temperature=0.2,
            dump_tag=f"utterance-gen:{plugin_name}",
        )
    except Exception as e:
        logger.warning("utterance generation failed for %s: %s", plugin_name, e)
        return _heuristic_utterances(plugin_name, description, tools)

    parsed = _parse_json_payload(raw)
    forbidden_terms = _sibling_forbidden_terms(plugin_name, sibling_summaries)
    if parsed:
        positives, _notes = parsed
    else:
        positives = _parse_utterances(raw)

    utterances = _validate_utterances(positives, forbidden_terms=forbidden_terms)[:_TARGET_COUNT]
    if len(utterances) < _MIN_ACCEPTABLE:
        fallback = _heuristic_utterances(plugin_name, description, tools)
        utterances = _validate_utterances(
            [*utterances, *fallback],
            forbidden_terms=forbidden_terms,
        )[:_TARGET_COUNT]

    if len(utterances) < _MIN_ACCEPTABLE:
        logger.warning(
            "utterance generation for %s returned only %d lines; skipping cache",
            plugin_name, len(utterances),
        )
        return utterances

    _write_cache(
        plugin_name,
        version,
        utterances,
        model=getattr(llm_service, "model", ""),
    )
    logger.info("generated %d utterances for plugin '%s'", len(utterances), plugin_name)
    return utterances
