# AI Diagnostics Agent

An LLM agent that investigates backend incidents the way an on-call engineer would. When an alert fires it queries metrics, logs, traces, Postgres internals, container state, config and source through 15 typed read-only tools, tests competing hypotheses, and submits a root-cause report in which every claim cites the tool call that proves it. It can propose fixes (restart a service, kill a query) but only as approval requests for a human; nothing executes on its own.

The model is Claude (Haiku 5.5 by default) called through the API. Nothing here is trained. The work is the agent loop, the tools and guardrails around it, a real fault-injection harness, and an evaluation that says honestly how well it works.

```
fault injected -> alert fires (Prometheus) -> incident opened -> agent loop (typed tools via gateway)
  -> submit_diagnosis -> evidence validator (cited steps must exist and succeed) -> Markdown report -> eval table
```

## Results

Every number below is from this repo's own harness: faults are injected into a live Docker stack (or a simulator), the agent never sees the injected label, and the answer is scored against it.

### Live Docker stack (real Postgres, Redis, Prometheus, Loki, Tempo)

**First run on 8 held-out faults, no prompt or taxonomy tuning beforehand: 83% top-1 (20 of 24 runs, 3 runs per fault).**
Top-3 88%, 1 confident wrong answer, all 24 alerts detected, all 24 reverts verified clean.
Median 15.8 s to a diagnosis, 11 tool calls, about $0.05 per investigation.

| Injected fault | Correct |
| --- | --- |
| Deadlock (carts lock two SKUs in opposite order) | 3/3 |
| Cache outage (Redis stopped) | 3/3 |
| Config error (payment timeout set to 30 ms) | 3/3 |
| Bad migration (real Alembic column rename; code still uses the old name) | 3/3 |
| Rotated credential (caller sends the old API key) | 3/3 |
| Feature-flag regression (slow code path behind a 50% rollout) | 3/3 |
| Memory limit too low | 2/3 |
| CPU throttling (0.1 core quota) | **0/3** |

The CPU-throttling failures were a real agent gap: it saw a full DB connection pool and an idle database and called the pool the cause, without checking the slow service's own CPU throttle. After seeing that, I added one prompt rule and tightened one taxonomy definition. A rerun of those faults went 12/12, but **those faults were then tuned on, so 12/12 is not a held-out number**; the 83% above is the clean one. A fresh set of held-out faults is next.

Earlier, on the 10 original scenarios (tuned on while building the stack): 82% top-1 (33 of 40), top-3 100%, and 0 false incidents in 10 runs with no fault injected.

### Simulated stack (28 failure scenarios, 3 runs each)

| System | Top-1 |
| --- | --- |
| Agent, 15-tool loop | **94%** (79/84) |
| No-tools LLM given an evidence dump (alerts, top errors, recent changes) | 76% |
| No-tools LLM given the alert text only | 10% |

The agent's first run on 20 of those scenarios, before any tuning on them, was 85%; 94% is after one round of fixes. The telemetry is simulated and cleaner than real data. **There is no no-tools baseline on the live stack yet**, so the 94% vs 76% comparison is simulator-only.

Raw run data, the exact tables, and example reports are in [`results/`](results/) and [`docs/example-reports/`](docs/example-reports/).

## How the agent works

- **Loop.** At most 15 tool steps, 120 seconds and a token cap per investigation. The model picks a tool, the gateway runs it, the result goes back, repeat. It ends by calling `submit_diagnosis`.
- **Tool gateway.** Every call is validated against a Pydantic schema, has a timeout, is output-capped at 6,000 characters, and is written to an audit table. Tool output is wrapped as untrusted data, because logs and source comments can contain text addressed to the model.
- **Evidence validator.** A diagnosis is rejected unless every step it cites exists and succeeded. One repair turn is allowed.
- **Approvals.** `restart_service` and `kill_query` only create approval rows. Across all evaluation runs, no write action was executed.
- **Taxonomy.** 27 categories, including `none` (no real failure) and `unknown`. Each has a definition that tells the model where the boundaries are, for example "a pool that fills because queries got slow is slow_query, not db_pool_exhaustion".

