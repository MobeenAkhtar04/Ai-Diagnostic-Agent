"""CLI: python -m diag {list,demo,eval}"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from .agent.llm import make_llm
from .runner import run_scenario
from .sim.scenarios import HELDOUT_IDS, MVP_IDS, SCENARIOS
from .store import Store
from .taxonomy import FAMILY
from .tools.gateway import StepResult

USE_COLOR = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def set_of(sid: int) -> str:
    return "heldout" if sid in HELDOUT_IDS else "mvp"


def pick_ids(spec: str, llm_name: str) -> list[int]:
    """--scenarios mvp | heldout | all | 1,2,3 (default: mvp for the mock LLM, all for a real one)."""
    spec = spec or ("mvp" if llm_name == "mock" else "all")
    if spec == "mvp":
        return list(MVP_IDS)
    if spec == "heldout":
        return list(HELDOUT_IDS)
    if spec == "all":
        return list(SCENARIOS)
    return [int(x) for x in spec.split(",")]


def cmd_list(_: argparse.Namespace) -> None:
    print(f"{'#':>3}  {'set':<8} {'family':<14} {'scenario':<34} label")
    for s in SCENARIOS.values():
        print(f"{s.id:>3}  {set_of(s.id):<8} {s.family:<14} {s.name:<34} {s.label}")


def _args_short(args: dict) -> str:
    s = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return s if len(s) < 80 else s[:77] + "..."


def cmd_demo(a: argparse.Namespace) -> None:
    llm = make_llm(a.llm, a.model)
    store = Store()
    sc = SCENARIOS[a.scenario]
    if not a.quiet:
        print(c(f"\n▶ Injecting scenario {sc.id}: {sc.name} ({sc.family})", "1"))
        print(f"  agent model: {llm.model}" + ("  (rule-based stand-in; use --llm anthropic|openai|ollama for a real LLM)"
                                              if a.llm == "mock" else ""))

    def on_step(r: StepResult, thought: str) -> None:
        if a.quiet:
            return
        mark = c("✓", "32") if r.ok else c("✗", "31")
        if thought:
            print(c(f"    💭 {thought.strip()[:160]}", "2"))
        line = f"  {mark} {c(r.step_id, '36')} {r.tool}({_args_short(r.args or {})}) {r.latency_ms:.0f}ms"
        print(line + (f"  {r.error}" if r.error else ""))

    def on_incident(world, alert) -> None:
        if a.quiet:
            return
        alerts = ", ".join(f"{x['name']}({x['labels']['service']}, {x['severity']})" for x in world.alerts)
        print(f"  alerts firing: {alerts}")
        print(c(f"  → incident opened for {alert['name']} on {alert['labels']['service']}; agent investigating\n", "1"))

    out = run_scenario(a.scenario, llm, store, a.seed, on_step=on_step, on_incident=on_incident)
    if out.alert is None:
        print(c("  No alert fired. No incident opened.", "33"))
        return
    d = out.run.diagnosis if out.run else None
    verdict = c("CORRECT", "32;1") if out.correct else c("WRONG", "31;1")
    conf = f"{d.confidence:.2f}" if d else "-"
    print(f"{verdict}  scenario {sc.id:>2} {sc.name:<22} expected={out.expected:<20} got={out.got:<20} "
          f"conf={conf} steps={len(out.run.steps) if out.run else 0} ttd={out.ttd_s:.2f}s")
    if not a.quiet and d:
        print(c("\n  Summary: ", "1") + d.summary)
        print(c("  Evidence:", "1"))
        for cl in d.claims:
            print(f"   - {cl.text}  [{', '.join(cl.step_ids)}]")
        if out.run and out.run.write_requests:
            print(c("  Awaiting approval: ", "33") + ", ".join(f"{w['tool']} {w['args']}" for w in out.run.write_requests))
        print(c("  Fix: ", "1") + d.fix)
    if not a.quiet:
        print(f"\n  Full report: {out.report_path}")


def cmd_eval(a: argparse.Namespace) -> None:
    llm = make_llm(a.llm, a.model)
    store = Store()
    ids = pick_ids(a.scenarios, a.llm)
    rows = []
    for sid in ids:
        for run_no in range(a.runs):
            o = run_scenario(sid, llm, store, seed=run_no)
            d = o.run.diagnosis if o.run else None
            hyps = [h.category.value for h in d.hypotheses] if d else []
            rows.append({
                "scenario": sid, "set": set_of(sid), "name": SCENARIOS[sid].name, "family": SCENARIOS[sid].family, "run": run_no,
                "expected": o.expected, "got": o.got, "correct": int(o.correct),
                "top3": int(o.expected in hyps or (o.expected == "none" and o.got == "none")),
                "confidence": round(d.confidence, 2) if d else 0.0,
                "steps": len(o.run.steps) if o.run else 0, "ttd_s": round(o.ttd_s, 3),
                "tokens_in": o.run.tokens_in if o.run else 0, "tokens_out": o.run.tokens_out if o.run else 0,
                "cost_usd": round(o.run.cost_usd, 5) if o.run else 0.0,
                "writes_requested": len(o.run.write_requests) if o.run else 0,
                "injection_followed": int(o.injection_followed),
            })
            print(f"  {'✓' if o.correct else '✗'} scenario {sid:>2} run {run_no}  expected={o.expected:<20} got={o.got}")

    out_dir = Path(".data/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results-{llm.model.replace('/', '_').replace(':', '_')}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    fail = [r for r in rows if r["expected"] != "none"]
    ctrl = [r for r in rows if r["expected"] == "none"]
    pct = lambda xs: f"{100 * sum(xs) / len(xs):.0f}%" if xs else "n/a"  # noqa: E731
    by_sc: dict[int, set] = {}
    for r in fail:
        by_sc.setdefault(r["scenario"], set()).add(r["got"])
    consistent = sum(1 for s in by_sc.values() if len(s) == 1)
    print("\n" + c(f"Results ({llm.model}, {a.runs} runs per scenario)", "1"))
    print(f"  Top 1 accuracy (failure scenarios) : {pct([r['correct'] for r in fail])}")
    for sname in ("mvp", "heldout"):
        sub = [r["correct"] for r in fail if r["set"] == sname]
        if sub and len({r["set"] for r in fail}) > 1:
            print(f"    top 1, {sname:<7} scenarios        : {pct(sub)}")
    print(f"  Top 3 accuracy                     : {pct([r['top3'] for r in fail])}")
    print(f"  False positives (controls)         : {sum(1 for r in ctrl if r['got'] != 'none')} of {len(ctrl)}")
    cw = [r for r in fail if not r["correct"] and r["confidence"] >= 0.7]
    print(f"  Confident wrong answers            : {len(cw)} of {len(fail)}")
    print(f"  Consistency (same answer each run) : {consistent} of {len(by_sc)} scenarios")
    ttd = sorted(r["ttd_s"] for r in rows)
    print(f"  Time to diagnosis median / p95     : {statistics.median(ttd):.2f}s / {ttd[int(0.95 * (len(ttd) - 1))]:.2f}s")
    print(f"  Tool calls per investigation (med) : {statistics.median(r['steps'] for r in rows):.0f}")
    print(f"  Tokens in per investigation (med)  : {statistics.median(r['tokens_in'] for r in rows):.0f}")
    print(f"  Cost per investigation (median)    : ${statistics.median(r['cost_usd'] for r in rows):.4f}")
    print(f"  Injection resistance               : {pct([1 - r['injection_followed'] for r in rows])}")
    wrong: dict[tuple[str, str], int] = {}
    for r in fail:
        if not r["correct"]:
            wrong[(r["expected"], r["got"])] = wrong.get((r["expected"], r["got"]), 0) + 1
    if wrong:
        print("  Mistakes (expected -> got)         : " + "; ".join(
            f"{e} -> {g} x{n}" for (e, g), n in sorted(wrong.items(), key=lambda kv: -kv[1])))
    fams: dict[str, list[int]] = {}
    for r in fail:
        fams.setdefault(FAMILY.get(r["expected"], "?"), []).append(r["correct"])
    print("  Accuracy by family                 : " + ", ".join(f"{k} {pct(v)}" for k, v in fams.items()))
    print(f"\n  CSV: {path}")


def cmd_baseline(a: argparse.Namespace) -> None:
    from .baseline import run_baseline

    llm = make_llm(a.llm, a.model)
    ids = pick_ids(a.scenarios, a.llm)
    rows = []
    for sid in ids:
        for run_no in range(a.runs):
            r = run_baseline(sid, llm, a.mode, seed=run_no)
            r["run"], r["set"], r["mode"] = run_no, set_of(sid), a.mode
            rows.append(r)
            print(f"  {'✓' if r['expected'] == r['got'] else '✗'} scenario {sid:>2} run {run_no}  "
                  f"expected={r['expected']:<20} got={r['got']}")
    out_dir = Path(".data/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"baseline-{a.mode}-{llm.model.replace('/', '_').replace(':', '_')}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    fail = [r for r in rows if r["expected"] != "none"]
    ctrl = [r for r in rows if r["expected"] == "none"]
    ok = sum(r["expected"] == r["got"] for r in fail)
    print("\n" + c(f"Baseline '{a.mode}' ({llm.model}, no tools, {a.runs} runs per scenario)", "1"))
    print(f"  Top 1 accuracy (failure scenarios) : {100 * ok / len(fail):.0f}%")
    print(f"  False positives (controls)         : {sum(1 for r in ctrl if r['got'] != 'none')} of {len(ctrl)}")
    print(f"  Cost per run (median)              : ${statistics.median(r['cost_usd'] for r in rows):.4f}")
    print(f"\n  CSV: {path}")


def cmd_realcheck(a: argparse.Namespace) -> None:
    """Call every tool once against the real stack and show what each returns (or why it failed)."""
    from .tools.backend import ToolError
    from .tools.real_backend import RealBackend

    be = RealBackend(repo_dir=Path.cwd())
    sha = a.sha
    calls = [
        ("query_metrics", lambda: be.query_metrics('http_latency_p95_seconds{service="shop_api"}', 5)),
        ("list_alerts", be.list_alerts),
        ("search_logs", lambda: be.search_logs("shop_api", None, None, 5, 3)),
        ("top_errors", lambda: be.top_errors(None, 15, 3)),
        ("find_slow_traces", lambda: be.find_slow_traces("shop_api", 0, 10, 3)),
        ("db_activity", be.db_activity),
        ("db_locks", be.db_locks),
        ("db_top_queries", lambda: be.db_top_queries("total_ms", 3)),
        ("container_stats", lambda: be.container_stats("shop_api")),
        ("recent_changes", lambda: be.recent_changes(1440)),
        ("get_config", lambda: be.get_config("shop_api")),
        ("read_source", lambda: be.read_source("services/shop_api/app/checkout.py", 1, 8, sha)),
    ]
    bad = 0
    for name, fn in calls:
        tr = None
        try:
            out = fn()
            print(c("✓ ", "32") + f"{name:<18} " + json.dumps(out, default=str)[:230])
            if name == "search_logs":
                old = [ln for ln in be.search_logs("shop_api", None, "POST /checkout", 5, 100)["lines"]
                       if ln.get("trace_id") and time.time() - datetime.fromisoformat(ln["ts"].replace("Z", "+00:00")).timestamp() > 30]
                tr = be.get_trace(old[0]["trace_id"]) if old else None
            if name == "search_logs" and tr:
                print(c("✓ ", "32") + f"{'get_trace':<18} {tr['root']} {tr['duration_ms']}ms, {len(tr['spans'])} spans")
        except ToolError as e:
            bad += 1
            print(c("✗ ", "31") + f"{name:<18} {e}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(c("✗ ", "31") + f"{name:<18} UNEXPECTED {type(e).__name__}: {e}")
    print(f"\n{len(calls) - bad} of {len(calls)} tools worked")


def _chaos_fence() -> float | None:
    """When a chaos scenario is active, hide everything older than its fence from the agent's tools."""
    try:
        active = json.loads(Path(".chaos/state.json").read_text()).get("active", {})
    except (OSError, ValueError):
        return None
    fences = [v["fence"] for v in active.values() if "fence" in v]
    return min(fences) if fences else None


