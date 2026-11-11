"""Alert rules, evaluated over the metric series. Mirrors infra/alert_rules.yml for Prometheus (P1)."""

from __future__ import annotations

from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sim.world import World

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _series(w: World, name: str, **labels: str):
    for s in w.series:
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
            return s
    return None


def _held(points, pred, minutes: float) -> bool:
    n = max(1, int(minutes * 2) + 1)  # 30 s step
    tail = points[-n:]
    return all(pred(v) for _, v in tail)


def evaluate_alerts(w: World) -> list[dict]:
    alerts: list[dict] = []

    def fire(name: str, severity: str, service: str, summary: str) -> None:
        alerts.append({"name": name, "severity": severity, "labels": {"service": service},
                       "summary": summary, "started_at": w.t_alert})

    for svc in ["shop_api", "payment_stub", "worker"]:
        err = _series(w, "http_error_ratio", service=svc)
        if err and _held(err.points, lambda v: v > 0.05, 1):
            fire("HighErrorRate", "critical", svc, f"5xx ratio above 5% for 1m on {svc}")
        lat = _series(w, "http_latency_p95_seconds", service=svc)
        if lat and _held(lat.points, lambda v: v > 1.0, 2):
            fire("HighLatencyP95", "warning", svc, f"p95 latency above 1s for 2m on {svc}")
        rps = _series(w, "http_requests_per_second", service=svc)
        if rps:
            base = median(v for _, v in rps.points[:10])
            if base > 0 and _held(rps.points, lambda v, b=base: v > 2 * b, 2):
                fire("TrafficSurge", "info", svc, f"request rate above 2x baseline on {svc}")
    integ = _series(w, "db_integrity_errors_per_minute", service="shop_api")
    if integ and _held(integ.points, lambda v: v > 5, 2):
        fire("IntegrityErrors", "warning", "shop_api", "more than 5 integrity violations per minute for 2m")
    for svc in ["shop_api", "payment_stub", "worker", "postgres", "redis"]:
        rs = _series(w, "container_restarts_total", service=svc)
        if rs and rs.points[-1][1] - rs.points[-21][1] > 0:
            fire("ContainerRestarting", "warning", svc, f"{svc} restarted in the last 10m")
        rss = _series(w, "container_memory_rss_bytes", service=svc)
        lim = _series(w, "container_memory_limit_bytes", service=svc)
        if rss and lim and rss.points[-1][1] / max(lim.points[-1][1], 1) > 0.85:
            fire("MemoryNearLimit", "warning", svc, f"{svc} memory above 85% of limit")
    cutoff = w.ts(-5)
    for svc in ["shop_api", "worker", "payment_stub"]:
        warns = sum(1 for log in w.logs if log.service == svc and log.level == "WARN" and log.ts >= cutoff)
        if warns > 60:
            fire("WarnLogRateHigh", "info", svc, f"{warns} WARN lines in 5m on {svc}")

    alerts.sort(key=lambda a: -SEVERITY_RANK[a["severity"]])
    return alerts


def pick_incident_alert(alerts: list[dict]) -> dict | None:
    return alerts[0] if alerts else None
