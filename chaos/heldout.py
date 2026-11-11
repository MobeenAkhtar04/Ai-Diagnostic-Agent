"""Real faults for the held-out scenarios (the agent's prompt was never tuned on these).

Batch A: faults that need config, schema, traffic or limit changes only.
"""

from __future__ import annotations

from . import changes, deploy, load
from .util import ROOT, compose, load_state, psql, recreate, save_state, set_env_file, set_flags, wait_http

MIGRATION_PATH = "services/shop_api/alembic/versions/0002_rename_orders_total_cents.py"
MIGRATION_SRC = '''"""rename orders.total_cents to amount_cents (matches the payments service naming)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("orders", "total_cents", new_column_name="amount_cents")


def downgrade():
    op.alter_column("orders", "amount_cents", new_column_name="total_cents")
'''


def _interp(**kv: str | None) -> None:
    st = load_state()
    for k, v in kv.items():
        if v is None:
            st["interp"].pop(k, None)
        else:
            st["interp"][k] = v
    save_state(st)


# ---- 5 deadlock: bundle promo; checkout locks inventory rows in cart order, so A,B and B,A carts deadlock ----
def inj_deadlock() -> dict:
    return {}  # the fault is the traffic pattern (load profile "bundle") hitting a latent lock-ordering bug


# ---- 19 cache outage ------------------------------------------------------------------------------------
def inj_cache_down() -> dict:
    compose("stop", "redis")
    return {}


def rev_cache_down(_: dict) -> None:
    compose("start", "redis")
    wait_http("http://localhost:8080/health", 60)


# ---- 22 payment timeout set too low ------------------------------------------------------------------------
def inj_timeout_low() -> dict:
    cid = changes.record("config", "shop_api", "PAYMENT_TIMEOUT_MS 5000 -> 30", "ops")
    set_env_file("shop_api", {"PAYMENT_TIMEOUT_MS": "30"})
    recreate("shop_api")
    return {"change_id": cid}


def rev_timeout_low(info: dict) -> None:
    set_env_file("shop_api", None)
    recreate("shop_api")
    changes.delete(info.get("change_id"))


# ---- 23 bad migration: a real Alembic migration, committed and applied; the app code was not updated ------
def inj_bad_migration() -> dict:
    prev = deploy.short_head()
    path = ROOT / MIGRATION_PATH
    path.write_text(MIGRATION_SRC)
    deploy.git("add", MIGRATION_PATH)
    deploy.git("commit", "-m", "migration: rename orders.total_cents to amount_cents", "--", MIGRATION_PATH)
    sha = deploy.short_head()
    compose("build", "migrate", timeout=600)
    compose("run", "--rm", "migrate", timeout=300)
    cid = changes.record("migration", "shop_api", "0002_rename_orders_total_cents", "ci", git_sha=sha)
    return {"commit": sha, "previous": prev, "change_id": cid}


def rev_bad_migration(info: dict) -> None:
    psql("ALTER TABLE orders RENAME COLUMN amount_cents TO total_cents; UPDATE alembic_version SET version_num = '0001'",
         check=False)
    if info.get("commit"):
        deploy.git("revert", "--no-edit", info["commit"])
    compose("build", "migrate", timeout=600)  # so the next `compose up` does not re-apply 0002
    changes.delete(info.get("change_id"))


# ---- 24 rotated credential: the provider rotated its key, shop_api still sends the old one ------------------
def inj_rotated_key() -> dict:
    cid = changes.record("secret", "payment_stub", "PAYMENT_API_KEY rotated", "security")
    _interp(CHAOS_STUB_API_KEY="pk_live_r0t4t3d_9c1e")
    compose("up", "-d", "--no-deps", "payment_stub")
    wait_http("http://localhost:8081/health", 60)
    return {"change_id": cid}


def rev_rotated_key(info: dict) -> None:
    _interp(CHAOS_STUB_API_KEY=None)
    compose("up", "-d", "--no-deps", "payment_stub")
    wait_http("http://localhost:8081/health", 60)
    changes.delete(info.get("change_id"))


# ---- 25 feature flag regression: v2 pricing engine rolled out to 50% of customers -----------------------------
def inj_flag() -> dict:
    cid = changes.record("flag", "shop_api", "checkout_v2_pricing: off -> on (50% rollout)", "product")
    set_flags(checkout_v2_pricing=50)
    return {"change_id": cid}


def rev_flag(info: dict) -> None:
    set_flags(checkout_v2_pricing=False)
    changes.delete(info.get("change_id"))


# ---- 26 memory limit too low ----------------------------------------------------------------------------------
def inj_mem_limit() -> dict:
    cid = changes.record("limit_change", "shop_api", "memory limit 512MB -> 96MB", "ops")
    _interp(SHOP_API_MEM="96m")
    compose("up", "-d", "--no-deps", "shop_api")
    return {"change_id": cid}


# ---- 27 CPU throttling ------------------------------------------------------------------------------------------
def inj_cpu_limit() -> dict:
    cid = changes.record("limit_change", "shop_api", "cpu quota 2.0 -> 0.1 cores", "ops")
    _interp(SHOP_API_CPUS="0.1")
    compose("up", "-d", "--no-deps", "shop_api")
    return {"change_id": cid}


def rev_limits(info: dict) -> None:
    _interp(SHOP_API_MEM=None, SHOP_API_CPUS=None)
    recreate("shop_api")
    changes.delete(info.get("change_id"))


__all__ = ["load"]
