"""
Token-aware context budget management for the JARV1S agent.

Budget model:
  history_budget = CONTEXT_MAX_INPUT_TOKENS - system_tokens - LLM_MAX_TOKENS - loop_headroom

Three-phase compaction:
  1. Offload: cap oversized tool results to short previews.
  2. Summarize: when total history exceeds CONTEXT_SUMMARIZE_THRESHOLD of
     the budget, summarize the oldest messages into a compact block.
  3. Trim: fill from newest to oldest until budget is exhausted.
"""

import asyncio
import json
import logging
from typing import Optional

import tiktoken

from core.config import settings
from core.prompts import SystemPrompt
from services.database.mongodb import extract_text_content

logger = logging.getLogger(__name__)

_LOOP_HEADROOM = 10_000
_PREVIEW_CHARS = 2000
_IMAGE_TOKEN_ESTIMATE = 1_000

_enc = tiktoken.get_encoding("o200k_base")

_SUMMARY_SYSTEM_PROMPT = (
    "Compress this conversation into one dense paragraph (under 150 words). "
    "Preserve: topics, decisions, actions taken, names, and specific data. "
    "Omit: tool syntax, raw JSON, code blocks, filler. Third person past tense."
)


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _message_tokens(msg: dict) -> int:
    """Count tokens for a single message including role/formatting overhead (~4 tokens)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        total = count_tokens(content) + 4
    else:
        total = 4
        for part in content or []:
            if part.get("type") == "text":
                total += count_tokens(part.get("text", ""))
            elif part.get("type") == "image_url":
                total += _IMAGE_TOKEN_ESTIMATE
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        total += count_tokens(json.dumps(tool_calls, default=str))
    return total


def cap_tool_result(output: str) -> str:
    """
    Truncate a tool output string to a short preview if it exceeds the offload threshold.
    Called in the agent loop before appending output to local_history — NOT before
    yielding the event (the orchestrator and MongoDB receive the full output).
    """
    original_tokens = count_tokens(output)
    if original_tokens <= settings.CONTEXT_OFFLOAD_THRESHOLD:
        return output
    preview = output[:_PREVIEW_CHARS]
    return f"{preview}\n[... truncated from {original_tokens} tokens — data persisted by tool]"


def _is_tool_result_message(msg: dict) -> bool:
    if msg.get("role") == "tool":
        return True
    content = msg.get("content")
    return msg.get("role") == "user" and isinstance(content, str) and "<tool_result>" in content


def _offload_tool_results(history: list[dict]) -> list[dict]:
    """First pass: replace oversized tool result messages with short previews."""
    processed: list[dict] = []
    for msg in history:
        if _is_tool_result_message(msg) and isinstance(msg.get("content"), str):
            content: str = msg["content"]
            original_tokens = count_tokens(content)
            if original_tokens > settings.CONTEXT_OFFLOAD_THRESHOLD:
                preview = content[:_PREVIEW_CHARS]
                if msg.get("role") == "tool":
                    msg = {**msg, "content": (
                        f"{preview}\n"
                        f"[... truncated from {original_tokens} tokens — data persisted by tool]"
                    )}
                else:
                    msg = {**msg, "content": (
                        f"<tool_result>\n{preview}\n"
                        f"[... truncated from {original_tokens} tokens — data persisted by tool]\n"
                        "</tool_result>"
                    )}
        processed.append(msg)
    return processed


def _compute_history_budget(system_prompt: str | SystemPrompt) -> int:
    system_tokens = count_tokens(str(system_prompt))
    return (
        settings.CONTEXT_MAX_INPUT_TOKENS
        - system_tokens
        - settings.LLM_MAX_TOKENS
        - _LOOP_HEADROOM
    )


def _format_messages_for_summary(messages: list[dict]) -> str:
    """Convert message dicts to a readable transcript for the summarizer."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = extract_text_content(msg.get("content", ""))
        if msg.get("role") == "tool" or "<tool_result>" in content:
            content = content[:200] + ("..." if len(content) > 200 else "")
        elif len(content) > 400:
            content = content[:400] + "..."
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


