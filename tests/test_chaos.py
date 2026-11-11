"""Pure-logic tests for the chaos tooling (Docker itself is exercised on the user's machine)."""

import subprocess

import pytest

from chaos import deploy
from chaos.scenarios import SPECS
from diag.live import pick, to_incident_alert
from diag.sim.scenarios import SCENARIOS
from diag.tools.real_backend import RealBackend

CHECKOUT_SRC = '''def build_response(order, payment, body: dict) -> dict:
    shipping = body.get("shipping_address") or {}
    return {
        "order_id": order.id,
        "ship_to": shipping.get("city", "pickup"),
    }
'''


def test_every_mvp_scenario_has_a_spec_with_matching_label():
    from diag.sim.scenarios import HELDOUT_IDS, MVP_IDS
    assert set(MVP_IDS) <= set(SPECS)
    held = {sid for sid, sp in SPECS.items() if sp.held_out}
    assert held <= set(HELDOUT_IDS) and not held & set(MVP_IDS)
    for sid, sp in SPECS.items():
        assert sp.label == SCENARIOS[sid].label
        assert (sp.label == "none") == (not sp.expect)


def test_bad_deploy_patch_changes_behaviour():
    patched = deploy.patch_text(CHECKOUT_SRC)
    ns: dict = {}
    exec(patched, ns)  # noqa: S102
    order = type("O", (), {"id": 1})()
    with pytest.raises(KeyError):
        ns["build_response"](order, {}, {})
    assert ns["build_response"](order, {}, {"shipping_address": {"city": "X"}})["ship_to"] == "X"


def test_bad_deploy_patch_refuses_unexpected_source():
    with pytest.raises(deploy.ChaosError):
        deploy.patch_text("def build_response(): pass\n")


def test_bad_deploy_and_revert_roundtrip_in_git(tmp_path, monkeypatch):
    def g(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    f = tmp_path / deploy.CHECKOUT
    f.parent.mkdir(parents=True)
    f.write_text(CHECKOUT_SRC)
    g("add", ".")
    g("commit", "-qm", "init")
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "_redeploy", lambda sha: None)
    monkeypatch.setattr(deploy, "psql", lambda *a, **k: "")
    info = deploy.bad_deploy()
    assert 'body["shipping_address"]["city"]' in f.read_text()
    deploy.rollback(info)
    assert f.read_text() == CHECKOUT_SRC


def test_prometheus_alert_conversion_and_pick():
    def mk(name, sev, svc, state="firing", at="2026-11-08T20:00:00Z"):
        return {"labels": {"alertname": name, "severity": sev, "service": svc}, "state": state, "activeAt": at,
                "annotations": {"summary": "s"}}
    a = to_incident_alert(mk("HighErrorRate", "critical", "shop_api"))
    assert a["labels"] == {"service": "shop_api"} and a["severity"] == "critical" and isinstance(a["started_at"], float)
    got = pick([mk("HighLatencyP95", "warning", "shop_api"), mk("HighErrorRate", "critical", "shop_api"),
                mk("ContainerDown", "critical", "payment_stub", state="pending")])
    assert got["name"] == "HighErrorRate"
    assert pick([mk("X", "warning", "a", state="pending")]) is None


def test_baseline_env_resolves_compose_defaults():
    be = RealBackend(".")
    env = be._baseline_env("shop_api")
    assert env["GIT_SHA"] == "a41f9c3"
    assert env["PAYMENT_API_KEY"] == "pk_live_a1b2c3"


def test_flag_rollout_is_deterministic_and_bounded(tmp_path, monkeypatch):
    import json

    from services.shop_api.app import flags
    f = tmp_path / "flags.json"
    monkeypatch.setattr(flags, "FLAG_FILE", str(f))
    f.write_text(json.dumps({"checkout_v2_pricing": 50}))
    hits = [flags.in_rollout("checkout_v2_pricing", f"cust-{i}") for i in range(2000)]
    assert 0.4 < sum(hits) / len(hits) < 0.6
    assert hits == [flags.in_rollout("checkout_v2_pricing", f"cust-{i}") for i in range(2000)]
    f.write_text(json.dumps({"checkout_v2_pricing": False}))
    assert not flags.in_rollout("checkout_v2_pricing", "cust-1")
    f.write_text(json.dumps({"checkout_v2_pricing": True}))
    assert flags.in_rollout("checkout_v2_pricing", "cust-1")


def test_heldout_migration_is_valid_alembic():
    from chaos.heldout import MIGRATION_SRC
    ns: dict = {}
    exec(MIGRATION_SRC.replace("from alembic import op", "op = None"), ns)  # noqa: S102
    assert ns["revision"] == "0002" and ns["down_revision"] == "0001"
