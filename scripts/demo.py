"""End-to-end demo against a RUNNING server.

    python scripts/demo.py                       # default http://localhost:8000, key "change-me"
    python scripts/demo.py --url http://localhost:8000 --key change-me
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def show(result: dict) -> None:
    print(f"status      : {result['status']}")
    print(f"type        : {result.get('document_type')}  (conf {result.get('classification_confidence')})")
    print(f"ocr         : {result.get('ocr_provider')}  (conf {result.get('ocr_confidence')})")
    print(f"overall conf: {result.get('overall_confidence')}")
    for reason in result.get("review_reasons", []):
        print(f"  review -> {reason}")
    for issue in result.get("issues", []):
        print(f"  [{issue['severity']:7}] {issue['rule']:18} {issue['field']}: {issue['message']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", default="change-me")
    args = ap.parse_args()
    client = httpx.Client(base_url=args.url, headers={"X-API-Key": args.key}, timeout=120)

    banner("1. Health check")
    try:
        print(client.get("/health").json())
    except httpx.ConnectError:
        print(f"Cannot reach {args.url}. Start the server first:  uvicorn app.main:app --port 8000")
        return 1

    banner("2. Synchronous processing of every sample")
    results = {}
    for name in ["claim_cms1500.txt", "lab_report.txt", "prescription.txt", "claim_with_errors.txt", "claim_scanned.png"]:
        print(f"\n--- {name}")
        r = client.post("/v1/documents/sync", files={"file": (name, (SAMPLES / name).read_bytes())})
        r.raise_for_status()
        results[name] = r.json()
        show(results[name])

    banner("3. Extracted data from the clean claim")
    data = results["claim_cms1500.txt"]["data"]
    p = data["patient"]
    print(f"Patient : {p['first_name']} {p['last_name']}, DOB {p['date_of_birth']}, member {p['member_id']}")
    print(f"Dx codes: {data['diagnosis_codes']}")
    for line in data["service_lines"]:
        print(f"  {line['date_of_service']}  CPT {line['cpt_hcpcs']}  ${line['charge']}  ptr {line['diagnosis_pointer']}")
    print(f"Total   : ${data['total_charge']}   Billing NPI: {data['billing_provider']['npi']}")

    banner("4. Async submit + polling")
    r = client.post("/v1/documents", files={"file": ("lab_report.txt", (SAMPLES / "lab_report.txt").read_bytes())})
    doc_id = r.json()["document_id"]
    print(f"submitted -> {r.status_code} id={doc_id}")
    for _ in range(30):
        got = client.get(f"/v1/documents/{doc_id}").json()
        if got["status"] in ("approved", "needs_review", "failed"):
            break
        time.sleep(0.5)
    print(f"final status: {got['status']}, {len(got['data']['results'])} lab results")

    banner("5. Human review queue")
    queue = client.get("/v1/documents", params={"status": "needs_review"}).json()
    for d in queue:
        print(f"  {d['document_id'][:8]}  {d['filename']:24} conf={d['overall_confidence']}")

    banner("6. Reviewer corrects the error claim and approves it")
    bad = results["claim_with_errors.txt"]
    r = client.post(f"/v1/documents/{bad['document_id']}/review", json={"reviewer": "demo@clinic"})
    print(f"approve without fixes -> HTTP {r.status_code} (expected 422)")
    fixed = bad["data"]
    fixed["billing_provider"]["npi"] = "1306849450"
    fixed["total_charge"] = "155.00"
    fixed["service_lines"][0]["diagnosis_pointer"] = "AB"
    r = client.post(f"/v1/documents/{bad['document_id']}/review", json={"reviewer": "demo@clinic", "data": fixed})
    print(f"approve with fixes    -> HTTP {r.status_code}, status={r.json()['status']}, reviewed_by={r.json()['reviewed_by']}")

    banner("7. Metrics")
    print(client.get("/v1/metrics").json())

    banner("8. Security: request without API key")
    print("HTTP", httpx.get(f"{args.url}/v1/metrics").status_code, "(401 expected when MEDX_API_KEY is set)")
    print("\nDemo complete. Interactive docs: " + args.url + "/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
