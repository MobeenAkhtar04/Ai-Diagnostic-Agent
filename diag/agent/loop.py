"""The bounded investigation loop."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..store import Store
from ..tools.gateway import Gateway, StepResult, tool_definitions
from ..tools.schemas import Diagnosis
from .llm import LLM, cost_usd
from .prompts import SYSTEM
from .validator import validate

MAX_STEPS = 15
MAX_SECONDS = 120
MAX_TOKENS_IN = 400_000
FORCE_AT = 3  # with this many steps left, stop exploring: only submit_diagnosis is offered
MAX_FORCED_TURNS = 3  # forced submit turns allowed (includes the validator's one repair turn)


@dataclass
class RunResult:
    status: str  # done | inconclusive
    diagnosis: Diagnosis | None
    steps: list[StepResult]
    thoughts: dict[str, str]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    seconds: float
    write_requests: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def investigate(llm: LLM, gateway: Gateway, store: Store, inv_id: int, first_message: str,
                on_step: Callable[[StepResult, str], None] | None = None) -> RunResult:
    tools = tool_definitions()
    messages: list[dict] = [{"role": "user", "content": first_message}]
    t0 = time.monotonic()
    tin = tout = 0
    thoughts: dict[str, str] = {}
    writes: list[dict] = []
    repaired = False
    problems: list[str] = []
    diagnosis: Diagnosis | None = None
    nudges = 0
    forced_turns = 0

    def finish(status: str) -> RunResult:
        cost = cost_usd(tin, tout)
        store.finish_investigation(inv_id, status, tin, tout, cost)
        return RunResult(status, diagnosis, list(gateway.results.values()), thoughts, tin, tout, cost,
                         time.monotonic() - t0, writes, problems)

    submit_only = [t for t in tools if t["name"] == "submit_diagnosis"]

    while True:
        elapsed = time.monotonic() - t0
        steps_left = MAX_STEPS - gateway.seq
        if elapsed > MAX_SECONDS or tin > MAX_TOKENS_IN or forced_turns >= MAX_FORCED_TURNS:
            problems.append("budget exhausted before a valid diagnosis")
            return finish("inconclusive")

        turn_tools = tools
        if steps_left <= FORCE_AT or elapsed > MAX_SECONDS * 0.75:
            # Out of room to explore. Offer only submit_diagnosis so the model must commit to its best answer.
            if forced_turns == 0:
                messages.append({"role": "user", "content": (
                    "Investigation budget is almost used up. Stop gathering evidence now and call "
                    "submit_diagnosis with your best-supported root cause. Cite only steps that succeeded. "
                    "If the evidence is thin, lower your confidence rather than skipping the submission.")})
            forced_turns += 1
            turn_tools = submit_only

        resp = llm.complete(SYSTEM, messages, turn_tools)
        tin += resp.tokens_in
        tout += resp.tokens_out
        calls = resp.tool_calls
        messages.append({"role": "assistant", "content": resp.text,
                         "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in calls]})
        if not calls:
            nudges += 1
            if nudges > 2:
                problems.append("model stopped calling tools")
                return finish("inconclusive")
            messages.append({"role": "user", "content": "Call a tool, or call submit_diagnosis to finish."})
            continue

        results = []
        for c in calls:
            if c.name == "submit_diagnosis":
                d, errs = validate(c.args, gateway.results)
                if resp.truncated:
                    d, errs = None, ["your reply was cut off before the diagnosis was complete. Resubmit a SHORTER "
                                     "one: at most 6 claims of one sentence each, and a fix under 80 words"]
                if d is not None:
                    diagnosis = d
                    results.append({"id": c.id, "name": c.name, "content": "accepted"})
                    continue
                problems.extend(errs)
                problems.append("rejected payload (first 1200 chars): " + json.dumps(c.args, default=str)[:1200])
                if repaired:
                    return finish("inconclusive")
                repaired = True
                results.append({"id": c.id, "name": c.name, "content":
                                "Diagnosis rejected by the evidence validator:\n- " + "\n- ".join(errs) +
                                "\nFix these and call submit_diagnosis again. This is your only repair turn."})
                continue
            res = gateway.run(c.name, c.args)
            thoughts[res.step_id] = resp.text
            if res.ok and isinstance(res.data, dict) and res.data.get("status") == "approval_required":
                writes.append({"tool": c.name, "args": c.args, "step_id": res.step_id})
            if on_step:
                on_step(res, resp.text)
            results.append({"id": c.id, "name": c.name, "content": res.content})
        if results and gateway.seq < MAX_STEPS:
            results[-1]["content"] += (f"\n[Budget: {MAX_STEPS - gateway.seq} of {MAX_STEPS} tool steps left. "
                                       "Submit once the root cause is supported by evidence.]")
        messages.append({"role": "tool", "results": results})
        if diagnosis is not None:
            return finish("done")


def diagnosis_json(d: Diagnosis | None) -> dict:
    return json.loads(d.model_dump_json()) if d else {"category": "unknown", "confidence": 0.0, "claims": []}
