"""AWS integrations with stubbed clients / moto (no real AWS calls)."""
from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from app.core.schemas import ProcessingResult, ProcessingStatus
from app.llm.base import LLMRequest, LLMRetryableError
from app.llm.bedrock import BedrockProvider
from app.ocr.textract import TextractProvider
from app.storage.repository import DynamoDBRepository, S3BlobStore


# ------------------------------------------------------------------ Textract
def _word(i, text):
    return {"Id": f"w{i}", "BlockType": "WORD", "Text": text}


TEXTRACT_RESPONSE = {
    "DocumentMetadata": {"Pages": 1},
    "Blocks": [
        {"Id": "l1", "BlockType": "LINE", "Text": "Billing NPI: 1306849450", "Confidence": 99.1, "Page": 1,
         "Geometry": {"BoundingBox": {"Left": .1, "Top": .1, "Width": .5, "Height": .02}}},
        _word(1, "Billing"), _word(2, "NPI:"), _word(3, "1306849450"),
        {"Id": "k1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"], "Confidence": 97.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}, {"Type": "VALUE", "Ids": ["v1"]}]},
        {"Id": "v1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"], "Confidence": 95.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
        {"Id": "t1", "BlockType": "TABLE", "Relationships": [{"Type": "CHILD", "Ids": ["c1", "c2"]}]},
        {"Id": "c1", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 1, "Relationships": [{"Type": "CHILD", "Ids": ["w1"]}]},
        {"Id": "c2", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 2, "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
    ],
}


class FakeTextract:
    def analyze_document(self, **kwargs):
        assert kwargs["FeatureTypes"] == ["FORMS", "TABLES"]
        return TEXTRACT_RESPONSE


def test_textract_parsing():
    r = TextractProvider(region="us-east-1", client=FakeTextract()).extract(b"%PDF-fake", "application/pdf")
    assert r.lines[0].text == "Billing NPI: 1306849450" and r.lines[0].confidence == pytest.approx(0.991)
    assert r.key_values[0].key == "Billing NPI:" and r.key_values[0].value == "1306849450"
    assert r.key_values[0].confidence == pytest.approx(0.95)
    assert r.tables == [[["Billing", "1306849450"]]]


# ------------------------------------------------------------------- Bedrock
class FakeBedrock:
    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")
        return {
            "output": {"message": {"content": [{"toolUse": {"toolUseId": "t", "name": "x", "input": {"ok": True}}}]}},
            "usage": {"inputTokens": 1200, "outputTokens": 300},
            "stopReason": "tool_use",
        }


REQ = LLMRequest(system="sys", prompt="p", tool_name="extract_x", tool_description="d",
                 input_schema={"type": "object", "$defs": {"A": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/A"}}})


def test_bedrock_forces_tool_and_flattens_schema():
    fake = FakeBedrock()
    resp = BedrockProvider("model-x", "us-east-1", client=fake).invoke(REQ)
    assert resp.data == {"ok": True} and resp.input_tokens == 1200
    cfg = fake.calls[0]["toolConfig"]
    assert cfg["toolChoice"] == {"tool": {"name": "extract_x"}}
    assert cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["a"] == {"type": "string"}
    assert fake.calls[0]["inferenceConfig"]["temperature"] == 0.0


def test_bedrock_retries_throttling(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    fake = FakeBedrock(fail_times=2)
    assert BedrockProvider("m", "us-east-1", client=fake).invoke(REQ).data == {"ok": True}
    assert len(fake.calls) == 3


def test_bedrock_gives_up(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(LLMRetryableError):
        BedrockProvider("m", "us-east-1", client=FakeBedrock(fail_times=10)).invoke(REQ, max_attempts=3)


# ------------------------------------------------------------- S3 / DynamoDB
@mock_aws
def test_s3_blob_store_encrypts():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="phi-docs")
    store = S3BlobStore("phi-docs", "us-east-1", client=s3)
    uri = store.put("a/b.txt", b"hello", "text/plain")
    assert uri == "s3://phi-docs/a/b.txt" and store.get("a/b.txt") == b"hello"
    assert s3.head_object(Bucket="phi-docs", Key="a/b.txt")["ServerSideEncryption"] == "AES256"


@mock_aws
def test_dynamodb_repository_roundtrip():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="results",
        KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("document_id", "status", "created_at")],
        GlobalSecondaryIndexes=[{
            "IndexName": "status-index",
            "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    repo = DynamoDBRepository("results", "us-east-1", resource=ddb)
    repo.save(ProcessingResult(document_id="d1", filename="f", status=ProcessingStatus.NEEDS_REVIEW, overall_confidence=0.5))
    repo.save(ProcessingResult(document_id="d2", filename="f", status=ProcessingStatus.APPROVED))
    assert repo.get("d1").overall_confidence == 0.5
    assert [r.document_id for r in repo.list(ProcessingStatus.NEEDS_REVIEW)] == ["d1"]


# -------------------------------------------------------------------- Worker
def test_lambda_handler_reports_partial_failures(container, sample, monkeypatch):
    from app.workers import sqs_worker

    container.blobs.put("in/claim.txt", sample("claim_cms1500.txt"), "text/plain")
    container.blobs.put("in/bad.bin", b"\x00\x01", "application/octet-stream")
    monkeypatch.setattr(sqs_worker, "get_container", lambda: container)
    event = {"Records": [
        {"messageId": "m1", "body": json.dumps({"document_id": "d1", "key": "in/claim.txt", "filename": "claim.txt"})},
        {"messageId": "m2", "body": json.dumps({"document_id": "d2", "key": "in/bad.bin", "filename": "bad.bin"})},
    ]}
    assert sqs_worker.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert container.repo.get("d1").status == ProcessingStatus.APPROVED


def test_worker_accepts_s3_event_notifications():
    from app.workers.sqs_worker import _jobs_from_body

    body = json.dumps({"Records": [{"s3": {"object": {"key": "incoming/2026/scan+1.pdf"}}}]})
    job = _jobs_from_body(body)[0]
    assert job["key"] == "incoming/2026/scan 1.pdf" and job["filename"] == "scan 1.pdf"
