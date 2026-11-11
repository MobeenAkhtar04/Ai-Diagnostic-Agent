# Real target stack (P1)

Everything the agent will investigate for real: three Python services, Postgres, Redis, and the telemetry stack.

```
shop_api (FastAPI)  --->  payment_stub (API key auth)
   |  \                       |
   |   redis (cache)          |
   +--- postgres <------------+---- worker (claims fulfilment jobs, calls shop_api /internal/fulfil with a JWT)
metrics -> Prometheus   logs -> Loki   traces -> Tempo   all viewable in Grafana (http://localhost:3000)
stack_exporter: container + Postgres metrics (reads the Docker API through a GET-only socket proxy)
```

## Run it

```bash
docker compose up -d --build            # first build takes a few minutes
docker compose run --rm seed            # seeds 1,000,000 orders (about a minute)
docker compose --profile load up -d loadgen   # steady traffic (about 15 users)

curl localhost:8080/products/SKU-5
curl -X POST localhost:8080/checkout -H 'content-type: application/json' \
  -d '{"customer_id":"cust-7","items":[{"sku":"SKU-5","qty":1,"price_cents":1500}]}'
```

| What | Where |
| --- | --- |
| shop API / payment stub / worker | :8080 / :8081 / :8082 (`/health`, `/metrics`) |
| Grafana (no login), Prometheus, Loki, Tempo | :3000, :9090, :3100, :3200 |
| Postgres (`shop/shop`, read only agent role `diag_ro/diag`) | :5432 |

Checks that P1 is done: open Grafana, Explore, Loki: `{service="shop_api"}`; click the `trace_id` link to open the trace in Tempo.
Then `docker compose stop postgres` and watch `http://localhost:9090/alerts`: HighErrorRate fires within about 2 minutes.

## Notes

- Postgres data is on a normal named volume (survives stop/start). The exporter reports database size against a virtual 1.5 GB quota (`PG_VOLUME_BYTES`), which the disk-full scenario fills.
- Metric names match the simulator's catalog (`http_error_ratio`, `http_latency_p95_seconds`, ...), produced by Prometheus
  recording rules in `infra/rules.yml`, so the agent's tools behave the same on the real stack.
- Services are limited to 512 MB / 2 CPUs (shop_api) so resource limit scenarios have something to change.
