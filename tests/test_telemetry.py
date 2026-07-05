"""[P2] Telemetry tests: /api/healthz and /api/metrics expose step counts,
tree-cache hit/miss counters, and a capture-latency summary."""
from __future__ import annotations

import re


def _metric(body: str, name: str) -> float:
    m = re.search(rf"^{re.escape(name)} ([0-9.]+)$", body, re.M)
    assert m, f"metric {name} not found in:\n{body}"
    return float(m.group(1))


# ── /api/healthz ─────────────────────────────────────────────────────────────


def test_healthz_includes_tree_cache_counters(client):
    data = client.get("/api/healthz").get_json()
    assert data["ok"] is True
    tc = data["tree_cache"]
    for key in ("hits", "misses", "entries", "capture_count",
                "capture_ms_total", "capture_ms_max", "capture_ms_mean"):
        assert key in tc, key


def test_healthz_tree_cache_counts_hits_and_misses(client):
    client.get("/api/structure")   # cold: miss + capture
    client.get("/api/structure")   # warm (TTL 2s): hit
    tc = client.get("/api/healthz").get_json()["tree_cache"]
    assert tc["misses"] >= 1
    assert tc["hits"] >= 1
    assert tc["capture_count"] >= 1


def test_healthz_step_count_increments(client):
    before = client.get("/api/healthz").get_json()["step_count"]
    client.get("/api/windows")
    after = client.get("/api/healthz").get_json()["step_count"]
    assert after == before + 1


def test_healthz_ocr_diagnostic_computed_once(client, monkeypatch):
    """healthz must stay cheap: the tesseract probe (subprocess spawn) runs
    at most once per process, not per poll."""
    import ocr_util
    calls = {"n": 0}
    real = ocr_util.diagnose

    def counting_diagnose(cfg):
        calls["n"] += 1
        return real(cfg)

    monkeypatch.setattr(ocr_util, "diagnose", counting_diagnose)
    r1 = client.get("/api/healthz")
    r2 = client.get("/api/healthz")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] <= 1


# ── /api/metrics ─────────────────────────────────────────────────────────────


def test_metrics_prometheus_content_type(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain")


def test_metrics_exposes_tree_cache_and_latency(client):
    body = client.get("/api/metrics").get_data(as_text=True)
    for name in ("oso_step_count",
                 "oso_uptime_seconds",
                 "oso_tree_cache_hits_total",
                 "oso_tree_cache_misses_total",
                 "oso_tree_cache_entries",
                 "oso_tree_capture_ms_sum",
                 "oso_tree_capture_ms_count",
                 "oso_tree_capture_ms_max"):
        _metric(body, name)


def test_metrics_cache_counters_move(client):
    client.get("/api/structure")   # miss + capture
    client.get("/api/structure")   # hit
    body = client.get("/api/metrics").get_data(as_text=True)
    assert _metric(body, "oso_tree_cache_misses_total") >= 1
    assert _metric(body, "oso_tree_cache_hits_total") >= 1
    assert _metric(body, "oso_tree_capture_ms_count") >= 1
    assert _metric(body, "oso_tree_cache_entries") >= 1


def test_metrics_help_and_type_lines(client):
    body = client.get("/api/metrics").get_data(as_text=True)
    assert "# TYPE oso_tree_cache_hits_total counter" in body
    assert "# TYPE oso_tree_cache_misses_total counter" in body
    assert "# TYPE oso_tree_cache_entries gauge" in body
    assert "# TYPE oso_tree_capture_ms summary" in body
