"""Real-stack eval: for each scenario x repeat, inject a real fault, let the agent investigate, revert, verify clean.

    python -m scripts.real_eval --runs 5 --llm anthropic            # all 10 scenarios, about 7 h
    python -m scripts.real_eval --runs 1 --scenarios 16,21           # quick smoke test
    python -m scripts.real_eval --runs 3 --set heldout               # held-out real faults (the clean number)
    python -m scripts.real_eval --summary .data/eval/real-*.jsonl    # print the table again

Resumable: rerun with the same --out and finished (scenario, repeat) pairs are skipped.
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

MVP = [1, 2, 4, 8, 11, 15, 16, 21, 29, 30]
HELDOUT = [5, 19, 22, 23, 24, 25, 26, 27]  # real faults for held-out scenarios (prompt never tuned on these)
SETS = {"mvp": MVP, "heldout": HELDOUT, "all": MVP + HELDOUT}


def sh(*args: str, timeout: float) -> int:
    print("$", " ".join(args), flush=True)
    try:
        return subprocess.run(args, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"  timed out after {timeout:.0f}s", flush=True)
        return 124


def done_pairs(out: Path) -> set[tuple[int, int]]:
    if not out.exists():
        return set()
    return {(r["scenario"], r["repeat"]) for r in map(json.loads, out.read_text().splitlines()) if "repeat" in r}


def run(a: argparse.Namespace) -> Path:
    ids = [int(x) for x in a.scenarios.split(",")] if a.scenarios else SETS[a.set]
    out = Path(a.out or f".data/eval/real-{datetime.now():%Y%m%d-%H%M}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jsonl")
    plan = [(sid, rep) for rep in range(1, a.runs + 1) for sid in ids]
    random.Random(a.seed).shuffle(plan)  # interleave scenarios so drift over the night does not favour any one
    skip = done_pairs(out)
    py = sys.executable
    for n, (sid, rep) in enumerate(plan, 1):
        if (sid, rep) in skip:
            continue
        print(f"\n===== [{n}/{len(plan)}] scenario {sid} repeat {rep} =====", flush=True)
        sh(py, "-m", "chaos", "revert", "all", timeout=600)
        inj = sh(py, "-m", "chaos", "run", str(sid), timeout=900)
        tmp.unlink(missing_ok=True)
        live = [py, "-m", "diag", "live", "--llm", a.llm, "--expect", str(sid), "--record", str(tmp),
                "--wait", "30" if sid in (29, 30) else "120"]
        if a.model:
            live += ["--model", a.model]
        sh(*live, timeout=600)
        rows = [json.loads(x) for x in tmp.read_text().splitlines()] if tmp.exists() else []
        row = rows[-1] if rows else {"scenario": sid, "got": "error", "correct": False}
        row.update({"repeat": rep, "alert_detected": inj == 0})
        rc = sh(py, "-m", "chaos", "revert", "all", timeout=900)
        row["reverted_clean"] = rc == 0
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"  -> {row.get('expected')} vs {row.get('got')}: {'CORRECT' if row.get('correct') else 'WRONG'}", flush=True)
    tmp.unlink(missing_ok=True)
    return out


def summary(paths: list[str]) -> None:
    rows = [json.loads(x) for p in paths for x in Path(p).read_text().splitlines() if x.strip()]
    if not rows:
        print("no rows")
        return
    fail = [r for r in rows if r.get("expected") != "none"]
    ctrl = [r for r in rows if r.get("expected") == "none"]
    by = defaultdict(list)
    for r in rows:
        by[r["scenario"]].append(r)
    print(f"\n{'id':>3} {'expected':<20} {'right':>6}  {'wrong answers':<34} {'alert':>6} {'conf':>5} {'steps':>5} {'ttd':>5}")
    for sid in sorted(by):
        rs = by[sid]
        wrong = sorted({r["got"] for r in rs if not r.get("correct")})
        conf = [r["confidence"] for r in rs if r.get("confidence") is not None]
        ttd = [r["ttd_s"] for r in rs if r.get("ttd_s")]
        print(f"{sid:>3} {rs[0].get('expected', '?'):<20} {sum(r.get('correct', False) for r in rs):>3}/{len(rs):<2}  "
              f"{', '.join(wrong) or '-':<34} {sum(r.get('alert_detected', False) for r in rs):>3}/{len(rs):<2} "
              f"{statistics.median(conf) if conf else 0:>5.2f} {statistics.median(r.get('steps', 0) for r in rs):>5.0f} "
              f"{statistics.median(ttd) if ttd else 0:>5.1f}")
    top1 = sum(r.get("correct", False) for r in fail)
    top3 = sum(r.get("expected") in (r.get("hypotheses") or [])[:3] for r in fail)
    confident_wrong = sum(1 for r in fail if not r.get("correct") and (r.get("confidence") or 0) >= 0.7)
    print(f"\nFailure runs: {len(fail)}   top-1 {top1}/{len(fail)} = {100 * top1 / max(1, len(fail)):.0f}%"
          f"   top-3 {100 * top3 / max(1, len(fail)):.0f}%   confident wrong (>=0.7) {confident_wrong}")
    print(f"Controls: {len(ctrl)} runs, false incidents {sum(1 for r in ctrl if r.get('got') != 'none')}")
    print(f"Alert detection: {sum(r.get('alert_detected', False) for r in rows)}/{len(rows)}   "
          f"clean revert: {sum(r.get('reverted_clean', False) for r in rows)}/{len(rows)}")
    inv = [r for r in fail if r.get("steps")]
    if inv:
        print(f"Median: {statistics.median(r['ttd_s'] for r in inv):.1f}s to diagnosis, "
              f"{statistics.median(r['steps'] for r in inv):.0f} tool calls, "
              f"{statistics.median(r['tokens_in'] for r in inv):.0f} input tokens, "
              f"${statistics.median(r['cost_usd'] for r in inv):.4f} per investigation")
    writes = [w for r in rows for w in r.get("write_requests") or []]
    print(f"Write actions requested (approval only, none executed): {len(writes)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--scenarios", default="", help="comma-separated ids (overrides --set)")
    p.add_argument("--set", default="mvp", choices=sorted(SETS))
    p.add_argument("--llm", default="anthropic")
    p.add_argument("--model", default=None)
    p.add_argument("--out", default=None, help="results file (rerun with the same file to resume)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--summary", nargs="*", default=None, help="only print the table for these result files")
    a = p.parse_args()
    if a.summary is not None:
        summary([f for g in a.summary for f in glob.glob(g)])
        return
    t0 = time.time()
    out = run(a)
    print(f"\nfinished in {(time.time() - t0) / 3600:.1f} h; results in {out}")
    summary([str(out)])


if __name__ == "__main__":
    main()
