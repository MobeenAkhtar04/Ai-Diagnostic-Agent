"""Is the stack back to baseline? Used after every revert so the next run starts clean."""

from __future__ import annotations

import json
import time

import httpx

from . import load
from .util import DEFAULT_FLAGS, FLAGS_FILE, STATE_DIR, compose, load_state, prom_query, psql

SKIP_SERVICES = {"migrate", "seed", "loadgen"}


def _scalar(q: str) -> float | None:
    try:
        r = prom_query(q)
        v = float(r[0]["value"][1]) if r else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    return None if v is None or v != v else v


def problems() -> list[str]:
    out: list[str] = []
    try:
        alerts = httpx.get("http://localhost:9090/api/v1/alerts", timeout=8).json()["data"]["alerts"]
        out += [f"alert firing: {a['labels']['alertname']} {a['labels'].get('service', '')}" for a in alerts
                if a["state"] == "firing"]
    except httpx.HTTPError as e:
        out.append(f"prometheus unreachable: {type(e).__name__}")
    err = _scalar('http_error_ratio{service="shop_api"}')
    if err is not None and err > 0.03:
        out.append(f"shop_api error ratio {err:.3f}")
    p95 = _scalar('http_latency_p95_seconds{service="shop_api"}')
    if p95 is not None and p95 > 0.5:
        out.append(f"shop_api p95 {p95:.2f}s")
    try:
        flags = json.loads(FLAGS_FILE.read_text())
    except (OSError, ValueError):
        flags = {}
    if {**DEFAULT_FLAGS, **flags} != DEFAULT_FLAGS:
        out.append(f"flags changed: {flags}")
    if any(STATE_DIR.glob("*.env")):
        out.append("chaos env override file present")
    if load_state().get("interp"):
        out.append(f"chaos interpolation vars set: {sorted(load_state()['interp'])}")
    if load.running() and set(load.running()) != {"noise"}:
        out.append(f"load running: {load.running()}")
    try:
        for line in compose("ps", "-a", "--format", "json", check=False).stdout.splitlines():
            c = json.loads(line)
            if c["Service"] not in SKIP_SERVICES and c["State"] != "running":
                out.append(f"container {c['Service']} is {c['State']}")
        if psql("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_orders_customer_id'", check=False) != "1":
            out.append("index ix_orders_customer_id missing")
        if psql("SELECT count(*) FROM information_schema.columns WHERE table_name = 'orders' "
                "AND column_name = 'total_cents'", check=False) != "1":
            out.append("orders.total_cents missing (migration not reverted)")
        if psql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'nightly_inventory_sync'",
                check=False) not in ("0", ""):
            out.append("lock holder still connected")
        if psql("SELECT count(*) FROM deploys WHERE at > now() - interval '24 hours'", check=False) not in ("0", ""):
            out.append("deploy rows from the last 24h still present")
    except (OSError, ValueError, KeyError) as e:
        out.append(f"could not inspect containers: {type(e).__name__}")
    return out


def wait_clean(timeout: float = 300, poll: float = 10, verbose: bool = True) -> list[str]:
    """Returns [] when clean, otherwise the problems still present at timeout."""
    end = time.time() + timeout
    last: list[str] = ["not checked"]
    while time.time() < end:
        last = problems()
        if not last:
            return []
        if verbose:
            print("  waiting for clean state:", "; ".join(last[:3]))
        time.sleep(poll)
    return last
