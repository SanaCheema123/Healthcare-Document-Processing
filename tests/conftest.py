from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.api.deps import build_container
from app.core.config import Settings

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
HAS_TESSERACT = shutil.which("tesseract") is not None


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.chdir(tmp_path)  # LocalBlobStore writes under ./.data
    return Settings(ocr_provider="tesseract", llm_provider="rules", environment="local", _env_file=None)


@pytest.fixture
def container(settings):
    return build_container(settings)


@pytest.fixture
def sample():
    return lambda name: (SAMPLES / name).read_bytes()
