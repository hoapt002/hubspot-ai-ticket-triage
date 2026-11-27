"""LLM providers returning schema-validated structured output."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from ticket_triage.settings import Settings


class LLMError(Exception):
    """The model call failed or returned unusable output."""


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, Any]
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class TriageModel(Protocol):
    provider: str
    model: str

    def classify(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult: ...


class AnthropicTriageModel:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model

    def classify(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        started = time.monotonic()
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except self._anthropic.APIError as exc:
            raise LLMError(f"anthropic: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("anthropic: model refused the request")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise LLMError(f"anthropic: no parsed output (stop_reason={response.stop_reason})")
        return LLMResult(
            output=response.parsed_output.model_dump(mode="json"),
            provider=self.provider,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class OpenAITriageModel:
    provider = "openai"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import openai

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model

    def classify(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        started = time.monotonic()
        try:
            response = self._client.responses.parse(
                model=self.model,
                instructions=system,
                input=[{"role": "user", "content": user}],
                text_format=schema,
            )
        except self._openai.OpenAIError as exc:
            raise LLMError(f"openai: {exc}") from exc
        parsed = response.output_parsed
        if parsed is None:
            raise LLMError(f"openai: no parsed output (status={response.status})")
        usage = response.usage
        return LLMResult(
            output=parsed.model_dump(mode="json"),
            provider=self.provider,
            model=response.model,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def build_triage_model(settings: Settings) -> TriageModel:
    model = settings.resolved_llm_model
    if settings.llm_provider == "anthropic":
        key = settings.anthropic_api_key.get_secret_value()
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        return AnthropicTriageModel(key, model, settings.llm_timeout_seconds)
    key = settings.openai_api_key.get_secret_value()
    if not key:
        raise LLMError("OPENAI_API_KEY is not set")
    return OpenAITriageModel(key, model, settings.llm_timeout_seconds)