def cmd_live(a: argparse.Namespace) -> None:
    """Wait for a firing alert in the real stack, then investigate it. --expect N scores it against chaos scenario N."""
    import httpx

    from .live import investigate_live, pick
    from .tools.real_backend import RealBackend

    llm = make_llm(a.llm, a.model)
    store = Store()
    be = RealBackend(".")
    end = time.time() + a.wait
    alert = None
    while time.time() < end:
        try:
            alert = pick(httpx.get(f"{be.prom}/api/v1/alerts", timeout=8).json()["data"]["alerts"])
        except httpx.HTTPError as e:
            print(c(f"prometheus unreachable: {type(e).__name__}", "31"))
        if alert:
            break
        time.sleep(5)
    if not alert:
        print(c("No alert is firing. No incident opened.", "33"))
        _record_live(a, got="none", alert=None, out=None)
        return
    print(c(f"\n▶ {alert['name']} on {alert['labels'].get('service')} ({alert['severity']}): {alert['summary']}", "1"))

    last_thought = [""]

    def on_step(r: StepResult, thought: str) -> None:
        mark = c("✓", "32") if r.ok else c("✗", "31")
        if thought and thought != last_thought[0]:
            last_thought[0] = thought
            print(c(f"    💭 {thought.strip()[:160]}", "2"))
        print(f"  {mark} {c(r.step_id, '36')} {r.tool}({_args_short(r.args or {})}) {r.latency_ms:.0f}ms" + (f"  {r.error}" if r.error else ""))

    be.t_ref = time.time()
    fence = _chaos_fence()
    if fence:
        be.not_before = fence
        print(c(f"  eval fence: tools see nothing before {datetime.fromtimestamp(fence).strftime('%H:%M:%S')} "
                "(the chaos run's baseline start)", "2"))
    out = investigate_live(alert, llm, store, be, label=f"live:{a.expect or 'unlabelled'}", on_step=on_step)
    d = out.run.diagnosis
    line = f"got={out.got} conf={d.confidence:.2f}" if d else f"got={out.got}"
    if a.expect:
        from .sim.scenarios import SCENARIOS as S
        exp = S[a.expect].label
        verdict = c("CORRECT", "32;1") if exp == out.got else c("WRONG", "31;1")
        line = f"{verdict}  expected={exp} {line}"
    print(f"{line} steps={len(out.run.steps)} ttd={out.ttd_s:.1f}s")
    if d:
        print(c("\n  Summary: ", "1") + d.summary)
        for cl in d.claims:
            print(f"   - {cl.text}  [{', '.join(cl.step_ids)}]")
        print(c("  Fix: ", "1") + d.fix)
    if out.run.write_requests:
        print(c("  Awaiting approval: ", "33") + ", ".join(f"{w['tool']} {w['args']}" for w in out.run.write_requests))
    print(f"\n  Full report: {out.report_path}")
    _record_live(a, got=out.got, alert=alert, out=out)


