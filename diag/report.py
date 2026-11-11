"""Markdown incident report: every claim links to the step that proves it."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .agent.loop import RunResult


def _short(args: dict) -> str:
    s = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return s if len(s) < 90 else s[:87] + "..."


def render(incident_id: int, alert: dict, run: RunResult, model: str, steps_rows) -> str:
    d = run.diagnosis
    ts = datetime.fromtimestamp(alert["started_at"], UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Incident #{incident_id}: {alert['name']} on {alert['labels'].get('service')}", ""]
    out += [f"- **Alert:** {alert['name']} ({alert['severity']}), {alert['summary']}, at {ts}",
            f"- **Status:** {run.status}",
            f"- **Model:** {model} | **steps:** {len(run.steps)} | **time to diagnosis:** {run.seconds:.1f}s | "
            f"**tokens:** {run.tokens_in} in / {run.tokens_out} out | **cost:** ${run.cost_usd:.4f}", ""]
    if d:
        out += [f"## Root cause: `{d.category.value}` (confidence {d.confidence:.2f})", "", d.summary, "",
                "## Evidence", "", "| # | Claim | Proven by |", "| --- | --- | --- |"]
        for i, c in enumerate(d.claims, 1):
            links = ", ".join(f"[{s}](#step-{s})" for s in c.step_ids)
            out.append(f"| {i} | {c.text.replace('|', '/')} | {links} |")
        out += ["", "## Ranked hypotheses", ""]
        for i, h in enumerate(d.hypotheses, 1):
            out.append(f"{i}. `{h.category.value}` ({h.confidence:.2f})")
        out += ["", "## Fix", "", d.fix, ""]
    else:
        out += ["## Inconclusive", "", "The agent did not produce a valid diagnosis.", ""]
        out += [f"- {p}" for p in run.problems] + [""]
    if run.write_requests:
        out += ["## Actions awaiting human approval", ""]
        out += [f"- `{w['tool']}` {_short(w['args'])} (step {w['step_id']})" for w in run.write_requests] + [""]
    out += ["## Investigation timeline", ""]
    for r in steps_rows:
        sid = r["id"]
        args = json.loads(r["args_json"])
        status = "error: " + r["error"] if r["error"] else "ok"
        out += [f'<a id="step-{sid}"></a>', f"### {sid} · `{r['tool']}`({_short(args)}) · {r['latency_ms']:.0f} ms · {status}",
                ""]
        thought = run.thoughts.get(sid)
        if thought:
            out += [f"> {thought}", ""]
        body = r["result_json"]
        try:
            body = json.dumps(json.loads(body), indent=1)
        except json.JSONDecodeError:
            pass
        if len(body) > 2500:
            body = body[:2500] + "\n... (truncated in report; full result in steps table)"
        out += ["<details><summary>result</summary>", "", "```json", body, "```", "", "</details>", ""]
    return "\n".join(out)
