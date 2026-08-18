"""In-process performance collector for turn diagnostics."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
import json
import logging
import time
from typing import Any, Dict, Iterator

from core.turns.visibility import HIDDEN_DELIVERIES, VISIBLE_DELIVERIES

logger = logging.getLogger(__name__)

_perf_context: ContextVar[dict[str, Any]] = ContextVar("perf_context", default={})

_SUMMARY_STAGE_LABELS = {
    "stt_batch": ("Transcribe", "Speech to text"),
    "stt_stream_start": ("STT connect", "Streaming STT websocket setup"),
    "stt_first_partial": ("First transcript", "Streaming STT first text"),
    "stt_finalize_wait": ("Finalize STT", "Post-endpoint transcript flush"),
    "stt_stream_total": ("STT stream", "Streaming STT total time"),
    "turn_detector": ("Turn detector", "Audio end-of-turn check"),
    "turn_lock_wait": ("Turn lock", "Wait for exclusive turn execution"),
    "db_history": ("History", "Conversation history load"),
    "ctx_budget": ("Context fit", "Context budget trimming"),
    "prompt_build": ("Prompt", "System prompt construction"),
    "tool_route": ("Tool routing", "Plugin/tool selection"),
    "llm": ("Model", "LLM time to first token"),
    "code_exec": ("Tool run", "Executed code/tool action"),
    "tts_first_chunk": ("Voice start", "TTS time to first audio"),
    "tts_sentence": ("Voice cont.", "TTS audio for a follow-on sentence"),
}

_SUMMARY_EXCLUDED_STAGES = {"response_latency", "turn_latency"}


class PerfLogger:
    """Tracks elapsed time for pipeline stages and builds compact turn summaries."""

    def __init__(self):
        self._timers: Dict[str, tuple[float, dict[str, Any]]] = {}
        self._turn_summaries: Dict[str, dict[str, Any]] = {}
        self._latest_turn_summary: dict[str, Any] | None = None
        self._enabled: bool | None = None  # resolved lazily, then cached

    def _is_enabled(self) -> bool:
        """Read PERF_ENABLED once, then cache — avoids per-call import overhead."""
        if self._enabled is None:
            try:
                from core import settings
                self._enabled = getattr(settings, "PERF_ENABLED", True)
            except ImportError:
                self._enabled = True
        return self._enabled

    @contextmanager
    def context(self, **metadata: Any) -> Iterator[None]:
        """Bind metadata, such as turn_id, to perf events in the current async context."""
        token = self.bind_context(**metadata)
        try:
            yield
        finally:
            self.reset_context(token)

    def bind_context(self, **metadata: Any) -> Token[dict[str, Any]]:
        """Bind metadata without forcing another indentation level around the caller."""
        current = _perf_context.get()
        return _perf_context.set({**current, **{k: v for k, v in metadata.items() if v is not None}})

    def reset_context(self, token: Token[dict[str, Any]]) -> None:
        _perf_context.reset(token)

    def _metadata(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = dict(_perf_context.get())
        if extra:
            metadata.update({k: v for k, v in extra.items() if v is not None})
        return metadata

    @staticmethod
    def _timer_key(stage: str, session_id: str, metadata: dict[str, Any]) -> str:
        turn_id = metadata.get("turn_id")
        if turn_id:
            return f"{session_id}:{turn_id}:{stage}"
        return f"{session_id}:{stage}"

    def start(self, stage: str, session_id: str = "", **metadata: Any) -> None:
        if self._is_enabled():
            event_metadata = self._metadata(metadata)
            key = self._timer_key(stage, session_id, event_metadata)
            self._timers[key] = (time.perf_counter(), event_metadata)

    def end(self, stage: str, session_id: str = "", **metadata: Any) -> float:
        if self._is_enabled():
            event_metadata = self._metadata(metadata)
            key = self._timer_key(stage, session_id, event_metadata)
            if key in self._timers:
                started_at, start_metadata = self._timers.pop(key)
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                payload = {
                    "stage": stage,
                    "ms": round(elapsed_ms, 1),
                    "session": session_id,
                }
                payload.update(start_metadata)
                payload.update(event_metadata)
                self._record_stage(stage, elapsed_ms, session_id, payload)
                return elapsed_ms
        return 0.0

    def discard(self, stage: str, session_id: str = "", **metadata: Any) -> None:
        """Drop an in-flight timer without logging a completed timing row."""
        if self._is_enabled():
            event_metadata = self._metadata(metadata)
            key = self._timer_key(stage, session_id, event_metadata)
            self._timers.pop(key, None)

    def log(self, message: str, **metadata: Any) -> None:
        if self._is_enabled():
            payload = {"event": message}
            payload.update(self._metadata(metadata))
            self._record_event(message, payload)

    def latest_turn_summary(self) -> dict[str, Any] | None:
        """Structured latest user-visible turn summary for live diagnostics."""
        completed = [
            summary
            for summary in self._turn_summaries.values()
            if summary.get("status") == "completed" and self._is_user_visible(summary)
        ]
        latest_completed_at = None
        if completed:
            latest_completed_at = max(
                summary.get("completed_at") or datetime.min.replace(tzinfo=timezone.utc)
                for summary in completed
            )

        running = [
            summary
            for summary in self._turn_summaries.values()
            if (
                summary.get("status") == "running"
                and self._is_user_visible(summary)
                and (
                    latest_completed_at is None
                    or (summary.get("started_at") or datetime.min.replace(tzinfo=timezone.utc)) > latest_completed_at
                )
            )
        ]
        if running:
            latest = max(running, key=lambda summary: summary.get("started_at") or datetime.min.replace(tzinfo=timezone.utc))
            return self._serialize_summary(latest)
        return json.loads(json.dumps(self._latest_turn_summary)) if self._latest_turn_summary else None

    @staticmethod
    def _summary_key(session_id: str, turn_id: str) -> str:
        return f"{session_id}:{turn_id}"

    @staticmethod
    def _modality_from(payload: dict[str, Any]) -> str:
        scenario = payload.get("scenario")
        if scenario in {"voice", "text", "system"}:
            return str(scenario)
        if scenario == "prefetched":
            return "system"
        source = payload.get("source")
        return "system" if source == "system" else "text"

    @staticmethod
    def _delivery_from(payload: dict[str, Any]) -> str | None:
        delivery = payload.get("delivery")
        if delivery:
            return str(delivery)
        if payload.get("scenario") == "prefetched":
            return "prefetched"
        return None

    @staticmethod
    def _origin_from(payload: dict[str, Any]) -> dict[str, Any] | None:
        trigger_source = payload.get("trigger_source")
        protocol_name = payload.get("protocol_name")
        if not trigger_source and not protocol_name:
            return None
        origin_type = trigger_source or ("protocol" if protocol_name else None)
        origin = {"type": origin_type}
        if payload.get("rule_id"):
            origin["id"] = payload["rule_id"]
        if payload.get("rule_name"):
            origin["name"] = payload["rule_name"]
        if protocol_name:
            origin["protocol_name"] = protocol_name
        if payload.get("instance_id"):
            origin["instance_id"] = payload["instance_id"]
        return {k: v for k, v in origin.items() if v is not None}

    def _get_turn_summary(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        turn_id = payload.get("turn_id")
        if not turn_id or not session_id:
            return None

        source = str(payload.get("source") or "user")
        owner_id = payload.get("owner_id")
        if not owner_id:
            return None
        owner_id = str(owner_id)
        connection_id = payload.get("connection_id")
        if not connection_id:
            return None
        connection_id = str(connection_id)
        delivery = self._delivery_from(payload)
        key = self._summary_key(session_id, str(turn_id))
        now = datetime.now(timezone.utc)
        summary = self._turn_summaries.get(key)
        if summary is None:
            summary = {
                "turn_id": str(turn_id),
                "owner_id": owner_id,
                "connection_id": connection_id,
                "node_id": payload.get("node_id"),
                "node_label": payload.get("node_label"),
                "location_ref": payload.get("location_ref"),
                "source": source,
                "modality": self._modality_from(payload),
                "delivery": delivery,
                "origin": self._origin_from(payload),
                "status": "running",
                "started_at": now,
                "completed_at": None,
                "response_ms": None,
                "total_ms": None,
                "stages": [],
                "stt": {},
                "turn_detection": {},
                "voice": {"recovered": False, "recovery_count": 0},
                "tool_routing": {},
                "model": None,
                "expires_at": now + timedelta(days=30),
            }
            self._turn_summaries[key] = summary
        else:
            summary["owner_id"] = owner_id
            summary["connection_id"] = summary.get("connection_id") or connection_id
            for key in ("node_id", "node_label", "location_ref"):
                if payload.get(key) is not None:
                    summary[key] = payload[key]
            summary["source"] = source or summary.get("source")
            summary["modality"] = self._modality_from(payload) or summary.get("modality")
            if delivery is not None:
                summary["delivery"] = delivery
            origin = self._origin_from(payload)
            if origin is not None:
                summary["origin"] = origin
        return summary

    @staticmethod
    def _is_user_visible(summary: dict[str, Any]) -> bool:
        if not summary.get("owner_id"):
            return False
        delivery = summary.get("delivery")
        if delivery in HIDDEN_DELIVERIES:
            return False
        if summary.get("source") == "user":
            return True
        return delivery in VISIBLE_DELIVERIES

    def _publish_if_visible(self, summary: dict[str, Any]) -> None:
        if summary.get("status") != "completed":
            return

        summary["completed_at"] = summary.get("completed_at") or datetime.now(timezone.utc)
        if self._is_user_visible(summary):
            self._latest_turn_summary = self._serialize_summary(summary)
        self._schedule_persist(summary)

    def completed_turn_summaries(self) -> list[dict[str, Any]]:
        """Completed summaries currently retained in memory."""
        return [
            self._serialize_summary(summary)
            for summary in self._turn_summaries.values()
            if summary.get("status") == "completed"
        ]

    @staticmethod
    def _serialize_summary(summary: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON-safe copy for diagnostics without mutating persisted datetimes."""
        return json.loads(json.dumps(summary, default=lambda value: value.isoformat()))

    def _schedule_persist(self, summary: dict[str, Any]) -> None:
        if not summary.get("owner_id") or not summary.get("turn_id"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        payload = dict(summary)
        loop.create_task(self._persist_turn_run(payload))

    async def _persist_turn_run(self, summary: dict[str, Any]) -> None:
        try:
            from services.database.mongodb import mongodb

            await mongodb.store_turn_run(summary)
        except Exception as exc:
            logger.warning("Failed to persist turn summary: %s", exc)

    def _record_stage(
        self,
        stage: str,
        elapsed_ms: float,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        summary = self._get_turn_summary(session_id, payload)
        if summary is None:
            return

        if stage == "response_latency":
            summary["response_ms"] = round(elapsed_ms, 1)
            if summary.get("status") == "running":
                summary["status"] = "completed"
            self._publish_if_visible(summary)
            return
        if stage == "turn_latency":
            summary["total_ms"] = round(elapsed_ms, 1)
            if summary.get("status") == "running":
                summary["status"] = "completed"
            self._publish_if_visible(summary)
            return
        if stage in _SUMMARY_EXCLUDED_STAGES:
            return

        label, detail = _SUMMARY_STAGE_LABELS.get(stage, (stage, stage.replace("_", " ")))
        if stage == "llm" and payload.get("model"):
            summary["model"] = payload.get("model")
        stage_item = {
            "key": stage,
            "label": label,
            "detail": detail,
            "ms": round(elapsed_ms, 1),
            "group": "post_first_audio" if summary.get("response_ms") is not None else "pre_response",
        }
        if payload.get("iteration") is not None:
            stage_item["iteration"] = payload["iteration"]
        if payload.get("stream_id") is not None:
            stage_item["stream_id"] = payload["stream_id"]
        if payload.get("status") is not None:
            stage_item["status"] = payload["status"]
        for key in ("attempt", "retry_count", "timeout_ms"):
            if payload.get(key) is not None:
                stage_item[key] = payload[key]
        summary["stages"].append(stage_item)
        if (summary.get("active_stage") or {}).get("key") == stage:
            summary["active_stage"] = None
        self._publish_if_visible(summary)

    def _record_event(self, message: str, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session") or "")

        if message == "playback_end_received":
            # Only annotate an existing turn summary; never invent one from playback_end alone.
            turn_id = payload.get("turn_id")
            if not turn_id or not session_id:
                return
            existing = self._turn_summaries.get(self._summary_key(session_id, str(turn_id)))
            if existing is None:
                return
            voice = dict(existing.get("voice") or {})
            voice["playback_end_received"] = True
            if payload.get("stale") is not None:
                voice["playback_end_stale"] = bool(payload.get("stale"))
            if payload.get("turn_active") is not None:
                voice["playback_end_turn_active"] = bool(payload.get("turn_active"))
            existing["voice"] = voice
            self._publish_if_visible(existing)
            return

        summary = self._get_turn_summary(session_id, payload)
        if summary is None:
            return

        if message == "process_turn_finally":
            outcome = payload.get("outcome")
            if outcome == "fast_recovery_handoff":
                summary["status"] = "handoff"
                summary["active_stage"] = None
                return
            if outcome == "audio_sent":
                summary["status"] = "completed"
                summary["active_stage"] = None
                self._publish_if_visible(summary)
            elif outcome == "no_audio_sent":
                # The assistant may answer in text while TTS is muted/unavailable.
                # Keep the turn visible; "cancelled" is reserved for work that did not complete.
                summary["status"] = "completed"
                summary["active_stage"] = None
                self._publish_if_visible(summary)
        elif message == "process_turn_cancelled":
            summary["status"] = "handoff" if payload.get("fast_recovery") else "cancelled"
            summary["active_stage"] = None
        elif message == "endpoint_candidate_cancelled":
            summary["status"] = "handoff"
            summary["active_stage"] = None
        elif message == "llm_stream_event":
            if payload.get("model"):
                summary["model"] = payload.get("model")
            status = payload.get("status")
            if status == "first_token":
                summary["active_stage"] = None
            else:
                timer_key = self._timer_key("llm", session_id, self._metadata(payload))
                started_at = self._timers.get(timer_key, (time.perf_counter(), {}))[0]
                label, detail = _SUMMARY_STAGE_LABELS["llm"]
                active_stage = {
                    "key": "llm",
                    "label": label,
                    "detail": detail,
                    "ms": round((time.perf_counter() - started_at) * 1000, 1),
                    "group": "post_first_audio" if summary.get("response_ms") is not None else "pre_response",
                    "status": status,
                }
                for key in ("iteration", "attempt", "retry_count", "timeout_ms"):
                    if payload.get(key) is not None:
                        active_stage[key] = payload[key]
                summary["active_stage"] = active_stage
        elif message == "reasoning_effort_resolved":
            if payload.get("reasoning_effort") is not None:
                summary["reasoning_effort"] = payload.get("reasoning_effort")
        elif message == "reasoning_chunk":
            summary["reasoning_chars"] = int(summary.get("reasoning_chars") or 0) + int(
                payload.get("reasoning_chars") or 0
            )
        elif message == "model_selected":
            if payload.get("model"):
                summary["model"] = payload.get("model")
        elif message == "stt_stream_summary":
            summary["stt"] = {
                key: payload.get(key)
                for key in (
                    "stream_id",
                    "status",
                    "finish_status",
                    "transcript_chars",
                    "latest_text_chars",
                    "feed_count",
                    "bytes_fed",
                    "transcript_events",
                    "partials_emitted",
                    "provider_protocol",
                    "provider_events",
                    "provider_interims",
                    "provider_finals",
                    "provider_turn_ends",
                    "finalize_chars",
                    "finalize_used_latest",
                )
                if payload.get(key) is not None
            }
        elif message == "turn_detector_decision":
            detection = dict(summary.get("turn_detection") or {})
            for key in (
                "decision",
                "reason",
                "confidence",
                "text_chars",
                "endpoint_age_ms",
                "end_of_turn_delay_ms",
                "transcription_delay_ms",
                "continue_count",
                "awaiting_stt_count",
                "vad_endpoint_count",
                "endpointing_profile",
            ):
                if payload.get(key) is not None:
                    detection[key] = payload[key]
            summary["turn_detection"] = detection
        elif message == "voice_turn_committed":
            voice = dict(summary.get("voice") or {})
            for key in (
                "audio_ms",
                "transcript_chars",
                "reason",
                "confidence",
                "stt_coverage_pct",
                "stt_audio_gap_ms",
            ):
                if payload.get(key) is not None:
                    voice[key] = payload[key]
            summary["voice"] = voice
        elif message == "tool_routing_complete":
            # Keep the user-facing/persisted contract compact. Full ranked scores
            # stay available in ToolRouter diagnostics/logs during active tuning.
            summary["tool_routing"] = {
                key: payload.get(key)
                for key in (
                    "policy_name",
                    "match_mode",
                    "matched_plugins",
                    "routed_tool_count",
                    "schema_tokens",
                    "route_latency_ms",
                    "used_routing_hint",
                    "used_session_carryover",
                )
                if payload.get(key) is not None
            }
        elif message == "fast_recovery_triggered":
            voice = dict(summary.get("voice") or {})
            voice["recovered"] = True
            voice["recovery_count"] = int(voice.get("recovery_count") or 0) + 1
            for key in (
                "elapsed_ms",
                "continuation_audio_ms",
                "transcript_chars",
                "active_stream_id",
                "had_response_to_retract",
            ):
                if payload.get(key) is not None:
                    voice[f"recovery_{key}"] = payload[key]
            summary["voice"] = voice


# Singleton instance
perf = PerfLogger()
