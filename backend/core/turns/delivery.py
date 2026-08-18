"""
Delivery layer for Jarvis turns.

Splits agent-loop output (produced in `_execute_turn`) from how that output reaches
the user. The agent yields `StreamEvent`s; a `DeliveryStrategy` decides what to do
with each one.

- `VoiceDelivery` owns the voice/WebSocket presentation path: WS fan-out (STATUS,
  RESPONSE, CODE, CODE_OUTPUT, UI_*, CONTEXT_METRICS), sentence buffering with
  early-clause and regex splitting, the speech gate (mid-chain suppression,
  pre-tool flush), the TTS streaming worker, and `perf.end("turn_latency")` on
  first audio chunk. Single writer of `session.tts_sentence_queue`,
  `session.first_audio_sent`, and `session.current_delivery`.
- `HeadlessDelivery` is a no-op. Used by silent automations, prefetch, and
  System Pulse escalations where the agent runs but produces no user-visible
  output.
- Spoken fallback while the model is silent before a tool lives in
  `core.turns.runtime_ack` — not in plugins or the capability catalog.

`_execute_turn` stays delivery-agnostic: it writes to `TurnResult` (traces,
tools called, final text) and forwards events to `delivery.on_stream()`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional, Protocol
from uuid import uuid4

from api.websockets.types import WSMessageType
from services.perf import perf
from core.time import parse_datetime
from core.turns.runtime_ack import phrase_for as runtime_ack_phrase
from core.turns.sanitizer import sanitize_assistant_output
from core.turns.visibility import HIDDEN_DELIVERIES as HIDDEN_DELIVERIES

logger = logging.getLogger(__name__)


# --- Module-level helpers (shared with orchestrator.handle_interruption) ---


@dataclass(frozen=True)
class DeliveryCancelIds:
    turn_id: str | None = None
    response_id: str | None = None
    dropped_sentences: int = 0


def signal_current_delivery_cancel(
    session: Any,
    *,
    drain_queue: bool = False,
) -> DeliveryCancelIds:
    """Signal only the active delivery; task cancellation remains caller-owned."""
    current_delivery = getattr(session, "current_delivery", None)
    turn_id = (
        getattr(current_delivery, "turn_id", None)
        if current_delivery is not None
        else None
    )
    response_id = (
        getattr(current_delivery, "response_id", None)
        if current_delivery is not None
        else None
    )
    if current_delivery is not None:
        current_delivery.signal_cancel()

    dropped_sentences = 0
    if drain_queue:
        sentence_queue = getattr(session, "tts_sentence_queue", None)
        if sentence_queue is not None:
            dropped_sentences = drain_sentence_queue(sentence_queue)

    return DeliveryCancelIds(
        turn_id=turn_id,
        response_id=response_id,
        dropped_sentences=dropped_sentences,
    )

# Sentence boundary regex — avoids splitting on abbreviations (Mr., Dr., U.S., etc.)
_SENTENCE_RE = re.compile(
    r'(?<!\bMr)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bMrs)(?<!\b[A-Z])(?<=[.!?])\s+'
)


NO_REPLY_SENTINEL = "NO_REPLY"
DEFER_SENTINEL = "DEFER"
DEFER_UNTIL_PREFIX = "DEFER_UNTIL:"
_DEFER_UNTIL_RE = re.compile(
    rf"^{re.escape(DEFER_UNTIL_PREFIX)}\s*(.+)$",
)


@dataclass(frozen=True, slots=True)
class EvaluateSentinel:
    action: Literal["defer", "suppress"]
    retry_at: datetime | None = None


def is_no_reply(text: str) -> bool:
    """Exact-equals-after-strip check. Empty strings are treated as NO_REPLY.

    Use for the evaluate delivery decision: only suppress when the agent
    emitted the sentinel as the entire response. Anything else gets spoken so
    the model can't accidentally swallow real content.
    """
    return text.strip() == NO_REPLY_SENTINEL or not text.strip()


def is_defer(text: str) -> bool:
    """Exact-equals-after-strip check for offer deferral."""
    return text.strip() == DEFER_SENTINEL


def parse_defer_until(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse a full-response ``DEFER_UNTIL: <when>`` sentinel into a UTC datetime.

    ``<when>`` accepts any expression the platform time parser understands
    (ISO-8601, clock times like ``9:35am``, relative phrases like
    ``in 30 minutes``). Returns None when the message is not a clean
    DEFER_UNTIL sentinel or the time cannot be parsed.
    """
    match = _DEFER_UNTIL_RE.fullmatch(text.strip())
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        return parse_datetime(raw, now=now)
    except ValueError:
        return None


