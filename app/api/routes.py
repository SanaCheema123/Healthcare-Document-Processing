from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ValidationError

from app.api.deps import Container, get_container, require_api_key
from app.core.schemas import (
    SCHEMA_REGISTRY,
    DocumentType,
    ProcessingResult,
    ProcessingStatus,
    ValidationSeverity,
)
from app.ocr.base import detect_mime

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

ALLOWED_MIME = {"application/pdf", "image/png", "image/jpeg", "image/tiff", "text/plain"}


class SubmitResponse(BaseModel):
    document_id: str
    status: ProcessingStatus
    queued: bool


class ReviewRequest(BaseModel):
    reviewer: str
    data: dict[str, Any] | None = None  # corrected payload; omit to approve as-is
    approve: bool = True
    document_type: DocumentType | None = None


async def _read_upload(file: UploadFile, c: Container) -> bytes:
    content = await file.read()
    if len(content) > c.settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"file exceeds {c.settings.max_upload_mb} MB")
    if not content:
        raise HTTPException(400, "empty file")
    if detect_mime(content, file.filename or "") not in ALLOWED_MIME:
        raise HTTPException(415, "unsupported file type (pdf, png, jpeg, tiff, txt)")
    return content


@router.post("/documents", response_model=SubmitResponse, status_code=202)
async def submit_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    document_type: DocumentType | None = Form(None),
    c: Container = Depends(get_container),
):
    """Asynchronous submission. Poll GET /v1/documents/{id} for the result."""
    content = await _read_upload(file, c)
    document_id = str(uuid.uuid4())
    filename = file.filename or "upload"
    key = f"incoming/{datetime.now(UTC):%Y/%m/%d}/{document_id}/{filename}"
    c.blobs.put(key, content, detect_mime(content, filename))
    c.repo.save(ProcessingResult(document_id=document_id, filename=filename, status=ProcessingStatus.RECEIVED))

    queued = c.enqueue(document_id, key, filename, document_type.value if document_type else None)
    if not queued:
        background.add_task(c.pipeline.process, document_id, filename, content, document_type)
    return SubmitResponse(document_id=document_id, status=ProcessingStatus.RECEIVED, queued=queued)


@router.post("/documents/sync", response_model=ProcessingResult)
async def process_sync(
    file: UploadFile = File(...),
    document_type: DocumentType | None = Form(None),
    c: Container = Depends(get_container),
):
    """Synchronous processing for small documents / interactive use."""
    content = await _read_upload(file, c)
    return c.pipeline.process(str(uuid.uuid4()), file.filename or "upload", content, document_type)


@router.get("/documents/{document_id}", response_model=ProcessingResult)
def get_document(document_id: str, c: Container = Depends(get_container)):
    result = c.repo.get(document_id)
    if not result:
        raise HTTPException(404, "document not found")
    return result


@router.get("/documents", response_model=list[ProcessingResult])
def list_documents(
    status: ProcessingStatus | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    c: Container = Depends(get_container),
):
    return c.repo.list(status=status, limit=limit)


@router.post("/documents/{document_id}/review", response_model=ProcessingResult)
def review_document(document_id: str, body: ReviewRequest, c: Container = Depends(get_container)):
    """Human-in-the-loop: accept corrections, re-validate, and approve or reject."""
    result = c.repo.get(document_id)
    if not result:
        raise HTTPException(404, "document not found")
    doc_type = body.document_type or result.document_type
    if doc_type not in SCHEMA_REGISTRY:
        raise HTTPException(422, "document_type required for this document")

    data = body.data if body.data is not None else result.data
    try:
        model = SCHEMA_REGISTRY[doc_type].model_validate(data)
    except ValidationError as err:
        raise HTTPException(422, err.errors(include_url=False)) from err

    issues = c.pipeline.validator.validate(doc_type, model)  # no grounding: human-entered values
    errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
    if body.approve and errors:
        raise HTTPException(422, {"message": "corrected data still fails validation", "issues": [e.model_dump() for e in errors]})

    result.document_type = doc_type
    result.data = model.model_dump(mode="json")
    result.issues = issues
    result.reviewed_by = body.reviewer
    result.status = ProcessingStatus.APPROVED if body.approve else ProcessingStatus.FAILED
    c.repo.save(result)
    return result


@router.get("/metrics")
def metrics(c: Container = Depends(get_container)):
    return c.pipeline.metrics.snapshot()


@router.get("/schemas/{document_type}")
def get_schema(document_type: DocumentType):
    if document_type not in SCHEMA_REGISTRY:
        raise HTTPException(404, "no schema for this type")
    return SCHEMA_REGISTRY[document_type].model_json_schema()
