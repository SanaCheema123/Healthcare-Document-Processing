from __future__ import annotations

from app.core.schemas import ClassificationResult, OCRResult
from app.extraction import prompts
from app.llm.base import LLMProvider, LLMRequest

_SCHEMA = ClassificationResult.model_json_schema()


class DocumentClassifier:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    def classify(self, ocr: OCRResult) -> ClassificationResult:
        resp = self.llm.invoke(
            LLMRequest(
                system=prompts.SYSTEM_CLASSIFY,
                prompt=prompts.classify_prompt(ocr),
                tool_name="classify_document",
                tool_description="Record the document type and your confidence (0-1).",
                input_schema=_SCHEMA,
                context={"document_text": ocr.text},
            )
        )
        return ClassificationResult.model_validate(resp.data)