def parse_evaluate_sentinel(text: str) -> EvaluateSentinel | None:
    """Classify a full evaluate response as defer, suppress, or not a sentinel."""
    retry_at = parse_defer_until(text)
    if retry_at is not None:
        return EvaluateSentinel("defer", retry_at)
    if is_defer(text):
        return EvaluateSentinel("defer", None)
    if is_no_reply(text):
        return EvaluateSentinel("suppress", None)
    return None


def contains_no_reply(text: str) -> bool:
    """Conservative substring check — true if the sentinel appears anywhere.

    Use for cache-write paths (e.g. prefetch) where speaking a partial leak
    like "NO_REPLY because nothing happened" would be catastrophic. Stricter
    than `is_no_reply` by design: prefer dropping a borderline cache entry
    over risking a leaked sentinel at fire time.
    """
    return NO_REPLY_SENTINEL in text


def strip_provider_control_tokens(text: str) -> str:
    """Remove leaked chat-template control tokens before text reaches the user."""
    return sanitize_assistant_output(text)


def split_sentences(text: str) -> list[str]:
    """Split a block of text into sentences using the streaming-path regex."""
    sentences: list[str] = []
    while text:
        match = _SENTENCE_RE.search(text)
        if match:
            sentences.append(text[: match.end()].strip())
            text = text[match.end():]
        else:
            sentences.append(text.strip())
            break
    return [s for s in sentences if s]


def drain_sentence_queue(queue: asyncio.Queue) -> int:
    """Drop pending sentences (interrupt / cancelled turn only). Returns items removed.

    Uses get_nowait + QueueEmpty — asyncio.Queue.empty() is not reliable across tasks.
    """
    n = 0
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        queue.task_done()
        n += 1
    return n


# --- Stream event + delivery protocol ---

