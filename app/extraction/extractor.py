"""Schema-driven extraction with a validate -> feedback -> retry loop."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.schemas import SCHEMA_REGISTRY, DocumentType, OCRResult, ValidationIssue, ValidationSeverity
from app.extraction import prompts
from app.llm.base import LLMProvider, LLMRequest
from app.validation.engine import ValidationEngine
from app.validation.repair import repair

log = logging.getLogger(__name__)


@dataclass
class ExtractionOutcome:
    model: BaseModel | None
    raw: dict[str, Any]
    issues: list[ValidationIssue]
    attempts: int
    input_tokens: int = 0
    output_tokens: int = 0
    history: list[list[str]] = field(default_factory=list)


def _format_pydantic_errors(err: ValidationError) -> list[str]:
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors()]


class Extractor:
    def __init__(self, llm: LLMProvider, validator: ValidationEngine, max_retries: int = 2):
        self.llm = llm
        self.validator = validator
        self.max_retries = max_retries

    def extract(self, doc_type: DocumentType, ocr: OCRResult) -> ExtractionOutcome:
        schema_cls = SCHEMA_REGISTRY[doc_type]
        base_prompt = prompts.extract_prompt(doc_type, ocr)
        prompt = base_prompt
        outcome = ExtractionOutcome(model=None, raw={}, issues=[], attempts=0)

        for attempt in range(1, self.max_retries + 2):
            outcome.attempts = attempt
            resp = self.llm.invoke(
                LLMRequest(
                    system=prompts.SYSTEM_EXTRACT,
                    prompt=prompt,
                    tool_name=f"extract_{doc_type.value}",
                    tool_description=f"Record the structured fields of the {doc_type.value.replace('_', ' ')}.",
                    input_schema=schema_cls.model_json_schema(),
                    context={"document_text": ocr.text, "attempt": attempt},
                )
            )
            outcome.raw = resp.data
            outcome.input_tokens += resp.input_tokens
            outcome.output_tokens += resp.output_tokens

            # 1) structural / type validation
            try:
                model = schema_cls.model_validate(resp.data)
            except ValidationError as err:
                errors = _format_pydantic_errors(err)
                outcome.issues = [
                    ValidationIssue(field=e.split(":")[0], rule="schema", message=e) for e in errors
                ]
                outcome.history.append(errors)
                log.info("schema validation failed (attempt %d): %d errors", attempt, len(errors))
                prompt = prompts.correction_prompt(base_prompt, resp.data, errors)
                continue

            # 2) deterministic OCR-confusion repair (flagged, never silent)
            repairs = repair(model)

            # 3) domain validation (NPI checksum, ICD-10 format, totals, grounding ...)
            outcome.model = model
            outcome.issues = repairs + self.validator.validate(
                doc_type, model, ocr, skip_grounding={r.field for r in repairs}
            )
            hard = [i for i in outcome.issues if i.severity == ValidationSeverity.ERROR and i.rule != "grounding"]
            if not hard:
                break
            errors = [f"{i.field}: {i.message}" for i in hard]
            outcome.history.append(errors)
            if attempt <= self.max_retries:
                log.info("domain validation failed (attempt %d): %d errors, retrying", attempt, len(errors))
                prompt = prompts.correction_prompt(base_prompt, resp.data, errors)

        return outcome
