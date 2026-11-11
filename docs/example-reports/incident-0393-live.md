# Incident #393: ContainerDown on redis

- **Alert:** ContainerDown (critical), redis is not running, at 2026-11-10 07:30 UTC
- **Status:** done
- **Model:** claude-haiku-5-5 | **steps:** 10 | **time to diagnosis:** 15.2s | **tokens:** 37300 in / 2520 out | **cost:** $0.0499

## Root cause: `cache_failure` (confidence 0.90)

Redis was stopped at about 07:30:29 (SIGTERM, clean exit 0, not OOM). shop_api's read-through cache (cache.py get_cached) calls redis.get with no try/except or fallback, so every cached endpoint now raises ConnectionError and returns 5xx; the shop_api error ratio climbed from about 1% to 35% within a minute. Postgres is idle and not the bottleneck, so the cache outage is the full cause.

## Evidence

| # | Claim | Proven by |
| --- | --- | --- |
| 1 | The redis container is not running: status exited, last event was a kill with signal 15 followed by exit code 0, and it was not OOM-killed. | [s1](#step-s1) |
| 2 | shop_api raised about 505 redis.exceptions.ConnectionError ('Name or service not known' / connection refused / reset) in get_cached, first seen at 07:30:29, right after Redis stopped. | [s5](#step-s5), [s1](#step-s1) |
| 3 | The shop_api 5xx error ratio rose from about 0.01 to 0.35 between 07:30:43 and 07:31:13. | [s4](#step-s4) |
| 4 | get_cached calls redis.get and redis.set with no exception handling or database fallback, so a Redis outage fails the request outright. | [s8](#step-s8) |
| 5 | Postgres is idle (cpu 0.01 cores, no restarts), and shop_api p95 latency stayed at about 0.09s, so the database and latency are not the cause; the failures are fast errors. | [s9](#step-s9), [s10](#step-s10) |
| 6 | No deploys or config changes were recorded in the last 60 minutes, so the Redis stop is an external or operational action that needs investigation. | [s6](#step-s6) |

## Ranked hypotheses

1. `cache_failure` (0.90)
2. `config_error` (0.05)
3. `dependency_down` (0.03)

## Fix

1. Restart redis (restart_service, needs approval) and confirm it is healthy and reachable from shop_api. 2. Find out who or what sent SIGTERM at about 07:30:29; no deploy explains it. 3. Patch shop_api get_cached to catch redis errors and fall through to the loader, so a cache outage degrades latency instead of causing 5xx. Add a short timeout on Redis calls. 4. Investigate the separate NoResultFound in render_receipt (receipts.py:11), which is not caused by the cache outage.

## Investigation timeline

<a id="step-s1"></a>
### s1 · `container_stats`(service="redis") · 150 ms · ok

<details><summary>result</summary>

```json
{
 "service": "redis",
 "running": false,
 "status": "exited",
 "cpu_cores": 0.0,
 "cpu_limit_cores": null,
 "cpu_throttled_pct": 0.0,
 "mem_rss_mb": 0.0,
 "mem_limit_mb": 268.4,
 "disk_used_pct": 0.0,
 "open_fds": null,
 "fd_limit": 1024,
 "restart_count": 0,
 "last_exit": {
  "reason": "stopped",
  "exit_code": 0,
  "minutes_before_alert": 0.9
 },
 "oom_killed": false,
 "started_minutes_before_alert": 36.6,
 "recent_events": [
  {
   "action": "kill",
   "minutes_before_alert": 0.9,
   "signal": "15"
  },
  {
   "action": "stop",
   "minutes_before_alert": 0.9
  },
  {
   "action": "die",
   "minutes_before_alert": 0.9,
   "exit_code": "0"
  }
 ]
}
```

</details>

<a id="step-s2"></a>
### s2 · `list_alerts`() · 5 ms · ok

<details><summary>result</summary>

```json
{
 "firing": [
  {
   "name": "ContainerDown",
   "severity": "critical",
   "labels": {
    "instance": "stack_exporter:9200",
    "job": "stack_exporter",
    "service": "redis"
   },
   "summary": "redis is not running",
   "started_at": "2026-11-10T07:30:48Z"
  }
 ]
}
```

</details>

<a id="step-s3"></a>
### s3 · `query_metrics`(promql="cache_hit_ratio{service=\"shop_api\"}", minutes=15) · 7 ms · ok

<details><summary>result</summary>

```json
{
 "query": "cache_hit_ratio{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:31:27Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:28:58",
     0.5592
    ],
    [
     "07:29:13",
     0.5608
    ],
    [
     "07:29:28",
     0.5466
    ],
    [
     "07:29:43",
     0.566
    ],
    [
     "07:29:58",
     0.5717
    ],
    [
     "07:30:13",
     0.5516
    ],
    [
     "07:30:28",
     0.5581
    ],
    [
     "07:30:43",
     0.568
    ],
    [
     "07:30:58",
     0.5306
    ],
    [
     "07:31:13",
     0.5652
    ]
   ],
   "first": 0.5591836734693877,
   "last": 0.5652173913043478,
   "min": 0.5306122448979592,
   "max": 0.5717344753747324
  }
 ],
 "note": null
}
```

</details>

<a id="step-s4"></a>
### s4 · `query_metrics`(promql="http_error_ratio{service=\"shop_api\"}", minutes=15) · 3 ms · ok

<details><summary>result</summary>

```json
{
 "query": "http_error_ratio{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T07:31:27Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:28:58",
     0.0096
    ],
    [
     "07:29:13",
     0.0118
    ],
    [
     "07:29:28",
     0.0172
    ],
    [
     "07:29:43",
     0.0184
    ],
    [
     "07:29:58",
     0.0183
    ],
    [
     "07:30:13",
     0.0129
    ],
    [
     "07:30:28",
     0.0097
    ],
    [
     "07:30:43",
     0.1015
    ],
    [
     "07:30:58",
     0.2242
    ],
    [
     "07:31:13",
     0.3546
    ]
   ],
   "first": 0.009615384615384616,
   "last": 0.35456353985029193,
   "min": 0.009615384615384616,
   "max": 0.35456353985029193
  }
 ],
 "note": null
}
```

</details>

<a id="step-s5"></a>
### s5 · `top_errors`(service="shop_api", minutes=10, limit=8) · 240 ms · ok

> Redis was stopped by SIGTERM (exit 0), not OOM-killed. Error ratio on shop_api is climbing fast. Checking what fails downstream and what changed.

<details><summary>result</summary>

```json
{
 "window_minutes": 10,
 "total_errors": 552,
 "signatures": [
  {
   "signature": "redis.exceptions.ConnectionError: Error -2 connecting to redis:6379. Name or service not known. @ services/shop_api/app/cache.py:12 in get_cached",
   "count": 505,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:30:29Z",
   "first_seen_minutes_before_alert": 0.9,
   "last_seen": "2026-11-10T07:31:29Z",
   "example_trace_id": "9bb3782429ee147c95475b2d3cbd0a94"
  },
  {
   "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
   "count": 38,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:29:00Z",
   "first_seen_minutes_before_alert": 2.4,
   "last_seen": "2026-11-10T07:31:26Z",
   "example_trace_id": "657ff2d2183e2005dfc64c1a148b5344"
  },
  {
   "signature": "redis.exceptions.ConnectionError: Connection closed by server. @ services/shop_api/app/cache.py:12 in get_cached",
   "count": 3,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:30:29Z",
   "first_seen_minutes_before_alert": 0.9,
   "last_seen": "2026-11-10T07:30:37Z",
   "example_trace_id": "7f41154fd0e412e1d8546a31683d821c"
  },
  {
   "signature": "redis.exceptions.ConnectionError: Error 111 connecting to redis:6379. Connect call failed ('172.21.0.2', 6379) @ services/shop_api/app/cache.py:12 in get_cached",
   "count": 1,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:30:29Z",
   "first_seen_minutes_before_alert": 0.9,
   "last_seen": "2026-11-10T07:30:29Z",
   "example_trace_id": "5ff01c77129d3c05aaa899f7353f0684"
  },
  {
   "signature": "redis.exceptions.ConnectionError: Error while reading from redis:6379 : (104, 'Connection reset by peer') @ services/shop_api/app/cache.py:12 in get_cached",
   "count": 1,
   "service": "shop_api",
   "first_seen": "2026-11-10T07:30:29Z",
   "first_seen_minutes_before_alert": 0.9,
   "last_seen": "2026-11-10T07:30:29Z",
   "example_trace_id": "24e043e646e349b60680b485bb5d8b31"
  }
 ]
}
```

</details>

<a id="step-s6"></a>
### s6 · `recent_changes`(since_minutes=60) · 9 ms · ok

> Redis was stopped by SIGTERM (exit 0), not OOM-killed. Error ratio on shop_api is climbing fast. Checking what fails downstream and what changed.

<details><summary>result</summary>

```json
{
 "since_minutes": 60,
 "changes": []
}
```

</details>

<a id="step-s7"></a>
### s7 · `query_metrics`(promql="db_queries_per_request{service=\"shop_api\"}", minutes=10) · 2 ms · ok

> Redis was stopped by SIGTERM (exit 0), not OOM-killed. Error ratio on shop_api is climbing fast. Checking what fails downstream and what changed.

<details><summary>result</summary>

```json
{
 "query": "db_queries_per_request{service=\"shop_api\"}",
 "window_minutes": 10,
 "end": "2026-11-10T07:31:30Z",
 "step": "~10s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:28:58",
     1.2692
    ],
    [
     "07:29:08",
     1.3144
    ],
    [
     "07:29:28",
     1.3402
    ],
    [
     "07:29:38",
     1.3373
    ],
    [
     "07:29:48",
     1.3581
    ],
    [
     "07:30:08",
     1.3914
    ],
    [
     "07:30:18",
     1.367
    ],
    [
     "07:30:38",
     1.3529
    ],
    [
     "07:30:48",
     1.2337
    ],
    [
     "07:30:58",
     1.1894
    ],
    [
     "07:31:18",
     1.1484
    ],
    [
     "07:31:28",
     1.1026
    ]
   ],
   "first": 1.269230769230769,
   "last": 1.1025849901031735,
   "min": 1.1025849901031735,
   "max": 1.3913978494623656
  }
 ],
 "note": null
}
```

</details>

<a id="step-s8"></a>
### s8 · `read_source`(path="services/shop_api/app/cache.py", start_line=1, end_line=30, sha=null) · 170 ms · ok

> Redis went down at about 07:30:29, and `shop_api` cache reads began failing at the same moment. I'll check whether the cache layer has a fallback and whether Postgres is absorbing the load.

<details><summary>result</summary>

```json
{
 "path": "services/shop_api/app/cache.py",
 "sha": "HEAD",
 "start_line": 1,
 "lines": [
  "   1  import json",
  "   2  import os",
  "   3  ",
  "   4  from redis.asyncio import Redis",
  "   5  ",
  "   6  from services.common import telemetry as t",
  "   7  ",
  "   8  redis = Redis.from_url(os.environ[\"REDIS_URL\"])",
  "   9  ",
  "  10  ",
  "  11  async def get_cached(key: str, loader, ttl_s: int = 60):",
  "  12      hit = await redis.get(key)",
  "  13      if hit is not None:",
  "  14          t.CACHE_OPS.labels(t.SERVICE, \"hit\").inc()",
  "  15          return json.loads(hit)",
  "  16      t.CACHE_OPS.labels(t.SERVICE, \"miss\").inc()",
  "  17      value = await loader()",
  "  18      await redis.set(key, json.dumps(value), ex=ttl_s)",
  "  19      return value"
 ]
}
```

</details>

<a id="step-s9"></a>
### s9 · `container_stats`(service="postgres") · 1726 ms · ok

> Redis went down at about 07:30:29, and `shop_api` cache reads began failing at the same moment. I'll check whether the cache layer has a fallback and whether Postgres is absorbing the load.

<details><summary>result</summary>

```json
{
 "service": "postgres",
 "running": true,
 "status": "running",
 "cpu_cores": 0.01,
 "cpu_limit_cores": null,
 "cpu_throttled_pct": 0.0,
 "mem_rss_mb": 20.1,
 "mem_limit_mb": 1073.7,
 "disk_used_pct": 19.4,
 "open_fds": null,
 "fd_limit": 1024,
 "restart_count": 0,
 "last_exit": null,
 "oom_killed": false,
 "started_minutes_before_alert": 1871.0,
 "recent_events": []
}
```

</details>

<a id="step-s10"></a>
### s10 · `query_metrics`(promql="http_latency_p95_seconds{service=\"shop_api\"}", minutes=10) · 5 ms · ok

> Redis went down at about 07:30:29, and `shop_api` cache reads began failing at the same moment. I'll check whether the cache layer has a fallback and whether Postgres is absorbing the load.

<details><summary>result</summary>

```json
{
 "query": "http_latency_p95_seconds{service=\"shop_api\"}",
 "window_minutes": 10,
 "end": "2026-11-10T07:31:34Z",
 "step": "~10s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "07:28:58",
     0.089
    ],
    [
     "07:29:08",
     0.0896
    ],
    [
     "07:29:28",
     0.0901
    ],
    [
     "07:29:38",
     0.0912
    ],
    [
     "07:29:48",
     0.0913
    ],
    [
     "07:30:08",
     0.0919
    ],
    [
     "07:30:18",
     0.0916
    ],
    [
     "07:30:38",
     0.0902
    ],
    [
     "07:30:48",
     0.09
    ],
    [
     "07:30:58",
     0.0894
    ],
    [
     "07:31:18",
     0.0904
    ],
    [
     "07:31:28",
     0.0898
    ]
   ],
   "first": 0.08902564102564102,
   "last": 0.08975369458128081,
   "min": 0.08902564102564102,
   "max": 0.09188311688311687
  }
 ],
 "note": null
}
```

</details>
