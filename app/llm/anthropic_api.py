"""Direct Anthropic Messages API provider (forced tool use).

Uses plain httpx to avoid an SDK dependency. For PHI, only use this with an
executed BAA / zero-data-retention agreement; otherwise prefer Bedrock.
"""
from __future__ import annotations

import time

import httpx

from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse, LLMRetryableError

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 4096, temperature: float = 0.0, timeout_s: float = 60):
        if not api_key:
            raise LLMError("MEDX_ANTHROPIC_API_KEY is not set")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.http = httpx.Client(
            timeout=timeout_s,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )

    def _invoke(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "tools": [
                {"name": request.tool_name, "description": request.tool_description, "input_schema": request.input_schema}
            ],
            "tool_choice": {"type": "tool", "name": request.tool_name},
        }
        try:
            resp = self.http.post(API_URL, json=body)
        except httpx.TimeoutException as exc:
            raise LLMRetryableError("timeout") from exc
        if resp.status_code in (429, 500, 502, 503, 529):
            raise LLMRetryableError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"Anthropic API HTTP {resp.status_code}: {resp.text[:500]}")

        payload = resp.json()
        for block in payload.get("content", []):
            if block.get("type") == "tool_use":
                usage = payload.get("usage", {})
                return LLMResponse(
                    data=block["input"],
                    model=payload.get("model", self.model),
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
        raise LLMError(f"model did not call tool (stop_reason={payload.get('stop_reason')})")
