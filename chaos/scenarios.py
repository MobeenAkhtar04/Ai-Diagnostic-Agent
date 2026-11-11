"""The injectable scenarios. Each maps a sim scenario id to a real fault in the Docker stack.

inject() returns a small info dict (kept in .chaos/state.json); revert(info) must restore the baseline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from diag.sim.scenarios import SCENARIOS

from . import deploy, heldout, load
from .util import (
    STUB,
    ChaosError,
    compose,
    psql,
    recreate,
    set_env_file,
    set_flags,
    wait_http,
)

LOCK_APP = "nightly_inventory_sync"


@dataclass
class Spec:
    id: int
    inject: Callable[[], dict]
    revert: Callable[[dict], None]
    expect: set[str]                      # any one of these alerts firing counts as detected; empty = no incident expected
    loads: list[str] = field(default_factory=list)
    summary: str = ""
    held_out: bool = False

    @property
    def name(self) -> str:
        return SCENARIOS[self.id].name

    @property
    def label(self) -> str:
        return SCENARIOS[self.id].label


# ---- 1 pool exhaustion: slow receipt rendering holds connections --------------------------------------
def _inj_pool() -> dict:
    set_env_file("shop_api", {"RECEIPT_RENDER_DELAY_S": "3", "DB_POOL_TIMEOUT_S": "5"})
    recreate("shop_api")
    return {}


def _rev_env(_: dict) -> None:
    set_env_file("shop_api", None)
    recreate("shop_api")


# ---- 2 missing index -----------------------------------------------------------------------------------
def _inj_index() -> dict:
    psql("DROP INDEX IF EXISTS ix_orders_customer_id")
    return {}


def _rev_index(_: dict) -> None:
    psql("CREATE INDEX IF NOT EXISTS ix_orders_customer_id ON orders (customer_id)", timeout=170)
    psql("ANALYZE orders")


# ---- 4 lock contention: an idle-in-transaction batch job holds the hot inventory row -----------------
def _inj_lock() -> dict:
    psql("BEGIN; UPDATE inventory SET qty = qty WHERE sku = 'SKU-HOT-1'; SELECT pg_sleep(3600);", app=LOCK_APP, detach=True)
    import time
    for _ in range(10):
        time.sleep(1)
        if psql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{LOCK_APP}'") != "0":
            return {}
    raise ChaosError("lock holder did not start")


def _rev_lock(_: dict) -> None:
    psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '{LOCK_APP}'")


# ---- 8 memory leak: a flag turns on an unbounded in-process cache ---------------------------------------
def _inj_leak() -> dict:
    set_flags(enable_recs_cache=True)
    return {}


def _rev_leak(_: dict) -> None:
    set_flags(enable_recs_cache=False)
    compose("restart", "shop_api")
    wait_http("http://localhost:8080/health", 60)


# ---- 15 / 16 payment latency / down -------------------------------------------------------------------
def _inj_pay_slow() -> dict:
    httpx.post(f"{STUB}/admin/behaviour", json={"delay_ms": 3000}, timeout=5).raise_for_status()
    return {}


def _rev_pay_slow(_: dict) -> None:
    httpx.post(f"{STUB}/admin/behaviour", json={"delay_ms": 40, "slow_rate": 0.0, "error_rate": 0.0}, timeout=5).raise_for_status()


def _inj_pay_down() -> dict:
    compose("stop", "payment_stub")
    return {}


def _rev_pay_down(_: dict) -> None:
    compose("start", "payment_stub")
    wait_http(f"{STUB}/health", 60)


# ---- 21 broken env var: the payment API key is wrong after a config change -----------------------------
def _inj_key() -> dict:
    from .util import load_state, save_state
    st = load_state()
    st["interp"]["CHAOS_PAYMENT_API_KEY"] = "pk_test_wrong"
    save_state(st)
    recreate("shop_api")
    return {}


def _rev_key(_: dict) -> None:
    from .util import load_state, save_state
    st = load_state()
    st["interp"].pop("CHAOS_PAYMENT_API_KEY", None)
    save_state(st)
    recreate("shop_api")


def _noop() -> dict:
    return {}


SPECS: dict[int, Spec] = {s.id: s for s in [
    Spec(1, _inj_pool, _rev_env, {"HighErrorRate", "HighLatencyP95"}, ["receipts"],
         "receipt rendering holds DB connections for 3 s; pool of 5 is exhausted"),
    Spec(2, _inj_index, _rev_index, {"HighLatencyP95"}, ["orders"], "DROP INDEX ix_orders_customer_id on a 1M row table"),
    Spec(4, _inj_lock, _rev_lock, {"HighErrorRate", "HighLatencyP95"}, ["hot_checkout"],
         "a batch job holds a row lock on the hot SKU inside an open transaction"),
    Spec(8, _inj_leak, _rev_leak, {"MemoryNearLimit", "ContainerRestarting"}, ["recs"],
         "enable_recs_cache flag turns on an unbounded in-process cache"),
    Spec(11, deploy.bad_deploy, deploy.rollback, {"HighErrorRate"}, [],
         "real commit that breaks checkout when shipping_address is missing, rebuilt and redeployed"),
    Spec(15, _inj_pay_slow, _rev_pay_slow, {"HighLatencyP95"}, [], "payment_stub answers in 3 s"),
    Spec(16, _inj_pay_down, _rev_pay_down, {"HighErrorRate", "ContainerDown"}, [], "payment_stub container stopped"),
    Spec(21, _inj_key, _rev_key, {"HighErrorRate"}, [], "shop_api starts with a wrong PAYMENT_API_KEY"),
    Spec(29, _noop, lambda _: None, set(), ["surge"], "healthy 5x traffic spike, nothing is broken"),
    Spec(30, _noop, lambda _: None, set(), [], "only background noise (404s, bad input)"),
    # ---- held-out (prompt never tuned on these) ----
    Spec(5, heldout.inj_deadlock, lambda _: None, {"HighErrorRate", "HighLatencyP95"}, ["bundle"],
         "bundle-promo carts lock two SKUs in opposite orders", held_out=True),
    Spec(19, heldout.inj_cache_down, heldout.rev_cache_down, {"HighErrorRate", "ContainerDown"}, [],
         "redis container stopped", held_out=True),
    Spec(22, heldout.inj_timeout_low, heldout.rev_timeout_low, {"HighErrorRate"}, [],
         "PAYMENT_TIMEOUT_MS changed to 30 ms (payments take ~40 ms)", held_out=True),
    Spec(23, heldout.inj_bad_migration, heldout.rev_bad_migration, {"HighErrorRate"}, [],
         "real Alembic migration renames orders.total_cents; app code still uses it", held_out=True),
    Spec(24, heldout.inj_rotated_key, heldout.rev_rotated_key, {"HighErrorRate"}, [],
         "payment provider rotated its API key; shop_api sends the old one", held_out=True),
    Spec(25, heldout.inj_flag, heldout.rev_flag, {"HighLatencyP95"}, ["checkout"],
         "checkout_v2_pricing flag on for 50% of customers; v2 pricing is slow", held_out=True),
    Spec(26, heldout.inj_mem_limit, heldout.rev_limits, {"ContainerRestarting", "MemoryNearLimit", "ContainerDown",
                                                          "HighErrorRate"}, [],
         "shop_api memory limit lowered to 96 MB", held_out=True),
    Spec(27, heldout.inj_cpu_limit, heldout.rev_limits, {"HighLatencyP95"}, [],
         "shop_api CPU quota lowered to 0.1 cores", held_out=True),
]}


def inject(sid: int, noise: bool = True) -> dict:
    sp = SPECS[sid]
    if noise:
        load.start("noise")
    info = sp.inject()
    for name in sp.loads:
        load.start(name)
    return info


def revert(sid: int, info: dict) -> None:
    sp = SPECS[sid]
    for name in sp.loads:
        load.stop(name)
    sp.revert(info)
