"""Unit tests for Phase 9a delivery/evaluation behavior.

Covers the pure helpers in `core.turns.delivery` (is_no_reply), a smoke test
for `HeadlessDelivery` as a true no-op strategy, the
`build_system_turn_message` context builder, and the routing_hint tool-routing
path added in Phase 9a+ for automation turns.

Run from backend/: `pytest tests/test_delivery_modes.py`
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from core.turns.delivery import (
    HeadlessDelivery,
    NO_REPLY_SENTINEL,
    StreamEvent,
    TurnResult,
    VoiceDelivery,
    contains_no_reply,
    is_no_reply,
    strip_provider_control_tokens,
)
from core.turns.runtime_ack import PHRASES
from api.websockets.types import WSMessageType
from core.agent.agent import AgentEvent, AgentEventType, JarvisAgent
from core.llm.types import TextEvent, ToolCallEvent
from core.preferences.models import AudioPreferences, UserPreferences
from core.prompts.builder import PromptMode
from core.prompts.system_turn_context import (
    SystemTurnContext,
    build_system_routing_hint,
    build_system_turn_message,
    project_reply_grounding,
    system_turn_context_from_trigger,
)
from core.routing.policies import SYSTEM_POLICY, TEXT_POLICY
from core.triggers.freshness import trigger_expiry_reason
from core.turns.orchestrator import (
    AssistantOrchestrator,
)
from core.turns.execution import resolve_prompt_mode
from core.turns.history import (
    latest_reply_context_system_row,
    load_turn_history,
    merge_user_history_with_system_tail,
    project_system_tail,
)
from core.triggers.models import AttentionPolicy, DeliveryPlan, FreshnessPolicy, TriggerAction, TriggerInstance, TriggerOrigin

# --- is_no_reply -----------------------------------------------------------


class TestIsNoReply:
    def test_exact_match(self):
        assert is_no_reply(NO_REPLY_SENTINEL) is True
        assert is_no_reply("NO_REPLY") is True

    def test_empty_string_treated_as_no_reply(self):
        assert is_no_reply("") is True
        assert is_no_reply("   ") is True

    def test_speakable_text_is_not_no_reply(self):
        assert is_no_reply("Your meeting starts in five minutes.") is False
        assert is_no_reply("no_reply") is False  # case-sensitive

    def test_no_reply_with_trailing_content_is_not_match(self):
        # Defensive: the agent must emit NO_REPLY as the entire response.
        assert is_no_reply("NO_REPLY.") is False
        assert is_no_reply("NO_REPLY because nothing happened") is False
        assert is_no_reply("Done. NO_REPLY") is False


# --- contains_no_reply -----------------------------------------------------


class TestContainsNoReply:
    """Stricter substring check used by cache-write paths (e.g. prefetch).

    Asymmetric with `is_no_reply` by design: speak path requires exact match
    so the agent can't accidentally swallow real content; cache-write path
    rejects on any sentinel appearance so a partial leak never reaches TTS.
    """

    def test_substring_anywhere_is_match(self):
        assert contains_no_reply("NO_REPLY because nothing happened") is True
        assert contains_no_reply("Done. NO_REPLY") is True
        assert contains_no_reply("prefix NO_REPLY suffix") is True

    def test_clean_speakable_text_is_not_match(self):
        assert contains_no_reply("Your meeting starts in five minutes.") is False
        assert contains_no_reply("") is False  # empty handled separately by callers


class TestStripProviderControlTokens:
    def test_strips_malformed_channel_leak_from_snapshot(self):
        text = "<|channel>thought\n<channel|><|channel>thought\n<channel|>"
        assert strip_provider_control_tokens(text).strip() == ""

    def test_strips_channel_remnants_after_token_removal(self):
        text = "thought thought thought Visible."
        assert strip_provider_control_tokens(text).strip() == "Visible."

    def test_strips_harmony_channel_header(self):
        text = "<|channel|>analysis<|message|>Let me check for you."
        assert strip_provider_control_tokens(text).strip() == "Let me check for you."

    def test_strips_harmony_message_header(self):
        text = "<|start|>assistant<|channel|>final<|message|>Done.<|return|>"
        assert strip_provider_control_tokens(text).strip() == "Done."

    def test_strips_common_chat_template_tokens(self):
        text = "<|im_start|>assistant\nDone.<|im_end|>"
        assert strip_provider_control_tokens(text).strip() == "Done."

    def test_preserves_speakable_text_around_markers(self):
        text = "<|channel>thought\n<channel|>Let me check for you."
        assert strip_provider_control_tokens(text).strip() == "Let me check for you."

    def test_strips_bare_thought_line_before_tool_call(self):
        text = "thought\n\n<tool_call>\nawait jarvis.setups.find(setup_type='automation')\n</tool_call>"
        assert strip_provider_control_tokens(text).lstrip().startswith("<tool_call>")

    def test_strips_boundary_markup_leak_from_snapshot(self):
        text = "<sup>\nI'm not sure which release you mean.\n</code>"
        assert strip_provider_control_tokens(text).strip() == "I'm not sure which release you mean."


class TestVoiceDeliverySanitizeNoReply:
    def test_sanitize_drops_exact_sentinel(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=False,
        )
        assert delivery._sanitize_for_delivery(NO_REPLY_SENTINEL, reason="test") == ""
        assert delivery._sanitize_for_delivery("  NO_REPLY  ", reason="test") == ""

    def test_sanitize_preserves_speakable_text(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=False,
        )
        text = "Your meeting starts in five minutes."
        assert delivery._sanitize_for_delivery(text, reason="test") == text


class TestVoiceDeliveryPreparation:
    @pytest.mark.asyncio
    async def test_audio_delivery_prepares_tts(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = MagicMock()
        delivery = VoiceDelivery(
            session,
            manager,
            tts,
            session_id="test",
            turn_id="turn-test",
            produce_audio=True,
        )

        await delivery.start()
        await delivery.aclose()

        tts.prepare_for_turn.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_non_audio_delivery_does_not_prepare_tts(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = MagicMock()
        delivery = VoiceDelivery(
            session,
            manager,
            tts,
            session_id="test",
            turn_id="turn-test",
            produce_audio=False,
        )

        await delivery.start()
        await delivery.aclose()

        tts.prepare_for_turn.assert_not_called()


class TestVoiceDeliveryCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_close_cancels_stuck_tts_worker(self):
        class HangingTTS:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def generate_audio_stream(self, *_args, **_kwargs):
                self.started.set()
                await self.release.wait()
                yield b"unreachable"

        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = HangingTTS()
        delivery = VoiceDelivery(session, manager, tts, session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="This will hang."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await asyncio.wait_for(tts.started.wait(), timeout=0.1)

        await asyncio.wait_for(delivery.aclose(cancelled=True), timeout=0.1)

        assert session.current_delivery is None
        assert session.tts_sentence_queue is None

    @pytest.mark.asyncio
    async def test_partial_tts_failure_is_not_completed_delivery(self):
        class PartialTTS:
            sample_rate = 24000

            async def generate_audio_stream(self, *_args, **_kwargs):
                yield b"partial"
                raise RuntimeError("stream failed")

        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
            last_turn_audio_completed=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session,
            manager,
            PartialTTS(),
            session_id="test",
            turn_id="turn-test",
            produce_audio=True,
        )

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="This will fail."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose()

        assert session.last_turn_audio_sent is True
        assert session.last_turn_audio_completed is False

    @pytest.mark.asyncio
    async def test_successful_tts_is_completed_delivery(self):
        class SuccessfulTTS:
            sample_rate = 24000

            async def generate_audio_stream(self, *_args, **_kwargs):
                yield b"complete"

        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
            last_turn_audio_completed=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session,
            manager,
            SuccessfulTTS(),
            session_id="test",
            turn_id="turn-test",
            produce_audio=True,
        )

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="This will finish."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose()

        assert session.last_turn_audio_sent is True
        assert session.last_turn_audio_completed is True


class TestVoiceDeliveryReasoning:
    @pytest.mark.asyncio
    async def test_text_turn_forwards_reasoning_ws(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, MagicMock(),
            session_id="test", turn_id="turn-test", produce_audio=False,
        )
        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="reasoning", content="plan step"))
        await delivery.aclose(cancelled=False)

        reasoning_calls = [
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.REASONING
        ]
        assert len(reasoning_calls) == 1
        assert reasoning_calls[0].args[2]["text"] == "plan step"
        assert reasoning_calls[0].args[2]["response_id"] == delivery.response_id

    @pytest.mark.asyncio
    async def test_audio_turn_drops_reasoning(self):
        session = SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
            preferences=UserPreferences(owner_id="u1", audio=AudioPreferences(tool_cues_enabled=False)),
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, MagicMock(),
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="reasoning", content="hidden plan"))
        await delivery.aclose(cancelled=False)

        reasoning_calls = [
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.REASONING
        ]
        assert reasoning_calls == []


class TestVoiceDeliveryTtsEnd:
    @staticmethod
    def _session() -> SimpleNamespace:
        return SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )

    @staticmethod
    def _message_types(manager: SimpleNamespace) -> list[WSMessageType]:
        return [call.args[1] for call in manager.send_voice_response.await_args_list]

    @pytest.mark.asyncio
    async def test_response_payload_includes_turn_id_and_response_id(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=False)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="Hello there."))
        await delivery.on_stream(StreamEvent(tag="final_text"))

        response_call = next(
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.RESPONSE
        )
        assert response_call.args[2] == {
            "text": "Hello there.",
            "response_id": delivery.response_id,
            "turn_id": "turn-test",
            "is_partial": True,
        }

    @pytest.mark.asyncio
    async def test_normal_spoken_delivery_sends_tts_end_after_audio(self):
        class FixedTTS:
            sample_rate = 24_000

            async def generate_audio_stream(self, *_args, **_kwargs):
                yield b"audio"

        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, FixedTTS(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="Hello there."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose(cancelled=False)

        message_types = self._message_types(manager)
        assert message_types.count(WSMessageType.JARVIS_AUDIO) == 1
        audio_call = next(
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.JARVIS_AUDIO
        )
        assert audio_call.args[2]["turn_id"] == "turn-test"
        assert session.active_audio_turn_id == "turn-test"
        assert WSMessageType.TTS_END not in message_types
        assert delivery.tts_end_ready is True

        await delivery.send_tts_end_if_ready()

        message_types = self._message_types(manager)
        tts_end_call = next(
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.TTS_END
        )
        assert tts_end_call.args[2]["turn_id"] == "turn-test"
        assert message_types.count(WSMessageType.TTS_END) == 1
        assert message_types.index(WSMessageType.JARVIS_AUDIO) < message_types.index(WSMessageType.TTS_END)

    @pytest.mark.asyncio
    async def test_no_audio_delivery_does_not_send_tts_end(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose(cancelled=False)
        await delivery.send_tts_end_if_ready()

        message_types = self._message_types(manager)
        assert WSMessageType.JARVIS_AUDIO not in message_types
        assert WSMessageType.TTS_END not in message_types

    @pytest.mark.asyncio
    async def test_cancel_racing_with_sentinel_suppresses_tts_end(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        delivery._first_audio_sent = True
        session.first_audio_sent = True
        delivery.signal_cancel()
        await delivery.aclose(cancelled=False)
        await delivery.send_tts_end_if_ready()

        assert WSMessageType.TTS_END not in self._message_types(manager)

    @pytest.mark.asyncio
    async def test_tool_cues_emit_for_each_tool_lifecycle_on_audio_path(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-2"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-2"))
        await delivery.aclose(cancelled=False)

        cue_calls = [
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.AUDIO_CUE
        ]
        assert [call.args[2] for call in cue_calls] == [
            {"phase": "start"},
            {"phase": "done"},
            {"phase": "start"},
            {"phase": "done"},
        ]

    @pytest.mark.asyncio
    async def test_tool_cues_emit_before_final_text_is_ready(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="text", content="Done."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose(cancelled=False)

        cue_calls = [
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.AUDIO_CUE
        ]
        assert [call.args[2] for call in cue_calls] == [{"phase": "start"}, {"phase": "done"}]

    @pytest.mark.asyncio
    async def test_tool_cues_are_suppressed_by_spoken_preamble(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="text", content="I'll check that for you."))
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="text", content="Done."))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose(cancelled=False)

        cue_calls = [
            call for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.AUDIO_CUE
        ]
        assert cue_calls == []

    @pytest.mark.asyncio
    async def test_tool_cues_do_not_emit_for_text_delivery(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=False)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="final_text"))
        await delivery.aclose(cancelled=False)

        assert WSMessageType.AUDIO_CUE not in self._message_types(manager)

    @pytest.mark.asyncio
    async def test_tool_cues_do_not_emit_when_user_preference_disabled(self):
        session = self._session()
        session.preferences = UserPreferences(
            owner_id="test-owner",
            audio=AudioPreferences(tool_cues_enabled=False),
        )
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(session, manager, MagicMock(), session_id="test", turn_id="turn-test", produce_audio=True)

        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(tag="tool_call", content="await jarvis.time.now()", tool_call_id="tool-1"))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.aclose(cancelled=False)

        assert WSMessageType.AUDIO_CUE not in self._message_types(manager)


class _GatedTTS:
    def __init__(self) -> None:
        self.sentences: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sample_rate = 24_000

    async def generate_audio_stream(self, sentence, *_args, **_kwargs):
        self.sentences.append(sentence)
        self.started.set()
        await self.release.wait()
        yield b"audio"


class _CapturingTTS:
    def __init__(self) -> None:
        self.sentences: list[str] = []
        self.sample_rate = 24_000

    async def generate_audio_stream(self, sentence, *_args, **_kwargs):
        self.sentences.append(sentence)
        yield b"audio"


class TestRuntimeToolAcks:
    @staticmethod
    def _session() -> SimpleNamespace:
        return SimpleNamespace(
            current_delivery=None,
            tts_sentence_queue=None,
            first_audio_sent=False,
            last_turn_audio_sent=False,
        )

    async def _deliver_calls(self, *events: StreamEvent, tts=None, produce_audio: bool = True):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = tts or _CapturingTTS()
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=produce_audio,
        )
        await delivery.start()
        for event in events:
            await delivery.on_stream(event)
        await delivery.aclose(cancelled=False)
        return delivery, tts

    @pytest.mark.asyncio
    async def test_fallback_when_model_is_silent(self):
        _, tts = await self._deliver_calls(StreamEvent(
            tag="tool_call",
            content="search.web(query='news')",
            tool_call_id="tool-1",
            capability="search.web",
        ))
        assert tts.sentences == [tts.sentences[0]]
        assert tts.sentences[0] in PHRASES

    @pytest.mark.asyncio
    async def test_native_prefix_suppresses_fallback(self):
        _, tts = await self._deliver_calls(
            StreamEvent(tag="text", content="I'll check that for you."),
            StreamEvent(
                tag="tool_call",
                content="search.web(query='news')",
                tool_call_id="tool-1",
                capability="search.web",
            ),
        )
        assert tts.sentences == ["I'll check that for you."]

    @pytest.mark.asyncio
    async def test_instant_controls_stay_silent(self):
        _, tts = await self._deliver_calls(StreamEvent(
            tag="tool_call",
            content="system.set_volume(level=20)",
            tool_call_id="tool-1",
            capability="system.set_volume",
        ))
        assert tts.sentences == []

    @pytest.mark.asyncio
    async def test_one_acknowledgement_per_parallel_and_chained_calls(self):
        _, parallel = await self._deliver_calls(
            StreamEvent(tag="tool_call", content="search.web()", tool_call_id="a", capability="search.web"),
            StreamEvent(tag="tool_call", content="calendar.get_events()", tool_call_id="b", capability="calendar.get_events"),
        )
        assert len(parallel.sentences) == 1
        assert parallel.sentences[0] in PHRASES

        _, chained = await self._deliver_calls(
            StreamEvent(tag="tool_call", content="search.web()", tool_call_id="a", capability="search.web"),
            StreamEvent(tag="tool_output", content="{}", tool_call_id="a"),
            StreamEvent(tag="tool_call", content="calendar.get_events()", tool_call_id="b", capability="calendar.get_events"),
        )
        assert len(chained.sentences) == 1
        assert chained.sentences[0] in PHRASES

    @staticmethod
    def _cue_phases(manager) -> list[str]:
        return [
            call.args[2]["phase"]
            for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.AUDIO_CUE
        ]

    @pytest.mark.asyncio
    async def test_lookup_ack_is_audio_only_without_click_or_transcript(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = _CapturingTTS()
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(
            tag="tool_call",
            content="search.web(query='news')",
            tool_call_id="tool-1",
            capability="search.web",
        ))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="{}", tool_call_id="tool-1"))
        await delivery.aclose(cancelled=False)

        assert tts.sentences[0] in PHRASES
        assert self._cue_phases(manager) == []
        responses = [
            call.args[2]["text"]
            for call in manager.send_voice_response.await_args_list
            if call.args[1] == WSMessageType.RESPONSE
        ]
        assert tts.sentences[0] not in responses

    @pytest.mark.asyncio
    async def test_instant_control_clicks_without_speech(self):
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        tts = _CapturingTTS()
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        await delivery.start()
        await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))
        await delivery.on_stream(StreamEvent(
            tag="tool_call",
            content="system.set_volume(level=20)",
            tool_call_id="tool-1",
            capability="system.set_volume",
        ))
        await delivery.on_stream(StreamEvent(tag="tool_output", content="ok", tool_call_id="tool-1"))
        await delivery.aclose(cancelled=False)

        assert tts.sentences == []
        assert self._cue_phases(manager) == ["start", "done"]

    @pytest.mark.asyncio
    async def test_silent_then_lookup_in_parallel_still_acks(self):
        _, tts = await self._deliver_calls(
            StreamEvent(tag="tool_call", content="spotify.skip()", tool_call_id="a", capability="spotify.skip"),
            StreamEvent(tag="tool_call", content="search.web()", tool_call_id="b", capability="search.web"),
        )
        assert len(tts.sentences) == 1
        assert tts.sentences[0] in PHRASES

    @pytest.mark.asyncio
    async def test_text_delivery_stays_silent(self):
        _, tts = await self._deliver_calls(
            StreamEvent(
                tag="tool_call",
                content="search.web(query='news')",
                tool_call_id="tool-1",
                capability="search.web",
            ),
            produce_audio=False,
        )
        assert tts.sentences == []

    @pytest.mark.asyncio
    async def test_ack_queue_does_not_wait_for_playback(self):
        tts = _GatedTTS()
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        await delivery.start()
        await asyncio.wait_for(
            delivery.on_stream(StreamEvent(
                tag="tool_call",
                content="search.web(query='news')",
                tool_call_id="tool-1",
                capability="search.web",
            )),
            timeout=0.5,
        )
        await asyncio.wait_for(tts.started.wait(), timeout=0.5)
        assert tts.sentences[0] in PHRASES
        tts.release.set()
        await delivery.aclose(cancelled=False)

    @pytest.mark.asyncio
    async def test_cancelled_close_drops_runtime_ack(self):
        tts = _GatedTTS()
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        await delivery.start()
        await delivery.on_stream(StreamEvent(
            tag="tool_call",
            content="search.web(query='news')",
            tool_call_id="tool-1",
            capability="search.web",
        ))
        await asyncio.wait_for(tts.started.wait(), timeout=0.5)
        await asyncio.wait_for(delivery.aclose(cancelled=True), timeout=0.5)
        assert delivery._cancel.is_set()

    @pytest.mark.asyncio
    async def test_execute_turn_does_not_block_on_ack_playback(self):
        tts = _GatedTTS()
        session = self._session()
        manager = SimpleNamespace(send_voice_response=AsyncMock())
        delivery = VoiceDelivery(
            session, manager, tts,
            session_id="test", turn_id="turn-test", produce_audio=True,
        )
        continued = asyncio.Event()

        async def _stream(*_args, **_kwargs):
            yield AgentEvent(
                type=AgentEventType.TOOL_CALL,
                content="search.web(query='news')",
                tool_call_id="tool-1",
                capability="search.web",
            )
            continued.set()
            yield AgentEvent(type=AgentEventType.TEXT, content="Here is the latest.")

        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        orchestrator.agent = MagicMock()
        orchestrator.agent.llm = MagicMock()
        orchestrator.agent.llm.model = "test-model"
        orchestrator.agent.process_stream = MagicMock(return_value=_stream())
        orchestrator.router = MagicMock()
        orchestrator.router.route = AsyncMock(return_value=orchestrator.agent)

        await delivery.start()
        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=set())
            result = TurnResult()
            turn = asyncio.create_task(orchestrator._execute_turn(
                transcript="What's the news?",
                source="user",
                connection_id="test-ack",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=False,
                attachments=None,
                delivery=delivery,
                result=result,
            ))
            await asyncio.wait_for(continued.wait(), timeout=1.0)
            assert tts.sentences[0] in PHRASES
            tts.release.set()
            await asyncio.wait_for(turn, timeout=1.0)

        await delivery.aclose(cancelled=False)


class TestBuildSystemTurnMessage:
    """Tests for the system turn context builder."""

    @staticmethod
    def _ctx(**overrides) -> str:
        kwargs = dict(message="")
        kwargs.update(overrides)
        return build_system_turn_message(SystemTurnContext(**kwargs))

    def test_tell_plain_alert(self):
        ctx = self._ctx(message="Standup starts in 5 minutes")
        assert "Always speak now" in ctx
        assert "Do not use NO_REPLY or DEFER on this path." in ctx

    def test_tell_plain_question_waits_for_user_answer(self):
        ctx = self._ctx(message="Did you do one page for Reading?")
        assert "ask it directly and wait for the user's answer" in ctx
        assert "do not answer it yourself" in ctx

    def test_tell_with_protocol(self):
        ctx = self._ctx(
            message="Morning routine",
            protocol_context="\nPROTOCOL: morning\nSteps:\n- greet user",
        )
        assert "Execute the protocol steps and speak a brief status." in ctx
        assert NO_REPLY_SENTINEL not in ctx

    def test_offer_plain_uses_offer_instruction(self):
        ctx = self._ctx(message="Background sync completed", decision="offer")
        assert "worth interrupting now" in ctx
        assert NO_REPLY_SENTINEL in ctx

    def test_offer_plain_with_instructions(self):
        ctx = self._ctx(
            message="Briefing",
            decision="offer",
            instructions="Gather the relevant live data, then speak a concise briefing.",
        )
        assert "worth interrupting now" in ctx
        assert "INSTRUCTIONS: Gather the relevant live data" in ctx
        assert NO_REPLY_SENTINEL in ctx

    def test_act_plain_executes_instructions(self):
        ctx = self._ctx(
            message="Turn off the living room light.",
            decision="act",
            instructions="Turn off the living room light.",
        )
        assert "Do the work described in INSTRUCTIONS" in ctx
        assert f"Respond {NO_REPLY_SENTINEL} when done." in ctx

    def test_offer_with_protocol_keeps_protocol_directive(self):
        ctx = self._ctx(
            message="Nightly backup",
            protocol_context="\nPROTOCOL: backup\nSteps:\n- rsync\n- verify",
            decision="offer",
        )
        assert "Execute the protocol steps; speak only if" in ctx
        assert NO_REPLY_SENTINEL in ctx

    def test_routing_hint_combines_message_instructions_and_protocol(self):
        hint = build_system_routing_hint(
            SystemTurnContext(
                message="How did your Consistent Sleep habit go?",
                instructions="Ask only if the user seems ready.",
                protocol_context="PROTOCOL: morning\nSteps:\n1. Check the calendar.",
            )
        )

        assert hint is not None
        assert "MESSAGE: How did your Consistent Sleep habit go?" in hint
        assert "INSTRUCTIONS: Ask only if the user seems ready." in hint
        assert "PROTOCOL:" in hint
        assert "Check the calendar." in hint

    def test_routing_hint_excludes_reply_grounding_and_live_state(self):
        hint = build_system_routing_hint(
            SystemTurnContext(
                message="Check in now.",
                reply_grounding={"habit_name": "Reading"},
                current_state="ACTIVE_COMMITMENTS: alarm",
                item_context={"subject": "private payload"},
            )
        )

        assert hint == "MESSAGE: Check in now."

    def test_routing_hint_is_bounded(self):
        hint = build_system_routing_hint(
            SystemTurnContext(
                message="Run the protocol.",
                protocol_context="x" * 10_000,
            )
        )

        assert hint is not None
        assert len(hint) == 4000

    def test_offer_with_item_context_classifies_before_executing(self):
        ctx = self._ctx(
            message="Mail check",
            item_context={"from": "a@b.com", "subject": "ping"},
            decision="offer",
        )
        assert "Classify the SOURCE EVENT" in ctx
        assert "from: a@b.com" in ctx
        assert NO_REPLY_SENTINEL in ctx

    def test_offer_with_task_result_keeps_relay_directive(self):
        ctx = self._ctx(
            message="Finished. Generated report",
            decision="offer",
            task_id="task-123",
        )
        assert "Relay this completed work result only if" in ctx
        assert NO_REPLY_SENTINEL in ctx

    def test_offer_with_current_state(self):
        ctx = self._ctx(
            message="How did you sleep?",
            decision="offer",
            current_state="ACTIVE_COMMITMENTS: none",
        )
        assert "worth interrupting now" in ctx
        assert "respond exactly DEFER" in ctx
        assert "DEFER_UNTIL" in ctx
        assert "respond exactly NO_REPLY" in ctx
        assert "CURRENT_STATE:" in ctx

    def test_instructions_renders_rule_and_instruction_blocks(self):
        ctx = self._ctx(
            message="FedEx tracking",
            item_context={"from": "noreply@fedex.com", "subject": "Shipment update"},
            rule_id="abc123",
            rule_name="FedEx tracker",
            instructions="Once you've extracted the tracking number, call delete_rule.",
            decision="tell",
        )
        assert 'RULE: id=abc123 name="FedEx tracker"' in ctx
        assert "INSTRUCTIONS: Once you've extracted the tracking number" in ctx
        assert "may require you to modify or remove the rule" in ctx

    def test_act_instructions_use_terse_suffix(self):
        ctx = self._ctx(
            message="FedEx tracking",
            item_context={"from": "noreply@fedex.com", "subject": "Shipment update"},
            rule_id="abc123",
            instructions="Extract tracking number.",
            decision="act",
        )
        assert "Honor the INSTRUCTIONS exactly" in ctx
        assert "may require you to modify" not in ctx

    def test_act_with_item_context_classifies_before_executing(self):
        ctx = self._ctx(
            message="Auto-queue Spotify request",
            item_context={"text": "Bohemian Rhapsody", "channel": "C123"},
            decision="act",
            instructions="Queue tracks using jarvis.spotify.queue",
        )
        assert "Classify the SOURCE EVENT against the INSTRUCTIONS and execute" in ctx
        assert f"Respond {NO_REPLY_SENTINEL} when done." in ctx
        assert "Announce" not in ctx
        assert "Speak only if worth it" not in ctx

    def test_act_plain_requires_instructions(self):
        ctx = self._ctx(
            message="Cleanup rule",
            decision="act",
            instructions="Cleanup rule",
        )
        assert "Do the work described in INSTRUCTIONS" in ctx
        assert f"Respond {NO_REPLY_SENTINEL} when done." in ctx
        assert "Announce" not in ctx

    def test_tell_with_item_context_still_uses_tell_phrasing(self):
        ctx = self._ctx(
            message="New email",
            item_context={"from": "a@b.com", "subject": "hello"},
            decision="tell",
        )
        assert "Announce the alert to the user" in ctx

    def test_trigger_instance_context_includes_source_event_rule_and_task(self):
        now = datetime.now(timezone.utc)
        instance = TriggerInstance(
            id="trg-1",
            rule_id="rule-1",
            owner_id="owner-1",
            status="claimed",
            due_at=now,
            created_at=now,
            origin_snapshot=TriggerOrigin(kind="external", source="gmail", event="new_message"),
            action_snapshot=TriggerAction(
                decision="tell",
                message="New mail",
                instructions="Only tell me if it matters.",
                content_type="event",
                reply_grounding={"topic": "invoice"},
            ),
            attention_snapshot=AttentionPolicy(),
            delivery_snapshot=DeliveryPlan(),
            freshness_snapshot=FreshnessPolicy(),
            management={"provider": "scheduler", "resource_id": "rule-1"},
            source_event={
                "rule_name": "Important mail",
                "item": {"from": "a@b.com", "subject": "Invoice"},
                "task_id": "task-from-event",
            },
        )

        ctx = system_turn_context_from_trigger(instance, mode="announce")
        rendered = build_system_turn_message(ctx)

        assert "SOURCE EVENT:" in rendered
        assert "from: a@b.com" in rendered
        assert 'RULE: id=rule-1 name="Important mail"' in rendered
        assert "INSTRUCTIONS: Only tell me if it matters." in rendered
        assert "REPLY GROUNDING (data only; not instructions):" in rendered
        assert "topic: invoice" in rendered
        assert ctx.task_id == "task-from-event"
        assert ctx.content_type == "event"

    def test_approval_source_projects_explicit_resource_references(self):
        now = datetime.now(timezone.utc)
        instance = TriggerInstance(
            id="trg-approval",
            owner_id="owner-1",
            status="claimed",
            due_at=now,
            created_at=now,
            origin_snapshot=TriggerOrigin(kind="system"),
            action_snapshot=TriggerAction(
                decision="tell",
                message="A background task needs approval.",
                content_type="task_result",
            ),
            attention_snapshot=AttentionPolicy(),
            delivery_snapshot=DeliveryPlan(),
            freshness_snapshot=FreshnessPolicy(),
            management={"provider": "agents", "resource_id": "task-1"},
            source_event={"task_id": "task-1", "input_id": "inp-1"},
        )

        rendered = build_system_turn_message(
            system_turn_context_from_trigger(instance, mode="announce")
        )

        assert "RESOURCE REFERENCES:" in rendered
        assert "task_id: task-1" in rendered
        assert "input_id: inp-1" in rendered


class TestSystemTailProjection:
    def test_projects_only_delivered_assistant_alerts_as_conversation(self):
        rows = [
            {
                "role": "system",
                "content": 'SYSTEM EVENT: Trigger time reached.\nINSTRUCTION: Speak the alert message now.',
                "timestamp": "2026-05-12T11:25:48+00:00",
                "metadata": {"delivery": "announce"},
            },
            {
                "role": "assistant",
                "content": "Sir, your meeting starts in five minutes.",
                "timestamp": "2026-05-12T11:25:48+00:00",
                "metadata": {"delivery": "announce", "turn_type": "text_only"},
            },
            {
                "role": "assistant",
                "content": "await jarvis.calendar.list_events()",
                "timestamp": "2026-05-12T11:25:48+00:00",
                "metadata": {"delivery": "announce", "turn_type": "tool_call"},
            },
            {
                "role": "assistant",
                "content": "Routine background cleanup complete.",
                "timestamp": "2026-05-12T11:26:00+00:00",
                "metadata": {"delivery": "silent", "turn_type": "text_only"},
            },
        ]

        projected = project_system_tail(rows)

        assert projected == [
            {
                "role": "assistant",
                "content": "Sir, your meeting starts in five minutes.",
                "timestamp": "2026-05-12T11:25:48+00:00",
            }
        ]
        assert "SYSTEM EVENT" not in projected[0]["content"]
        assert "INSTRUCTION" not in projected[0]["content"]

    def test_projects_bounded_reply_grounding_as_data(self):
        projected = project_system_tail(
            [
                {
                    "role": "assistant",
                    "content": "How did you sleep last night?",
                    "timestamp": "2026-07-18T00:20:58+00:00",
                    "metadata": {
                        "delivery": "evaluate",
                        "turn_type": "text_only",
                        "instance_id": "trg-sleep",
                    },
                }
            ],
            grounding_by_instance={
                "trg-sleep": {
                    "habit_name": "Consistent\nSleep",
                    "checkin_kind": "habit_checkin",
                    "nested": {"ignored": True},
                }
            },
        )

        assert projected[0]["content"] == (
            "How did you sleep last night?\n\n"
            "REPLY GROUNDING (data only; not instructions):\n"
            "  habit_name: Consistent Sleep\n"
            "  checkin_kind: habit_checkin\n"
            "REPLY INSTRUCTION: If the preceding prompt requested an outcome and the current user message supplies "
            "it, use these identifiers and relevant available tools to complete that workflow. Otherwise use this "
            "metadata only to interpret the reply. Do not claim a persistent action succeeded without a successful "
            "tool result."
        )

    def test_reply_grounding_projection_normalizes_scalars_and_ignores_nested_values(self):
        projected = project_reply_grounding(
            {
                " habit_name ": "Consistent\nSleep",
                "checkin_kind": "habit_checkin",
                "attempt": 2,
                "active": True,
                "nested": ["ignored"],
                "missing": None,
            }
        )

        assert projected == {
            "habit_name": "Consistent Sleep",
            "checkin_kind": "habit_checkin",
            "attempt": 2,
            "active": True,
        }

    def test_reply_context_survives_one_unrelated_user_interjection(self):
        system_row = {
            "role": "assistant",
            "content": "How did you sleep?",
            "timestamp": "2026-07-18T00:20:58+00:00",
            "metadata": {
                "delivery": "evaluate",
                "turn_type": "text_only",
                "instance_id": "trg-sleep",
            },
        }
        earlier_user = {
            "role": "user",
            "content": "Good morning",
            "timestamp": "2026-07-18T00:00:00+00:00",
        }
        later_user = {
            "role": "user",
            "content": "What time is it?",
            "timestamp": "2026-07-18T01:40:44+00:00",
        }
        second_later_user = {
            "role": "user",
            "content": "Eight hours",
            "timestamp": "2026-07-18T01:41:44+00:00",
        }

        assert latest_reply_context_system_row([earlier_user], [system_row]) == system_row
        assert (
            latest_reply_context_system_row([earlier_user, later_user], [system_row])
            == system_row
        )
        assert (
            latest_reply_context_system_row(
                [earlier_user, later_user, second_later_user],
                [system_row],
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_loads_settled_grounding_and_tools_for_same_node_reconnect(self):
        system_row = {
            "role": "assistant",
            "content": "How did you sleep last night?",
            "timestamp": "2026-07-18T00:20:58+00:00",
            "metadata": {
                "delivery": "evaluate",
                "turn_type": "text_only",
                "instance_id": "trg-sleep",
                "routed_tools": ["habits.log_habit_by_name"],
            },
        }
        with (
            patch("core.turns.history.mongodb") as mock_db,
            patch(
                "core.triggers.service.trigger_service.get_delivered_reply_grounding",
                new=AsyncMock(
                    return_value={
                        "trg-sleep": {
                            "habit_name": "Consistent Sleep",
                            "checkin_kind": "habit_checkin",
                        }
                    }
                ),
            ) as get_grounding,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(side_effect=[[], [system_row]])

            loaded = await load_turn_history(
                owner_id="owner-1",
                session_context={"node_id": "bedroom"},
                current_turn_id="turn-reply",
                policy="interactive_user",
            )

        get_grounding.assert_awaited_once_with(
            owner_id="owner-1",
            instance_ids=["trg-sleep"],
        )
        assert loaded.reply_tools == frozenset({"habits.log_habit_by_name"})
        assert "habit_name: Consistent Sleep" in loaded.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_unsettled_trigger_does_not_restore_grounding_or_tools(self):
        system_row = {
            "role": "assistant",
            "content": "How did you sleep last night?",
            "timestamp": "2026-07-18T00:20:58+00:00",
            "metadata": {
                "delivery": "evaluate",
                "turn_type": "text_only",
                "instance_id": "trg-sleep",
                "routed_tools": ["habits.log_habit_by_name"],
            },
        }
        with (
            patch("core.turns.history.mongodb") as mock_db,
            patch(
                "core.triggers.service.trigger_service.get_delivered_reply_grounding",
                new=AsyncMock(return_value={}),
            ),
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(side_effect=[[], [system_row]])

            loaded = await load_turn_history(
                owner_id="owner-1",
                session_context={"node_id": "bedroom"},
                current_turn_id="turn-reply",
                policy="interactive_user",
            )

        assert loaded.reply_tools == frozenset()
        assert loaded.messages == [
            {"role": "assistant", "content": "How did you sleep last night?"}
        ]

    @pytest.mark.asyncio
    async def test_suppressed_user_turn_stays_in_context_without_no_reply_sentinel(self):
        suppressed_turn = [
            {
                "role": "user",
                "content": "Don't worry about that alarm, I'm awake.",
                "timestamp": "2026-07-23T23:28:54+00:00",
            },
            {
                "role": "assistant",
                "content": 'scheduler.cancel_alert({"instance_id":"trg-1"})',
                "timestamp": "2026-07-23T23:28:56+00:00",
                "metadata": {
                    "turn_type": "tool_call",
                    "tool_call_id": "tc-1",
                    "capability": "scheduler.cancel_alert",
                    "arguments": {"instance_id": "trg-1"},
                    "spoken": "",
                },
            },
            {
                "role": "user",
                "content": "Success: Notification cancelled.",
                "timestamp": "2026-07-23T23:28:57+00:00",
                "metadata": {
                    "turn_type": "tool_result",
                    "tool_call_id": "tc-1",
                    "output": "Success: Notification cancelled.",
                },
            },
            {
                "role": "assistant",
                "content": "NO_REPLY",
                "timestamp": "2026-07-23T23:28:58+00:00",
            },
        ]
        with patch("core.turns.history.mongodb") as mock_db:
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(side_effect=[suppressed_turn, []])

            loaded = await load_turn_history(
                owner_id="owner-1",
                session_context={"node_id": "bedroom"},
                current_turn_id="turn-next",
                policy="proactive_bounded",
            )

        user_history_call = mock_db.get_history.await_args_list[0]
        assert "exclude_deliveries" not in user_history_call.kwargs

        contents = [m.get("content") for m in loaded.messages]
        assert "Don't worry about that alarm, I'm awake." in contents
        assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in loaded.messages)
        tool_msg = next(m for m in loaded.messages if m.get("role") == "tool")
        assert "cancelled" in tool_msg["content"]
        assert "NO_REPLY" not in contents
        assert mock_db.get_history.await_args_list[0].kwargs.get("include_metadata") is True

    def test_merges_delivered_tail_chronologically(self):
        user_history = [
            {
                "role": "user",
                "content": "Yeah, that'd be great.",
                "timestamp": "2026-05-21T02:27:52+00:00",
            },
            {
                "role": "assistant",
                "content": "I've started searching for those alternative perspectives.",
                "timestamp": "2026-05-21T02:28:02+00:00",
            },
        ]
        system_tail = [
            {
                "role": "assistant",
                "content": "Finished. The research found AIPAC influence arguments.",
                "timestamp": "2026-05-21T02:29:17+00:00",
            }
        ]

        merged = merge_user_history_with_system_tail(
            user_history,
            system_tail,
        )

        assert merged == [
            {"role": "user", "content": "Yeah, that'd be great."},
            {
                "role": "assistant",
                "content": "I've started searching for those alternative perspectives.",
            },
            {
                "role": "assistant",
                "content": "Finished. The research found AIPAC influence arguments.",
            },
        ]


class TestTriggerDeliveryFreshness:
    def test_explicit_expires_at_policy_wins(self):
        now = datetime(2026, 5, 22, 1, 20, tzinfo=timezone.utc)
        instance = SimpleNamespace(
            attention_snapshot=AttentionPolicy(level="critical", sound="alarm", requires_ack=True),
            origin_snapshot=TriggerOrigin(kind="time"),
            source_event={},
            due_at=now - timedelta(minutes=1),
            freshness_snapshot=FreshnessPolicy(expires_at=now - timedelta(seconds=1)),
        )

        assert trigger_expiry_reason(instance, now=now) == "freshness_expired"

    def test_explicit_expire_after_due_policy_is_deterministic(self):
        now = datetime(2026, 5, 22, 1, 20, tzinfo=timezone.utc)
        instance = SimpleNamespace(
            attention_snapshot=AttentionPolicy(level="normal", sound="chime"),
            origin_snapshot=TriggerOrigin(kind="time"),
            source_event={},
            due_at=now - timedelta(minutes=11),
            freshness_snapshot=FreshnessPolicy(expire_after_due_s=600),
        )

        assert trigger_expiry_reason(instance, now=now) == "delivery_ttl_expired"

    def test_calendar_source_event_staleness_expires_after_event_start(self):
        now = datetime(2026, 5, 22, 1, 12, tzinfo=timezone.utc)
        instance = SimpleNamespace(
            attention_snapshot=AttentionPolicy(level="normal", sound="chime"),
            origin_snapshot=TriggerOrigin(kind="external", source="calendar", offset_minutes=-1),
            source_event={"item": {"start": "2026-05-22T01:10:00+00:00"}},
            due_at=now - timedelta(minutes=3),
            freshness_snapshot=FreshnessPolicy(stale_if_source_event_started=True),
        )

        assert trigger_expiry_reason(instance, now=now) == "calendar_event_started"

    def test_alarm_style_trigger_does_not_expire_before_ack(self):
        now = datetime(2026, 5, 22, 1, 20, tzinfo=timezone.utc)
        instance = SimpleNamespace(
            attention_snapshot=AttentionPolicy(level="critical", sound="alarm", requires_ack=True),
            origin_snapshot=TriggerOrigin(kind="time"),
            source_event={},
            due_at=now - timedelta(hours=1),
            freshness_snapshot=FreshnessPolicy(),
        )

        assert trigger_expiry_reason(instance, now=now) is None

    def test_normal_trigger_without_freshness_deadline_does_not_expire(self):
        now = datetime(2026, 5, 22, 1, 20, tzinfo=timezone.utc)
        instance = SimpleNamespace(
            attention_snapshot=AttentionPolicy(level="normal", sound="chime"),
            origin_snapshot=TriggerOrigin(kind="time"),
            source_event={},
            due_at=now - timedelta(minutes=6),
            freshness_snapshot=FreshnessPolicy(),
        )

        assert trigger_expiry_reason(instance, now=now) is None


# --- Background-run audit persistence -------------------------------------


class TestPersistTraceDeliveryTagging:
    """`_persist_trace` is the single writer for every turn shape — announce,
    silent, suppressed, evaluate, prefetched. The `delivery` kwarg drives
    main-transcript vs audit-feed routing; `origin` carries audit fields.
    """

    @pytest.fixture
    def orchestrator(self) -> AssistantOrchestrator:
        return AssistantOrchestrator.__new__(AssistantOrchestrator)

    @pytest.mark.asyncio
    async def test_delivery_tag_injected_into_every_row(self, orchestrator):
        trace = [
            ("system", "SYSTEM EVENT: ...", None),
            ("assistant", "thinking...\n<tool_call>\njarvis.gmail.archive(...)\n</tool_call>",
             {"turn_type": "tool_call", "tool_call_id": "tc-1"}),
            ("user", "<tool_result>archived 3</tool_result>",
             {"turn_type": "tool_result", "tool_call_id": "tc-1", "output": "archived 3"}),
            ("assistant", "archived",
             {"turn_type": "text_only"}),
        ]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock()
            await orchestrator._persist_trace(
                "user-1", "system", trace,
                turn_id="turn-test123",
                delivery="silent",
                origin={"trigger_source": "automation", "rule_id": "r1", "rule_name": "Archive"},
            )
        assert mock_db.store_message.await_count == 4
        for call in mock_db.store_message.await_args_list:
            meta = call.kwargs["metadata"]
            assert meta["delivery"] == "silent"
            assert meta["trigger_source"] == "automation"
            assert meta["rule_id"] == "r1"
            assert meta["rule_name"] == "Archive"
        # Existing per-row metadata (turn_type, tool_call_id) is preserved.
        tool_call_meta = mock_db.store_message.await_args_list[1].kwargs["metadata"]
        assert tool_call_meta["turn_type"] == "tool_call"
        assert tool_call_meta["tool_call_id"] == "tc-1"
        tool_result_meta = mock_db.store_message.await_args_list[2].kwargs["metadata"]
        assert tool_result_meta["output"] == "archived 3"

    @pytest.mark.asyncio
    async def test_origin_does_not_overwrite_existing_keys(self, orchestrator):
        """Trace rows that already declare a key (e.g. `model`) keep their value —
        origin only fills gaps."""
        trace = [("assistant", "x", {"model": "row-model"})]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock()
            await orchestrator._persist_trace(
                "user-1", "system", trace,
                turn_id="turn-test456",
                delivery="announce",
                origin={"model": "meta-model", "rule_id": "r2"},
            )
        meta = mock_db.store_message.await_args.kwargs["metadata"]
        assert meta["model"] == "row-model"  # row wins
        assert meta["rule_id"] == "r2"       # filled from origin

    @pytest.mark.asyncio
    async def test_no_delivery_kwarg_leaves_metadata_untouched(self, orchestrator):
        """User turns pass no delivery — existing metadata flows through unchanged."""
        trace = [("user", "hi", None), ("assistant", "hello", {"turn_type": "text_only"})]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock()
            await orchestrator._persist_trace("user-1", "user", trace, turn_id="turn-test789")
        first = mock_db.store_message.await_args_list[0].kwargs["metadata"]
        second = mock_db.store_message.await_args_list[1].kwargs["metadata"]
        assert first == {"turn_id": "turn-test789"}
        assert second["turn_type"] == "text_only"
        assert second["turn_id"] == "turn-test789"
        assert "delivery" not in second

    @pytest.mark.asyncio
    async def test_per_row_failure_does_not_drop_remaining_rows(self, orchestrator):
        trace = [("assistant", "a", None), ("assistant", "b", None), ("assistant", "c", None)]
        side_effects = [None, RuntimeError("boom"), None]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock(side_effect=side_effects)
            await orchestrator._persist_trace(
                "user-1", "system", trace,
                turn_id="turn-testAAA",
                delivery="suppressed",
            )
        assert mock_db.store_message.await_count == 3

    @pytest.mark.asyncio
    async def test_reasoning_row_metadata_preserved(self, orchestrator):
        trace = [
            ("assistant", "planning", {
                "turn_type": "reasoning",
                "response_id": "resp-1",
                "model": "claude-sonnet-4-6",
                "reasoning_effort": "medium",
            }),
        ]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock()
            await orchestrator._persist_trace(
                "user-1", "system", trace,
                turn_id="turn-reason",
                delivery="silent",
                origin={"trigger_source": "prefetch"},
            )
        meta = mock_db.store_message.await_args.kwargs["metadata"]
        assert meta["turn_type"] == "reasoning"
        assert meta["response_id"] == "resp-1"
        assert meta["reasoning_effort"] == "medium"
        assert meta["delivery"] == "silent"
        assert meta["trigger_source"] == "prefetch"

    @pytest.mark.asyncio
    async def test_delivery_string_persisted_not_voicedelivery_object(self, orchestrator):
        """Regression: `process_turn` once shadowed its `delivery: Optional[str]`
        kwarg with a local `VoiceDelivery` instance, leaking the object into
        metadata. Lock the contract: only str values land in metadata.delivery.
        """
        trace = [("assistant", "ok", None)]
        with patch("core.turns.orchestrator.mongodb") as mock_db:
            mock_db.store_message = AsyncMock()
            await orchestrator._persist_trace(
                "user-1", "system", trace,
                turn_id="turn-testBBB", delivery="announce",
            )
        meta = mock_db.store_message.await_args.kwargs["metadata"]
        assert isinstance(meta["delivery"], str)
        assert meta["delivery"] == "announce"


# --- routing_hint tool routing for automation turns -----------------------


class TestRoutingHint:
    """Verify that _execute_turn routes tools via routing_hint for system turns
    and still routes on transcript for user turns."""

    @pytest.fixture
    def orchestrator(self) -> AssistantOrchestrator:
        orch = AssistantOrchestrator.__new__(AssistantOrchestrator)
        orch.agent = MagicMock()
        orch.agent.llm = MagicMock()
        orch.agent.llm.model = "test-model"
        # process_stream must return an async iterable, not a coroutine.
        orch.agent.process_stream = MagicMock(return_value=AsyncIterEmpty())
        # Stub the router so route() returns the fake agent without embedding calls.
        orch.router = MagicMock()
        orch.router.route = AsyncMock(return_value=orch.agent)
        return orch

    @pytest.mark.asyncio
    async def test_system_turn_with_directive_routes_on_hint(self, orchestrator):
        """A silent automation with a directive should call tool_router.route
        on the directive text, not the full system_context."""
        directive = "Queue Spotify tracks using jarvis.spotify.queue"
        fake_routed = {"spotify.queue", "spotify.search"}

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=fake_routed)

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="SYSTEM EVENT: ...",
                source="system",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                routing_hint=directive,
            )

            mock_router.route.assert_awaited_once_with(
                directive,
                "test-routing",
                policy=SYSTEM_POLICY,
            )
            assert set(result.routed_tools) == fake_routed

    @pytest.mark.asyncio
    async def test_system_turn_without_hint_gets_empty_set(self, orchestrator):
        """A plain protocol turn with no routing_hint should get empty routed_tools."""
        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock()

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="SYSTEM EVENT: Execute protocol",
                source="system",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
            )

            mock_router.route.assert_not_awaited()
            assert result.routed_tools == []

    @pytest.mark.asyncio
    async def test_user_turn_still_routes_on_transcript(self, orchestrator):
        """User turns must route on the transcript, ignoring routing_hint."""
        fake_routed = {"weather.get_forecast"}

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=fake_routed)

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="What's the weather like?",
                source="user",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=True,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                routing_hint="this should be ignored",
            )

            mock_router.route.assert_awaited_once_with(
                "What's the weather like?",
                "test-routing",
                policy=TEXT_POLICY,
            )

    @pytest.mark.asyncio
    async def test_user_turn_restores_delivered_route_carryover_after_reconnect(self, orchestrator):
        system_row = {
            "role": "assistant",
            "content": "How did you sleep last night?",
            "timestamp": "2026-07-18T00:20:58+00:00",
            "metadata": {
                "delivery": "evaluate",
                "turn_type": "text_only",
                "instance_id": "trg-sleep",
                "routed_tools": ["habits.log_habit_by_name"],
            },
        }
        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
            patch(
                "core.triggers.service.trigger_service.get_delivered_reply_grounding",
                new=AsyncMock(
                    return_value={
                        "trg-sleep": {
                            "habit_name": "Consistent Sleep",
                            "checkin_kind": "habit_checkin",
                        }
                    }
                ),
            ),
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(side_effect=[[], [system_row]])
            mock_router.route = AsyncMock(return_value={"habits.log_habit_by_name"})

            await orchestrator._execute_turn(
                transcript="About seven hours.",
                source="user",
                connection_id="new-connection",
                owner_id="test-owner",
                session_context={"timezone": "UTC", "node_id": "bedroom"},
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=TurnResult(),
                current_turn_id="turn-reply",
            )

        mock_router.record_route_carryover.assert_called_once_with(
            "new-connection",
            tools={"habits.log_habit_by_name"},
        )

    @pytest.mark.asyncio
    async def test_user_turn_applies_inactivity_window_to_history_queries(self, orchestrator):
        since = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=since)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=set())

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="What's next?",
                source="user",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC", "node_id": "bedroom"},
                text_input=True,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                current_turn_id="turn-current",
            )

            mock_db.resolve_conversation_window_start.assert_awaited_once_with(
                "test-owner",
                "bedroom",
                gap=ANY,
                exclude_turn_id="turn-current",
                visible_deliveries=ANY,
            )
            for call in mock_db.get_history.await_args_list:
                assert call.kwargs["node_id"] == "bedroom"
                assert call.kwargs["since"] == since

    @pytest.mark.asyncio
    async def test_system_turn_uses_bounded_proactive_history_projection(self, orchestrator):
        since = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=since)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock()

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="SYSTEM EVENT: Trigger time reached.",
                source="system",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC", "node_id": "bedroom"},
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                current_turn_id="turn-current",
            )

            mock_db.resolve_conversation_window_start.assert_awaited_once_with(
                "test-owner",
                "bedroom",
                gap=ANY,
                exclude_turn_id="turn-current",
                visible_deliveries=ANY,
            )
            assert mock_db.get_history.await_count == 2
            user_call, system_tail_call = mock_db.get_history.await_args_list
            assert user_call.kwargs["source_filter"] == ["user"]
            assert user_call.kwargs["limit"] == 100
            assert system_tail_call.kwargs["source_filter"] == ["system"]
            assert system_tail_call.kwargs["limit"] == 5
            for call in mock_db.get_history.await_args_list:
                assert call.kwargs["node_id"] == "bedroom"
                assert call.kwargs["since"] == since
                assert call.kwargs["exclude_turn_id"] == "turn-current"

    @pytest.mark.asyncio
    async def test_headless_minimal_history_policy_loads_no_transcript(self, orchestrator):
        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock()
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock()

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="SYSTEM EVENT: Run silently.",
                source="system",
                connection_id="test-routing",
                owner_id="test-owner",
                session_context={"timezone": "UTC", "node_id": "bedroom"},
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                history_policy="headless_minimal",
            )

            mock_db.resolve_conversation_window_start.assert_not_awaited()
            mock_db.get_history.assert_not_awaited()


class TestPromptModeResolution:
    def test_user_turns_use_full_prompt(self):
        assert resolve_prompt_mode("user") == PromptMode.FULL

    def test_system_turns_use_background_prompt(self):
        assert resolve_prompt_mode("system") == PromptMode.BACKGROUND
        assert resolve_prompt_mode("automation") == PromptMode.BACKGROUND


class TestAssistantSanitizationBoundary:
    @pytest.mark.asyncio
    async def test_execute_turn_sanitizes_before_delivery_and_persistence(self):
        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        orchestrator.agent = MagicMock()
        orchestrator.agent.llm = MagicMock()
        orchestrator.agent.llm.model = "test-model"
        orchestrator.agent.process_stream = MagicMock(return_value=AsyncIterEvents([
            AgentEvent(
                type=AgentEventType.TEXT,
                content="<|channel>thought\n<channel|>Visible answer.",
            )
        ]))
        orchestrator.router = MagicMock()
        orchestrator.router.route = AsyncMock(return_value=orchestrator.agent)
        delivery = RecordingDelivery()

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=set())

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="Hello",
                source="user",
                connection_id="test-sanitize",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=True,
                attachments=None,
                delivery=delivery,
                result=result,
            )

        assert delivery.text_events == ["Visible answer."]
        assert result.full_response == "Visible answer."
        assert result.turn_trace[-1][1] == "Visible answer."
        assert "<|channel" not in result.turn_trace[-1][1]

    @pytest.mark.asyncio
    async def test_runtime_error_is_not_model_text(self):
        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        orchestrator.agent = MagicMock()
        orchestrator.agent.llm = MagicMock()
        orchestrator.agent.llm.model = "test-model"
        orchestrator.agent.process_stream = MagicMock(return_value=AsyncIterEvents([
            AgentEvent(
                type=AgentEventType.ERROR,
                content="I'm having trouble reaching my language model.",
            )
        ]))
        orchestrator.router = MagicMock()
        orchestrator.router.route = AsyncMock(return_value=orchestrator.agent)
        delivery = RecordingDelivery()

        with (
            patch("core.turns.execution.require_llm_ready", return_value=None),
            patch("core.turns.history.mongodb") as mock_db,
            patch("core.tool_router.tool_router") as mock_router,
        ):
            mock_db.resolve_conversation_window_start = AsyncMock(return_value=None)
            mock_db.get_history = AsyncMock(return_value=[])
            mock_router.route = AsyncMock(return_value=set())

            result = TurnResult()
            await orchestrator._execute_turn(
                transcript="Hello",
                source="user",
                connection_id="test-runtime-error",
                owner_id="test-owner",
                session_context={"timezone": "UTC"},
                text_input=True,
                attachments=None,
                delivery=delivery,
                result=result,
            )

        assert result.runtime_error == "I'm having trouble reaching my language model."
        assert result.full_response == ""
        assert delivery.text_events == []
        assert result.turn_trace[-1][2]["turn_type"] == "runtime_error"


class TestAgentToolIdentityBoundary:
    @pytest.mark.asyncio
    async def test_structured_call_uses_owner_id_context(self):
        class FakeLLM:
            model = "test-model"

            async def chat_stream(self, **kwargs):
                yield ToolCallEvent(
                    call_id="tcall-1",
                    name="db__clear_conversation_history",
                    arguments={},
                )

        from core.plugins.capabilities import CapabilityCall, CapabilityOutcome, InvocationRecord, InvocationStatus

        captured: list[CapabilityCall] = []

        async def fake_dispatch(call: CapabilityCall) -> CapabilityOutcome:
            captured.append(call)
            return CapabilityOutcome(
                call_id=call.call_id,
                capability=call.capability,
                status=InvocationStatus.SUCCEEDED,
                data="Success: Deleted 3 messages.",
                invocation=InvocationRecord(
                    invocation_id="inv-1",
                    capability=call.capability,
                    status=InvocationStatus.SUCCEEDED,
                    source="structured",
                    tool_call_id=call.call_id,
                ),
            )

        definition = SimpleNamespace(fqn="db.clear_conversation_history")
        agent = JarvisAgent(FakeLLM())
        agent.prompt_builder.build = MagicMock(return_value="")

        with (
            patch("core.agent.agent.compact_history", AsyncMock(return_value=([], {}))),
            patch("core.agent.agent.dispatcher.dispatch", side_effect=fake_dispatch),
            patch("core.agent.agent.registry.resolve_provider_name", return_value=definition),
            patch("core.agent.agent.registry.provider_tools", return_value=[]),
        ):
            events = [
                event
                async for event in agent.process_stream(
                    "clear history",
                    [],
                    "conn-browser",
                    context={
                        "owner_id": "geoff",
                        "connection_id": "conn-browser",
                        "timezone": "Australia/Sydney",
                    },
                    max_iterations=1,
                )
            ]

        assert captured[0].capability == "db.clear_conversation_history"
        assert any(event.type == AgentEventType.TOOL_OUTPUT for event in events)
        assert any(event.type == AgentEventType.TOOL_CALL and event.capability == "db.clear_conversation_history" for event in events)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [
        "```python\nawait jarvis.db.clear_conversation_history()\n```",
        "I'll check. <tool_call>\nawait jarvis.db.clear_conversation_history()",
        '<|tool_call>call:get_weather{location:<|"|>Tokyo<|"|>}<tool_call|>',
    ])
    async def test_legacy_action_text_is_not_executed(self, text):
        class FakeLLM:
            model = "test-model"

            async def chat_stream(self, **_kwargs):
                yield TextEvent(text=text)

        dispatched = []

        async def fake_dispatch(call):
            dispatched.append(call)
            raise AssertionError("should not dispatch")

        agent = JarvisAgent(FakeLLM())
        agent.prompt_builder.build = MagicMock(return_value="")

        with (
            patch("core.agent.agent.compact_history", AsyncMock(return_value=([], {}))),
            patch("core.agent.agent.dispatcher.dispatch", side_effect=fake_dispatch),
            patch("core.agent.agent.registry.provider_tools", return_value=[]),
        ):
            events = [
                event
                async for event in agent.process_stream(
                    "do it",
                    [],
                    "conn-browser",
                    context={"owner_id": "geoff", "connection_id": "conn-browser"},
                    max_iterations=1,
                )
            ]

        assert dispatched == []
        assert not any(event.type == AgentEventType.TOOL_CALL for event in events)


class AsyncIterEmpty:
    """Async iterator that yields nothing — stub for agent.process_stream."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class AsyncIterEvents:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


class RecordingDelivery(HeadlessDelivery):
    def __init__(self):
        self.text_events: list[str] = []
        self.delivered_text = ""

    async def on_stream(self, event):
        if event.tag == "text":
            self.text_events.append(event.content)
            self.delivered_text += event.content