The 15 read tools cover alerts, metrics (PromQL), logs, error signatures, stack traces, slow traces, single traces, Postgres activity, locks and top queries (with plans), container stats and events, recent changes, config vs baseline, source files and git diffs. All tools talk to a `Backend` interface, with a simulated and a real implementation behind it.

## How it is evaluated

- **Real faults.** `python -m chaos run N` injects a fault into the live stack (stop Redis, rename a column with a real migration, rotate a key, lower a CPU quota, ship a real git commit), starts realistic traffic, and waits for a Prometheus alert.
- **No leaks.** An eval fence hides everything older than the run's baseline window, so one run cannot see the debris of the previous one. Faults are reverted and the stack is verified healthy between runs, and scenario order is shuffled.
- **Ground truth.** The injected scenario's label, which the agent never sees. A run is correct if the top-ranked root cause matches.
- **Held-out vs tuned.** Scenarios the prompt was never tuned on are reported separately from ones it was. The first run on a fresh set is the clean number; any rerun after fixing misses is labeled as tuned.

## Quickstart

Python 3.11+, no Docker needed for the simulator:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                       # add ONE provider key; set a spend cap in the provider console first

python -m diag list                        # all scenarios
python -m diag demo --scenario 11 --llm anthropic        # bad deploy, simulated: watch the investigation
python -m diag eval --runs 3 --llm anthropic             # simulated results table
pytest -q
```

Live stack (Docker Desktop, a few GB of RAM; details in [STACK.md](STACK.md)):

```bash
docker compose up -d --build
docker compose run --rm seed
docker compose --profile load up -d loadgen

python -m chaos list                                         # injectable faults
python -m chaos run 25 --revert                              # inject, wait for the alert, revert
python -m diag live --llm anthropic --expect 25              # wait for the alert and investigate it
python -m scripts.real_eval --runs 3 --set heldout --out .data/eval/run.jsonl
```

Reports land in `.data/reports/`. The default `--llm mock` is a rule-based stand-in for testing the plumbing for free; its scores say nothing about agent quality. Passwords and API keys in `docker-compose.yml` and the fault injector (including the `pk_live_...` strings) are fake local demo values.

## Layout

```
diag/
  taxonomy.py     root-cause enum and definitions
  detect.py       alert rules for the simulator
  sim/            simulated stack: scenarios, noise, decoys, a planted prompt-injection log line
  tools/          Backend protocol, SimBackend, RealBackend (Prometheus, Loki, Tempo, Postgres, Docker, git),
                  Pydantic schemas, gateway
  agent/          loop, LLM adapters (Anthropic, OpenAI-compatible, Ollama), validator, prompts
  live.py         turn a firing Prometheus alert into an incident and investigate it
chaos/            fault injector: scenarios, load profiles, real deploys and migrations, health checks
services/         the target system: shop_api, payment_stub, worker (FastAPI), shared telemetry
infra/            Prometheus rules, Loki, Tempo, Grafana, Postgres init, container/DB exporter
scripts/          real_eval.py (live eval harness), seed.py, verify_stack.py
results/          raw evaluation data (JSONL/CSV) and run logs
tests/            gateway, validator, approvals, injection, adapters, baselines, RealBackend, chaos
```

## Limitations

- One model family (Claude Haiku 5.5). No comparison yet against other models or a local one.
- 8 held-out faults, 3 runs each. Runs of the same fault are not independent, so 83% has a wide interval (roughly 63 to 95%).
- I wrote both the faults and the taxonomy, so the test is not adversarial. Faults are single-cause; real incidents often have several.
- The target is a three-service shop on a laptop, not a production system.
- Evidence validity (do the cited tool outputs really support each claim) has not been hand-audited yet.
- Prompt-injection resistance has only been exercised with one planted log line in the simulator; broader safety tests are planned.
- No patch-proposal sandbox or dashboard yet.

## Status

Done: simulator, live Docker stack, fault injector, live agent, evaluation harness. Next: a fresh held-out fault set (code-level faults such as N+1 queries and CPU hogs), a no-tools baseline on the live stack, a hand audit of cited evidence, then safety tests and a demo.
