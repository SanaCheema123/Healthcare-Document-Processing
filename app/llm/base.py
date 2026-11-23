"""Provider-agnostic interface for schema-constrained LLM calls.

Every call forces the model to answer through a single tool whose input schema
is the Pydantic JSON schema of the target object. This gives reliably parseable
JSON without brittle "please answer in JSON" prompting.
"""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar


class LLMError(RuntimeError):
    pass


class LLMRetryableError(LLMError):
    """Throttling / 5xx / timeouts: safe to retry with backoff."""


@dataclass
class LLMRequest:
    system: str
    prompt: str
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any]
    # Raw material for non-LLM providers (rules engine) and for tracing.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def _invoke(self, request: LLMRequest) -> LLMResponse: ...

    def invoke(self, request: LLMRequest, max_attempts: int = 4) -> LLMResponse:
        return with_backoff(lambda: self._invoke(request), max_attempts=max_attempts)


T = TypeVar("T")


def with_backoff(fn: Callable[[], T], max_attempts: int = 4, base_s: float = 0.5, cap_s: float = 8.0) -> T:
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except LLMRetryableError:
            if attempt == max_attempts:
                raise
            time.sleep(min(cap_s, base_s * 2 ** (attempt - 1)) * (0.5 + random.random() / 2))
    raise AssertionError("unreachable")


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve `$ref`/`$defs` so providers that dislike references get a flat schema."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(defs[name])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(n) for n in node]
        return node

    return resolve(schema)
