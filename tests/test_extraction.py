"""Extractor behaviour with scripted fake LLMs (no network)."""
from __future__ import annotations

import copy

from app.core.schemas import DocumentType, OCRLine, OCRResult
from app.extraction.extractor import Extractor
from app.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMRetryableError
from app.llm.rules import extract_cms1500
from app.validation.engine import ValidationEngine
from tests.conftest import SAMPLES

TEXT = (SAMPLES / "claim_cms1500.txt").read_text()
OCR = OCRResult(provider="test", page_count=1, lines=[OCRLine(text=l, confidence=0.99) for l in TEXT.splitlines() if l.strip()])
GOOD = extract_cms1500(TEXT)


class ScriptedLLM(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def _invoke(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request.prompt)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LLMResponse(data=nxt, model="fake", input_tokens=100, output_tokens=50)


def test_self_correction_after_schema_error():
    bad = copy.deepcopy(GOOD)
    bad["patient"]["date_of_birth"] = "not a date"
    llm = ScriptedLLM([bad, GOOD])
    out = Extractor(llm, ValidationEngine()).extract(DocumentType.CMS1500_CLAIM, OCR)
    assert out.attempts == 2
    assert out.model is not None
    assert "patient.date_of_birth" in llm.prompts[1]  # error fed back to the model
    assert out.input_tokens == 200


def test_self_correction_after_domain_error():
    bad = copy.deepcopy(GOOD)
    bad["billing_provider"]["npi"] = "1306849451"  # bad checksum
    llm = ScriptedLLM([bad, GOOD])
    out = Extractor(llm, ValidationEngine()).extract(DocumentType.CMS1500_CLAIM, OCR)
    assert out.attempts == 2
    assert "Luhn" in llm.prompts[1]
    assert out.model.billing_provider.npi == "1306849450"


def test_gives_up_after_max_retries():
    bad = copy.deepcopy(GOOD)
    bad["service_lines"] = "oops"
    llm = ScriptedLLM([bad, bad, bad])
    out = Extractor(llm, ValidationEngine(), max_retries=2).extract(DocumentType.CMS1500_CLAIM, OCR)
    assert out.model is None and out.attempts == 3


def test_hallucinated_identifier_is_flagged():
    hallucinated = copy.deepcopy(GOOD)
    hallucinated["rendering_provider"]["npi"] = "1407855935"  # valid checksum but NOT in the document
    out = Extractor(ScriptedLLM([hallucinated]), ValidationEngine(), max_retries=0).extract(DocumentType.CMS1500_CLAIM, OCR)
    grounding = [i for i in out.issues if i.rule == "grounding"]
    assert grounding and grounding[0].field == "rendering_provider.npi"
    assert grounding[0].severity.value == "error"


def test_retryable_errors_back_off(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    llm = ScriptedLLM([LLMRetryableError("throttled"), LLMRetryableError("throttled"), GOOD])
    out = Extractor(llm, ValidationEngine()).extract(DocumentType.CMS1500_CLAIM, OCR)
    assert out.model is not None and out.attempts == 1
