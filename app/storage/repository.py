"""Persistence for processing results and raw documents."""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from app.core.schemas import ProcessingResult, ProcessingStatus


# ------------------------------------------------------------ result store
class ResultRepository(ABC):
    @abstractmethod
    def save(self, result: ProcessingResult) -> None: ...

    @abstractmethod
    def get(self, document_id: str) -> ProcessingResult | None: ...

    @abstractmethod
    def list(self, status: ProcessingStatus | None = None, limit: int = 100) -> list[ProcessingResult]: ...


class InMemoryRepository(ResultRepository):
    def __init__(self):
        self._items: dict[str, ProcessingResult] = {}
        self._lock = threading.Lock()

    def save(self, result: ProcessingResult) -> None:
        result.updated_at = datetime.now(UTC)
        with self._lock:
            self._items[result.document_id] = result.model_copy(deep=True)

    def get(self, document_id: str) -> ProcessingResult | None:
        item = self._items.get(document_id)
        return item.model_copy(deep=True) if item else None

    def list(self, status: ProcessingStatus | None = None, limit: int = 100) -> list[ProcessingResult]:
        items = sorted(self._items.values(), key=lambda r: r.created_at, reverse=True)
        if status:
            items = [i for i in items if i.status == status]
        return [i.model_copy(deep=True) for i in items[:limit]]


class DynamoDBRepository(ResultRepository):
    """Table: PK `document_id` (S); GSI `status-index` on (`status`, `created_at`)."""

    def __init__(self, table_name: str, region: str, resource=None):
        import boto3

        self.table = (resource or boto3.resource("dynamodb", region_name=region)).Table(table_name)

    def save(self, result: ProcessingResult) -> None:
        result.updated_at = datetime.now(UTC)
        item = json.loads(result.model_dump_json())
        item = _floats_to_str(item)
        self.table.put_item(Item={"document_id": result.document_id, "status": result.status.value,
                                  "created_at": item["created_at"], "payload": json.dumps(item)})

    def get(self, document_id: str) -> ProcessingResult | None:
        resp = self.table.get_item(Key={"document_id": document_id})
        if "Item" not in resp:
            return None
        return ProcessingResult.model_validate_json(resp["Item"]["payload"])

    def list(self, status: ProcessingStatus | None = None, limit: int = 100) -> list[ProcessingResult]:
        from boto3.dynamodb.conditions import Key

        if status:
            resp = self.table.query(IndexName="status-index", KeyConditionExpression=Key("status").eq(status.value),
                                    ScanIndexForward=False, Limit=limit)
        else:
            resp = self.table.scan(Limit=limit)
        return [ProcessingResult.model_validate_json(i["payload"]) for i in resp.get("Items", [])]


def _floats_to_str(obj):
    if isinstance(obj, dict):
        return {k: _floats_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_str(v) for v in obj]
    return obj


# -------------------------------------------------------------- blob store
class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, content: bytes, content_type: str) -> str: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...


class LocalBlobStore(BlobStore):
    def __init__(self, root: str = ".data/blobs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, content: bytes, content_type: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class S3BlobStore(BlobStore):
    """Server-side encryption with a customer-managed KMS key (required for PHI)."""

    def __init__(self, bucket: str, region: str, kms_key_id: str | None = None, client=None):
        import boto3

        self.bucket = bucket
        self.kms_key_id = kms_key_id
        self.client = client or boto3.client("s3", region_name=region)

    def put(self, key: str, content: bytes, content_type: str) -> str:
        extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key_id} if self.kms_key_id else {"ServerSideEncryption": "AES256"}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type, **extra)
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
