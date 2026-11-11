"""Investigate a real incident: read firing alerts from Prometheus, run the agent against the RealBackend."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .agent.llm import LLM
from .agent.loop import RunResult, investigate
from .agent.prompts import incident_message
from .detect import SEVERITY_RANK
from .report import render
from .sim.world import SERVICE_MAP_YAML
from .store import Store
from .tools.backend import iso
from .tools.gateway import Gateway, StepResult
from .tools.real_backend import RealBackend


def to_incident_alert(a: dict) -> dict:
    """Prometheus /api/v1/alerts entry -> the alert dict the runner and report use."""
    lbl = a["labels"]
    active = datetime.fromisoformat(a["activeAt"].replace("Z", "+00:00")).timestamp()
    return {"name": lbl["alertname"], "severity": lbl.get("severity", "warning"),
            "labels": {k: v for k, v in lbl.items() if k not in ("alertname", "severity")},
            "summary": a.get("annotations", {}).get("summary", ""), "started_at": active}


def pick(alerts: list[dict]) -> dict | None:
    """Most severe first, then the one that started first."""
    firing = [to_incident_alert(a) for a in alerts if a["state"] == "firing"]
    firing.sort(key=lambda a: (-SEVERITY_RANK.get(a["severity"], 0), a["started_at"]))
    return firing[0] if firing else None


@dataclass
class LiveOutcome:
    alert: dict
    incident_id: int
    run: RunResult
    got: str
    report_path: Path
    ttd_s: float


def investigate_live(alert: dict, llm: LLM, store: Store, backend: RealBackend, label: str = "live",
                     out_dir: Path = Path(".data/reports"),
                     on_step: Callable[[StepResult, str], None] | None = None) -> LiveOutcome:
    t0 = time.monotonic()
    incident_id = store.create_incident(alert, label)
    inv_id = store.create_investigation(incident_id, llm.model)
    gateway = Gateway(backend, store, inv_id)
    deploys = [c for c in backend.recent_changes(24 * 60)["changes"] if c["type"] == "deploy"]
    msg = incident_message({**alert, "started_at_iso": iso(alert["started_at"])}, SERVICE_MAP_YAML,
                           [{"service": d["service"], "git_sha": d.get("git_sha"),
                             "minutes_before_alert": d["minutes_before_alert"]} for d in deploys])
    run = investigate(llm, gateway, store, inv_id, msg, on_step)
    got = run.diagnosis.category.value if run.diagnosis else "unknown"
    md = render(incident_id, alert, run, llm.model, store.steps(inv_id))
    if run.diagnosis:
        store.save_diagnosis(inv_id, run.diagnosis.model_dump(mode="json"), md)
    store.set_incident_status(incident_id, "diagnosed" if run.diagnosis else "inconclusive")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"incident-{incident_id:04d}-live.md"
    path.write_text(md)
    return LiveOutcome(alert, incident_id, run, got, path, time.monotonic() - t0)
