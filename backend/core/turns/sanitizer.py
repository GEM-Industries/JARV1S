"""Assistant-output sanitization before delivery, persistence, or reuse as context."""

from __future__ import annotations

import re


# Gemma (and similar) may emit a lone channel label when reasoning markers
# are stripped or abbreviated before the executable tool-call delimiter.
_LEADING_REASONING_LABEL_BEFORE_CODE = re.compile(
    r"^\s*(?:thought|analysis|reasoning|plan|tool)\s*:?\s*\n*(?=<tool_call>)",
    re.IGNORECASE | re.MULTILINE,
)

_LEADING_REASONING_LABEL_RE = re.compile(
    r"^\s*(?:(?:thought|analysis|reasoning|plan|tool)\s*:?\s*)+(?=\n|$|<tool_call>)",
    re.IGNORECASE,
)

_BOUNDARY_MARKUP_LEAK_RE = re.compile(
    r"\A\s*<\s*(?:sup|code)\s*>\s*|\s*<\s*/\s*(?:sup|code)\s*>\s*\Z",
    re.IGNORECASE,
)

_PROVIDER_CONTROL_TOKEN_RE = re.compile(
    r"<\|(?:start|im_start|start_header_id)\|>\s*"
    r"(?:system|developer|user|assistant|tool|function)?\s*"
    r"|<\|(?:end|message|channel|constrain|return|call|im_end|end_header_id|eot_id|begin_of_text|endoftext)\|>\s*"
    r"(?:analysis|commentary|final|thought|content)?\s*"
    r"|<\|channel\>\s*(?:analysis|commentary|final|thought|content)?\s*"
    r"|<channel\|>\s*",
    re.IGNORECASE,
)

_INLINE_PROVIDER_LABEL_RE = re.compile(
    r"(?P<prefix>(?<=\S)\s+)"
    r"(?:<\|(?:message|channel)\|>|<\|channel\>|<channel\|>)\s*"
    r"(?P<label>analysis|commentary|final|thought|content)(?=\s|[.,;:!?)]|$)",
    re.IGNORECASE,
)

_TOOL_RESULT_BLOCK_RE = re.compile(
    r"\s*<\s*tool_result\s*>.*?<\s*/\s*(?:tool_result|_result)\s*>\s*",
    re.IGNORECASE | re.DOTALL,
)

_INLINE_CONTROL_LABEL_WORD_RE = re.compile(
    r"(?P<prefix>(?<=\S)\s+)<\s*(?P<label>thought|analysis|final|content|code)\s*>(?=\s|[.,;:!?)]|$)",
    re.IGNORECASE,
)

_LEADING_CONTROL_TAG_RE = re.compile(
    r"\A(?:\s*<\s*(?:thought|analysis|final|content|code)\s*>\s*)+",
    re.IGNORECASE,
)

_TRAILING_CONTROL_TAG_RE = re.compile(
    r"(?:\s*<\s*/\s*(?:thought|analysis|final|content|code)\s*>\s*)+\Z",
    re.IGNORECASE,
)

_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MARKDOWN_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_MARKDOWN_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_MARKDOWN_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$", re.MULTILINE)
_MARKDOWN_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_MARKDOWN_EMPHASIS_RE = re.compile(
    r"(\*\*\*|___|\*\*|__|\*|_|~~|`)(?=\S)|(?<=\S)(\*\*\*|___|\*\*|__|\*|_|~~|`)"
)


def sanitize_assistant_output(text: str) -> str:
    """Remove provider/chat-template control tokens from assistant-visible text.

    Keep this content-light and deterministic: no model-specific parsing tree, no
    transcript storage, just strip leaked channel/control markers before text can
    reach users, MongoDB, or future LLM context.
    """
    if not text:
        return ""
    cleaned = _INLINE_PROVIDER_LABEL_RE.sub(lambda match: f"{match.group('prefix')}{match.group('label')}", text)
    cleaned = _PROVIDER_CONTROL_TOKEN_RE.sub("", cleaned)
    had_tool_result = bool(_TOOL_RESULT_BLOCK_RE.search(cleaned))
    cleaned = _TOOL_RESULT_BLOCK_RE.sub(" ", cleaned)
    cleaned = _BOUNDARY_MARKUP_LEAK_RE.sub("", cleaned)
    cleaned = _LEADING_REASONING_LABEL_RE.sub("", cleaned)
    cleaned = _LEADING_REASONING_LABEL_BEFORE_CODE.sub("", cleaned)
    cleaned = _INLINE_CONTROL_LABEL_WORD_RE.sub(lambda match: f"{match.group('prefix')}{match.group('label')}", cleaned)
    cleaned = _LEADING_CONTROL_TAG_RE.sub("", cleaned)
    cleaned = _TRAILING_CONTROL_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"(?:thought\s*){3,}", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    if had_tool_result:
        cleaned = cleaned.strip()
    return cleaned


def sanitize_tts_text(text: str) -> str:
    """Convert assistant text to a plain transcript for TTS providers.

    Cartesia handles plain text and supported SSML-like tags, not Markdown. Keep
    this as formatting-only cleanup so display text can remain untouched.
    """
    cleaned = sanitize_assistant_output(text)
    if not cleaned:
        return ""

    cleaned = _MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = _MARKDOWN_FENCE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_HORIZONTAL_RULE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_HEADING_RE.sub("", cleaned)
    cleaned = _MARKDOWN_BLOCKQUOTE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_LIST_MARKER_RE.sub("", cleaned)
    cleaned = _MARKDOWN_EMPHASIS_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
