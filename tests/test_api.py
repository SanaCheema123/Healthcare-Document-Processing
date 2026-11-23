import pytest
from fastapi.testclient import TestClient

from app.api.deps import build_container, get_container
from app.main import app


@pytest.fixture
def client(container):
    app.dependency_overrides[get_container] = lambda: container
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(client, sample, name, path="/v1/documents/sync", **data):
    return client.post(path, files={"file": (name, sample(name), "text/plain")}, data=data)


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_sync_processing(client, sample):
    r = _upload(client, sample, "claim_cms1500.txt")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["data"]["patient"]["last_name"] == "Doe"


def test_async_submit_then_poll(client, sample):
    r = _upload(client, sample, "lab_report.txt", path="/v1/documents")
    assert r.status_code == 202
    doc_id = r.json()["document_id"]
    # TestClient runs background tasks before returning
    got = client.get(f"/v1/documents/{doc_id}").json()
    assert got["status"] == "approved" and got["document_type"] == "lab_report"


def test_document_type_hint(client, sample):
    r = _upload(client, sample, "prescription.txt", document_type="prescription")
    assert r.json()["classification_confidence"] == 1.0


def test_review_queue_and_correction(client, sample):
    doc = _upload(client, sample, "claim_with_errors.txt").json()
    assert doc["status"] == "needs_review"
    queue = client.get("/v1/documents", params={"status": "needs_review"}).json()
    assert doc["document_id"] in [d["document_id"] for d in queue]

    # approving uncorrected data is refused
    r = client.post(f"/v1/documents/{doc['document_id']}/review", json={"reviewer": "coder@clinic"})
    assert r.status_code == 422

    fixed = doc["data"]
    fixed["billing_provider"]["npi"] = "1306849450"
    fixed["total_charge"] = "155.00"
    fixed["service_lines"][0]["diagnosis_pointer"] = "AB"
    r = client.post(f"/v1/documents/{doc['document_id']}/review", json={"reviewer": "coder@clinic", "data": fixed})
    assert r.status_code == 200
    assert r.json()["status"] == "approved" and r.json()["reviewed_by"] == "coder@clinic"


def test_rejects_unsupported_file(client):
    r = client.post("/v1/documents/sync", files={"file": ("x.zip", b"PK\x03\x04\x00\xff\xfe", "application/zip")})
    assert r.status_code == 415


def test_404(client):
    assert client.get("/v1/documents/nope").status_code == 404


def test_schema_endpoint(client):
    schema = client.get("/v1/schemas/lab_report").json()
    assert "results" in schema["properties"]


def test_metrics(client, sample):
    _upload(client, sample, "claim_cms1500.txt")
    m = client.get("/v1/metrics").json()
    assert m["processed"] == 1 and m["straight_through_rate"] == 1.0


def test_api_key_enforced(settings, sample):
    settings.api_key = "s3cret"
    c = build_container(settings)
    app.dependency_overrides[get_container] = lambda: c
    try:
        client = TestClient(app)
        assert _upload(client, sample, "claim_cms1500.txt").status_code == 401
        r = client.post("/v1/documents/sync", files={"file": ("a.txt", sample("claim_cms1500.txt"))}, headers={"X-API-Key": "s3cret"})
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
