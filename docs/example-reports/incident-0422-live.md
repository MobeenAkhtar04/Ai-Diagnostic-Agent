# Incident #422: HighLatencyP95 on shop_api

- **Alert:** HighLatencyP95 (warning), p95 latency above 1s for 2m on shop_api, at 2026-11-11 06:08 UTC
- **Status:** done
- **Model:** claude-haiku-5-5 | **steps:** 9 | **time to diagnosis:** 15.6s | **tokens:** 39092 in / 2841 out | **cost:** $0.0533

## Root cause: `bad_deploy` (confidence 0.85)

Deploy 5a3837b added a per-checkout "fraud heuristic" that runs a catastrophic-backtracking regex (^(a+)+$) on a 24-character probe. Starting at 06:08, shop_api CPU went from about 0.1 to 2.0 cores against a 2.0-core limit, with about 88% of CPU periods throttled, and p95 latency rose from 0.09s to 2.6s, breaching the 1s SLO. The NoResultFound errors in render_receipt are a separate, unrelated symptom and do not explain the latency.

## Evidence

| # | Claim | Proven by |
| --- | --- | --- |
| 1 | p95 latency for shop_api jumped from about 0.09s to 1.5s at 06:08 and reached 2.6s by 06:10. | [s1](#step-s1) |
| 2 | The 5a3837b deploy went out at 06:07:59, about 2.3 minutes before the alert, and its change is 'checkout: add fraud heuristic before pricing'. | [s4](#step-s4) |
| 3 | The diff adds _risk_score, which runs re.match(r"^(a+)+$") on a 24-character string for every /checkout request, a pattern with catastrophic backtracking. | [s8](#step-s8) |
| 4 | shop_api CPU rose from about 0.1 cores to about 2.0 cores, which is its 2.0-core limit, and the cgroup throttled 88% of CPU periods. | [s6](#step-s6), [s7](#step-s7), [s3](#step-s3) |
| 5 | DB pool wait stayed flat at about 5-8ms, so the database and connection pool are not the bottleneck. | [s2](#step-s2) |
| 6 | The receipts NoResultFound errors are a separate signature on GET /receipts and do not appear in the checkout path changed by the deploy. | [s5](#step-s5), [s9](#step-s9) |

## Ranked hypotheses

1. `bad_deploy` (0.85)
2. `cpu_saturation` (0.10)
3. `resource_limit` (0.03)

## Fix

Roll back shop_api to 8d12421 now, or hot-patch by removing the _risk_score call from checkout(). Do not ship a regex with nested quantifiers like (a+)+. If the heuristic is needed, rewrite it without backtracking, add a timeout, and load-test it before rollout. Once the CPU is back under the limit, confirm that p95 returns to about 0.1s.

## Investigation timeline

<a id="step-s1"></a>
### s1 · `query_metrics`(promql="http_latency_p95_seconds{service=\"shop_api\"}", minutes=15) · 6 ms · ok

<details><summary>result</summary>

```json
{
 "query": "http_latency_p95_seconds{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-11T06:10:18Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "06:06:25",
     0.0937
    ],
    [
     "06:06:40",
     0.0945
    ],
    [
     "06:07:10",
     0.0921
    ],
    [
     "06:07:25",
     0.0938
    ],
    [
     "06:07:40",
     0.0926
    ],
    [
     "06:08:10",
     1.5382
    ],
    [
     "06:08:25",
     2.1108
    ],
    [
     "06:08:55",
     2.3272
    ],
    [
     "06:09:10",
     2.379
    ],
    [
     "06:09:25",
     2.3691
    ],
    [
     "06:09:55",
     2.3469
    ],
    [
     "06:10:10",
     2.6152
    ]
   ],
   "first": 0.0937439024390244,
   "last": 2.615196078431376,
   "min": 0.09188701923076922,
   "max": 2.615196078431376
  }
 ],
 "note": null
}
```

</details>

<a id="step-s2"></a>
### s2 · `query_metrics`(promql="db_pool_wait_seconds{service=\"shop_api\"}", minutes=15) · 2 ms · ok

<details><summary>result</summary>

```json
{
 "query": "db_pool_wait_seconds{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-11T06:10:18Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "06:06:25",
     0.008
    ],
    [
     "06:06:40",
     0.0079
    ],
    [
     "06:07:10",
     0.0075
    ],
    [
     "06:07:25",
     0.0079
    ],
    [
     "06:07:40",
     0.0079
    ],
    [
     "06:08:10",
     0.0078
    ],
    [
     "06:08:25",
     0.0054
    ],
    [
     "06:08:55",
     0.0048
    ],
    [
     "06:09:10",
     0.0048
    ],
    [
     "06:09:25",
     0.0048
    ],
    [
     "06:09:55",
     0.0048
    ],
    [
     "06:10:10",
     0.0048
    ]
   ],
   "first": 0.007995762711864399,
   "last": 0.00477944214876033,
   "min": 0.0047733529990167155,
   "max": 0.008049342105263148
  }
 ],
 "note": null
}
```

</details>

<a id="step-s3"></a>
### s3 · `query_metrics`(promql="container_cpu_throttled_ratio{service=\"shop_api\"}", minutes=15) · 1 ms · ok

<details><summary>result</summary>

```json
{
 "query": "container_cpu_throttled_ratio{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-11T06:10:18Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "06:06:25",
     0.0
    ],
    [
     "06:06:40",
     0.0
    ],
    [
     "06:07:10",
     0.0
    ],
    [
     "06:07:25",
     0.0
    ],
    [
     "06:07:40",
     0.0
    ],
    [
     "06:08:10",
     0.0
    ],
    [
     "06:08:25",
     0.0
    ],
    [
     "06:08:55",
     0.0
    ],
    [
     "06:09:10",
     0.8815
    ],
    [
     "06:09:25",
     0.8618
    ],
    [
     "06:09:55",
     0.8944
    ],
    [
     "06:10:10",
     0.8944
    ]
   ],
   "first": 0.0,
   "last": 0.8943661971830986,
   "min": 0.0,
   "max": 0.8943661971830986
  }
 ],
 "note": null
}
```

</details>

<a id="step-s4"></a>
### s4 · `recent_changes`(since_minutes=30) · 10 ms · ok

<details><summary>result</summary>

```json
{
 "since_minutes": 30,
 "changes": [
  {
   "type": "deploy",
   "service": "shop_api",
   "by": "ci",
   "minutes_before_alert": 2.3,
   "at": "2026-11-11T06:07:59Z",
   "git_sha": "5a3837b",
   "previous_sha": "8d12421",
   "detail": "checkout: add fraud heuristic before pricing"
  }
 ]
}
```

</details>

<a id="step-s5"></a>
### s5 · `top_errors`(service=null, minutes=15, limit=8) · 42 ms · ok

<details><summary>result</summary>

```json
{
 "window_minutes": 15,
 "total_errors": 57,
 "signatures": [
  {
   "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
   "count": 56,
   "service": "shop_api",
   "first_seen": "2026-11-11T06:06:27Z",
   "first_seen_minutes_before_alert": 3.8,
   "last_seen": "2026-11-11T06:10:13Z",
   "example_trace_id": "b8d0da47f5431ec7900653df360e198e"
  }
 ]
}
```

</details>

<a id="step-s6"></a>
### s6 · `container_stats`(service="shop_api") · 1548 ms · ok

> The latency jump lines up with CPU throttling (about 0.88 from 06:09) and the 5a3837b deploy. Next I'll check container limits and the deploy diff.

<details><summary>result</summary>

```json
{
 "service": "shop_api",
 "running": true,
 "status": "running",
 "cpu_cores": 1.97,
 "cpu_limit_cores": 2.0,
 "cpu_throttled_pct": 87.7,
 "mem_rss_mb": 146.5,
 "mem_limit_mb": 536.9,
 "disk_used_pct": 0.0,
 "open_fds": 52,
 "fd_limit": 1024,
 "restart_count": 0,
 "last_exit": null,
 "oom_killed": false,
 "started_minutes_before_alert": 2.3,
 "recent_events": []
}
```

</details>

<a id="step-s7"></a>
### s7 · `query_metrics`(promql="container_cpu_cores{service=\"shop_api\"}", minutes=15) · 2 ms · ok

> The latency jump lines up with CPU throttling (about 0.88 from 06:09) and the 5a3837b deploy. Next I'll check container limits and the deploy diff.

<details><summary>result</summary>

```json
{
 "query": "container_cpu_cores{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-11T06:10:21Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "06:06:25",
     0.0804
    ],
    [
     "06:06:40",
     0.0803
    ],
    [
     "06:07:10",
     0.104
    ],
    [
     "06:07:25",
     0.104
    ],
    [
     "06:07:40",
     0.0998
    ],
    [
     "06:08:10",
     0.1244
    ],
    [
     "06:08:25",
     0.1244
    ],
    [
     "06:08:55",
     2.0023
    ],
    [
     "06:09:10",
     2.0649
    ],
    [
     "06:09:25",
     2.0011
    ],
    [
     "06:09:55",
     1.9724
    ],
    [
     "06:10:10",
     1.9724
    ]
   ],
   "first": 0.08036173913043479,
   "last": 1.97239950310559,
   "min": 0.0803370508054523,
   "max": 2.064851385390428
  }
 ],
 "note": null
}
```

</details>

<a id="step-s8"></a>
### s8 · `git_diff`(from_sha="8d12421", to_sha="5a3837b", path=null) · 40 ms · ok

> The latency jump lines up with CPU throttling (about 0.88 from 06:09) and the 5a3837b deploy. Next I'll check container limits and the deploy diff.

<details><summary>result</summary>

```json
{
 "from": "8d12421",
 "to": "5a3837b",
 "diff": "diff --git a/services/shop_api/app/checkout.py b/services/shop_api/app/checkout.py\nindex 213d770..00fbe36 100644\n--- a/services/shop_api/app/checkout.py\n+++ b/services/shop_api/app/checkout.py\n@@ -1,3 +1,7 @@\n+import asyncio\n+import re\n+from concurrent.futures import ProcessPoolExecutor\n+\n from fastapi import APIRouter, Depends\n from sqlalchemy.ext.asyncio import AsyncSession\n \n@@ -10,10 +14,18 @@ from .pricing import price\n from .schemas import CheckoutRequest\n \n router = APIRouter()\n+_POOL = ProcessPoolExecutor(max_workers=2)\n+\n+\n+def _risk_score(customer_id: str) -> int:\n+    \"\"\"Rough fraud heuristic on the customer id.\"\"\"\n+    probe = \"a\" * 23 + \"!\"\n+    return 0 if re.match(r\"^(a+)+$\", probe) else len(customer_id)\n \n \n @router.post(\"/checkout\")\n async def checkout(req: CheckoutRequest, session: AsyncSession = Depends(get_session)):\n+    await asyncio.get_running_loop().run_in_executor(_POOL, _risk_score, req.customer_id)\n     total = await price(req.items, req.customer_id, flags.in_rollout(\"checkout_v2_pricing\", req.customer_id))\n     async with session.begin():\n         items = await reserve_inventory(session, req.items)\n"
}
```

</details>

<a id="step-s9"></a>
### s9 · `get_stack_trace`(signature="sqlalchemy.exc.NoResultFound: No row was found when one was required @ servi...) · 39 ms · ok

> The diff adds a regex with catastrophic backtracking (`^(a+)+$` on a 24-character input) that runs on every checkout. Before I submit, I'll check the receipts error to see whether it is related.

<details><summary>result</summary>

```json
{
 "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
 "occurrences": 57,
 "service": "shop_api",
 "stack": "rlette/middleware/base.py\", line 179, in call_next\n    raise app_exc from app_exc.__cause__ or app_exc.__context__\n  File \"/usr/local/lib/python3.12/site-packages/starlette/middleware/base.py\", line 149, in coro\n    await self.app(scope, receive_or_disconnect, send_no_error)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/middleware/exceptions.py\", line 63, in __call__\n    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/middleware/asyncexitstack.py\", line 18, in __call__\n    await self.app(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/routing.py\", line 683, in __call__\n    await self.middleware_stack(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 2811, in app\n    await route.handle(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 1330, in handle\n    await super().handle(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/routing.py\", line 289, in handle\n    await self.app(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 166, in app\n    await wrap_app_handling_exceptions(app, request)(scope, receive, send)\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 53, in wrapped_app\n    raise exc\n  File \"/usr/local/lib/python3.12/site-packages/starlette/_exception_handler.py\", line 42, in wrapped_app\n    await app(scope, receive, sender)\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 152, in app\n    response = await f(request)\n               ^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routing.py\", line 745, in app\n    raw_response = await run_endpoint_function(\n                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/fastapi/routi
... (truncated in report; full result in steps table)
```

</details>
