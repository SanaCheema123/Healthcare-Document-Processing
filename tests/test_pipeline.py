import pytest

from app.core.schemas import DocumentType, ProcessingStatus
from tests.conftest import HAS_TESSERACT


@pytest.mark.parametrize("name,doc_type,status", [
    ("claim_cms1500.txt", DocumentType.CMS1500_CLAIM, ProcessingStatus.APPROVED),
    ("lab_report.txt", DocumentType.LAB_REPORT, ProcessingStatus.APPROVED),
    ("prescription.txt", DocumentType.PRESCRIPTION, ProcessingStatus.APPROVED),
    ("claim_with_errors.txt", DocumentType.CMS1500_CLAIM, ProcessingStatus.NEEDS_REVIEW),
])
def test_samples_route_correctly(container, sample, name, doc_type, status):
    r = container.pipeline.process("id-" + name, name, sample(name))
    assert r.document_type == doc_type
    assert r.status == status, r.review_reasons
    assert set(r.timings_ms) >= {"ocr", "classify", "extract", "score"}


def test_error_claim_reports_every_problem(container, sample):
    r = container.pipeline.process("e", "claim_with_errors.txt", sample("claim_with_errors.txt"))
    rules = {i.rule for i in r.issues}
    assert {"npi_checksum", "total_mismatch", "dx_pointer", "ocr_repair"} <= rules
    assert r.data["diagnosis_codes"] == ["M54.50", "I25.10"]


def test_lab_values(container, sample):
    r = container.pipeline.process("l", "lab_report.txt", sample("lab_report.txt"))
    a1c = r.data["results"][0]
    assert (a1c["test_name"], a1c["value"], a1c["flag"]) == ("Hemoglobin A1c", "7.8", "H")
    assert any(i.rule == "flag_consistency" for i in r.issues)  # LDL 131 vs <100 unflagged


def test_unknown_document_goes_to_review(container):
    r = container.pipeline.process("u", "note.txt", b"Grocery list: milk, eggs, bread, coffee and more items")
    assert r.status == ProcessingStatus.NEEDS_REVIEW
    assert r.document_type == DocumentType.UNKNOWN


def test_failure_is_captured_not_raised(container):
    r = container.pipeline.process("f", "bad.bin", b"\x00\x01\x02 not a document")
    assert r.status == ProcessingStatus.FAILED and r.error
    assert container.pipeline.metrics.snapshot()["failed"] == 1


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_scanned_claim_ocr_and_repair(container, sample):
    r = container.pipeline.process("s", "claim_scanned.png", sample("claim_scanned.png"))
    assert r.ocr_provider == "tesseract"
    assert r.data["billing_provider"]["npi"] == "1306849450"
    assert r.data["total_charge"] == "242.00"
    assert len(r.data["service_lines"]) == 3
    # OCR confusions repaired but held for human confirmation
    assert r.data["diagnosis_codes"] == ["E11.9", "I10", "Z79.4"]
    assert r.status == ProcessingStatus.NEEDS_REVIEW