StreamTag = Literal[
    "text",
    "reasoning",
    "tool_status",
    "tool_call",
    "tool_output",
    "ui_update",
    "ui_delete",
    "context_metrics",
    "final_text",
]


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event yielded by `_execute_turn` to the delivery layer.

    `content` carries the raw payload (chunk text, code block, JSON envelope, etc).
    `tool_call_id` is set only on `tool_call` and `tool_output` so the delivery can
    correlate WS messages. `capability` is set on `tool_call` for the isolated
    runtime-ack fallback.
    """

    tag: StreamTag
    content: str = ""
    tool_call_id: Optional[str] = None
    capability: Optional[str] = None


class DeliveryStrategy(Protocol):
    """Single-method contract for delivering stream events.

    Lifecycle methods (start/aclose) are concrete on the implementation, not on
    the protocol — only `on_stream` must be implemented by every strategy.
    """

    async def on_stream(self, event: StreamEvent) -> None: ...


# --- Turn result ---


@dataclass
class TurnResult:
    """Caller-owned, mutated by `_execute_turn`.

    Mutated in place (not returned) so partial state survives CancelledError:
    the caller's `finally` can persist `turn_trace` whether the turn completed
    or was interrupted mid-stream.
    """

    transcript: str = ""
    full_response: str = ""            # raw model text accumulated across the turn
    delivered_text: str = ""           # what VoiceDelivery sent to TTS ("" for headless)
    runtime_error: str | None = None   # harness/provider failure; never model TEXT
    turn_trace: list = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    routed_tools: list[str] = field(default_factory=list)
    model: str = ""
    interrupted: bool = False


# --- Delivery strategies ---


class HeadlessDelivery:
    """No-op delivery: ignores every stream event.

    Used by silent automations, prefetch, and System Pulse escalations where the
    agent loop runs but no user-facing output is produced.
    """

    async def on_stream(self, event: StreamEvent) -> None:  # noqa: D401
        return None


class VoiceDelivery:
    """Voice/WebSocket delivery: WS fan-out + sentence buffering + TTS worker.

    Per-turn lifecycle:
      1. Caller constructs with session/manager/tts + session_id + produce_audio.
      2. Caller awaits `start()` before streaming (assigns session TTS fields,
         spawns worker if audio).
      3. `_execute_turn` calls `on_stream(event)` for each agent yield and once
         with tag="final_text" after the loop completes.
  4. Caller awaits `aclose(cancelled=...)` in a `finally` — drains the queue,
         joins the worker, resets all session TTS fields it touched.

    Single writer of: `session.tts_sentence_queue`, `session.first_audio_sent`,
    `session.current_delivery`. Closes
    `perf.end("turn_latency")` inside the TTS worker on first audio chunk (span
    starts in caller's ingest; crossing the boundary is by design).
    """

    def __init__(
        self,
        session: Any,
        manager: Any,
        tts: Any,
        *,
        session_id: str,
        turn_id: str,
        produce_audio: bool,
    ) -> None:
        self._session = session
        self._manager = manager
        self._tts = tts
        self._session_id = session_id
        self._turn_id = turn_id
        self._produce_audio = produce_audio

        # Turn-scoped identifiers
        self._response_id = str(uuid4())
        self._turn_context_id = str(uuid4())

        # Sentence streaming state
        self._cumulative_response = ""
        self._sentence_buffer = ""
        self._first_text_at: Optional[float] = None
        self._first_sentence_sent = False
        self._speaking_status_sent = False
        self._in_tool_chain = False
        self._tools_ran = False
        self._runtime_ack_emitted = False

        # TTS worker state (only when produce_audio)
        self._sentence_queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._first_audio_sent = False
        self._tts_failed = False
        self._tts_unavailable = False
        self._audio_completed = False
        self._sentences_queued = 0
        self._cancel = asyncio.Event()
        self._tts_end_ready = False
        self._tts_end_sent = False

    # --- Public surface used by _execute_turn / process_turn ---

    @property
    def first_audio_sent(self) -> bool:
        return self._first_audio_sent

    @property
    def delivered_text(self) -> str:
        return self._cumulative_response.strip()

    @property
    def response_id(self) -> str:
        return self._response_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def produce_audio(self) -> bool:
        return self._produce_audio

    @property
    def tts_end_ready(self) -> bool:
        return self._tts_end_ready and not self._tts_end_sent

    def signal_cancel(self) -> None:
        """Cooperatively stop this delivery without touching newer turns."""
        self._cancel.set()

    async def send_tts_end_if_ready(self) -> None:
        if not self.tts_end_ready:
            return
        await self._manager.send_voice_response(
            self._session_id,
            WSMessageType.TTS_END,
            {"turn_id": self._turn_id},
        )
        self._tts_end_sent = True

    async def start(self) -> None:
        """Prepare session fields + spawn TTS worker (audio path only)."""
        self._session.current_delivery = self
        if self._produce_audio:
            prepare_for_turn = getattr(self._tts, "prepare_for_turn", None)
            if callable(prepare_for_turn):
                prepare_for_turn()
            self._sentence_queue = asyncio.Queue()
            self._session.tts_sentence_queue = self._sentence_queue
            self._worker_task = asyncio.create_task(self._tts_worker())
        perf.log(
            "voice_delivery_started",
            session=self._session_id,
            produce_audio=self._produce_audio,
            response_id=self._response_id,
        )

    async def aclose(self, *, cancelled: bool = False) -> None:
        """Drain the queue, join the worker, clear session-owned state.

        `cancelled=True` drains pending sentences before the sentinel so a
        cancelled turn doesn't keep generating audio.
        """
        try:
            if self._produce_audio and self._sentence_queue is not None:
                if cancelled:
                    self.signal_cancel()
                    dropped = drain_sentence_queue(self._sentence_queue)
                    logger.debug(f"TTS cancel cleanup: drained={dropped} for {self._session_id}")
                    if self._worker_task is not None:
                        self._worker_task.cancel()
                        await asyncio.gather(self._worker_task, return_exceptions=True)
                else:
                    await self._sentence_queue.put(None)
                    if self._worker_task is not None:
                        await self._worker_task
        finally:
            # Always clear session TTS state — even if worker join raised —
            # so the caller's finally and _handle_interruption see a clean slate.
            # Snapshot the audio outcome before the in-flight reset so callers
            # (e.g. alert finalization) can read it after process_turn returns.
            if cancelled and self._first_audio_sent and not self._tts_failed:
                self._audio_completed = True
            self._session.last_turn_audio_sent = self._first_audio_sent
            self._session.last_turn_audio_completed = self._audio_completed
            self._session.tts_sentence_queue = None
            self._session.first_audio_sent = False
            if getattr(self._session, "current_delivery", None) is self:
                self._session.current_delivery = None
            perf.log(
                "voice_delivery_closed",
                session=self._session_id,
                cancelled=cancelled,
                first_audio_sent=self._first_audio_sent,
                sentences_queued=self._sentences_queued,
                delivered_chars=len(self.delivered_text),
            )

    async def on_stream(self, event: StreamEvent) -> None:
        """Dispatch one stream event to the appropriate handler."""
        tag = event.tag
        if tag == "text":
            await self._handle_text(event.content)
        elif tag == "reasoning":
            await self._handle_reasoning(event.content)
        elif tag == "tool_status":
            await self._handle_tool_status(event.content)
        elif tag == "tool_call":
            await self._handle_tool_call(
                event.content,
                event.tool_call_id,
                event.capability,
            )
        elif tag == "tool_output":
            await self._handle_tool_output(event.content, event.tool_call_id)
        elif tag == "ui_update":
            await self._handle_ui_update(event.content)
        elif tag == "ui_delete":
            await self._handle_ui_delete(event.content)
        elif tag == "context_metrics":
            await self._handle_context_metrics(event.content)
        elif tag == "final_text":
            await self._handle_final_text()

    # --- Handlers ---

    async def _handle_reasoning(self, chunk: str) -> None:
        """Forward provider reasoning to text clients; drop on audio-bound turns."""
        if self._produce_audio or not chunk:
            return
        await self._manager.send_voice_response(
            self._session_id,
            WSMessageType.REASONING,
            {
                "text": chunk,
                "response_id": self._response_id,
                "turn_id": self._turn_id,
                "is_partial": True,
            },
        )

    async def _handle_tool_status(self, stage: str) -> None:
        """Tool lifecycle status before a complete executable block is available.

        Audio cues wait until ``_handle_tool_call`` so we never click *and*
        speak a wait phrase for the same call.
        """
        await self._manager.send_voice_response(
            self._session_id, WSMessageType.STATUS, {"stage": stage}
        )

    async def _handle_text(self, chunk: str) -> None:
        """Text chunk: buffer, apply speech gate, split into sentences."""
        self._sentence_buffer += chunk

        if self._first_text_at is None:
            self._first_text_at = time.monotonic()
            perf.log(
                "assistant_first_text",
                session=self._session_id,
                chunk_chars=len(chunk),
                produce_audio=self._produce_audio,
            )

        # Mid-chain text (between tools) is accumulated but not flushed —
        # the speech gate at the next tool_call will discard it, and
        # _handle_final_text flushes the post-chain final response.
        if self._in_tool_chain:
            return

        # Early clause flush: 800ms + 60 chars without a sentence break, first sentence only
        if (
            not self._first_sentence_sent
            and (time.monotonic() - self._first_text_at) >= 0.8
            and len(self._sentence_buffer) >= 60
        ):
            clause_match = re.search(r'[,;:]\s+', self._sentence_buffer)
            if clause_match:
                split_at = clause_match.end()
                sentence = self._sentence_buffer[:split_at].strip()
                self._sentence_buffer = self._sentence_buffer[split_at:]
                self._first_sentence_sent = True
                if sentence:
                    await self._flush_sentence(sentence, reason="early_clause")

        # Regex-based sentence boundary flush
        match = _SENTENCE_RE.search(self._sentence_buffer)
        if match:
            split_at = match.end()
            sentence = self._sentence_buffer[:split_at].strip()
            self._sentence_buffer = self._sentence_buffer[split_at:]
            self._first_sentence_sent = True
            if sentence:
                await self._flush_sentence(sentence, reason="sentence_boundary")

    async def _handle_tool_call(
        self,
        code: str,
        tool_call_id: Optional[str],
        capability: Optional[str] = None,
    ) -> None:
        """Tool call: wait-speech decision, CODE WS, rotate response_id.

        Agent TEXT from this LLM round is already flushed into the buffer
        before this event (tool calls are yielded only after the stream
        ends), so a fast model cannot race native speech vs fallback.
        Dispatch starts only after this handler returns; the TTS worker
        is a separate task, so we never wait for playback.
        """
        self._tools_ran = True

        if self._in_tool_chain:
            # Speech gate: discard mid-chain filler between tool calls
            if self._sentence_buffer.strip():
                logger.debug("Speech gate: suppressed mid-chain text %r", self._sentence_buffer.strip())
            self._sentence_buffer = ""
            self._cumulative_response = ""
        else:
            # Native model speech wins; otherwise one delivery-owned phrase.
            spoken_prefix = self._sanitize_for_delivery(self._sentence_buffer, reason="pre_tool_flush").strip()
            if spoken_prefix:
                self._cumulative_response += spoken_prefix
                await self._send_response(is_partial=True)
                if self._produce_audio and self._sentence_queue is not None:
                    await self._queue_sentence(spoken_prefix, reason="pre_tool_flush")
            elif self._sentence_buffer.strip():
                logger.debug("Speech gate: stripped internal channel markers from pre-tool text")
            self._sentence_buffer = ""

            if self._cumulative_response.strip():
                self._runtime_ack_emitted = True
                await self._send_response(is_partial=False)
            else:
                await self._queue_runtime_ack(capability)

        # Instant controls stay silent: optional click, no spoken wait.
        # Lookups that already queued speech suppress the click.
        if self._can_emit_audio_cue():
            await self._send_audio_cue("start")

        await self._manager.send_voice_response(
            self._session_id, WSMessageType.STATUS, {"stage": "running_tool"}
        )
        perf.log(
            "tool_call_started",
            session=self._session_id,
            tool_call_id=tool_call_id,
            code_chars=len(code),
            had_spoken_prefix=self._runtime_ack_emitted,
            capability=capability,
        )
        await self._manager.send_voice_response(
            self._session_id, WSMessageType.CODE,
            {"text": code, "tool_call_id": tool_call_id},
        )

        # Rotate response correlation for the post-tool assistant turn
        self._cumulative_response = ""
        self._response_id = str(uuid4())

    async def _queue_runtime_ack(self, capability: Optional[str]) -> None:
        """Queue one TTS-only wait phrase; never waits for playback.

        Not a model RESPONSE — it must not enter the transcript or history.
        Instant capabilities return None from ``phrase_for`` and stay quiet;
        ``_runtime_ack_emitted`` is left false so a parallel lookup in the
        same batch can still speak.
        """
        if self._runtime_ack_emitted or self._cancel.is_set():
            return
        if not self._produce_audio or self._sentence_queue is None:
            return
        phrase = runtime_ack_phrase(capability)
        if not phrase:
            return
        self._runtime_ack_emitted = True
        if not self._speaking_status_sent:
            self._speaking_status_sent = True
            await self._manager.send_voice_response(
                self._session_id, WSMessageType.STATUS, {"stage": "speaking"}
            )
        await self._queue_sentence(phrase, reason="runtime_ack")

    async def _handle_tool_output(self, output: str, tool_call_id: Optional[str]) -> None:
        """Tool output: emit CODE_OUTPUT WS, transition status to thinking."""
        if tool_call_id:
            await self._manager.send_voice_response(
                self._session_id, WSMessageType.CODE_OUTPUT,
                {"text": output, "tool_call_id": tool_call_id},
            )
        perf.log(
            "tool_output_received",
            session=self._session_id,
            tool_call_id=tool_call_id,
            output_chars=len(output),
        )
        if self._can_emit_audio_cue():
            await self._send_audio_cue("done")
        self._in_tool_chain = True
        self._first_text_at = None
        await self._manager.send_voice_response(
            self._session_id, WSMessageType.STATUS, {"stage": "thinking"}
        )

    async def _handle_ui_update(self, payload: str) -> None:
        try:
            ui_data = json.loads(payload)
            await self._manager.send_voice_response(
                self._session_id, WSMessageType.UI_UPDATE, ui_data
            )
            logger.info(f"Pushed AI-triggered UI update: {ui_data.get('component')}")
        except Exception as e:
            logger.error(f"Failed to push AI UI update: {e}")

    async def _handle_ui_delete(self, widget_id: str) -> None:
        try:
            await self._manager.send_voice_response(
                self._session_id, WSMessageType.UI_DELETE, {"widget_id": widget_id}
            )
            logger.info(f"Pushed AI-triggered UI delete: {widget_id}")
        except Exception as e:
            logger.error(f"Failed to push AI UI delete: {e}")

    async def _handle_context_metrics(self, payload: str) -> None:
        await self._manager.send_voice_response(
            self._session_id, WSMessageType.CONTEXT_METRICS, json.loads(payload)
        )

    async def _handle_final_text(self) -> None:
        """End-of-stream: split remaining buffer into sentences, flush, final RESPONSE."""
        cleaned_buffer = self._sanitize_for_delivery(self._sentence_buffer, reason="final_text").strip()
        if cleaned_buffer:
            # Split any remaining buffer into sentences before enqueuing.
            # Without this, a post-tool-chain final response (e.g. 857 chars)
            # would hit Cartesia as one block causing 16-26s generation.
            for sentence in split_sentences(cleaned_buffer):
                self._cumulative_response += sentence + " "
                await self._send_response(is_partial=True)
                if self._produce_audio and self._sentence_queue is not None:
                    await self._queue_sentence(sentence, reason="final_split")
        elif self._sentence_buffer.strip():
            logger.debug("Speech gate: stripped internal channel markers from final text")
        if self._sentence_buffer.strip():
            self._sentence_buffer = ""

        if self._cumulative_response.strip():
            await self._send_response(is_partial=False)

    # --- Internal helpers ---

    async def _send_audio_cue(self, phase: str) -> None:
        preferences = getattr(self._session, "preferences", None)
        if preferences is not None and not preferences.audio.tool_cues_enabled:
            return
        await self._manager.send_voice_response(
            self._session_id,
            WSMessageType.AUDIO_CUE,
            {"phase": phase},
        )

    def _can_emit_audio_cue(self) -> bool:
        return (
            self._produce_audio
            and self._sentences_queued == 0
            and not self._sentence_buffer.strip()
            and not self._cumulative_response.strip()
        )

    async def _flush_sentence(self, sentence: str, *, reason: str) -> None:
        """Send a complete sentence to the frontend + TTS queue."""
        sentence = self._sanitize_for_delivery(sentence, reason=reason).strip()
        if not sentence:
            logger.debug("Speech gate: stripped internal channel markers from sentence")
            return
        self._cumulative_response += sentence + " "
        await self._send_response(is_partial=True)
        if self._produce_audio and self._sentence_queue is not None:
            # Emit backend speaking status on first sentence enqueue — closes
            # the gap between "thinking" and audio so fast-recovery cancellation
            # transitions cleanly (speaking -> listening).
            if not self._speaking_status_sent:
                self._speaking_status_sent = True
                await self._manager.send_voice_response(
                    self._session_id, WSMessageType.STATUS, {"stage": "speaking"}
                )
            await self._queue_sentence(sentence, reason=reason)

    async def _queue_sentence(self, sentence: str, *, reason: str) -> None:
        if self._sentence_queue is None:
            return
        self._sentences_queued += 1
        queue_depth_before = self._sentence_queue.qsize()
        perf.log(
            "tts_sentence_queued",
            session=self._session_id,
            sentence_index=self._sentences_queued,
            reason=reason,
            sentence_chars=len(sentence),
            queue_depth_before=queue_depth_before,
            first_sentence=not self._first_audio_sent and self._sentences_queued == 1,
        )
        await self._sentence_queue.put(sentence)

    def _sanitize_for_delivery(self, text: str, *, reason: str) -> str:
        cleaned = strip_provider_control_tokens(text)
        if is_no_reply(cleaned):
            return ""
        return cleaned

    async def _send_response(self, *, is_partial: bool) -> None:
        await self._manager.send_voice_response(
            self._session_id,
            WSMessageType.RESPONSE,
            {
                "text": self._cumulative_response.strip(),
                "response_id": self._response_id,
                "turn_id": self._turn_id,
                "is_partial": is_partial,
            },
        )

    async def _tts_worker(self) -> None:
        """Background worker: pull sentences, stream TTS audio to frontend.

        Cooperative cancellation is delivery-scoped; task.cancel() is the
        backstop for long awaits (e.g. TTS API latency). Closes latency spans on
        the first audio chunk; these spans cross the process_turn/delivery boundary.
        """
        session = self._session
        session_id = self._session_id
        queue = self._sentence_queue
        assert queue is not None, "worker spawned without queue"

        while True:
            sentence = await queue.get()

            # Cooperative cancellation wins if it races with the normal sentinel.
            if self._cancel.is_set():
                perf.log(
                    "tts_worker_stopped",
                    session=session_id,
                    reason="cancelled",
                    first_audio_sent=self._first_audio_sent,
                    queue_depth=queue.qsize(),
                )
                queue.task_done()
                break

            if sentence is None:
                if self._first_audio_sent:
                    self._tts_end_ready = True
                    self._audio_completed = not self._tts_failed
                perf.log(
                    "tts_worker_stopped",
                    session=session_id,
                    reason="sentinel",
                    first_audio_sent=self._first_audio_sent,
                    queue_depth=queue.qsize(),
                )
                queue.task_done()
                break

            # Only the turn's first audio is "Voice start" (time-to-first-audio).
            # Later sentences measure their own synthesis latency under a separate
            # diagnostic key so the summary doesn't show duplicate "Voice start" rows.
            tts_stage = "tts_first_chunk" if not self._first_audio_sent else "tts_sentence"
            perf.start(tts_stage, session_id)

            chunk_index = 0
            audio_bytes_sent = 0
            try:
                if self._tts_unavailable:
                    continue
                ensure_ready = getattr(self._tts, "initialize", None)
                if callable(ensure_ready) and not getattr(self._tts, "ready", True):
                    if not await ensure_ready():
                        # Spoken replies are switched off or the provider cannot start.
                        # Text still delivers, so retry once per turn rather than per
                        # sentence — each retry costs a helper round trip.
                        self._tts_unavailable = True
                        logger.info("No spoken output for %s; delivering text only", session_id)
                        continue

                async for audio_bytes in self._tts.generate_audio_stream(
                    sentence, self._turn_context_id, add_silence_ms=250
                ):
                    # Cooperative cancel between every audio chunk
                    if self._cancel.is_set():
                        perf.log(
                            "tts_sentence_cancelled",
                            session=session_id,
                            sentence_chars=len(sentence),
                            chunks_sent=chunk_index,
                            audio_bytes=audio_bytes_sent,
                        )
                        break

                    if audio_bytes:
                        if chunk_index == 0:
                            perf.end(tts_stage, session_id)

                        if not self._first_audio_sent:
                            perf.end("response_latency", session_id)
                            perf.end("turn_latency", session_id)
                            self._first_audio_sent = True
                            if session:
                                session.first_audio_sent = True
                                session.active_audio_turn_id = self._turn_id
                            perf.log(
                                "first_audio_sent",
                                session=session_id,
                                sentence_chars=len(sentence),
                                queue_depth=queue.qsize(),
                            )

                        await self._manager.send_voice_response(
                            session_id,
                            WSMessageType.JARVIS_AUDIO,
                            {
                                "audio": base64.b64encode(audio_bytes).decode("utf-8"),
                                "encoding": "base64",
                                "sample_rate": self._tts.sample_rate,
                                "turn_id": self._turn_id,
                            },
                        )
                        audio_bytes_sent += len(audio_bytes)
                        chunk_index += 1
                perf.log(
                    "tts_sentence_completed",
                    session=session_id,
                    sentence_chars=len(sentence),
                    chunks_sent=chunk_index,
                    audio_bytes=audio_bytes_sent,
                    cancelled=self._cancel.is_set(),
                )
            except asyncio.CancelledError:
                perf.log(
                    "tts_worker_cancelled",
                    session=session_id,
                    sentence_chars=len(sentence),
                    chunks_sent=chunk_index,
                    audio_bytes=audio_bytes_sent,
                )
                raise
            except Exception as e:
                self._tts_failed = True
                logger.error(f"TTS Worker Error for {session_id}: {e}")
                perf.log(
                    "tts_sentence_error",
                    session=session_id,
                    sentence_chars=len(sentence),
                    chunks_sent=chunk_index,
                    audio_bytes=audio_bytes_sent,
                    error=type(e).__name__,
                )
            finally:
                if chunk_index == 0:
                    perf.discard(tts_stage, session_id)
                queue.task_done()
