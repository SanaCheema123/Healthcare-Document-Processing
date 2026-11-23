"""End-to-end document pipeline.

    upload -> OCR -> classify -> extract (LLM, self-correcting) -> validate
           -> ground & score -> route: APPROVED | NEEDS_REVIEW | FAILED
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

from app.core.config import Settings
from app.core.schemas import (
    SCHEMA_REGISTRY,
    DocumentType,
    ProcessingResult,
    ProcessingStatus,
    ValidationSeverity,
)
from app.extraction.classifier import DocumentClassifier
from app.extraction.extractor import Extractor
from app.llm.base import LLMProvider
from app.ocr.base import detect_mime
from app.ocr.factory import OCRRouter
from app.storage.repository import ResultRepository
from app.validation.engine import ValidationEngine
from app.validation.grounding import CRITICAL_SUFFIXES, Grounder

log = logging.getLogger(__name__)

MIN_CLASSIFICATION_CONFIDENCE = 0.70


@dataclass
class Metrics:
    processed: int = 0
    approved: int = 0
    needs_review: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_ms: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, result: ProcessingResult, in_tok: int, out_tok: int) -> None:
        with self._lock:
            self.processed += 1
            self.input_tokens += in_tok
            self.output_tokens += out_tok
            self.total_ms += sum(result.timings_ms.values())
            if result.status == ProcessingStatus.APPROVED:
                self.approved += 1
            elif result.status == ProcessingStatus.NEEDS_REVIEW:
                self.needs_review += 1
            elif result.status == ProcessingStatus.FAILED:
                self.failed += 1

    def snapshot(self) -> dict:
        n = max(self.processed, 1)
        return {
            "processed": self.processed,
            "approved": self.approved,
            "needs_review": self.needs_review,
            "failed": self.failed,
            "straight_through_rate": round(self.approved / n, 3),
            "avg_latency_ms": round(self.total_ms / n, 1),
            "llm_input_tokens": self.input_tokens,
            "llm_output_tokens": self.output_tokens,
        }


class DocumentPipeline:
    def __init__(self, settings: Settings, ocr: OCRRouter, llm: LLMProvider, repo: ResultRepository,
                 validator: ValidationEngine | None = None):
        self.settings = settings
        self.ocr = ocr
        self.llm = llm
        self.repo = repo
        self.validator = validator or ValidationEngine()
        self.classifier = DocumentClassifier(llm)
        self.extractor = Extractor(llm, self.validator, max_retries=settings.llm_max_retries)
        self.metrics = Metrics()

    @contextmanager
    def _stage(self, result: ProcessingResult, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            result.timings_ms[name] = ms
            log.info("stage complete", extra={"document_id": result.document_id, "stage": name, "duration_ms": ms})

    def process(self, document_id: str, filename: str, content: bytes,
                doc_type_hint: DocumentType | None = None) -> ProcessingResult:
        result = self.repo.get(document_id) or ProcessingResult(
            document_id=document_id, filename=filename, status=ProcessingStatus.RECEIVED
        )
        result.llm_provider = self.llm.name
        in_tok = out_tok = 0
        try:
            # 1. OCR -----------------------------------------------------
            with self._stage(result, "ocr"):
                ocr = self.ocr.extract(content, detect_mime(content, filename))
            result.ocr_provider = ocr.provider
            result.ocr_confidence = round(ocr.mean_confidence, 4)
            result.status = ProcessingStatus.OCR_COMPLETE
            if not ocr.lines:
                raise ValueError("OCR returned no text")
            if ocr.mean_confidence < self.settings.ocr_min_confidence:
                result.review_reasons.append(f"low OCR confidence ({ocr.mean_confidence:.2f})")

            # 2. Classify ------------------------------------------------
            with self._stage(result, "classify"):
                if doc_type_hint and doc_type_hint != DocumentType.UNKNOWN:
                    doc_type, cls_conf = doc_type_hint, 1.0
                else:
                    cls = self.classifier.classify(ocr)
                    doc_type, cls_conf = cls.document_type, cls.confidence
            result.document_type, result.classification_confidence = doc_type, cls_conf
            result.status = ProcessingStatus.CLASSIFIED
            if doc_type not in SCHEMA_REGISTRY:
                result.status = ProcessingStatus.NEEDS_REVIEW
                result.review_reasons.append("unsupported or unrecognised document type")
                return self._finish(result, in_tok, out_tok)
            if cls_conf < MIN_CLASSIFICATION_CONFIDENCE:
                result.review_reasons.append(f"low classification confidence ({cls_conf:.2f})")

            # 3. Extract + validate (self-correcting loop) ----------------
            with self._stage(result, "extract"):
                outcome = self.extractor.extract(doc_type, ocr)
            in_tok, out_tok = outcome.input_tokens, outcome.output_tokens
            result.extraction_attempts = outcome.attempts
            result.issues = outcome.issues
            if outcome.model is None:
                result.status = ProcessingStatus.NEEDS_REVIEW
                result.data = outcome.raw
                result.review_reasons.append("extraction did not match schema after retries")
                return self._finish(result, in_tok, out_tok)
            result.data = outcome.model.model_dump(mode="json")
            result.status = ProcessingStatus.EXTRACTED

            # 4. Confidence scoring & routing ----------------------------
            with self._stage(result, "score"):
                result.field_confidence = Grounder(ocr).score(outcome.model)
                result.overall_confidence = self._overall(result)
                self._route(result)
        except Exception as exc:  # noqa: BLE001 - pipeline must never crash the worker
            log.exception("pipeline failed", extra={"document_id": document_id})
            result.status = ProcessingStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
        return self._finish(result, in_tok, out_tok)

    # ------------------------------------------------------------ helpers
    def _overall(self, result: ProcessingResult) -> float:
        fcs = result.field_confidence
        if not fcs:
            return 0.0
        weights = [2.0 if f.field.split(".")[-1].split("[")[0].endswith(CRITICAL_SUFFIXES) else 1.0 for f in fcs]
        field_score = sum(f.confidence * w for f, w in zip(fcs, weights)) / sum(weights)
        n_err = sum(1 for i in result.issues if i.severity == ValidationSeverity.ERROR)
        penalty = min(0.5, 0.1 * n_err)
        score = 0.15 * (result.classification_confidence or 0) + 0.15 * (result.ocr_confidence or 0) + 0.7 * field_score
        return round(max(0.0, score - penalty), 4)

    def _route(self, result: ProcessingResult) -> None:
        errors = [i for i in result.issues if i.severity == ValidationSeverity.ERROR]
        if errors:
            result.review_reasons.append(f"{len(errors)} validation error(s): " + ", ".join(sorted({e.rule for e in errors})))
        low = [f.field for f in result.field_confidence if f.confidence < self.settings.field_review_threshold]
        if low:
            result.review_reasons.append(f"{len(low)} low-confidence field(s): {', '.join(low[:5])}{'...' if len(low) > 5 else ''}")
        repairs = [i for i in result.issues if i.rule == "ocr_repair"]
        if repairs:
            result.review_reasons.append(f"{len(repairs)} OCR auto-correction(s) need confirmation")
        crit = [i for i in result.issues if i.rule == "critical_value"]
        if crit:
            result.review_reasons.append("critical lab value present")
        if (result.overall_confidence or 0) < self.settings.auto_approve_threshold:
            result.review_reasons.append(f"overall confidence {result.overall_confidence} below {self.settings.auto_approve_threshold}")
        result.status = ProcessingStatus.NEEDS_REVIEW if result.review_reasons else ProcessingStatus.APPROVED

    def _finish(self, result: ProcessingResult, in_tok: int, out_tok: int) -> ProcessingResult:
        self.repo.save(result)
        self.metrics.record(result, in_tok, out_tok)
        return result
