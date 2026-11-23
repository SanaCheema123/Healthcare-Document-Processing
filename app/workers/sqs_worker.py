"""Background workers.

Two deployment options share the same pipeline:
  * `python -m app.workers.sqs_worker`  - long-running ECS/Fargate consumer
  * `lambda_handler`                    - AWS Lambda triggered by SQS (or S3 -> SQS)

Messages: {"document_id", "key", "filename", "document_type"?}
S3 event notifications are also accepted (document_id is derived from the key).
Failures are left on the queue -> retried -> moved to the DLQ after maxReceiveCount.
"""
from __future__ import annotations

import json
import logging
import signal
import urllib.parse
import uuid

from app.api.deps import get_container
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.schemas import DocumentType, ProcessingStatus

log = logging.getLogger("worker")


def _jobs_from_body(body: str) -> list[dict]:
    msg = json.loads(body)
    if "Records" in msg:  # S3 event notification
        jobs = []
        for rec in msg["Records"]:
            key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
            jobs.append({"document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)), "key": key, "filename": key.rsplit("/", 1)[-1]})
        return jobs
    return [msg]


def handle_job(job: dict) -> ProcessingStatus:
    c = get_container()
    content = c.blobs.get(job["key"])
    hint = DocumentType(job["document_type"]) if job.get("document_type") else None
    result = c.pipeline.process(job["document_id"], job["filename"], content, hint)
    log.info("processed document", extra={"document_id": result.document_id, "stage": result.status.value})
    if result.status == ProcessingStatus.FAILED:
        raise RuntimeError(result.error or "processing failed")
    return result.status


def lambda_handler(event, _context):
    """SQS-triggered Lambda with partial batch failure reporting."""
    failures = []
    for record in event.get("Records", []):
        try:
            for job in _jobs_from_body(record["body"]):
                handle_job(job)
        except Exception:  # noqa: BLE001
            log.exception("job failed")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def run_forever() -> None:  # pragma: no cover - integration path
    import boto3

    settings = get_settings()
    configure_logging(settings.log_level, settings.redact_phi_in_logs)
    if not settings.sqs_queue_url:
        raise SystemExit("MEDX_SQS_QUEUE_URL is not set")
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker started")
    while running:
        resp = sqs.receive_message(QueueUrl=settings.sqs_queue_url, MaxNumberOfMessages=5, WaitTimeSeconds=20, VisibilityTimeout=300)
        for msg in resp.get("Messages", []):
            try:
                for job in _jobs_from_body(msg["Body"]):
                    handle_job(job)
                sqs.delete_message(QueueUrl=settings.sqs_queue_url, ReceiptHandle=msg["ReceiptHandle"])
            except Exception:  # noqa: BLE001
                log.exception("job failed; message will be retried / dead-lettered")


if __name__ == "__main__":  # pragma: no cover
    run_forever()
