"""Dependency wiring (one place to swap implementations)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings
from app.llm.factory import build_llm
from app.ocr.factory import build_ocr_router
from app.pipeline.orchestrator import DocumentPipeline
from app.storage.repository import (
    BlobStore,
    DynamoDBRepository,
    InMemoryRepository,
    LocalBlobStore,
    ResultRepository,
    S3BlobStore,
)


@dataclass
class Container:
    settings: Settings
    repo: ResultRepository
    blobs: BlobStore
    pipeline: DocumentPipeline
    sqs = None

    def enqueue(self, document_id: str, key: str, filename: str, doc_type: str | None) -> bool:
        """Send to SQS when configured; returns False so caller processes in-process."""
        if not self.settings.sqs_queue_url:
            return False
        if self.sqs is None:
            import boto3

            self.sqs = boto3.client("sqs", region_name=self.settings.aws_region)
        self.sqs.send_message(
            QueueUrl=self.settings.sqs_queue_url,
            MessageBody=json.dumps({"document_id": document_id, "key": key, "filename": filename, "document_type": doc_type}),
        )
        return True


def build_container(settings: Settings) -> Container:
    if settings.storage_backend == "dynamodb" and settings.dynamodb_table:
        repo: ResultRepository = DynamoDBRepository(settings.dynamodb_table, settings.aws_region)
    else:
        repo = InMemoryRepository()
    blobs: BlobStore = (
        S3BlobStore(settings.s3_bucket, settings.aws_region, settings.kms_key_id) if settings.s3_bucket else LocalBlobStore()
    )
    pipeline = DocumentPipeline(settings, build_ocr_router(settings), build_llm(settings), repo)
    return Container(settings=settings, repo=repo, blobs=blobs, pipeline=pipeline)


@lru_cache
def get_container() -> Container:
    return build_container(get_settings())


def require_api_key(x_api_key: str | None = Header(default=None), c: Container = Depends(get_container)) -> None:
    import hmac

    expected = c.settings.api_key
    if expected and not hmac.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
