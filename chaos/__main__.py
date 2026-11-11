"""python -m chaos list | inject N | revert [N|all] | status | run N [--revert] | clean"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import health, load
from .scenarios import SPECS, inject, revert
from .util import ChaosError, alert_names, firing_alerts, load_state, psql, save_state

RUNS = Path(".data/chaos_runs.jsonl")
FENCE_LEAD_S = 90  # baseline the agent may see before the injection


def cmd_list(_: argparse.Namespace) -> None:
    print(f"{'id':>3}  {'name':<26} {'label':<22} expect alert")
    for sid, sp in SPECS.items():
        print(f"{sid:>3}  {sp.name:<26} {sp.label:<22} {', '.join(sorted(sp.expect)) or '(none)'}\n       {sp.summary}")


def reset_pg_stats() -> None:
    """pg_stat_statements remembers our own DDL (DROP INDEX, CREATE INDEX, ...). Clear it so the agent only sees real traffic."""
    try:
        psql("SELECT pg_stat_statements_reset()")
    except ChaosError:
        pass


def do_inject(sid: int, noise: bool = True) -> float:
    st = load_state()
    if st["active"]:
        raise ChaosError(f"scenarios already active: {sorted(st['active'])}; run `python -m chaos revert all` first")
    t0 = time.time()
    info = inject(sid, noise)
    reset_pg_stats()
    st = load_state()
    # the agent's tools will not show data older than the fence, so a run never sees the previous run's debris
    st["active"][str(sid)] = {"started": t0, "fence": t0 - FENCE_LEAD_S, **info}
    save_state(st)
    return t0


def do_revert(which: str) -> None:
    st = load_state()
    ids = list(st["active"]) if which == "all" else [which]
    for sid in ids:
        info = st["active"].get(str(sid), {})
        print(f"reverting scenario {sid} ...")
        revert(int(sid), info)
        st = load_state()
        st["active"].pop(str(sid), None)
        save_state(st)
    load.stop_all()
    load.start("noise")
    reset_pg_stats()


def cmd_inject(a: argparse.Namespace) -> None:
    do_inject(a.id, not a.no_noise)
    print(f"injected {a.id} ({SPECS[a.id].name}). Watch http://localhost:9090/alerts; revert with `python -m chaos revert {a.id}`")


def cmd_revert(a: argparse.Namespace) -> None:
    do_revert(a.id)
    bad = health.wait_clean(a.timeout)
    print("clean" if not bad else f"NOT CLEAN: {bad}")
    sys.exit(1 if bad else 0)


def cmd_status(_: argparse.Namespace) -> None:
    st = load_state()
    print("active:", {k: datetime.fromtimestamp(v["started"]).strftime("%H:%M:%S") for k, v in st["active"].items()} or "none")
    print("load running:", load.running() or "none")
    print("firing:", sorted((a["labels"]["alertname"], a["labels"].get("service", "")) for a in firing_alerts()) or "none")
    print("problems:", health.problems() or "none (clean)")


def wait_for_alert(sp, t0: float, timeout: float) -> tuple[bool, float | None, set[str]]:
    """Detected = any expected alert is firing (or, for no-fault scenarios, nothing fires for the whole window)."""
    end = time.time() + timeout
    seen: set[str] = set()
    while time.time() < end:
        names = alert_names()
        seen |= names
        if sp.expect and names & sp.expect:
            return True, time.time() - t0, seen
        time.sleep(5)
    return (not sp.expect and not seen), None, seen


def cmd_run(a: argparse.Namespace) -> None:
    sp = SPECS[a.id]
    bad = health.problems()
    if bad and a.id:
        print("stack is not clean, cleaning first:", bad[:3])
        do_revert("all")
        bad = health.wait_clean(300)
        if bad:
            sys.exit(f"cannot start clean: {bad}")
    if a.settle > 0:
        print(f"settling {a.settle:.0f}s so the agent's baseline window holds only normal traffic ...")
        time.sleep(a.settle)
    print(f"injecting {a.id} ({sp.name}): {sp.summary}")
    t0 = do_inject(a.id)
    print(f"  injected in {time.time() - t0:.0f}s; waiting up to {a.timeout}s for {sorted(sp.expect) or 'no alerts'}")
    ok, tta, seen = wait_for_alert(sp, time.time(), a.timeout)
    verdict = "PASS" if ok else "FAIL"
    print(f"{verdict}: scenario {a.id} " + (f"alert after {tta:.0f}s" if tta else "") + f" seen={sorted(seen)}")
    RUNS.parent.mkdir(exist_ok=True)
    with RUNS.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "scenario": a.id, "ok": ok, "time_to_alert_s": tta, "seen": sorted(seen)}) + "\n")
    if a.revert:
        do_revert(str(a.id))
        bad = health.wait_clean(300)
        print("reverted, clean" if not bad else f"reverted but NOT CLEAN: {bad}")
        if bad:
            ok = False
    sys.exit(0 if ok else 1)


def main() -> None:
    p = argparse.ArgumentParser(prog="chaos")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    i = sub.add_parser("inject")
    i.add_argument("id", type=int, choices=sorted(SPECS))
    i.add_argument("--no-noise", action="store_true")
    i.set_defaults(fn=cmd_inject)
    r = sub.add_parser("revert")
    r.add_argument("id", nargs="?", default="all")
    r.add_argument("--timeout", type=float, default=300)
    r.set_defaults(fn=cmd_revert)
    sub.add_parser("clean", help="same as revert all").set_defaults(fn=lambda a: cmd_revert(argparse.Namespace(id="all", timeout=300)))
    sub.add_parser("status").set_defaults(fn=cmd_status)
    ru = sub.add_parser("run", help="inject, wait for the alert, optionally revert and verify clean")
    ru.add_argument("id", type=int, choices=sorted(SPECS))
    ru.add_argument("--timeout", type=float, default=480)
    ru.add_argument("--revert", action="store_true")
    ru.add_argument("--settle", type=float, default=120, help="quiet seconds before injecting")
    ru.set_defaults(fn=cmd_run)
    a = p.parse_args()
    try:
        a.fn(a)
    except ChaosError as e:
        sys.exit(f"chaos error: {e}")


if __name__ == "__main__":
    main()
