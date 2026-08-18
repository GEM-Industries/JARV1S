from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class LLMRequestPolicy:
    """Provider/model-specific request params kept out of the hot LLM service path."""

    model_contains: tuple[str, ...]
    params: dict[str, Any]
    only_without_reasoning_effort: bool = True

    def applies_to(self, model: str, *, reasoning_effort: str | None) -> bool:
        model_key = model.lower()
        if self.only_without_reasoning_effort and reasoning_effort:
            return False
        return any(fragment in model_key for fragment in self.model_contains)


@dataclass(frozen=True)
class LLMProviderPreset:
    name: str
    label: str
    signup_url: str
    base_url: str
    recommended_model: str
    credential_names: tuple[str, ...]
    stability: Literal["stable", "preview"] = "stable"
    requires_api_key: bool = True
    request_policies: tuple[LLMRequestPolicy, ...] = field(default_factory=tuple)

    @property
    def model(self) -> str:
        return self.recommended_model

    def extra_request_params(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for policy in self.request_policies:
            if policy.applies_to(model, reasoning_effort=reasoning_effort):
                params.update(policy.params)
        return params


_GEMMA4_NO_REASONING = LLMRequestPolicy(
    model_contains=("gemma-4", "gemma4"),
    params={
        # Keep Gemma 4 fast by default on OpenAI-compatible providers that
        # expose reasoning controls outside LiteLLM's common parameter set.
        "extra_body": {"reasoning_effort": "none"},
    },
)

# Ollama requires a top-level reasoning_effort; LiteLLM translates "none" to
# the native `think=false`. Explicit low/medium/high efforts pass through.
_GEMMA4_NO_REASONING_OLLAMA = LLMRequestPolicy(
    model_contains=("gemma-4", "gemma4"),
    params={"reasoning_effort": "none"},
)


LLM_PROVIDER_PRESETS: dict[str, LLMProviderPreset] = {
    "deepinfra": LLMProviderPreset(
        name="deepinfra",
        label="DeepInfra",
        signup_url="https://deepinfra.com/dash/api_keys",
        base_url="https://api.deepinfra.com/v1/openai",
        recommended_model="google/gemma-4-26B-A4B-it",
        credential_names=("DEEPINFRA_API_KEY", "DEEPINFRA_TOKEN"),
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
    "openrouter": LLMProviderPreset(
        name="openrouter",
        label="OpenRouter",
        signup_url="https://openrouter.ai/keys",
        base_url="https://openrouter.ai/api/v1",
        recommended_model="google/gemma-4-26b-a4b-it",
        credential_names=("OPENROUTER_API_KEY", "LLM_API_KEY"),
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
    "google-ai-studio": LLMProviderPreset(
        name="google-ai-studio",
        label="Google AI Studio",
        signup_url="https://aistudio.google.com/apikey",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        recommended_model="gemini-3.5-flash",
        credential_names=("GOOGLE_AI_STUDIO_API_KEY", "GEMINI_API_KEY"),
    ),
    "groq": LLMProviderPreset(
        name="groq",
        label="Groq",
        signup_url="https://console.groq.com/keys",
        base_url="https://api.groq.com/openai/v1",
        recommended_model="openai/gpt-oss-120b",
        credential_names=("GROQ_API_KEY",),
    ),
    "together": LLMProviderPreset(
        name="together",
        label="Together AI",
        signup_url="https://api.together.ai/settings/api-keys",
        base_url="https://api.together.xyz/v1",
        recommended_model="google/gemma-4-31b-it",
        credential_names=("TOGETHER_API_KEY",),
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
    "cerebras": LLMProviderPreset(
        name="cerebras",
        label="Cerebras",
        signup_url="https://cloud.cerebras.ai/platform",
        base_url="https://api.cerebras.ai/v1",
        recommended_model="gemma-4-31b",
        credential_names=("CEREBRAS_API_KEY",),
        stability="preview",
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
    "anthropic": LLMProviderPreset(
        name="anthropic",
        label="Anthropic",
        signup_url="https://console.anthropic.com/settings/keys",
        base_url="https://api.anthropic.com/v1",
        recommended_model="claude-sonnet-4-6",
        credential_names=("ANTHROPIC_API_KEY",),
    ),
    "custom": LLMProviderPreset(
        name="custom",
        label="Custom",
        signup_url="",
        base_url="",
        recommended_model="",
        credential_names=("LLM_API_KEY",),
    ),
    "ollama": LLMProviderPreset(
        name="ollama",
        label="Ollama",
        signup_url="https://ollama.com/download",
        base_url="http://127.0.0.1:11434/v1",
        recommended_model="",
        credential_names=(),
        requires_api_key=False,
        request_policies=(_GEMMA4_NO_REASONING_OLLAMA,),
    ),
    "lmstudio": LLMProviderPreset(
        name="lmstudio",
        label="LM Studio",
        signup_url="https://lmstudio.ai/",
        base_url="http://127.0.0.1:1234/v1",
        recommended_model="",
        credential_names=(),
        requires_api_key=False,
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
    "llamacpp": LLMProviderPreset(
        name="llamacpp",
        label="llama.cpp",
        signup_url="https://github.com/ggml-org/llama.cpp",
        base_url="http://127.0.0.1:8080/v1",
        recommended_model="",
        credential_names=(),
        requires_api_key=False,
        request_policies=(_GEMMA4_NO_REASONING,),
    ),
}


LOCAL_LLM_PROVIDERS = frozenset({"ollama", "lmstudio", "llamacpp"})


# Fallback only — persisted ResolvedLlmConfig.provider is authoritative.
# Ports match LOCAL_MODEL_LANE defaults + managed sidecar (:11435).
_LOCAL_LOOPBACK_PORTS = {
    ":11434": "ollama",
    ":11435": "ollama",
    ":1234": "lmstudio",
    ":8080": "llamacpp",
}


def infer_provider_from_base_url(base_url: str | None) -> str:
    """Best-effort URL → preset when provider_name was not set explicitly."""
    url = (base_url or "").lower()
    if "anthropic.com" in url:
        return "anthropic"
    if "openrouter.ai" in url:
        return "openrouter"
    if "deepinfra.com" in url:
        return "deepinfra"
    if "groq.com" in url:
        return "groq"
    if "together.xyz" in url:
        return "together"
    if "cerebras.ai" in url:
        return "cerebras"
    if "generativelanguage.googleapis.com" in url:
        return "google-ai-studio"
    if "ollama" in url:
        return "ollama"
    for port, provider in _LOCAL_LOOPBACK_PORTS.items():
        if port in url:
            return provider
    return "custom"


def normalize_llm_provider(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def get_llm_provider(name: str) -> LLMProviderPreset:
    normalized = normalize_llm_provider(name)
    try:
        return LLM_PROVIDER_PRESETS[normalized]
    except KeyError as exc:
        valid = ", ".join(sorted(LLM_PROVIDER_PRESETS))
        raise ValueError(f"Unknown LLM provider '{name}'. Valid providers: {valid}") from exc
