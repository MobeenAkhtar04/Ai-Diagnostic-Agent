from diag.tools.backend import ToolError
from diag.tools.real_backend import RealBackend


def test_loki_retry_smaller(monkeypatch):
    b = RealBackend()
    calls = []
    def fake_get(url, _timeout=None, **params):
        calls.append((params["limit"], _timeout))
        if len(calls) == 1:
            raise ToolError("backend unreachable: ReadTimeout")
        return {"data": {"result": []}}
    monkeypatch.setattr(b, "_get", fake_get)
    assert b._loki_lines("shop_api", "ERROR", "exc_type", 15, 300) == []
    assert calls == [(300, 4.0), (100, 7.0)]
    assert b._loki_limit_used == 100

def test_loki_other_errors_not_retried(monkeypatch):
    b = RealBackend()
    n = []
    def fake_get(url, _timeout=None, **params):
        n.append(1)
        raise ToolError("backend returned HTTP 500: x")
    monkeypatch.setattr(b, "_get", fake_get)
    try:
        b._loki_lines("shop_api", "ERROR", None, 15, 10)
        raise AssertionError("should raise")
    except ToolError:
        pass
    assert len(n) == 1
