"""Amazon Bedrock provider (Converse API, forced tool use).

Bedrock keeps PHI inside your AWS account boundary and is HIPAA-eligible,
which is why it is the recommended production provider.
"""
from __future__ import annotations

import time
from typing import Any

from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse, LLMRetryableError, inline_refs

_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException", "InternalServerException"}


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(self, model_id: str, region: str, max_tokens: int = 4096, temperature: float = 0.0, client: Any | None = None):
        import boto3
        from botocore.config import Config

        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = client or boto3.client(
            "bedrock-runtime", region_name=region, config=Config(read_timeout=120, retries={"max_attempts": 0})
        )

    def _invoke(self, request: LLMRequest) -> LLMResponse:
        from botocore.exceptions import ClientError

        started = time.perf_counter()
        try:
            resp = self.client.converse(
                modelId=self.model_id,
                system=[{"text": request.system}],
                messages=[{"role": "user", "content": [{"text": request.prompt}]}],
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": request.tool_name,
                                "description": request.tool_description,
                                "inputSchema": {"json": inline_refs(request.input_schema)},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": request.tool_name}},
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE:
                raise LLMRetryableError(code) from exc
            raise LLMError(f"Bedrock error {code}: {exc}") from exc

        for block in resp["output"]["message"]["content"]:
            if "toolUse" in block:
                usage = resp.get("usage", {})
                return LLMResponse(
                    data=block["toolUse"]["input"],
                    model=self.model_id,
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
        raise LLMError(f"model did not call tool (stopReason={resp.get('stopReason')})")
