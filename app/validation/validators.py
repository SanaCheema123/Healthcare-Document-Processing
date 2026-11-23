"""Atomic healthcare-identifier validators (pure functions, no I/O)."""
from __future__ import annotations

import re

ICD10_CM = re.compile(r"^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
CPT = re.compile(r"^\d{4}[0-9FTU]$")  # Cat I (5 digits), Cat II (####F), Cat III (####T), PLA (####U)
HCPCS_II = re.compile(r"^[A-V]\d{4}$")
MODIFIER = re.compile(r"^[A-Z0-9]{2}$")
TAX_ID = re.compile(r"^\d{2}-?\d{7}$")
DEA = re.compile(r"^[A-Z][A-Z9]\d{7}$")
NDC = re.compile(r"^(\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d{1}|\d{5}-\d{4}-\d{2}|\d{11})$")
LOINC = re.compile(r"^\d{1,7}-\d$")
POS = re.compile(r"^\d{2}$")


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def is_valid_npi(npi: str | None) -> bool:
    """NPI = 10 digits, Luhn check over the '80840' card-issuer prefix + NPI."""
    if not npi or not re.fullmatch(r"[12]\d{9}", npi):
        return False
    return luhn_valid("80840" + npi)


def is_valid_icd10(code: str | None) -> bool:
    return bool(code) and bool(ICD10_CM.match(code.upper()))


def is_valid_procedure_code(code: str | None) -> bool:
    return bool(code) and bool(CPT.match(code) or HCPCS_II.match(code))


def is_valid_dea(dea: str | None, prescriber_last_name: str | None = None) -> tuple[bool, str | None]:
    if not dea or not DEA.match(dea):
        return False, "format must be 2 letters + 7 digits"
    d = [int(c) for c in dea[2:]]
    check = (d[0] + d[2] + d[4] + 2 * (d[1] + d[3] + d[5])) % 10
    if check != d[6]:
        return False, "check digit mismatch"
    if prescriber_last_name and dea[1] != "9" and dea[1] != prescriber_last_name.strip()[0].upper():
        return True, "second letter does not match prescriber last-name initial"
    return True, None


def parse_reference_range(rng: str | None) -> tuple[float | None, float | None]:
    if not rng:
        return None, None
    rng = rng.replace(" ", "")
    if m := re.fullmatch(r"([\d.]+)-([\d.]+)", rng):
        return float(m.group(1)), float(m.group(2))
    if m := re.fullmatch(r"<=?([\d.]+)", rng):
        return None, float(m.group(1))
    if m := re.fullmatch(r">=?([\d.]+)", rng):
        return float(m.group(1)), None
    return None, None


# Small illustrative list; production should use an RxNorm/NDC-backed DEA schedule lookup.
CONTROLLED_SUBSTANCES = {
    "oxycodone", "hydrocodone", "morphine", "fentanyl", "hydromorphone", "methadone", "codeine",
    "tramadol", "alprazolam", "lorazepam", "clonazepam", "diazepam", "zolpidem",
    "amphetamine", "methylphenidate", "lisdexamfetamine", "buprenorphine", "pregabalin",
}
