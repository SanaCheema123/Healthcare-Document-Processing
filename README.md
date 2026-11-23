# MedExtract AI: Healthcare Document Processing (OCR + LLM)
[screen-capture (12).webm](https://github.com/user-attachments/assets/6f977052-5124-4651-8c9f-4c2a6c969770)

A production-oriented service that turns healthcare documents (CMS-1500 claims, lab reports, prescriptions) into **validated, structured data**. It combines OCR, schema-constrained LLM extraction, deterministic healthcare validation, hallucination checks and human-in-the-loop review.

```
upload ─► OCR ─► classify ─► LLM extract ─► repair ─► validate ─► ground & score ─► route
          │                    ▲                          │                           ├─► APPROVED (straight-through)
          │                    └── self-correction ◄──────┘                           ├─► NEEDS_REVIEW (human queue)
          └ text layer → Textract → Tesseract                                         └─► FAILED (DLQ / alert)
```

## Features

| Capability | Implementation |
|---|---|
| **OCR routing** | Digital PDFs/text use the free text layer. Scans go to **AWS Textract** (FORMS + TABLES, sync and async multi-page), with **Tesseract** as a local/fallback engine. |
| **LLM extraction** | **Amazon Bedrock** (Converse API) or the **Anthropic API**, with *forced tool use*. The model must answer with JSON matching the Pydantic schema, so no free-text JSON parsing is needed. |
| **Self-correction loop** | Schema errors (Pydantic) and domain errors (bad NPI checksum, invalid ICD-10, ...) are sent back to the model for up to `N` retries. The model is told to return `null` instead of guessing. |
| **Healthcare validation** | NPI Luhn check, ICD-10-CM format, CPT/HCPCS, modifiers, POS, EIN, DEA check digit and name initial, NDC, LOINC, dx-pointer integrity, claim totals vs line sums, DOB/DOS chronology, lab flag vs reference range, critical values, controlled substances without a DEA number. |
| **Hallucination guard (grounding)** | Every extracted value is searched for in the OCR text. An ungrounded critical identifier blocks auto-approval. |
| **OCR-confusion repair** | Recovers `£11.9→E11.9`, `110→I10`, `279.4→Z79.4` when exactly one valid candidate exists. These fixes are always flagged for a human to confirm, never applied silently. |
| **Confidence scoring and routing** | Field confidence = OCR confidence of the source line when the value is grounded. Overall score combines classification, OCR, field scores and an error penalty. Thresholds are configurable. |
| **Human-in-the-loop** | Review queue (`GET /v1/documents?status=needs_review`) and a correction endpoint. Corrections are re-validated, approval is refused while errors remain, and the reviewer is recorded. |
| **Async at scale** | API → S3 → SQS → Lambda (or ECS worker), with partial-batch failure reporting, a DLQ and alarms. Batch drop-folder ingestion goes through S3 event notifications. |
| **HIPAA-minded** | KMS everywhere, TLS-only bucket policy, PHI-redacting JSON logs, least-privilege IAM, a prompt-injection guard in the system prompt, and only HIPAA-eligible AWS services. |
| **Evaluation harness** | Field-level precision, recall and F1, classification accuracy, straight-through rate, latency and tokens. Run it before every prompt or model change. |
| **Offline mode** | The `rules` provider is a deterministic extractor, so the whole system runs and tests with no credentials. It also serves as a degraded-mode fallback and an accuracy baseline. |

## Quick start

```bash
pip install -r requirements-dev.txt      # needs the tesseract binary for image OCR (apt/brew install tesseract)
cp .env.example .env
make test                                # 62 tests, no network
make run                                 # http://localhost:8000/docs

curl -F "file=@samples/claim_cms1500.txt" localhost:8000/v1/documents/sync -H "X-API-Key: change-me"
curl -F "file=@samples/claim_scanned.png" localhost:8000/v1/documents/sync -H "X-API-Key: change-me"
```

**Switch to a real LLM:**

```bash
MEDX_LLM_PROVIDER=bedrock MEDX_BEDROCK_MODEL_ID=<model-or-inference-profile-id> make run
# or: MEDX_LLM_PROVIDER=anthropic MEDX_ANTHROPIC_API_KEY=... make run
```

The model must be enabled in your Bedrock account and region. The default ID in `.env.example` is a placeholder; replace it with the one your account has access to.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/documents` | Async submit (multipart `file`, optional `document_type`). Returns `202` with `document_id`. |
| `POST` | `/v1/documents/sync` | Process inline and return the full result. |
| `GET` | `/v1/documents/{id}` | Result: data, field confidences, issues, review reasons, timings. |
| `GET` | `/v1/documents?status=needs_review` | Review queue. |
| `POST` | `/v1/documents/{id}/review` | `{reviewer, data?, approve}`: re-validates, then approves or rejects. |
| `GET` | `/v1/metrics` | Straight-through rate, latency, token usage. |
| `GET` | `/v1/schemas/{type}` | JSON schema for each document type. |
| `GET` | `/health` | Liveness and configured providers. |

Example result (abridged), for a scanned claim:

```json
{
  "status": "needs_review",
  "document_type": "cms1500_claim",
  "ocr_provider": "tesseract", "ocr_confidence": 0.93, "overall_confidence": 0.88,
  "data": { "diagnosis_codes": ["E11.9", "I10", "Z79.4"], "total_charge": "242.00", "...": "..." },
  "issues": [{ "field": "diagnosis_codes[1]", "rule": "ocr_repair",
               "message": "auto-corrected likely OCR error '110' -> 'I10'; please confirm", "severity": "warning" }],
  "review_reasons": ["3 OCR auto-correction(s) need confirmation", "overall confidence 0.88 below 0.9"],
  "timings_ms": { "ocr": 992.4, "classify": 0.1, "extract": 16.9, "score": 0.5 }
}
```

## Project layout

```
app/
  api/            FastAPI routes + dependency wiring (swap implementations in deps.py)
  core/           settings, Pydantic domain schemas, PHI-redacting JSON logging
  ocr/            text-layer, Textract, Tesseract providers + router with fallback
  llm/            provider interface, Bedrock, Anthropic, offline rules engine, backoff
  extraction/     versioned prompts, classifier, self-correcting extractor
  validation/     identifier validators, document rules engine, grounding, OCR repair
  pipeline/       orchestrator: stages, timings, confidence scoring, routing, metrics
  storage/        in-memory/DynamoDB results; local/S3 (KMS) blobs
  workers/        SQS long-poll worker + Lambda handler (partial batch failures)
infra/terraform/  KMS, S3, SQS+DLQ, DynamoDB, IAM, Lambda, CloudWatch alarms
scripts/          evaluate.py (accuracy harness), make_scanned_sample.py
eval/             manifest + ground-truth labels
samples/          claim, error claim, scanned claim (PNG), lab report, prescription
tests/            62 tests: validators, extraction loop, pipeline, API, AWS (moto/stubs), logging
```

## Deploying to AWS

```bash
docker build -f Dockerfile.lambda -t medextract-worker .   # push to ECR
cd infra/terraform
terraform init
terraform apply -var worker_image_uri=<ecr-uri> -var environment=dev
```

- **Worker:** a Lambda function triggered by SQS (Textract + Bedrock). The `max_concurrency` setting protects service quotas.
- **API:** the main `Dockerfile` on ECS Fargate or App Runner, with `MEDX_S3_BUCKET`, `MEDX_SQS_QUEUE_URL`, `MEDX_STORAGE_BACKEND=dynamodb` and `MEDX_DYNAMODB_TABLE` set from the Terraform outputs.
- **Batch ingestion:** drop files under `s3://<bucket>/drop/`. They are queued and processed automatically.

## Design decisions

- **Forced tool use over "reply in JSON".** Constraining the model to one tool whose input schema *is* the Pydantic schema removes a whole class of parse failures. Pydantic is still the source of truth.
- **Deterministic checks around the LLM.** The LLM handles layout variance and messy text; code handles what must be exactly right (checksums, totals, code formats). Each covers the other's weak spots.
- **Grounding as a hard gate.** A plausible-looking NPI that isn't in the document is worse than a missing one. Ungrounded critical identifiers never auto-approve.
- **Corrections are suggestions.** OCR repair and the LLM retry loop improve accuracy, but nothing invented or altered is approved without a human seeing it.
- **Text layer first.** Many "scans" are digital PDFs. Skipping OCR for them is faster, cheaper and more accurate.
- **Provider abstraction.** Bedrock, the Anthropic API, Textract and Tesseract sit behind small interfaces, so swapping models or adding a provider touches one file.

## Evaluation

```bash
make eval                                   # rules baseline
MEDX_LLM_PROVIDER=bedrock make eval         # compare the LLM against the baseline
```

Current baseline on the bundled samples: **classification 100%, field F1 0.994**. The scanned claim's OCR misreads are caught and routed to review rather than passed through.

> ⚠️ The bundled ground truth only proves the harness works. Before claiming accuracy numbers, replace `eval/` with a labelled set of real, de-identified documents from the client (50–200 per type is a good start).

## Compliance notes

- Use the Bedrock and Textract paths for PHI; both are HIPAA-eligible under an AWS BAA. Use the Anthropic API path only with an appropriate BAA or zero-data-retention agreement.
- Logs are JSON with regex PHI redaction (SSN, phone, email, dates, MRN). Keep payload logging off in production.
- Data is encrypted at rest with KMS (S3, SQS, DynamoDB, CloudWatch Logs) and in transit with TLS only. Retention is configurable through the S3 lifecycle rule.

## Roadmap / extension points

- Add document types: define a Pydantic model, register it in `SCHEMA_REGISTRY`, add rules in `ValidationEngine`.
- Code-set lookups: validate ICD-10/CPT against the current CMS code tables (and NPPES for NPI existence), not just format.
- Page chunking with map-reduce extraction for very long records. Today documents over 60k characters are truncated.
- Feed reviewer corrections back as few-shot examples and as additions to the eval set.
- Step Functions orchestration if per-stage retries or parallel branches become necessary.
- Reviewer UI on top of the review API, with field bounding boxes from Textract highlighted on the page image.
