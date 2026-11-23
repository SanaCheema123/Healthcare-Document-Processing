"""Field-level accuracy evaluation against labelled ground truth.

    python scripts/evaluate.py                       # uses MEDX_LLM_PROVIDER (default: rules)
    MEDX_LLM_PROVIDER=bedrock python scripts/evaluate.py --out eval/report_bedrock.json

eval/manifest.json maps each sample file to its ground-truth JSON. Metrics:
  * precision / recall / F1 over leaf fields (exact match after normalisation)
  * straight-through-processing (STP) rate = share auto-approved
  * per-document latency and tokens
Run it on every prompt / model change and compare reports before shipping.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.deps import build_container  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.schemas import DocumentType  # noqa: E402


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{prefix}[{i}]")
    elif obj not in (None, "", []):
        yield prefix, str(obj).strip().lower()


def score(pred: dict | None, truth: dict) -> dict:
    p, t = dict(flatten(pred or {})), dict(flatten(truth))
    tp = sum(1 for k, v in p.items() if t.get(k) == v)
    fp, fn = len(p) - tp, sum(1 for k, v in t.items() if p.get(k) != v)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    wrong = sorted(k for k, v in t.items() if p.get(k) != v)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1, "mismatched_fields": wrong}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(ROOT / "eval/manifest.json"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    logging.disable(logging.CRITICAL)

    container = build_container(get_settings())
    manifest = json.loads(Path(args.manifest).read_text())
    rows, totals = [], {"tp": 0, "fp": 0, "fn": 0}
    for item in manifest:
        content = (ROOT / item["file"]).read_bytes()
        truth = json.loads((ROOT / item["truth"]).read_text())
        res = container.pipeline.process(str(uuid.uuid4()), Path(item["file"]).name, content)
        s = score(res.data, truth)
        for k in totals:
            totals[k] += s[k]
        rows.append({
            "file": item["file"],
            "type_correct": res.document_type == DocumentType(item["document_type"]),
            "status": res.status.value,
            "confidence": res.overall_confidence,
            "latency_ms": round(sum(res.timings_ms.values()), 1),
            **{k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()},
        })

    tp, fp, fn = totals["tp"], totals["fp"], totals["fn"]
    prec, rec = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    summary = {
        "llm_provider": container.pipeline.llm.name,
        "documents": len(rows),
        "classification_accuracy": round(sum(r["type_correct"] for r in rows) / len(rows), 3),
        "field_precision": round(prec, 4),
        "field_recall": round(rec, 4),
        "field_f1": round(2 * prec * rec / max(prec + rec, 1e-9), 4),
        **{k: v for k, v in container.pipeline.metrics.snapshot().items() if k in ("straight_through_rate", "avg_latency_ms", "llm_input_tokens", "llm_output_tokens")},
    }

    print(f"\n{'file':34} {'status':13} {'conf':>6} {'P':>6} {'R':>6} {'F1':>6}  mismatches")
    for r in rows:
        print(f"{r['file']:34} {r['status']:13} {r['confidence'] or 0:6.3f} {r['precision']:6.3f} {r['recall']:6.3f} {r['f1']:6.3f}  {', '.join(r['mismatched_fields'][:4])}")
    print("\nSUMMARY", json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "documents": rows}, indent=2))


if __name__ == "__main__":
    main()
