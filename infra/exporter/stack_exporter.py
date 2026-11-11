"""Exports what cAdvisor and postgres_exporter would, for this stack only, on :9200.

* container metrics from the Docker API (read through a GET-only socket proxy): cpu, memory, limits,
  throttling, restart counts, OOM kills.  Labels: service (the compose service name).
* Postgres: backends by state (pg_connections), database size vs the volume size limit (disk used ratio),
  deadlocks and rolled back transactions.
"""

from __future__ import annotations

import asyncio
import os
import time

import asyncpg
import docker
from prometheus_client import Gauge, start_http_server

DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://docker-proxy:2375")
DSN = os.environ["PG_DSN"]
DISK_LIMIT = float(os.environ.get("PG_VOLUME_BYTES", str(1536 * 1024 * 1024)))
PROJECT = os.environ.get("COMPOSE_PROJECT", "")

MEM_RSS = Gauge("container_memory_rss_bytes", "RSS bytes", ["service"])
MEM_LIM = Gauge("container_memory_limit_bytes", "Memory limit bytes", ["service"])
CPU = Gauge("container_cpu_cores", "CPU cores in use", ["service"])
THROT = Gauge("container_cpu_throttled_ratio", "Fraction of CPU periods throttled", ["service"])
RESTARTS = Gauge("container_restarts_total", "Restart count", ["service"])
RUNNING = Gauge("container_running", "1 if running", ["service"])
OOM = Gauge("container_oom_killed", "1 if last exit was an OOM kill", ["service"])
PG_CONN = Gauge("pg_connections", "Postgres backends by state", ["state"])
DISK = Gauge("container_disk_used_ratio", "database size / volume size", ["service"])
DEADLOCKS = Gauge("pg_deadlocks_total", "Deadlocks detected")
LOCK_WAITERS = Gauge("pg_lock_waiters", "Sessions waiting on a lock")

_prev: dict[str, tuple[float, float, float, float]] = {}


def collect_containers(client: docker.DockerClient) -> None:
    for c in client.containers.list(all=True):
        svc = c.labels.get("com.docker.compose.service")
        if not svc or (PROJECT and c.labels.get("com.docker.compose.project") != PROJECT):
            continue
        c.reload()
        st = c.attrs["State"]
        RUNNING.labels(svc).set(1 if st.get("Running") else 0)
        RESTARTS.labels(svc).set(c.attrs.get("RestartCount", 0) + (0 if not st.get("Running") else 0))
        OOM.labels(svc).set(1 if st.get("OOMKilled") else 0)
        if not st.get("Running"):
            MEM_RSS.labels(svc).set(0)
            CPU.labels(svc).set(0)
            continue
        s = c.stats(stream=False)
        mem = s.get("memory_stats", {})
        rss = mem.get("stats", {}).get("anon", mem.get("usage", 0))
        MEM_RSS.labels(svc).set(rss)
        MEM_LIM.labels(svc).set(mem.get("limit", 0))
        cpu = s.get("cpu_stats", {})
        pre = s.get("precpu_stats", {})
        dt = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        ds = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or 1
        CPU.labels(svc).set(dt / ds * ncpu if ds > 0 else 0)
        th = cpu.get("throttling_data", {})
        prev = _prev.get(svc, (0, 0, 0, 0))
        periods, throttled = th.get("periods", 0), th.get("throttled_periods", 0)
        dp, dth = periods - prev[0], throttled - prev[1]
        THROT.labels(svc).set(dth / dp if dp > 0 else 0)
        _prev[svc] = (periods, throttled, 0, 0)


async def collect_pg() -> None:
    conn = await asyncpg.connect(DSN, timeout=3)
    try:
        for state in ("active", "idle", "idle in transaction"):
            PG_CONN.labels(state.replace(" ", "_")).set(0)
        for r in await conn.fetch("SELECT coalesce(state,'other') s, count(*) c FROM pg_stat_activity "
                                  "WHERE datname IS NOT NULL GROUP BY 1"):
            PG_CONN.labels(r["s"].replace(" ", "_")).set(r["c"])
        size = await conn.fetchval("SELECT sum(pg_database_size(datname)) FROM pg_database")
        DISK.labels("postgres").set(min(1.0, float(size) / DISK_LIMIT))
        DEADLOCKS.set(await conn.fetchval("SELECT coalesce(sum(deadlocks),0) FROM pg_stat_database"))
        LOCK_WAITERS.set(await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"))
    finally:
        await conn.close()


def main() -> None:
    start_http_server(9200)
    client = docker.DockerClient(base_url=DOCKER_HOST)
    while True:
        try:
            collect_containers(client)
        except Exception as e:  # noqa: BLE001
            print("container collect failed:", e, flush=True)
        try:
            asyncio.run(collect_pg())
        except Exception as e:  # noqa: BLE001
            print("pg collect failed:", e, flush=True)
            PG_CONN.labels("active").set(0)
        time.sleep(5)


if __name__ == "__main__":
    main()
