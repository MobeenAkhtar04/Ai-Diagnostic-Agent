"""No-tools baselines: one LLM call, no investigation loop.

mode "alert":   the model sees only the incident message (alert, service map, recent deploys).
mode "context": the same plus a fixed evidence dump we paste in for it (alerts, top errors per service,
                recent changes). This is the fair "just give a good LLM the obvious data" comparison.
The agent must beat both to justify the tool loop.
"""

from __future__ import annotations

import json
import re

from .agent.llm import LLM, cost_usd
from .agent.prompts import CATEGORY_LIST, incident_message
from .detect import pick_incident_alert
from .sim.scenarios import SCENARIOS, build_world
from .sim.world import SERVICE_MAP_YAML
from .taxonomy import RootCause
from .tools.backend import SimBackend, iso

SYSTEM = (
    "You are an on-call engineer. Classify the root cause of the incident in ONE answer, with no tools.\n"
    "category must be exactly one of these (name: meaning):\n" + CATEGORY_LIST + "\n"
    'Reply with only JSON: {"category": "...", "confidence": 0.0-1.0, "reason": "one sentence"}.'
)


def context_dump(world) -> str:
    be = SimBackend(world)
    parts = {"alerts": be.list_alerts(), "recent_changes_60m": be.recent_changes(60)}
    for svc in ("shop_api", "payment_stub", "worker"):
        parts[f"top_errors_{svc}_15m"] = be.top_errors(svc, 15, 8)
    return json.dumps(parts, indent=1)[:12000]


def parse_category(text: str) -> tuple[str, float]:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0)) if m else {}
        cat = str(obj.get("category", "")).strip()
        conf = float(obj.get("confidence", 0) or 0)
    except (ValueError, TypeError):
        return "unknown", 0.0
    return (cat if cat in {c.value for c in RootCause} else "unknown"), conf


def run_baseline(scenario_id: int, llm: LLM, mode: str, seed: int = 0) -> dict:
    sc = SCENARIOS[scenario_id]
    world = build_world(scenario_id, seed)
    alert = pick_incident_alert(world.alerts)
    row = {"scenario": scenario_id, "expected": sc.label, "got": "none", "confidence": 0.0,
           "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    if alert is None:
        return row
    deploys = [{"service": c["service"], "git_sha": c["git_sha"], "minutes_before_alert": c["minutes_before_alert"]}
               for c in world.changes if c["type"] == "deploy" and c["minutes_before_alert"] <= 24 * 60]
    msg = incident_message({**alert, "started_at_iso": iso(alert["started_at"])}, SERVICE_MAP_YAML, deploys)
    msg = msg.replace("Investigate and submit a diagnosis.", "Give your classification.")
    if mode == "context":
        msg += "\n\nEvidence collected for you (you cannot request more):\n```json\n" + context_dump(world) + "\n```"
    resp = llm.complete(SYSTEM, [{"role": "user", "content": msg}], [])
    got, conf = parse_category(resp.text)
    row.update(got=got, confidence=round(conf, 2), tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
               cost_usd=round(cost_usd(resp.tokens_in, resp.tokens_out), 5))
    return row
