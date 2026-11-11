"""Shared helpers: docker compose, psql, Prometheus, flags, the on-disk state file."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".chaos"
STATE_FILE = STATE_DIR / "state.json"
FLAGS_FILE = ROOT / "flags" / "flags.json"
DEFAULT_FLAGS = {"enable_recs_cache": False, "new_checkout_banner": False, "checkout_v2_pricing": False}
PROM = os.environ.get("PROM_URL", "http://localhost:9090")
API = os.environ.get("SHOP_API_URL", "http://localhost:8080")
STUB = os.environ.get("PAYMENT_STUB_URL", "http://localhost:8081")


class ChaosError(RuntimeError):
    pass


# ---- state -------------------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"active": {}, "interp": {}}


def save_state(st: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


# ---- docker compose -------------------------------------------------------------------------------
def compose(*args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run docker compose in the project. Interpolated chaos variables (GIT_SHA, ...) always come from the state file."""
    env = {**os.environ, **load_state().get("interp", {})}
    p = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise ChaosError(f"docker compose {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def psql(sql: str, *, app: str | None = None, detach: bool = False, check: bool = True, timeout: int = 120) -> str:
    args = ["exec", "-T"]
    if detach:
        args = ["exec", "-d"]
    if app:
        args += ["-e", f"PGAPPNAME={app}"]
    args += ["postgres", "psql", "-U", "shop", "-d", "shop", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql]
    return compose(*args, check=check, timeout=timeout).stdout.strip()


def set_env_file(service: str, values: dict[str, str] | None) -> None:
    """Override environment variables for a service via .chaos/<service>.env (None or empty removes the file)."""
    STATE_DIR.mkdir(exist_ok=True)
    f = STATE_DIR / f"{service}.env"
    if values:
        f.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    elif f.exists():
        f.unlink()


def recreate(service: str) -> None:
    compose("up", "-d", "--no-deps", service)
    wait_http(f"{API}/health" if service == "shop_api" else f"{STUB}/health", 60)


def wait_http(url: str, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise ChaosError(f"{url} did not become healthy within {timeout:.0f}s")


# ---- flags ---------------------------------------------------------------------------------------
def set_flags(**flags: bool) -> None:
    cur = {**DEFAULT_FLAGS}
    try:
        cur.update(json.loads(FLAGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    cur.update(flags)
    FLAGS_FILE.write_text(json.dumps(cur, indent=2) + "\n")


# ---- prometheus --------------------------------------------------------------------------------
def prom_query(q: str) -> list[dict]:
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=8).json()
    return r["data"]["result"]


def firing_alerts() -> list[dict]:
    data = httpx.get(f"{PROM}/api/v1/alerts", timeout=8).json()["data"]["alerts"]
    return [a for a in data if a["state"] == "firing"]


def alert_names(alerts: list[dict] | None = None) -> set[str]:
    return {a["labels"]["alertname"] for a in (firing_alerts() if alerts is None else alerts)}
