"""Deterministic, offline "LLM" provider built from regex/heuristic rules.

Purpose:
  * lets the whole pipeline run locally and in CI without credentials or cost;
  * acts as a degraded-mode fallback if the LLM endpoint is down;
  * produces a baseline to measure the LLM's lift in `scripts/evaluate.py`.

It honours the same tool contract as the real providers: given the tool name
it returns a dict that matches the requested schema.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse

DATE = r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
MONEY = r"\$?\s*([\d,]+\.\d{2})"


def _find(text: str, label: str, pattern: str = r"(.+)") -> str | None:
    m = re.search(rf"{label}\s*[:#]?\s*{pattern}", text, re.I | re.M)
    if not m:
        return None
    value = m.group(1).strip()
    # stop at the next "Label:" on the same line (two+ spaces separate fields)
    value = re.split(r"\s{2,}[A-Z][\w .#/]*:", value)[0].strip()
    return value or None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _datetime(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def _money(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(MONEY, value)
    return m.group(1).replace(",", "") if m else None


def _name(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "," in value:
        last, first = [p.strip() for p in value.split(",", 1)]
    else:
        parts = value.split()
        first, last = parts[0], " ".join(parts[1:]) or None
    title = lambda s: s.title() if s and s.isupper() else s  # noqa: E731
    return title(first), title(last)


def _patient(text: str) -> dict[str, Any]:
    first, last = _name(_find(text, r"Patient(?: Name)?", r"([A-Za-z ,.'-]+?)(?:\s{2,}|$)"))
    return {
        "first_name": first,
        "last_name": last,
        "date_of_birth": _date(_find(text, r"(?:Patient )?(?:DOB|Date of Birth)", DATE)),
        "sex": _find(text, r"Sex", r"([MFU])\b"),
        "member_id": _find(text, r"(?:Insured|Member|Subscriber) ID", r"([A-Z0-9-]+)"),
        "mrn": _find(text, r"MRN", r"([A-Z0-9-]+)"),
        "address": _find(text, r"Patient Address"),
    }


# --------------------------------------------------------------- classifiers
_KEYWORDS = {
    "cms1500_claim": ["cms-1500", "claim form", "cpt", "place of service", "total charge", "billing provider", "diagnosis code"],
    "lab_report": ["laboratory", "lab report", "reference range", "specimen", "accession", "result", "collected"],
    "prescription": ["rx", "prescription", "sig", "refills", "dispense", "dea", "prescriber"],
}


def classify(text: str) -> dict[str, Any]:
    lowered = text.lower()
    scores = {k: sum(lowered.count(w) for w in words) for k, words in _KEYWORDS.items()}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if scores[best] == 0:
        return {"document_type": "unknown", "confidence": 0.3, "rationale": "no domain keywords found"}
    conf = round(min(0.99, 0.5 + 0.5 * scores[best] / total), 2)
    return {"document_type": best, "confidence": conf, "rationale": f"keyword scores: {scores}"}


# ---------------------------------------------------------------- extractors
def extract_cms1500(text: str) -> dict[str, Any]:
    dx_line = _find(text, r"Diagnosis Codes?") or ""
    dx = re.findall(r"\b[A-L]\.\s*(\S[0-9A-Z.]{2,7})", dx_line)

    lines = []
    row = re.compile(
        rf"^\s*{DATE}\s+(\d{{2}})\s+([A-Z]?\d{{4,5}})\s+((?:[A-Z0-9]{{2}}\s+)*?)([A-L]{{1,4}})\s+{MONEY}\s+(\d+)\s*$",
        re.M,
    )
    for m in row.finditer(text):
        lines.append(
            {
                "date_of_service": _date(m.group(1)),
                "place_of_service": m.group(2),
                "cpt_hcpcs": m.group(3),
                "modifiers": m.group(4).split(),
                "diagnosis_pointer": m.group(5),
                "charge": m.group(6).replace(",", ""),
                "units": int(m.group(7)),
            }
        )

    rendering_name = _find(text, r"Rendering Provider", r"(.+?)(?:\s{2,}NPI|$)")
    return {
        "patient": _patient(text),
        "insurance_plan": _find(text, r"Insurance Plan"),
        "billing_provider": {
            "name": _find(text, r"Billing Provider"),
            "npi": _find(text, r"Billing NPI", r"(\d{10})"),
            "tax_id": _find(text, r"(?:Federal )?Tax ID", r"(\d{2}-?\d{7})"),
            "address": _find(text, r"Billing Address"),
            "phone": _find(text, r"Billing Phone", r"([\d() .-]{10,})"),
        },
        "rendering_provider": {
            "name": rendering_name,
            "npi": _find(text, r"Rendering Provider.*?NPI", r"(\d{10})"),
        }
        if rendering_name
        else None,
        "diagnosis_codes": dx,
        "service_lines": lines,
        "total_charge": _money(_find(text, r"Total Charge")),
        "amount_paid": _money(_find(text, r"Amount Paid")),
        "prior_authorization": _find(text, r"Prior Authori[sz]ation", r"([A-Z0-9-]+)"),
    }


def extract_lab(text: str) -> dict[str, Any]:
    results = []
    # Test | Value | Unit | Range | Flag  (pipe- or 2+space-separated rows)
    row = re.compile(r"^\s*([A-Za-z][\w ,()/-]+?)\s*\|\s*([<>]?[\d.]+|[A-Za-z]+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([A-Z]{0,2})\s*$", re.M)
    for m in row.finditer(text):
        name = m.group(1).strip()
        if name.lower() in ("test", "test name"):
            continue
        results.append(
            {
                "test_name": name,
                "value": m.group(2),
                "unit": m.group(3) or None,
                "reference_range": m.group(4) or None,
                "flag": m.group(5) or None,
            }
        )
    return {
        "patient": _patient(text),
        "ordering_provider": {
            "name": _find(text, r"Ordering (?:Provider|Physician)", r"(.+?)(?:\s{2,}NPI|$)"),
            "npi": _find(text, r"Ordering (?:Provider|Physician).*?NPI", r"(\d{10})"),
        },
        "lab_name": _find(text, r"(?:Performing )?Lab(?:oratory)?(?: Name)?", r"(.+)"),
        "accession_number": _find(text, r"Accession(?: No\.?| Number| #)?", r"([A-Z0-9-]+)"),
        "collected_at": _datetime(_find(text, r"Collected", r"([\d/ :-]+)")),
        "reported_at": _datetime(_find(text, r"Reported", r"([\d/ :-]+)")),
        "results": results,
    }


def extract_rx(text: str) -> dict[str, Any]:
    meds = []
    for block in re.split(r"\n(?=Rx\s*\d*\s*:)", text):
        name_line = _find(block, r"Rx\s*\d*")
        if not name_line or not re.match(r"Rx", block.strip(), re.I):
            continue
        m = re.match(r"(.+?)\s+(\d+(?:\.\d+)?\s*(?:mg|mcg|g|mL|units?)(?:/\d*\s*mL)?)\s*(.*)$", name_line, re.I)
        refills = _find(block, r"Refills", r"(\d+)")
        meds.append(
            {
                "name": m.group(1) if m else name_line,
                "strength": m.group(2) if m else None,
                "dosage_form": (m.group(3) or None) if m else None,
                "sig": _find(block, r"Sig"),
                "quantity": _find(block, r"(?:Qty|Quantity|Disp(?:ense)?)", r"(.+?)(?:\s{2,}|$)"),
                "refills": int(refills) if refills else None,
                "ndc": _find(block, r"NDC", r"([\d-]{10,13})"),
            }
        )
    sub = _find(text, r"Substitution(?: Allowed| Permitted)?", r"(Yes|No|Y|N)")
    return {
        "patient": _patient(text),
        "prescriber": {
            "name": _find(text, r"Prescriber", r"(.+?)(?:\s{2,}NPI|$)"),
            "npi": _find(text, r"Prescriber.*?NPI", r"(\d{10})") or _find(text, r"NPI", r"(\d{10})"),
            "phone": _find(text, r"Prescriber Phone", r"([\d() .-]{10,})"),
        },
        "dea_number": _find(text, r"DEA(?: #| No\.?| Number)?", r"([A-Z]{2}\d{7})"),
        "date_written": _date(_find(text, r"Date(?: Written)?", DATE)),
        "medications": meds,
        "substitution_allowed": None if sub is None else sub.upper().startswith("Y"),
    }


_EXTRACTORS = {
    "extract_cms1500_claim": extract_cms1500,
    "extract_lab_report": extract_lab,
    "extract_prescription": extract_rx,
}


class RulesProvider(LLMProvider):
    name = "rules"

    def _invoke(self, request: LLMRequest) -> LLMResponse:
        text = request.context.get("document_text", "")
        if request.tool_name == "classify_document":
            data = classify(text)
        elif request.tool_name in _EXTRACTORS:
            data = _EXTRACTORS[request.tool_name](text)
        else:
            raise LLMError(f"rules provider has no handler for tool {request.tool_name}")
        return LLMResponse(data=data, model="rules-v1")
