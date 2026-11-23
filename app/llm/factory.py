from __future__ import annotations

from app.core.config import Settings
from app.llm.base import LLMProvider


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "bedrock":
        from app.llm.bedrock import BedrockProvider

        return BedrockProvider(
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )
    if settings.llm_provider == "anthropic":
        from app.llm.anthropic_api import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key or "",
            model=settings.anthropic_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            timeout_s=settings.llm_timeout_s,
        )
    from app.llm.rules import RulesProvider

    return RulesProvider()