def _record_live(a: argparse.Namespace, got: str, alert: dict | None, out) -> None:
    if not getattr(a, "record", None):
        return
    from .sim.scenarios import SCENARIOS as S
    exp = S[a.expect].label if a.expect else None
    run = out.run if out else None
    d = run.diagnosis if run else None
    row = {"ts": time.time(), "scenario": a.expect, "expected": exp, "got": got, "correct": exp == got,
           "hypotheses": [h.category.value for h in d.hypotheses] if d else [],
           "confidence": d.confidence if d else None, "alert": alert["name"] if alert else None,
           "alert_service": alert["labels"].get("service") if alert else None,
           "steps": len(run.steps) if run else 0, "failed_steps": sum(1 for st in run.steps if not st.ok) if run else 0,
           "ttd_s": round(out.ttd_s, 1) if out else None, "tokens_in": run.tokens_in if run else 0,
           "tokens_out": run.tokens_out if run else 0, "cost_usd": round(run.cost_usd, 5) if run else 0.0,
           "write_requests": run.write_requests if run else [], "model": a.model or a.llm,
           "report": str(out.report_path) if out else None}
    p = Path(a.record)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(prog="diag", description="AI diagnostics agent (simulated stack)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenarios").set_defaults(fn=cmd_list)
    d = sub.add_parser("demo", help="inject one scenario and investigate it")
    d.add_argument("--scenario", type=int, default=11, choices=sorted(SCENARIOS))
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--quiet", action="store_true")
    d.set_defaults(fn=cmd_demo)
    e = sub.add_parser("eval", help="run every scenario N times and print the results table")
    e.add_argument("--runs", type=int, default=3)
    e.add_argument("--scenarios", default="", help="mvp | heldout | all | comma separated ids (default: mvp for mock, all for real LLMs)")
    e.set_defaults(fn=cmd_eval)
    lv = sub.add_parser("live", help="wait for a firing alert in the real stack and investigate it")
    lv.add_argument("--wait", type=float, default=180, help="seconds to wait for an alert")
    lv.add_argument("--expect", type=int, default=0, help="chaos scenario id, to score the diagnosis")
    lv.add_argument("--record", default=None, help="append a JSON result line to this file")
    lv.set_defaults(fn=cmd_live)
    rc = sub.add_parser("realcheck", help="call every tool once against the real Docker stack")
    rc.add_argument("--sha", default=None, help="commit to read source from (default: HEAD or latest deploy)")
    rc.set_defaults(fn=cmd_realcheck)
    b = sub.add_parser("baseline", help="no-tools LLM baseline for comparison with the agent")
    b.add_argument("--runs", type=int, default=3)
    b.add_argument("--scenarios", default="")
    b.add_argument("--mode", choices=["alert", "context"], default="context")
    b.set_defaults(fn=cmd_baseline)
    for sp in (d, e, b, lv):
        sp.add_argument("--llm", default="mock", choices=["mock", "anthropic", "openai", "ollama"])
        sp.add_argument("--model", default=None)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
