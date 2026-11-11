"""Evidence validator: a diagnosis is accepted only if every claim cites real, successful steps."""

from __future__ import annotations

import json

from pydantic import ValidationError

from ..tools.gateway import StepResult
from ..tools.schemas import Diagnosis


def _unstringify(raw: dict) -> dict:
    """Some models send list fields as a JSON string. Accept that; everything else is validated strictly."""
    out = dict(raw)
    for key in ("claims", "hypotheses"):
        if isinstance(out.get(key), str):
            try:
                out[key] = json.loads(out[key])
            except json.JSONDecodeError:
                pass
    return out


def validate(raw: dict, steps: dict[str, StepResult]) -> tuple[Diagnosis | None, list[str]]:
    raw = _unstringify(raw)
    try:
        d = Diagnosis.model_validate(raw)
    except ValidationError as e:
        return None, [f"schema: {err['loc']}: {err['msg']}" for err in e.errors(include_url=False)]
    problems: list[str] = []
    if d.category.value == "unknown":
        problems.append("category 'unknown' is reserved for inconclusive runs; pick a category or 'none'")
    if d.hypotheses[0].category != d.category:
        problems.append("hypotheses[0].category must equal category")
    for i, c in enumerate(d.claims):
        for sid in c.step_ids:
            if sid not in steps:
                problems.append(f"claim {i} cites {sid}, which does not exist")
            elif not steps[sid].ok:
                problems.append(f"claim {i} cites {sid}, which returned an error")
    for h in d.hypotheses:
        for sid in h.evidence_step_ids:
            if sid not in steps:
                problems.append(f"hypothesis {h.category.value} cites {sid}, which does not exist")
    return (d if not problems else None), problems
