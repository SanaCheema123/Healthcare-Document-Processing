"""OCR-confusion repair for medical codes.

Scanned and faxed documents produce systematic character confusions
(I<->1, Z<->2, O<->0, S<->5, B<->8, E<->£). When a code fails its format check
and exactly ONE confusion-swap makes it valid, we propose the repaired value.

Repairs are never silent: each one is recorded as a warning issue with the
original value, and because the repaired value is not literally in the source
text, the grounding check keeps the document in the human-review queue. The
reviewer sees a pre-filled suggestion instead of a blank error.
"""
from __future__ import annotations

from itertools import product

from pydantic import BaseModel

from app.core.schemas import CMS1500Claim, ValidationIssue, ValidationSeverity
from app.validation.validators import ICD10_CM, is_valid_procedure_code

# position-aware substitutions
TO_LETTER = {"1": "I", "0": "O", "2": "Z", "5": "S", "8": "B", "6": "G", "£": "E", "€": "E", "7": "T"}
TO_DIGIT = {"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"}


def _icd_candidates(code: str) -> set[str]:
    code = code.strip().upper().replace(" ", "")
    if not code:
        return set()
    options = []
    for i, ch in enumerate(code):
        if ch == ".":
            options.append(["."])
        elif i == 0:  # ICD-10 position 1 is always a letter
            options.append([ch] + ([TO_LETTER[ch]] if ch in TO_LETTER else []))
        elif i == 1:  # position 2 is always a digit
            options.append([ch] + ([TO_DIGIT[ch]] if ch in TO_DIGIT else []))
        else:
            options.append([ch])
    return {"".join(p) for p in product(*options) if ICD10_CM.match("".join(p))}


def _cpt_candidates(code: str) -> set[str]:
    options = [[c] + ([TO_DIGIT[c]] if c in TO_DIGIT else []) for c in code.strip()[:4]] + [[c] for c in code.strip()[4:]]
    return {"".join(p) for p in product(*options) if is_valid_procedure_code("".join(p))}


def repair(model: BaseModel) -> list[ValidationIssue]:
    """Mutates `model` in place; returns the list of repairs made."""
    fixes: list[ValidationIssue] = []
    if isinstance(model, CMS1500Claim):
        for i, code in enumerate(model.diagnosis_codes):
            if ICD10_CM.match(code):
                continue
            cands = _icd_candidates(code)
            if len(cands) == 1:
                new = cands.pop()
                model.diagnosis_codes[i] = new
                fixes.append(_issue(f"diagnosis_codes[{i}]", code, new))
        for i, line in enumerate(model.service_lines):
            if line.cpt_hcpcs and not is_valid_procedure_code(line.cpt_hcpcs):
                cands = _cpt_candidates(line.cpt_hcpcs)
                if len(cands) == 1:
                    new = cands.pop()
                    fixes.append(_issue(f"service_lines[{i}].cpt_hcpcs", line.cpt_hcpcs, new))
                    line.cpt_hcpcs = new
    return fixes


def _issue(field: str, old: str, new: str) -> ValidationIssue:
    return ValidationIssue(
        field=field,
        rule="ocr_repair",
        message=f"auto-corrected likely OCR error {old!r} -> {new!r}; please confirm",
        severity=ValidationSeverity.WARNING,
    )
