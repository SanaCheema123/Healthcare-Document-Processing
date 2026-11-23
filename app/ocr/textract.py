"""AWS Textract provider.

- Single-page images / small PDFs: synchronous `AnalyzeDocument` (FORMS + TABLES).
- Multi-page PDFs in S3: asynchronous `StartDocumentAnalysis` + polling.
Textract is a HIPAA-eligible service; keep it inside a BAA-covered account.
"""
from __future__ import annotations

import time
from typing import Any

from app.core.schemas import BoundingBox, OCRKeyValue, OCRLine, OCRResult
from app.ocr.base import OCRError, OCRProvider

_FEATURES = ["FORMS", "TABLES"]


class TextractProvider(OCRProvider):
    name = "textract"

    def __init__(self, region: str, client: Any | None = None, poll_interval_s: float = 2.0, max_wait_s: float = 300):
        import boto3

        self.client = client or boto3.client("textract", region_name=region)
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s

    # ---------------------------------------------------------------- public
    def extract(self, content: bytes, mime_type: str) -> OCRResult:
        if not (mime_type.startswith("image/") or mime_type == "application/pdf"):
            raise OCRError(f"Textract cannot process {mime_type}")
        try:
            resp = self.client.analyze_document(Document={"Bytes": content}, FeatureTypes=_FEATURES)
        except Exception as exc:  # botocore ClientError, e.g. UnsupportedDocumentException for multi-page
            raise OCRError(f"Textract analyze_document failed: {exc}") from exc
        return self._parse(resp["Blocks"], resp.get("DocumentMetadata", {}).get("Pages", 1))

    def extract_s3(self, bucket: str, key: str) -> OCRResult:
        """Async path for multi-page PDFs already in S3."""
        job = self.client.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}, FeatureTypes=_FEATURES
        )
        job_id, waited, blocks, pages = job["JobId"], 0.0, [], 1
        while True:
            resp = self.client.get_document_analysis(JobId=job_id)
            status = resp["JobStatus"]
            if status == "SUCCEEDED":
                pages = resp["DocumentMetadata"]["Pages"]
                blocks.extend(resp["Blocks"])
                token = resp.get("NextToken")
                while token:
                    resp = self.client.get_document_analysis(JobId=job_id, NextToken=token)
                    blocks.extend(resp["Blocks"])
                    token = resp.get("NextToken")
                return self._parse(blocks, pages)
            if status == "FAILED":
                raise OCRError(resp.get("StatusMessage", "Textract job failed"))
            if waited >= self.max_wait_s:
                raise OCRError(f"Textract job {job_id} timed out")
            time.sleep(self.poll_interval_s)
            waited += self.poll_interval_s

    # --------------------------------------------------------------- parsing
    def _parse(self, blocks: list[dict], page_count: int) -> OCRResult:
        by_id = {b["Id"]: b for b in blocks}
        lines: list[OCRLine] = []
        kvs: list[OCRKeyValue] = []
        tables: list[list[list[str]]] = []

        for b in blocks:
            bt = b["BlockType"]
            if bt == "LINE":
                bb = b.get("Geometry", {}).get("BoundingBox")
                lines.append(
                    OCRLine(
                        text=b["Text"],
                        confidence=b.get("Confidence", 0) / 100,
                        page=b.get("Page", 1),
                        bbox=BoundingBox(left=bb["Left"], top=bb["Top"], width=bb["Width"], height=bb["Height"]) if bb else None,
                    )
                )
            elif bt == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
                key_text = self._child_text(b, by_id)
                value_block = next(
                    (by_id[i] for r in b.get("Relationships", []) if r["Type"] == "VALUE" for i in r["Ids"]), None
                )
                value_text = self._child_text(value_block, by_id) if value_block else ""
                conf = min(b.get("Confidence", 0), (value_block or {}).get("Confidence", 0)) / 100
                if key_text:
                    kvs.append(OCRKeyValue(key=key_text, value=value_text, confidence=conf, page=b.get("Page", 1)))
            elif bt == "TABLE":
                cells = [
                    by_id[i] for r in b.get("Relationships", []) if r["Type"] == "CHILD" for i in r["Ids"]
                    if by_id[i]["BlockType"] == "CELL"
                ]
                if not cells:
                    continue
                rows = max(c["RowIndex"] for c in cells)
                cols = max(c["ColumnIndex"] for c in cells)
                grid = [["" for _ in range(cols)] for _ in range(rows)]
                for c in cells:
                    grid[c["RowIndex"] - 1][c["ColumnIndex"] - 1] = self._child_text(c, by_id)
                tables.append(grid)

        return OCRResult(provider=self.name, page_count=page_count, lines=lines, key_values=kvs, tables=tables)

    @staticmethod
    def _child_text(block: dict | None, by_id: dict[str, dict]) -> str:
        if not block:
            return ""
        words = []
        for rel in block.get("Relationships", []):
            if rel["Type"] != "CHILD":
                continue
            for cid in rel["Ids"]:
                child = by_id.get(cid, {})
                if child.get("BlockType") == "WORD":
                    words.append(child["Text"])
                elif child.get("BlockType") == "SELECTION_ELEMENT" and child.get("SelectionStatus") == "SELECTED":
                    words.append("[X]")
        return " ".join(words)
