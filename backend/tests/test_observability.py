"""The Kong dashboard stack (infra/kong/observability): prices match costs.py, and the dashboard is well formed."""
import json
import re
from pathlib import Path

from deadlang import costs

OBS = Path(__file__).resolve().parents[2] / "infra" / "kong" / "observability"
DASHBOARD = json.loads((OBS / "grafana" / "dashboards" / "watchdog-kong.json").read_text())
# Metrics Kong 3.12's prometheus plugin exports (ai_metrics on), plus our recording rule.
KNOWN = {"kong_http_requests_total", "kong_ai_llm_tokens_total", "kong_ai_llm_requests_total",
         "kong_ai_llm_provider_latency_ms_bucket", "kong_kong_latency_ms_bucket", "watchdog:price_usd_per_mtok"}


def price_rules() -> dict[tuple[str, str], float]:
    text = (OBS / "prices.rules.yml").read_text()
    found = re.findall(r'vector\(([\d.]+)\), "ai_model", "([^"]+)", "", ""\), "token_type", "(\w+)"', text)
    return {(model, kind): float(p) for p, model, kind in found}


def test_price_rules_match_costs_py():
    rules = price_rules()
    assert len(rules) >= 8
    for (model, kind), price in rules.items():
        assert costs.price_for(model) is not None, model
        expected = costs.price_for(model)[0 if kind == "prompt_tokens" else 1]
        assert price == expected, (model, kind)


def test_dashboard_panels_query_prometheus_with_known_metrics():
    assert DASHBOARD["uid"] == "watchdog-kong"
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids)) >= 10
    for p in DASHBOARD["panels"]:
        assert p["datasource"]["uid"] == "prometheus" and p["targets"], p["title"]
        for t in p["targets"]:
            names = set(re.findall(r"\b(kong_[a-z_]+|watchdog:[a-z_]+)", t["expr"]))
            assert names and names <= KNOWN, (p["title"], names - KNOWN)


def test_cost_queries_join_tokens_to_prices_by_model_and_type():
    cost = [t["expr"] for p in DASHBOARD["panels"] for t in p["targets"] if "price_usd_per_mtok" in t["expr"]]
    assert cost and all("on(ai_model, token_type) group_left()" in e and "/ 1e6" in e for e in cost)
