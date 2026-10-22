"""OpenAI-compatible servers (Chat Completions): Ollama, vLLM, LM Studio, OpenRouter, Together, any other.

Registry names and env vars:

=================  =========================================  =================================
name               base URL env (default)                     API key env
=================  =========================================  =================================
openai_compatible  ``OPENAI_COMPATIBLE_BASE_URL`` (required)  ``OPENAI_COMPATIBLE_API_KEY`` (opt.)
ollama             ``OLLAMA_BASE_URL`` (localhost:11434/v1)   none
vllm               ``VLLM_BASE_URL`` (localhost:8000/v1)      ``VLLM_API_KEY`` (opt.)
lmstudio           ``LMSTUDIO_BASE_URL`` (localhost:1234/v1)  none
openrouter         ``OPENROUTER_BASE_URL`` (openrouter.ai)    ``OPENROUTER_API_KEY`` (required)
together           ``TOGETHER_BASE_URL`` (api.together.xyz)   ``TOGETHER_API_KEY`` (required)
=================  =========================================  =================================

Model refs keep everything after the first ``/``: ``openrouter/anthropic/claude-sonnet-4``
sends model ``anthropic/claude-sonnet-4`` to OpenRouter.
"""
from __future__ import annotations

from squadhq.http import Transport
from squadhq.providers.openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"
    default_base_url = ""
    key_envs = ("OPENAI_COMPATIBLE_API_KEY",)
    base_url_envs = ("OPENAI_COMPATIBLE_BASE_URL",)
    key_required = False
    default_style = "chat"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 extra_headers: dict[str, str] | None = None, transport: Transport | None = None) -> None:
        super().__init__(api_key=api_key, base_url=base_url, api_style="chat",
                         extra_headers=extra_headers, transport=transport)
        self._require(self.base_url, " / ".join(self.base_url_envs))


class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    default_base_url = "http://localhost:11434/v1"
    key_envs = ("OLLAMA_API_KEY",)
    base_url_envs = ("OLLAMA_BASE_URL",)


class VLLMProvider(OpenAICompatibleProvider):
    name = "vllm"
    default_base_url = "http://localhost:8000/v1"
    key_envs = ("VLLM_API_KEY",)
    base_url_envs = ("VLLM_BASE_URL",)


class LMStudioProvider(OpenAICompatibleProvider):
    name = "lmstudio"
    default_base_url = "http://localhost:1234/v1"
    key_envs = ("LMSTUDIO_API_KEY",)
    base_url_envs = ("LMSTUDIO_BASE_URL",)


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    key_envs = ("OPENROUTER_API_KEY",)
    base_url_envs = ("OPENROUTER_BASE_URL",)
    key_required = True


class TogetherProvider(OpenAICompatibleProvider):
    name = "together"
    default_base_url = "https://api.together.xyz/v1"
    key_envs = ("TOGETHER_API_KEY",)
    base_url_envs = ("TOGETHER_BASE_URL",)
    key_required = True
