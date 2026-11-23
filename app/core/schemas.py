"""Domain schemas.

Each supported document type has a Pydantic model. The same model is used to:
  1. generate the JSON schema handed to the LLM (tool / structured output),
  2. parse and type-check the LLM response,
  3. drive downstream validation rules.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    CMS1500_CLAIM = "cms1500_claim"
    LAB_REPORT = "lab_report"
    PRESCRIPTION = "prescription"
    UNKNOWN = "unknown"


class ProcessingStatus(str, Enum):
    RECEIVED = "received"
    OCR_COMPLETE = "ocr_complete"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


# ---------------------------------------------------------------- OCR models
class BoundingBox(BaseModel):
    left: float
    top: float
    width: float
    height: float


class OCRLine(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    page: int = 1
    bbox: BoundingBox | None = None


class OCRKeyValue(BaseModel):
    key: str
    value: str
    confidence: float = Field(ge=0, le=1)
    page: int = 1


class OCRResult(BaseModel):
    provider: str
    page_count: int
    lines: list[OCRLine]
    key_values: list[OCRKeyValue] = []
    tables: list[list[list[str]]] = []

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


# ---------------------------------------------------------- Shared sub-models
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Patient(_Strict):
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    sex: str | None = Field(None, description="M, F, or U")
    member_id: str | None = Field(None, description="Insurance member / subscriber ID")
    mrn: str | None = Field(None, description="Medical record number, if present")
    address: str | None = None


class Provider(_Strict):
    name: str | None = None
    npi: str | None = Field(None, description="10-digit National Provider Identifier")
    tax_id: str | None = Field(None, description="EIN, format NN-NNNNNNN")
    address: str | None = None
    phone: str | None = None


# --------------------------------------------------------------- CMS-1500
class ServiceLine(_Strict):
    date_of_service: date | None = None
    place_of_service: str | None = Field(None, description="2-digit POS code")
    cpt_hcpcs: str | None = Field(None, description="CPT (5 digits) or HCPCS (letter + 4 digits)")
    modifiers: list[str] = []
    diagnosis_pointer: str | None = Field(None, description="Letters A-L referencing diagnosis_codes")
    charge: Decimal | None = None
    units: int | None = None


class CMS1500Claim(_Strict):
    patient: Patient
    insurance_plan: str | None = None
    billing_provider: Provider
    rendering_provider: Provider | None = None
    diagnosis_codes: list[str] = Field(default_factory=list, description="ICD-10-CM codes in order A, B, C...")
    service_lines: list[ServiceLine] = []
    total_charge: Decimal | None = None
    amount_paid: Decimal | None = None
    prior_authorization: str | None = None


# --------------------------------------------------------------- Lab report
class LabResult(_Strict):
    test_name: str
    loinc_code: str | None = None
    value: str
    unit: str | None = None
    reference_range: str | None = None
    flag: str | None = Field(None, description="H, L, HH, LL, A, or null if normal")


class LabReport(_Strict):
    patient: Patient
    ordering_provider: Provider | None = None
    lab_name: str | None = None
    accession_number: str | None = None
    collected_at: datetime | None = None
    reported_at: datetime | None = None
    results: list[LabResult] = []


# ------------------------------------------------------------- Prescription
class Medication(_Strict):
    name: str
    strength: str | None = None
    dosage_form: str | None = None
    sig: str | None = Field(None, description="Directions for use")
    quantity: str | None = None
    refills: int | None = None
    ndc: str | None = None


class Prescription(_Strict):
    patient: Patient
    prescriber: Provider
    dea_number: str | None = None
    date_written: date | None = None
    medications: list[Medication] = []
    substitution_allowed: bool | None = None


SCHEMA_REGISTRY: dict[DocumentType, type[BaseModel]] = {
    DocumentType.CMS1500_CLAIM: CMS1500Claim,
    DocumentType.LAB_REPORT: LabReport,
    DocumentType.PRESCRIPTION: Prescription,
}


# ------------------------------------------------------------ Result models
class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    field: str
    rule: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR


class FieldConfidence(BaseModel):
    field: str
    value: Any
    confidence: float
    grounded: bool = Field(description="Value was found verbatim/normalised in the OCR text")


class ClassificationResult(BaseModel):
    document_type: DocumentType
    confidence: float
    rationale: str | None = None


class ProcessingResult(BaseModel):
    document_id: str
    filename: str
    status: ProcessingStatus
    document_type: DocumentType | None = None
    classification_confidence: float | None = None
    ocr_provider: str | None = None
    ocr_confidence: float | None = None
    llm_provider: str | None = None
    data: dict[str, Any] | None = None
    field_confidence: list[FieldConfidence] = []
    overall_confidence: float | None = None
    issues: list[ValidationIssue] = []
    review_reasons: list[str] = []
    extraction_attempts: int = 0
    timings_ms: dict[str, float] = {}
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_by: str | None = None