async def compact_history(
    history: list[dict],
    system_prompt: str | SystemPrompt,
    llm_service: Optional[object] = None,
    session_id: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """
    Two-phase context compaction: offload tool results, then summarize + trim.

    If total history tokens exceed CONTEXT_SUMMARIZE_THRESHOLD of the budget AND
    an LLM service is provided, the oldest messages are summarized into a compact
    block before the newest-first fill. Otherwise falls back to truncation only.

    When messages are dropped and session_id is provided, a background task
    backfills embeddings for any un-indexed messages so they remain searchable
    via recall_conversation.

    Returns (compacted_history, stats).
    """
    budget = _compute_history_budget(system_prompt)

    if budget <= 0:
        logger.warning(
            "System prompt leaves no room for history (budget=%d). "
            "Consider increasing CONTEXT_MAX_INPUT_TOKENS.",
            budget,
        )
        return [], {
            "tokens_used": 0,
            "budget": 0,
            "messages_kept": 0,
            "messages_dropped": len(history),
            "summarized": False,
        }

    # Phase 1: offload oversized tool results
    processed = _offload_tool_results(history)

    # Compute total history tokens
    msg_tokens = [_message_tokens(m) for m in processed]
    total_tokens = sum(msg_tokens)
    threshold = int(budget * settings.CONTEXT_SUMMARIZE_THRESHOLD)

    summarized = False

    # Phase 2: summarize oldest messages if over threshold and LLM available
    if total_tokens > threshold and llm_service is not None and len(processed) > 4:
        summarized = await _summarize_and_compact(processed, msg_tokens, budget, llm_service)

    # Phase 3: fill from newest to oldest until budget is exhausted
    kept: list[dict] = []
    used = 0
    for msg in reversed(processed):
        tokens = _message_tokens(msg)
        if used + tokens > budget:
            break
        kept.append(msg)
        used += tokens

    dropped = len(processed) - len(kept)
    if dropped:
        logger.warning(
            "Context budget: dropped %d oldest message(s) (%d tokens used / %d budget, summarized=%s)",
            dropped, used, budget, summarized,
        )
        # Background-backfill embeddings for dropped messages so they stay searchable
        if session_id:
            _schedule_embedding_backfill(session_id)
    else:
        logger.debug(
            "Context budget: %d tokens used / %d budget (%d messages, summarized=%s)",
            used, budget, len(kept), summarized,
        )

    stats = {
        "tokens_used": used,
        "budget": budget,
        "messages_kept": len(kept),
        "messages_dropped": dropped,
        "summarized": summarized,
    }
    return list(reversed(kept)), stats


async def _summarize_and_compact(
    processed: list[dict],
    msg_tokens: list[int],
    budget: int,
    llm_service: object,
) -> bool:
    """Summarize the oldest messages and replace them with a summary block in-place.

    Returns True if summarization succeeded.
    """
    # Determine how many oldest messages to summarize: enough to bring total
    # under 60% of budget (leaving room for the summary itself + recent messages).
    target = int(budget * 0.6)
    total = sum(msg_tokens)
    tokens_to_free = total - target
    if tokens_to_free <= 0:
        return False

    # Walk from oldest, accumulating until we've freed enough
    summarize_count = 0
    freed = 0
    for i, tok in enumerate(msg_tokens):
        freed += tok
        summarize_count = i + 1
        if freed >= tokens_to_free:
            break

    if summarize_count < 2:
        return False

    to_summarize = processed[:summarize_count]
    transcript = _format_messages_for_summary(to_summarize)

    try:
        summary = await llm_service.chat(
            user_message=f"Summarize this conversation excerpt:\n\n{transcript}",
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            temperature=0.3,
        )
        summary = summary.strip()
        if not summary:
            return False
    except Exception as e:
        logger.warning("Context summarization failed (non-fatal): %s", e)
        return False

    summary_msg = {
        "role": "system",
        "content": f"[CONVERSATION SUMMARY — earlier messages compressed]\n{summary}",
    }

    # Replace the summarized messages with the summary block in-place
    processed[:summarize_count] = [summary_msg]
    logger.info(
        "Context compaction: summarized %d messages (%d tokens) into ~%d tokens",
        summarize_count, freed, count_tokens(summary),
    )
    return True


def _schedule_embedding_backfill(session_id: str) -> None:
    """Fire-and-forget: backfill embeddings for conversation messages missing them."""
    async def _run() -> None:
        try:
            from services.database.mongodb import mongodb
            await mongodb.backfill_conversation_embeddings(session_id, limit=50)
        except Exception as e:
            logger.warning("Embedding backfill task failed: %s", e)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass
