"""Document-level validation: identifier checks, cross-field consistency, grounding."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.core.schemas import (
    CMS1500Claim,
    DocumentType,
    LabReport,
    OCRResult,
    Patient,
    Prescription,
    Provider,
    ValidationIssue,
    ValidationSeverity,
)
from app.validation import validators as v
from app.validation.grounding import CRITICAL_SUFFIXES, Grounder

E, W = ValidationSeverity.ERROR, ValidationSeverity.WARNING


class ValidationEngine:
    def __init__(self, today: date | None = None):
        self.today = today or date.today()

    def validate(self, doc_type: DocumentType, model: BaseModel, ocr: OCRResult | None = None,
                 skip_grounding: set[str] | None = None) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if isinstance(model, CMS1500Claim):
            issues += self._claim(model)
        elif isinstance(model, LabReport):
            issues += self._lab(model)
        elif isinstance(model, Prescription):
            issues += self._rx(model)
        if ocr is not None:
            issues += self._grounding(model, ocr, skip_grounding or set())
        return issues

    # ---------------------------------------------------------------- shared
    def _patient(self, p: Patient, ref_date: date | None, prefix: str = "patient") -> list[ValidationIssue]:
        out = []
        if not (p.first_name or p.last_name):
            out.append(ValidationIssue(field=f"{prefix}.last_name", rule="required", message="patient name missing"))
        if p.date_of_birth:
            if p.date_of_birth > self.today:
                out.append(ValidationIssue(field=f"{prefix}.date_of_birth", rule="dob_future", message="date of birth is in the future"))
            elif (self.today.year - p.date_of_birth.year) > 120:
                out.append(ValidationIssue(field=f"{prefix}.date_of_birth", rule="dob_range", message="patient age > 120 years"))
            if ref_date and p.date_of_birth > ref_date:
                out.append(ValidationIssue(field=f"{prefix}.date_of_birth", rule="dob_after_service", message="date of birth is after the service/document date"))
        if p.sex and p.sex.upper() not in {"M", "F", "U", "X"}:
            out.append(ValidationIssue(field=f"{prefix}.sex", rule="enum", message=f"unexpected sex value {p.sex!r}", severity=W))
        return out

    def _provider(self, p: Provider | None, prefix: str, npi_required: bool) -> list[ValidationIssue]:
        out = []
        if p is None:
            if npi_required:
                out.append(ValidationIssue(field=prefix, rule="required", message="provider missing"))
            return out
        if p.npi:
            if not v.is_valid_npi(p.npi):
                out.append(ValidationIssue(field=f"{prefix}.npi", rule="npi_checksum", message=f"NPI {p.npi} fails Luhn/format check"))
        elif npi_required:
            out.append(ValidationIssue(field=f"{prefix}.npi", rule="required", message="NPI missing"))
        if p.tax_id and not v.TAX_ID.match(p.tax_id):
            out.append(ValidationIssue(field=f"{prefix}.tax_id", rule="tax_id_format", message="EIN must be NN-NNNNNNN"))
        return out

    # ----------------------------------------------------------------- claim
    def _claim(self, c: CMS1500Claim) -> list[ValidationIssue]:
        first_dos = min((s.date_of_service for s in c.service_lines if s.date_of_service), default=None)
        out = self._patient(c.patient, first_dos)
        out += self._provider(c.billing_provider, "billing_provider", npi_required=True)
        out += self._provider(c.rendering_provider, "rendering_provider", npi_required=False)
        if not c.patient.member_id:
            out.append(ValidationIssue(field="patient.member_id", rule="required", message="insured/member ID missing"))

        if not c.diagnosis_codes:
            out.append(ValidationIssue(field="diagnosis_codes", rule="required", message="at least one ICD-10 code required"))
        if len(c.diagnosis_codes) > 12:
            out.append(ValidationIssue(field="diagnosis_codes", rule="max_items", message="CMS-1500 allows max 12 diagnoses"))
        for i, code in enumerate(c.diagnosis_codes):
            if not v.is_valid_icd10(code):
                out.append(ValidationIssue(field=f"diagnosis_codes[{i}]", rule="icd10_format", message=f"{code!r} is not a valid ICD-10-CM code"))

        if not c.service_lines:
            out.append(ValidationIssue(field="service_lines", rule="required", message="no service lines"))
        valid_pointers = "ABCDEFGHIJKL"[: len(c.diagnosis_codes)]
        line_sum = Decimal("0")
        for i, s in enumerate(c.service_lines):
            f = f"service_lines[{i}]"
            if not v.is_valid_procedure_code(s.cpt_hcpcs):
                out.append(ValidationIssue(field=f"{f}.cpt_hcpcs", rule="cpt_format", message=f"{s.cpt_hcpcs!r} is not a valid CPT/HCPCS code"))
            for m in s.modifiers:
                if not v.MODIFIER.match(m):
                    out.append(ValidationIssue(field=f"{f}.modifiers", rule="modifier_format", message=f"bad modifier {m!r}"))
            if s.place_of_service and not v.POS.match(s.place_of_service):
                out.append(ValidationIssue(field=f"{f}.place_of_service", rule="pos_format", message="POS must be 2 digits"))
            if s.diagnosis_pointer:
                bad = [p for p in s.diagnosis_pointer if p not in valid_pointers]
                if bad:
                    out.append(ValidationIssue(field=f"{f}.diagnosis_pointer", rule="dx_pointer", message=f"pointer(s) {bad} reference no diagnosis"))
            if s.date_of_service and s.date_of_service > self.today:
                out.append(ValidationIssue(field=f"{f}.date_of_service", rule="dos_future", message="date of service in the future"))
            if s.charge is not None:
                if s.charge < 0:
                    out.append(ValidationIssue(field=f"{f}.charge", rule="non_negative", message="negative charge"))
                line_sum += s.charge
        if c.total_charge is not None and c.service_lines and abs(line_sum - c.total_charge) > Decimal("0.01"):
            out.append(ValidationIssue(field="total_charge", rule="total_mismatch", message=f"total {c.total_charge} != sum of lines {line_sum}"))
        return out

    # ------------------------------------------------------------------- lab
    def _lab(self, r: LabReport) -> list[ValidationIssue]:
        ref = r.collected_at.date() if r.collected_at else None
        out = self._patient(r.patient, ref)
        out += self._provider(r.ordering_provider, "ordering_provider", npi_required=False)
        if r.collected_at and r.reported_at and r.reported_at < r.collected_at:
            out.append(ValidationIssue(field="reported_at", rule="chronology", message="reported before collected"))
        if not r.results:
            out.append(ValidationIssue(field="results", rule="required", message="no results extracted"))
        for i, res in enumerate(r.results):
            f = f"results[{i}]"
            if res.loinc_code and not v.LOINC.match(res.loinc_code):
                out.append(ValidationIssue(field=f"{f}.loinc_code", rule="loinc_format", message="invalid LOINC format", severity=W))
            lo, hi = v.parse_reference_range(res.reference_range)
            try:
                val = float(re.sub(r"[<>]", "", res.value))
            except ValueError:
                continue
            out_of_range = (lo is not None and val < lo) or (hi is not None and val > hi)
            if out_of_range and not res.flag:
                out.append(ValidationIssue(field=f"{f}.flag", rule="flag_consistency", message=f"{res.test_name} {val} outside {res.reference_range} but not flagged", severity=W))
            if res.flag in ("HH", "LL"):
                out.append(ValidationIssue(field=f"{f}.flag", rule="critical_value", message=f"critical value for {res.test_name}: {res.value}", severity=W))
        return out

    # ------------------------------------------------------------------- rx
    def _rx(self, rx: Prescription) -> list[ValidationIssue]:
        out = self._patient(rx.patient, rx.date_written)
        out += self._provider(rx.prescriber, "prescriber", npi_required=True)
        if rx.date_written and rx.date_written > self.today:
            out.append(ValidationIssue(field="date_written", rule="future_date", message="prescription dated in the future"))
        if not rx.medications:
            out.append(ValidationIssue(field="medications", rule="required", message="no medications extracted"))
        controlled = False
        for i, m in enumerate(rx.medications):
            f = f"medications[{i}]"
            if any(cs in m.name.lower() for cs in v.CONTROLLED_SUBSTANCES):
                controlled = True
            if not m.sig:
                out.append(ValidationIssue(field=f"{f}.sig", rule="required", message="directions (sig) missing", severity=W))
            if m.ndc and not v.NDC.match(m.ndc):
                out.append(ValidationIssue(field=f"{f}.ndc", rule="ndc_format", message=f"invalid NDC {m.ndc!r}"))
            if m.refills is not None and not 0 <= m.refills <= 11:
                out.append(ValidationIssue(field=f"{f}.refills", rule="range", message="refills outside 0-11"))
        if rx.dea_number:
            ok, msg = v.is_valid_dea(rx.dea_number, _last_name(rx.prescriber.name))
            if not ok:
                out.append(ValidationIssue(field="dea_number", rule="dea_checksum", message=f"DEA invalid: {msg}"))
            elif msg:
                out.append(ValidationIssue(field="dea_number", rule="dea_name", message=msg, severity=W))
        elif controlled:
            out.append(ValidationIssue(field="dea_number", rule="controlled_requires_dea", message="controlled substance prescribed without DEA number"))
        return out

    # ------------------------------------------------------------- grounding
    def _grounding(self, model: BaseModel, ocr: OCRResult, skip: set[str]) -> list[ValidationIssue]:
        out = []
        for fc in Grounder(ocr).score(model):
            if fc.grounded or fc.field in skip:  # repaired fields are reported by the repair step
                continue
            leaf = re.sub(r"\[\d+\]", "", fc.field.split(".")[-1])
            critical = leaf.endswith(CRITICAL_SUFFIXES) or fc.field.startswith("diagnosis_codes")
            out.append(
                ValidationIssue(
                    field=fc.field,
                    rule="grounding",
                    message=f"value {fc.value!r} not found in source document",
                    severity=E if critical else W,
                )
            )
        return out


def _last_name(full: str | None) -> str | None:
    if not full:
        return None
    cleaned = re.sub(r",?\s*(MD|DO|NP|PA|PA-C|DDS|DMD)\.?$", "", full.replace("Dr.", "").strip(), flags=re.I)
    return cleaned.split()[-1] if cleaned.split() else None
