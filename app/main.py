from __future__ import annotations

from fastapi import FastAPI

from app.api.deps import get_container
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, settings.redact_phi_in_logs)

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="OCR + LLM document processing and data extraction for healthcare documents.",
)
app.include_router(router)


@app.get("/health", tags=["ops"])
def health():
    c = get_container()
    return {
        "status": "ok",
        "environment": c.settings.environment,
        "llm_provider": c.pipeline.llm.name,
        "ocr_engines": ["text_layer"] + [e.name for e in c.pipeline.ocr.engines],
    }
