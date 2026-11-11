# Incident #397: HighLatencyP95 on shop_api

- **Alert:** HighLatencyP95 (warning), p95 latency above 1s for 2m on shop_api, at 2026-11-10 07:48 UTC
- **Status:** done
- **Model:** claude-haiku-5-5 | **steps:** 12 | **time to diagnosis:** 15.3s | **tokens:** 38949 in / 2490 out | **cost:** $0.0514

## Root cause: `resource_limit` (confidence 0.85)

An ops change at 07:48:13 cut shop_api's CPU quota from 2.0 to 0.1 cores. From 07:48:58 the container was throttled almost continuously (about 99-100%), event loop lag rose to about 2s, and p95 latency went from about 0.09s to about 4.6s. Requests queued behind the 5-connection DB pool, which filled as a symptom of CPU starvation; Postgres itself was idle (9 connections).

## Evidence

| # | Claim | Proven by |
| --- | --- | --- |
| 1 | The shop_api CPU quota was changed from 2.0 to 0.1 cores by ops at 07:48:13, about 2.7 minutes before the alert. | [s12](#step-s12) |
| 2 | shop_api is running at its 0.1-core CPU limit with 99.9% of CPU time throttled. | [s6](#step-s6) |
| 3 | container_cpu_throttled_ratio jumped from 0 to about 0.99-1.0 at 07:48:58 and stayed there. | [s10](#step-s10) |
| 4 | Event loop lag rose from about 3ms to 0.25-1.9s starting 07:48:43, and p95 latency rose from about 0.09s to about 4.6s over the same window. | [s11](#step-s11), [s2](#step-s2) |
| 5 | The DB pool checked-out count went to 5 of 5 at 07:48:43 while Postgres showed only 9 total connections, so the database was not the bottleneck. | [s7](#step-s7), [s5](#step-s5) |
| 6 | A NoResultFound error in render_receipt (29 occurrences) started before the throttling and is background noise, not the cause of the latency. | [s4](#step-s4) |

## Ranked hypotheses

1. `resource_limit` (0.85)
2. `db_pool_exhaustion` (0.08)
3. `cpu_saturation` (0.05)

## Fix

Restore shop_api's CPU quota to 2.0 cores (the value before the 07:48:13 change) and confirm throttling drops to 0 and p95 returns under 1s. Ops should review why the quota was lowered to 0.1 cores. Separately, investigate the NoResultFound in render_receipt (receipts.py:11), which is a code issue but not the cause of this latency incident.

## Investigation timeline

<a id="step-s1"></a>
### s1 · `list_alerts`() · 6 ms · ok

<details><summary>result</summary>

```json
{
 "firing": [
  {
   "name": "HighLatencyP95",
   "severity": "warning",
   "labels": {
    "service": "shop_api"
   },
   "summary": "p95 latency above 1s for 2m on shop_api",
   "started_at": "2026-11-10T07:48:53Z"
  }
 ]
}
```

</details>

<a id="step-s2"></a>
### s2 · `query_metrics`(promql="http_latency_p95_seconds{service=\"shop_api\"}", minutes=15) · 3 ms · ok

<details><summary>result</summary>

```json
{
 "query": "http_latency_p95_seconds{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:50:58Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:46:43",
     0.0899
    ],
    [
     "07:47:13",
     0.0891
    ],
    [
     "07:47:28",
     0.0898
    ],
    [
     "07:47:58",
     0.0921
    ],
    [
     "07:48:13",
     0.0911
    ],
    [
     "07:48:43",
     0.0894
    ],
    [
     "07:48:58",
     3.7097
    ],
    [
     "07:49:28",
     4.6742
    ],
    [
     "07:49:43",
     4.643
    ],
    [
     "07:50:13",
     4.7706
    ],
    [
     "07:50:28",
     4.6423
    ],
    [
     "07:50:58",
     4.5826
    ]
   ],
   "first": 0.0898732718894009,
   "last": 4.582568807339449,
   "min": 0.08914893617021273,
   "max": 4.770642201834861
  }
 ],
 "note": null
}
```

</details>

<a id="step-s3"></a>
### s3 · `query_metrics`(promql="db_pool_wait_seconds{service=\"shop_api\"}", minutes=15) · 2 ms · ok

<details><summary>result</summary>

```json
{
 "query": "db_pool_wait_seconds{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:50:58Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:46:43",
     0.0049
    ],
    [
     "07:47:13",
     0.0049
    ],
    [
     "07:47:28",
     0.0049
    ],
    [
     "07:47:58",
     0.0049
    ],
    [
     "07:48:13",
     0.0049
    ],
    [
     "07:48:43",
     0.0048
    ],
    [
     "07:48:58",
     1.6389
    ],
    [
     "07:49:28",
     2.4098
    ],
    [
     "07:49:43",
     2.3689
    ],
    [
     "07:50:13",
     2.3659
    ],
    [
     "07:50:28",
     2.2954
    ],
    [
     "07:50:58",
     2.2951
    ]
   ],
   "first": 0.004885268346111719,
   "last": 2.2950787401574804,
   "min": 0.004845573440643863,
   "max": 2.4098101265822782
  }
 ],
 "note": null
}
```

</details>

<a id="step-s4"></a>
### s4 · `top_errors`(service="shop_api", minutes=15, limit=8) · 23 ms · ok

<details><summary>result</summary>

```json
{
 "window_minutes": 15,
 "total_errors": 42,
 "signatures": [
  {
   "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
   "count": 29,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:46:44Z",
   "first_seen_minutes_before_alert": 4.2,
   "last_seen": "2026-11-10T07:50:52Z",
   "example_trace_id": "a4c77dfe5fa4b959c34da9213b3e0c52"
  }
 ]
}
```

</details>

<a id="step-s5"></a>
### s5 · `db_activity`() · 28 ms · ok

<details><summary>result</summary>

```json
{
 "total_connections": 9,
 "max_connections": 100,
 "by_state": {
  "idle": 5,
  "idle_in_transaction": 4
 },
 "by_application": {
  "worker": {
   "idle": 1
  },
  "payment_stub": {
   "idle": 3
  },
  "shop_api": {
   "idle_in_transaction": 4,
   "idle": 1
  }
 },
 "longest_non_idle": [
  {
   "pid": 85902,
   "application": "shop_api",
   "state": "idle in transaction",
   "wait_event": "Client:ClientRead",
   "query": "SELECT order_items.order_id, order_items.id, order_items.product_id, order_items.sku, order_items.qty, order_items.price_cents \nFROM order_items \nWHERE order_items.order_id IN ($1::INTEGER, $2::INTEGE",
   "duration_s": 0.18
  },
  {
   "pid": 85905,
   "application": "shop_api",
   "state": "idle in transaction",
   "wait_event": "Client:ClientRead",
   "query": "SELECT sku, name, price_cents FROM products WHERE sku = $1",
   "duration_s": 0.18
  },
  {
   "pid": 85903,
   "application": "shop_api",
   "state": "idle in transaction",
   "wait_event": "Client:ClientRead",
   "query": "SELECT orders.id, orders.customer_id, orders.total_cents, orders.status, orders.created_at \nFROM orders \nWHERE orders.customer_id = $1::VARCHAR \n LIMIT $2::INTEGER",
   "duration_s": 0.08
  },
  {
   "pid": 85901,
   "application": "shop_api",
   "state": "idle in transaction",
   "wait_event": "Client:ClientRead",
   "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1",
   "duration_s": 0.07
  }
 ]
}
```

</details>

<a id="step-s6"></a>
### s6 · `container_stats`(service="shop_api") · 1995 ms · ok

> Latency jumped at about 07:48:58, and pool wait rose with it while Postgres shows only 9 connections. Next I'll check shop_api's own resources, the receipt exception, and slow traces.

<details><summary>result</summary>

```json
{
 "service": "shop_api",
 "running": true,
 "status": "running",
 "cpu_cores": 0.1,
 "cpu_limit_cores": 0.1,
 "cpu_throttled_pct": 99.9,
 "mem_rss_mb": 88.4,
 "mem_limit_mb": 536.9,
 "disk_used_pct": 0.0,
 "open_fds": 43,
 "fd_limit": 1024,
 "restart_count": 0,
 "last_exit": null,
 "oom_killed": false,
 "started_minutes_before_alert": 2.7,
 "recent_events": []
}
```

</details>

<a id="step-s7"></a>
### s7 · `query_metrics`(promql="db_pool_checked_out{service=\"shop_api\"}", minutes=15) · 3 ms · ok

> Latency jumped at about 07:48:58, and pool wait rose with it while Postgres shows only 9 connections. Next I'll check shop_api's own resources, the receipt exception, and slow traces.

<details><summary>result</summary>

```json
{
 "query": "db_pool_checked_out{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:51:02Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:46:43",
     0.0
    ],
    [
     "07:46:58",
     0.0
    ],
    [
     "07:47:28",
     0.0
    ],
    [
     "07:47:43",
     0.0
    ],
    [
     "07:48:13",
     0.0
    ],
    [
     "07:48:43",
     5.0
    ],
    [
     "07:49:13",
     5.0
    ],
    [
     "07:49:28",
     5.0
    ],
    [
     "07:49:58",
     4.0
    ],
    [
     "07:50:13",
     4.0
    ],
    [
     "07:50:43",
     5.0
    ],
    [
     "07:50:58",
     4.0
    ]
   ],
   "first": 0.0,
   "last": 4.0,
   "min": 0.0,
   "max": 5.0
  }
 ],
 "note": null
}
```

</details>

<a id="step-s8"></a>
### s8 · `find_slow_traces`(service="shop_api", min_duration_ms=1000, minutes=15, limit=5) · 209 ms · ok

> Latency jumped at about 07:48:58, and pool wait rose with it while Postgres shows only 9 connections. Next I'll check shop_api's own resources, the receipt exception, and slow traces.

<details><summary>result</summary>

```json
{
 "traces_in_window": 500,
 "matched": 15,
 "traces": [
  {
   "trace_id": "0c20d136b805f2079b08006cc53a737e",
   "root": "GET /products/{sku}",
   "duration_ms": 4496.0,
   "error": false,
   "ts": "2026-11-10T07:49:19Z"
  },
  {
   "trace_id": "32fe8530de7e68f88d6ced9049110e90",
   "root": "POST /checkout",
   "duration_ms": 4197.0,
   "error": false,
   "ts": "2026-11-10T07:49:16Z"
  },
  {
   "trace_id": "0ee52ee90476a82ab574d39a900516ae",
   "root": "GET /receipts/{order_id}",
   "duration_ms": 4099.0,
   "error": false,
   "ts": "2026-11-10T07:49:21Z"
  },
  {
   "trace_id": "1815030e665c9c6ba7713441cd799916",
   "root": "POST /checkout",
   "duration_ms": 3300.0,
   "error": false,
   "ts": "2026-11-10T07:49:16Z"
  },
  {
   "trace_id": "39302a5a68be75dca6b2262734c2a05a",
   "root": "GET /orders",
   "duration_ms": 2797.0,
   "error": false,
   "ts": "2026-11-10T07:49:20Z"
  }
 ]
}
```

</details>

<a id="step-s9"></a>
### s9 · `get_stack_trace`(signature="sqlalchemy.exc.NoResultFound: No row was found when one was required @ servi...) · 16 ms · ok

> Latency jumped at about 07:48:58, and pool wait rose with it while Postgres shows only 9 connections. Next I'll check shop_api's own resources, the receipt exception, and slow traces.

<details><summary>result</summary>

```json
{
 "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
 "occurrences": 29,
 "service": "shop_api",
 "stack": "rlette/middleware/base.py\", line 179, in call_next\n    raise app_exc from app_exc.__cause__ or app_exc.__context__\n  File \"/usr/local/lib/python3.12/site-packages/starlette/middleware/base.py\", line 149, in coro\n    await self.app(scope, receive_or_disconnect, send_no_error)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/middleware/exceptions.py\", line 63, in __call__\n    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py\", line 18, in __call__\n    await self.app(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/routing.py\", line 683, in __call__\n    await self.middleware_stack(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 2811, in app\n    await route.handle(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 1330, in handle\n    await super().handle(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/routing.py\", line 289, in handle\n    await self.app(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 166, in app\n    await wrap_app_handling_exceptions(app, request)(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 152, in app\n    response = await f(request)\n               ^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 745, in app\n    raw_response = await run_endpoint_function(\n                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routi
... (truncated in report; full result in steps table)
```

</details>

<a id="step-s10"></a>
### s10 · `query_metrics`(promql="container_cpu_throttled_ratio{service=\"shop_api\"}", minutes=15) · 4 ms · ok

> The shop_api container is pinned at its CPU limit (0.1 cores, 99.9% throttled), which points to CPU starvation rather than a database problem. I'll confirm the throttling trend and check what changed.

<details><summary>result</summary>

```json
{
 "query": "container_cpu_throttled_ratio{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:51:04Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:46:43",
     0.0
    ],
    [
     "07:47:13",
     0.0
    ],
    [
     "07:47:28",
     0.0
    ],
    [
     "07:47:58",
     0.0
    ],
    [
     "07:48:13",
     0.0
    ],
    [
     "07:48:43",
     0.0
    ],
    [
     "07:48:58",
     0.993
    ],
    [
     "07:49:28",
     1.0
    ],
    [
     "07:49:43",
     1.0
    ],
    [
     "07:50:13",
     1.0
    ],
    [
     "07:50:28",
     1.0
    ],
    [
     "07:50:58",
     1.0
    ]
   ],
   "first": 0.0,
   "last": 1.0,
   "min": 0.0,
   "max": 1.0
  }
 ],
 "note": null
}
```

</details>

<a id="step-s11"></a>
### s11 · `query_metrics`(promql="event_loop_lag_seconds{service=\"shop_api\"}", minutes=15) · 3 ms · ok

> The shop_api container is pinned at its CPU limit (0.1 cores, 99.9% throttled), which points to CPU starvation rather than a database problem. I'll confirm the throttling trend and check what changed.

<details><summary>result</summary>

```json
{
 "query": "event_loop_lag_seconds{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:51:04Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:46:43",
     0.0048
    ],
    [
     "07:46:58",
     0.0033
    ],
    [
     "07:47:28",
     0.0024
    ],
    [
     "07:47:43",
     0.0033
    ],
    [
     "07:48:13",
     0.0032
    ],
    [
     "07:48:43",
     0.2473
    ],
    [
     "07:49:13",
     1.9424
    ],
    [
     "07:49:28",
     0.252
    ],
    [
     "07:49:58",
     0.1603
    ],
    [
     "07:50:13",
     0.6583
    ],
    [
     "07:50:43",
     0.1503
    ],
    [
     "07:50:58",
     0.1435
    ]
   ],
   "first": 0.004789167011040263,
   "last": 0.14349699999729637,
   "min": 0.0017833750025602058,
   "max": 1.9423688349925214
  }
 ],
 "note": null
}
```

</details>

<a id="step-s12"></a>
### s12 · `recent_changes`(since_minutes=30) · 17 ms · ok

> The shop_api container is pinned at its CPU limit (0.1 cores, 99.9% throttled), which points to CPU starvation rather than a database problem. I'll confirm the throttling trend and check what changed.

<details><summary>result</summary>

```json
{
 "since_minutes": 30,
 "changes": [
  {
   "type": "limit_change",
   "service": "shop_api",
   "by": "ops",
   "minutes_before_alert": 2.7,
   "at": "2026-11-10T07:48:13Z",
   "detail": "cpu quota 2.0 -> 0.1 cores"
  }
 ]
}
```

</details>
