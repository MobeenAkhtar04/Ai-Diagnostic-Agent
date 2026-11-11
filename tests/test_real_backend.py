"""RealBackend parsing tests with canned HTTP responses (no stack needed)."""

import base64
import json
import subprocess
import time

import httpx
import pytest

from diag.tools.backend import ToolError
from diag.tools.real_backend import RealBackend, _signature

NOW = time.time()


def make(handler) -> RealBackend:
    b = RealBackend(repo_dir=".")
    b.http = httpx.Client(transport=httpx.MockTransport(handler))
    return b


def test_query_metrics_shapes_and_validation():
    def h(req):
        assert req.url.path == "/api/v1/query_range"
        vals = [[NOW - 600 + i * 15, str(0.1 + i * 0.01)] for i in range(40)]
        return httpx.Response(200, json={"status": "success", "data": {"result": [
            {"metric": {"__name__": "http_error_ratio", "service": "shop_api", "job": "x"}, "values": vals}]}})

    out = make(h).query_metrics('http_error_ratio{service="shop_api"}', 10)
    s = out["series"][0]
    assert s["labels"] == {"service": "shop_api"} and len(s["points"]) <= 12 and s["last"] > s["first"]
    with pytest.raises(ToolError):
        make(h).query_metrics("rate(http_error_ratio[1m])", 10)
    with pytest.raises(ToolError):
        make(h).query_metrics("made_up_metric", 10)


def test_list_alerts_only_firing():
    def h(req):
        return httpx.Response(200, json={"data": {"alerts": [
            {"state": "firing", "activeAt": "2026-11-08T20:00:00.5Z", "labels": {"alertname": "HighErrorRate",
             "severity": "critical", "service": "shop_api"}, "annotations": {"summary": "x"}},
            {"state": "pending", "activeAt": "2026-11-08T20:00:00Z", "labels": {"alertname": "Other"},
             "annotations": {}}]}})

    out = make(h).list_alerts()["firing"]
    assert len(out) == 1 and out[0]["name"] == "HighErrorRate" and out[0]["labels"] == {"service": "shop_api"}


STACK = ('Traceback (most recent call last):\n  File "/usr/lib/x.py", line 3, in f\n'
         '  File "/app/services/shop_api/app/orders.py", line 10, in create_order\nValueError: boom')


def test_signature_uses_last_app_frame():
    assert _signature("boom", STACK) == "boom @ services/shop_api/app/orders.py:10 in create_order"


def test_top_errors_and_stack_trace():
    line = json.dumps({"ts": NOW, "level": "ERROR", "service": "shop_api", "msg": "boom", "exc_type": "ValueError",
                       "stack": STACK, "trace_id": "ab" * 16})

    def h(req):
        values = [[str(int(NOW * 1e9)), line]] * 3
        return httpx.Response(200, json={"data": {"result": [{"stream": {"service": "shop_api"}, "values": values}]}})

    b = make(h)
    out = b.top_errors("shop_api", 15, 5)
    assert out["total_errors"] == 3 and out["signatures"][0]["count"] == 3
    sig = out["signatures"][0]["signature"]
    assert "orders.py:10" in sig
    assert "ValueError" in b.get_stack_trace(sig)["stack"]


def test_get_trace_handles_base64_ids_and_depth():
    def b64(h):
        return base64.b64encode(bytes.fromhex(h)).decode()

    root, child = "aa" * 8, "bb" * 8
    spans = [
        {"spanId": b64(root), "parentSpanId": "", "name": "POST /checkout", "startTimeUnixNano": "1000000000",
         "endTimeUnixNano": "1500000000", "status": {}, "attributes": []},
        {"spanId": b64(child), "parentSpanId": b64(root), "name": "SELECT", "startTimeUnixNano": "1100000000",
         "endTimeUnixNano": "1200000000", "status": {"code": 2}, "attributes": [
             {"key": "db.system", "value": {"stringValue": "postgresql"}}]},
    ]
    body = {"batches": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "shop_api"}}]},
                         "scopeSpans": [{"spans": spans}]}]}
    out = make(lambda r: httpx.Response(200, json=body)).get_trace("ab" * 16)
    assert out["root"] == "POST /checkout" and out["duration_ms"] == 500.0
    assert [s["depth"] for s in out["spans"]] == [0, 1] and out["spans"][1]["status"] == "error"
    with pytest.raises(ToolError):
        make(lambda r: httpx.Response(404)).get_trace("ab" * 16)


def test_find_slow_traces():
    def h(req):
        if "duration" in req.url.params["q"]:
            return httpx.Response(200, json={"traces": [{"traceID": "ab" * 16, "rootTraceName": "POST /checkout",
                                                         "durationMs": 2500, "startTimeUnixNano": str(int(NOW * 1e9))}]})
        return httpx.Response(200, json={"traces": [{}] * 7})

    out = make(h).find_slow_traces("shop_api", 1000, 15, 5)
    assert out["traces_in_window"] == 7 and out["traces"][0]["duration_ms"] == 2500.0


def test_git_read_source_and_diff(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    f = tmp_path / "services/shop_api/app/checkout.py"
    f.parent.mkdir(parents=True)
    f.write_text("a = 1\nb = 2\n")
    git("add", ".")
    git("commit", "-qm", "one")
    sha1 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    f.write_text("a = 1\nb = 3\n")
    git("commit", "-qam", "two")
    sha2 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    b = RealBackend(repo_dir=tmp_path)
    src = b.read_source("services/shop_api/app/checkout.py", 1, 5, sha1)
    assert "b = 2" in src["lines"][1]
    d = b.git_diff(sha1, sha2, "services/shop_api/app/checkout.py")
    assert "-b = 2" in d["diff"] and "+b = 3" in d["diff"]
    with pytest.raises(ToolError):
        b.read_source("services/shop_api/app/nope.py", 1, 5, sha1)


def test_explain_binds_placeholders_with_typed_constants():
    be = RealBackend(".")
    out = be._bind_placeholders("SELECT 1 FROM orders WHERE customer_id = $1::VARCHAR LIMIT $2::INTEGER AND id = $3")
    assert "$" not in out
    assert "'x'::varchar" in out and "50::integer" in out and out.endswith("id = 50")


def test_fence_clamps_every_window(monkeypatch):
    import time as _t
    be = RealBackend(".")
    assert be._start(1000.0, 10) == 400.0 and be._minutes(15) == 15
    be.not_before = _t.time() - 150  # fence 2.5 min ago
    now = _t.time()
    assert abs(be._start(now, 15) - be.not_before) < 1e-6   # 15 min window cut to the fence
    assert be._start(now, 1) == now - 60                    # window inside the fence is unchanged
    assert be._minutes(15) == 3
