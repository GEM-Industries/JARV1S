"""Idempotent Jarvis runtime initialization after setup."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core import settings
from core.credentials.store import credential_store
from core.setup.llm_config import llm_config_store, resolve_llm_config

logger = logging.getLogger(__name__)


class JarvisRuntime:
    def __init__(self) -> None:
        self.core_ready = False
        self.initializing = False
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self.background_agent_ready = False
        self.background_agent_last_error: Optional[str] = None

    async def initialize_if_ready(self, *, force: bool = False) -> bool:
        async with self._lock:
            if self.core_ready and not force:
                return True

            llm_config = await resolve_llm_config()
            if not llm_config.attemptable:
                self.core_ready = False
                return False

            self.initializing = True
            self.last_error = None
            try:
                from api.websockets.handlers import initialize_llm_component, llm

                llm_config.apply_to(llm)
                await initialize_llm_component()
                if not llm.is_initialized:
                    raise RuntimeError("LLM client failed to initialize")

                from core.setup.validation import probe_action_capability

                if llm_config.action_capable is not True:
                    action_capable = await probe_action_capability(llm)
                    await llm_config_store.save(
                        provider=llm_config.provider,
                        model=llm_config.model,
                        base_url=llm_config.base_url,
                        action_capable=action_capable,
                    )
                    if not action_capable:
                        logger.info("Configured model is chat-only; tools will not be sent")

                from core.tool_router import tool_router

                background_llm = await self._initialize_background_llm()
                await tool_router.initialize(llm_service=llm)
                if background_llm is not None:
                    self._register_background_agent(background_llm)
                else:
                    await self._unregister_background_agent()
                self.core_ready = True
                self._prewarm_optional_voice_output()
                logger.info("Jarvis runtime initialized")
                return True
            except Exception as exc:
                self.core_ready = False
                self.last_error = str(exc)
                logger.error("Jarvis runtime initialization failed: %s", exc)
                return False
            finally:
                self.initializing = False

    async def _initialize_background_llm(self):
        self.background_agent_ready = False
        self.background_agent_last_error = None
        api_key = credential_store.get_stored_secret("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        from core.llm.providers import get_llm_provider
        from core.llm.service import LLMService

        provider = get_llm_provider("anthropic")
        background_llm = LLMService(
            api_key=api_key,
            base_url=provider.base_url,
            model=settings.BACKGROUND_AGENT_MODEL,
            provider_name="anthropic",
            first_token_timeout_s=settings.BACKGROUND_AGENT_STREAM_FIRST_TOKEN_TIMEOUT_S,
            first_token_retries=settings.BACKGROUND_AGENT_STREAM_FIRST_TOKEN_RETRIES,
            stream_idle_timeout_s=settings.BACKGROUND_AGENT_STREAM_IDLE_TIMEOUT_S,
            request_timeout_s=settings.BACKGROUND_AGENT_HTTP_TIMEOUT_S,
        )
        try:
            await background_llm.initialize()
            return background_llm
        except Exception as exc:
            self.background_agent_last_error = str(exc)
            logger.warning("Background agent LLM initialization failed: %s", exc)
            return None

    def _register_background_agent(self, background_llm) -> None:
        from core.agent.agent import JarvisAgent
        from core.integrations.manager import integrations

        background_agent = JarvisAgent(llm_service=background_llm)
        integrations.register("background_agent", lambda config: background_agent, config_keys=[])
        self.background_agent_ready = True
        self.background_agent_last_error = None
        logger.info("Background agent registered (model=%s)", settings.BACKGROUND_AGENT_MODEL)

    async def _unregister_background_agent(self) -> None:
        from core.integrations.manager import integrations

        await integrations.unregister("background_agent")
        self.background_agent_ready = False

    def _prewarm_optional_voice_output(self) -> None:
        from core.voice.config import resolve_voice_config_sync

        config = resolve_voice_config_sync()
        if config.tts_provider == "off":
            return

        async def _runner() -> None:
            try:
                from api.websockets.handlers import tts

                await tts.initialize()
            except Exception as exc:
                logger.warning("Optional voice output initialization failed: %s", exc)

        asyncio.create_task(_runner())


jarvis_runtime = JarvisRuntime()
