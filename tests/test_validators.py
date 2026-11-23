from datetime import date

import pytest

from app.core.schemas import CMS1500Claim, DocumentType, Patient, Provider, ServiceLine
from app.validation import validators as v
from app.validation.engine import ValidationEngine
from app.validation.repair import repair


@pytest.mark.parametrize("npi,ok", [
    ("1245319599", True), ("1306849450", True), ("1306849451", False),
    ("124531959", False), ("3245319599", False), ("abcdefghij", False), (None, False),
])
def test_npi(npi, ok):
    assert v.is_valid_npi(npi) is ok


@pytest.mark.parametrize("code,ok", [
    ("E11.9", True), ("I10", True), ("Z79.4", True), ("S72.001A", True), ("U07.1", True),
    ("110", False), ("E11.99999", False), ("E1", False), ("", False),
])
def test_icd10(code, ok):
    assert v.is_valid_icd10(code) is ok


@pytest.mark.parametrize("code,ok", [("99214", True), ("0001U", True), ("3074F", True), ("J1100", True), ("9921", False), ("ABCDE", False)])
def test_procedure_codes(code, ok):
    assert v.is_valid_procedure_code(code) is ok


def test_dea():
    assert v.is_valid_dea("AP4815291", "Patel") == (True, None)
    assert v.is_valid_dea("AP4815292", "Patel")[0] is False
    ok, warn = v.is_valid_dea("AP4815291", "Chen")
    assert ok and "initial" in warn


def test_reference_range():
    assert v.parse_reference_range("70-99") == (70, 99)
    assert v.parse_reference_range("<100") == (None, 100)
    assert v.parse_reference_range(">60") == (60, None)
    assert v.parse_reference_range("negative") == (None, None)


def _claim(**kw):
    base = dict(
        patient=Patient(first_name="Jane", last_name="Doe", date_of_birth=date(1985, 3, 14), member_id="X1"),
        billing_provider=Provider(name="Clinic", npi="1306849450", tax_id="12-3456789"),
        diagnosis_codes=["E11.9"],
        service_lines=[ServiceLine(date_of_service=date(2026, 1, 15), cpt_hcpcs="99214", diagnosis_pointer="A", charge="100.00")],
        total_charge="100.00",
    )
    base.update(kw)
    return CMS1500Claim(**base)


def test_clean_claim_has_no_issues():
    assert ValidationEngine(today=date(2026, 6, 1)).validate(DocumentType.CMS1500_CLAIM, _claim()) == []


def test_claim_cross_field_rules():
    claim = _claim(
        total_charge="150.00",
        service_lines=[ServiceLine(date_of_service=date(2027, 1, 1), cpt_hcpcs="99214", diagnosis_pointer="AB", charge="100.00")],
    )
    rules = {i.rule for i in ValidationEngine(today=date(2026, 6, 1)).validate(DocumentType.CMS1500_CLAIM, claim)}
    assert {"total_mismatch", "dx_pointer", "dos_future"} <= rules


def test_dob_after_service():
    claim = _claim(patient=Patient(first_name="A", last_name="B", date_of_birth=date(2026, 2, 1), member_id="X"))
    rules = {i.rule for i in ValidationEngine(today=date(2026, 6, 1)).validate(DocumentType.CMS1500_CLAIM, claim)}
    assert "dob_after_service" in rules


def test_ocr_repair_fixes_unambiguous_confusions():
    claim = _claim(diagnosis_codes=["£11.9", "110", "279.4", "E11.9"])
    fixes = repair(claim)
    assert claim.diagnosis_codes == ["E11.9", "I10", "Z79.4", "E11.9"]
    assert len(fixes) == 3 and all(f.rule == "ocr_repair" for f in fixes)


def test_ocr_repair_leaves_garbage_alone():
    claim = _claim(diagnosis_codes=["XXXXX"])
    assert repair(claim) == []
    assert claim.diagnosis_codes == ["XXXXX"]
