# Incident #380: HighErrorRate on shop_api

- **Alert:** HighErrorRate (critical), 5xx ratio above 5% for 1m on shop_api, at 2026-11-10 06:12 UTC
- **Status:** done
- **Model:** claude-haiku-5-5 | **steps:** 9 | **time to diagnosis:** 15.4s | **tokens:** 51625 in / 2557 out | **cost:** $0.0644

## Root cause: `deadlock` (confidence 0.85)

Concurrent POST /checkout requests reserve inventory by running one UPDATE per cart item, in the order the client sent the items, with no sorting. Two transactions that touch the same SKUs in opposite order deadlock, Postgres aborts one, and checkout returns 500. This produced about 124 deadlock errors in 15 minutes and pushed the 5xx ratio to about 5-6%, above the 5% SLO.

## Evidence

| # | Claim | Proven by |
| --- | --- | --- |
| 1 | shop_api logged 124 'deadlock detected' errors in reserve_inventory (inventory.py:7) in the 15-minute window, starting about 1.8 minutes before the alert. | [s2](#step-s2), [s6](#step-s6) |
| 2 | The 5xx ratio rose from about 0.4% to about 5-6% and crossed the 5% alert threshold at about 06:12-06:13. | [s3](#step-s3) |
| 3 | db_locks shows the blocking chain is entirely shop_api sessions (pids 62524, 62525, 62526, 62527, 62576) all running the same inventory UPDATE, with 8 deadlocks in the last 15 minutes. | [s4](#step-s4) |
| 4 | reserve_inventory issues one UPDATE per item in the caller's item order with no sort by SKU, so concurrent carts that share SKUs in different orders acquire row locks in opposite order. | [s5](#step-s5), [s7](#step-s7) |
| 5 | No deploys or config changes were recorded in the last 3 hours, so the trigger is the existing code under the current traffic mix. | [s8](#step-s8) |
| 6 | The failing POST /checkout trace returned 500 after about 1 second, which fits a transaction aborted after lock waits. | [s9](#step-s9) |

## Ranked hypotheses

1. `deadlock` (0.85)
2. `lock_contention` (0.10)
3. `db_pool_exhaustion` (0.03)

## Fix

In services/shop_api/app/inventory.py, sort items by sku before the loop in reserve_inventory so every transaction locks rows in the same global order. Optionally add a bounded retry for deadlock errors (SQLSTATE 40P01) at the checkout transaction level. Also investigate the separate NoResultFound in receipts.py:11 render_receipt (30 errors), which is unrelated to the deadlock.

## Investigation timeline

<a id="step-s1"></a>
### s1 · `list_alerts`() · 6 ms · ok

<details><summary>result</summary>

```json
{
 "firing": [
  {
   "name": "HighErrorRate",
   "severity": "critical",
   "labels": {
    "service": "shop_api"
   },
   "summary": "5xx ratio above 5% for 1m on shop_api",
   "started_at": "2026-11-10T06:12:53Z"
  }
 ]
}
```

</details>

<a id="step-s2"></a>
### s2 · `top_errors`(service="shop_api", minutes=15, limit=8) · 79 ms · ok

<details><summary>result</summary>

```json
{
 "window_minutes": 15,
 "total_errors": 158,
 "signatures": [
  {
   "signature": "sqlalchemy.exc.DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) deadlock detected @ services/shop_api/app/inventory.py:7 in reserve_inventory",
   "count": 124,
   "service": "shop_api",
   "first_seen": "2026-11-10T06:12:14Z",
   "first_seen_minutes_before_alert": 1.8,
   "last_seen": "2026-11-10T06:14:01Z",
   "example_trace_id": "4f55edf43bf77b79a3cfec30fd6c9aeb"
  },
  {
   "signature": "sqlalchemy.exc.NoResultFound: No row was found when one was required @ services/shop_api/app/receipts.py:11 in render_receipt",
   "count": 30,
   "service": "shop_api",
   "first_seen": "2026-11-10T06:10:48Z",
   "first_seen_minutes_before_alert": 3.2,
   "last_seen": "2026-11-10T06:13:57Z",
   "example_trace_id": "91507bc4ddffe87f2f6a2c28b39723af"
  }
 ]
}
```

</details>

<a id="step-s3"></a>
### s3 · `query_metrics`(promql="http_error_ratio{service=\"shop_api\"}", minutes=15) · 2 ms · ok

<details><summary>result</summary>

```json
{
 "query": "http_error_ratio{service=\"shop_api\"}",
 "window_minutes": 15,
 "end": "2026-11-10T06:14:01Z",
 "step": "~15s",
 "series": [
  {
   "labels": {
    "service": "shop_api"
   },
   "points": [
    [
     "06:10:42",
     0.0043
    ],
    [
     "06:10:57",
     0.0064
    ],
    [
     "06:11:12",
     0.0107
    ],
    [
     "06:11:42",
     0.015
    ],
    [
     "06:11:57",
     0.0128
    ],
    [
     "06:12:12",
     0.0096
    ],
    [
     "06:12:27",
     0.0213
    ],
    [
     "06:12:42",
     0.0374
    ],
    [
     "06:12:57",
     0.0532
    ],
    [
     "06:13:27",
     0.0588
    ],
    [
     "06:13:42",
     0.0562
    ],
    [
     "06:13:57",
     0.0518
    ]
   ],
   "first": 0.004250797024442083,
   "last": 0.0517515923566879,
   "min": 0.004250797024442083,
   "max": 0.06272727272727271
  }
 ],
 "note": null
}
```

</details>

<a id="step-s4"></a>
### s4 · `db_locks`() · 221 ms · ok

> The main error is Postgres deadlocks in `reserve_inventory`. I'll check the locks, the source, and the stack trace together.

<details><summary>result</summary>

```json
{
 "blocking_chains": [
  {
   "blocker_pid": 62524,
   "blocker_application": "shop_api",
   "blocker_state": "active",
   "blocker_xact_age_s": 1.4,
   "blocker_query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1",
   "blocked": [
    {
     "pid": 62527,
     "application": "shop_api",
     "waiting_s": 0.4,
     "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1"
    },
    {
     "pid": 62525,
     "application": "shop_api",
     "waiting_s": 0.3,
     "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1"
    }
   ]
  },
  {
   "blocker_pid": 62527,
   "blocker_application": "shop_api",
   "blocker_state": "active",
   "blocker_xact_age_s": 0.4,
   "blocker_query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1",
   "blocked": [
    {
     "pid": 62576,
     "application": "shop_api",
     "waiting_s": 0.4,
     "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1"
    }
   ]
  },
  {
   "blocker_pid": 62576,
   "blocker_application": "shop_api",
   "blocker_state": "active",
   "blocker_xact_age_s": 1.4,
   "blocker_query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1",
   "blocked": [
    {
     "pid": 62526,
     "application": "shop_api",
     "waiting_s": 1.4,
     "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1"
    },
    {
     "pid": 62524,
     "application": "shop_api",
     "waiting_s": 0.4,
     "query": "UPDATE inventory SET qty = qty - $1 WHERE sku = $2 AND qty >= $1"
    }
   ]
  }
 ],
 "deadlocks_last_15m": [
  {
   "minutes_before_alert": 0.1,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": 0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": 0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": 0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": -0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": -0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": -0.0,
   "victim_application": "see detail",
   "detail": ""
  },
  {
   "minutes_before_alert": -0.1,
   "victim_application": "see detail",
   "detail": ""
  }
 ]
}
```

</details>

<a id="step-s5"></a>
### s5 · `read_source`(path="services/shop_api/app/inventory.py", start_line=1, end_line=60, sha=null) · 46 ms · ok

> The main error is Postgres deadlocks in `reserve_inventory`. I'll check the locks, the source, and the stack trace together.

<details><summary>result</summary>

```json
{
 "path": "services/shop_api/app/inventory.py",
 "sha": "HEAD",
 "start_line": 1,
 "lines": [
  "   1  from sqlalchemy import text",
  "   2  ",
  "   3  ",
  "   4  async def reserve_inventory(session, items):",
  "   5      reserved = []",
  "   6      for item in items:",
  "   7          await session.execute(",
  "   8              text(\"UPDATE inventory SET qty = qty - :n WHERE sku = :sku AND qty >= :n\"),",
  "   9              {\"n\": item.qty, \"sku\": item.sku},",
  "  10          )",
  "  11          reserved.append(item)",
  "  12      return reserved"
 ]
}
```

</details>

<a id="step-s6"></a>
### s6 · `get_stack_trace`(signature="sqlalchemy.exc.DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) de...) · 50 ms · ok

> The main error is Postgres deadlocks in `reserve_inventory`. I'll check the locks, the source, and the stack trace together.

<details><summary>result</summary>

```json
{
 "signature": "sqlalchemy.exc.DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) deadlock detected @ services/shop_api/app/inventory.py:7 in reserve_inventory",
 "occurrences": 125,
 "service": "shop_api",
 "stack": "onn.execute(\n             ^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 1494, in execute\n    return meth(\n           ^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/sql/elements.py\", line 534, in _execute_on_connection\n    return connection._execute_clauseelement(\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 1714, in _execute_clauseelement\n    ret = self._execute_context(\n          ^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 1868, in _execute_context\n    return self._exec_single_context(\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 1996, in _exec_single_context\n    self._handle_dbapi_exception(\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 2351, in _handle_dbapi_exception\n    raise sqlalchemy_exception.with_traceback(exc_info[2]) from e\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py\", line 1977, in _exec_single_context\n    self.dialect.do_execute(\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/default.py\", line 1192, in do_execute\n    cursor.execute(statement, parameters)\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/dialects/postgresql/asyncpg.py\", line 656, in execute\n    await_(self._prepare_and_execute(operation, parameters))\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/util/concurrency.py\", line 167, in await_\n    return current.parent.switch(awaitable)  # type: ignore[no-any-return]\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/util/concurrency.py\", line 205, in greenlet_spawn\n    value = await result\n            ^^^^^^^^^^^^\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/dialects/postgresql/asyncpg.py\", line 618, in _prepare_and_execute\n    self._handle_exception(error)\n  File \"/usr/local/lib/python3.12/site-packages/sqlalchemy/dialects/postgresql/asyncpg.py\", line 568, in _handle_exception\n    self._adapt_connection._
... (truncated in report; full result in steps table)
```

</details>

<a id="step-s7"></a>
### s7 · `read_source`(path="services/shop_api/app/checkout.py", start_line=1, end_line=60, sha=null) · 72 ms · ok

> The deadlock is in `reserve_inventory`, which updates SKUs in the order the cart supplies them. I'll check whether the caller sorts items, and whether anything changed recently.

<details><summary>result</summary>

```json
{
 "path": "services/shop_api/app/checkout.py",
 "sha": "HEAD",
 "start_line": 1,
 "lines": [
  "   1  from fastapi import APIRouter, Depends",
  "   2  from sqlalchemy.ext.asyncio import AsyncSession",
  "   3  ",
  "   4  from . import flags",
  "   5  from .db import get_session",
  "   6  from .inventory import reserve_inventory",
  "   7  from .orders import create_order",
  "   8  from .payment_client import charge",
  "   9  from .pricing import price",
  "  10  from .schemas import CheckoutRequest",
  "  11  ",
  "  12  router = APIRouter()",
  "  13  ",
  "  14  ",
  "  15  @router.post(\"/checkout\")",
  "  16  async def checkout(req: CheckoutRequest, session: AsyncSession = Depends(get_session)):",
  "  17      total = await price(req.items, req.customer_id, flags.in_rollout(\"checkout_v2_pricing\", req.customer_id))",
  "  18      async with session.begin():",
  "  19          items = await reserve_inventory(session, req.items)",
  "  20          order = await create_order(session, req.customer_id, items, total)",
  "  21      payment = await charge(order_id=order.id, amount_cents=order.total_cents)",
  "  22      return build_response(order, payment, req.model_dump())",
  "  23  ",
  "  24  ",
  "  25  def build_response(order, payment, body: dict) -> dict:",
  "  26      shipping = body.get(\"shipping_address\") or {}",
  "  27      return {",
  "  28          \"order_id\": order.id,",
  "  29          \"status\": payment[\"status\"],",
  "  30          \"ship_to\": shipping.get(\"city\", \"pickup\"),",
  "  31      }"
 ]
}
```

</details>

<a id="step-s8"></a>
### s8 · `recent_changes`(since_minutes=180) · 14 ms · ok

> The deadlock is in `reserve_inventory`, which updates SKUs in the order the cart supplies them. I'll check whether the caller sorts items, and whether anything changed recently.

<details><summary>result</summary>

```json
{
 "since_minutes": 180,
 "changes": []
}
```

</details>

<a id="step-s9"></a>
### s9 · `get_trace`(trace_id="2d21f3ec60075fd4232266acabcbfaa6") · 32 ms · ok

> No deploys in the last 3 hours, so the cause is probably in the code path itself. I'll check one failing trace to confirm that multiple SKUs are reserved in differing order.

<details><summary>result</summary>

```json
{
 "trace_id": "2d21f3ec60075fd4232266acabcbfaa6",
 "root": "POST /checkout",
 "duration_ms": 1023.6,
 "ts": "2026-11-10T06:14:02Z",
 "spans": [
  {
   "depth": 0,
   "name": "POST /checkout",
   "service": "shop_api",
   "start_ms": 0.0,
   "duration_ms": 1023.6,
   "status": "error",
   "attrs": {
    "http.scheme": "http",
    "http.host": "172.21.0.12:8080",
    "http.flavor": "1.1",
    "http.target": "/checkout",
    "http.url": "http://localhost:8080/checkout",
    "http.method": "POST",
    "http.server_name": "localhost:8080",
    "http.user_agent": "python-httpx/0.28.1",
    "http.route": "/checkout",
    "http.status_code": "500"
   }
  },
  {
   "depth": 1,
   "name": "POST /checkout http receive",
   "service": "shop_api",
   "start_ms": 1.4,
   "duration_ms": 0.0,
   "status": "ok",
   "attrs": {
    "asgi.event.type": "http.request"
   }
  },
  {
   "depth": 1,
   "name": "POST /checkout http send",
   "service": "shop_api",
   "start_ms": 1023.2,
   "duration_ms": 0.0,
   "status": "error",
   "attrs": {
    "asgi.event.type": "http.response.start",
    "http.status_code": "500"
   }
  },
  {
   "depth": 1,
   "name": "POST /checkout http send",
   "service": "shop_api",
   "start_ms": 1023.5,
   "duration_ms": 0.0,
   "status": "ok",
   "attrs": {
    "asgi.event.type": "http.response.body"
   }
  }
 ]
}
```

</details>
