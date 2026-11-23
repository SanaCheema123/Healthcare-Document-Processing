"""Hallucination guard: verify extracted values actually appear in the source text.

LLMs occasionally produce plausible but absent values (a made-up NPI, a
"corrected" ICD code). Every leaf value is normalised and searched for in the
OCR text; ungrounded values lower confidence and, for critical identifiers,
force human review.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.core.schemas import FieldConfidence, OCRResult

# Fields whose values are derived/normalised rather than copied verbatim.
_SKIP_SUFFIXES = ("sex", "substitution_allowed", "flag", "units", "refills")
CRITICAL_SUFFIXES = ("npi", "member_id", "diagnosis_codes", "cpt_hcpcs", "dea_number", "tax_id", "ndc", "total_charge", "charge")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def iter_leaves(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from iter_leaves(getattr(obj, name), f"{prefix}.{name}" if prefix else name)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from iter_leaves(item, f"{prefix}[{i}]")
    elif obj is not None and obj != "":
        yield prefix, obj


def _candidates(value: Any) -> list[str]:
    if isinstance(value, datetime):
        d = value
        return [
            f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.month}/{d.day}/{d.year}", d.date().isoformat(),
            d.strftime("%m%d%Y"),
        ]
    if isinstance(value, date):
        d = value
        return [f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.month}/{d.day}/{d.year}", d.isoformat(), d.strftime("%m%d%Y")]
    if isinstance(value, Decimal):
        q = value.quantize(Decimal("0.01"))
        return [f"{q}", f"{q:,}"]
    return [str(value)]


class Grounder:
    def __init__(self, ocr: OCRResult):
        self.ocr = ocr
        self._norm_lines = [(_norm(l.text), l.confidence) for l in ocr.lines]
        self._norm_lines += [(_norm(f"{kv.key} {kv.value}"), kv.confidence) for kv in ocr.key_values]
        self._full = _norm(ocr.text)

    def locate(self, value: Any) -> float | None:
        """Return OCR confidence of the best line containing the value, or None."""
        best = None
        for cand in _candidates(value):
            n = _norm(cand)
            if not n:  # symbol-only values such as "%": raw substring match
                for line in self.ocr.lines:
                    if cand and cand in line.text:
                        best = max(best or 0.0, line.confidence)
                continue
            for line, conf in self._norm_lines:
                if n in line:
                    best = max(best or 0.0, conf)
            if best is None and len(n) >= 4 and n in self._full:  # value split across lines
                best = self.ocr.mean_confidence
        return best

    def score(self, model: BaseModel) -> list[FieldConfidence]:
        out: list[FieldConfidence] = []
        for path, value in iter_leaves(model):
            leaf = re.sub(r"\[\d+\]", "", path.split(".")[-1])
            if leaf.endswith(_SKIP_SUFFIXES) or isinstance(value, bool):
                out.append(FieldConfidence(field=path, value=_jsonable(value), confidence=0.9, grounded=True))
                continue
            conf = self.locate(value)
            if conf is None and isinstance(value, str) and " " in value:
                # names/addresses: accept if every token is present somewhere
                tokens = [t for t in value.split() if len(_norm(t)) > 1]
                if tokens and all(_norm(t) in self._full for t in tokens):
                    conf = self.ocr.mean_confidence * 0.95
            grounded = conf is not None
            out.append(
                FieldConfidence(
                    field=path,
                    value=_jsonable(value),
                    confidence=round(conf if grounded else 0.35, 3),
                    grounded=grounded,
                )
            )
        return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v
