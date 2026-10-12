"""The public demo's API: same guard as MCP, rate limits, and English mode off without a key."""

import asyncio
import json

import pytest
from conftest import requires_db
from starlette.requests import Request

from safesql.config import Settings
from safesql.web import DailyBudget, DemoApp, RateLimiter

pytestmark = [pytest.mark.db, requires_db]


def call(handler, method="GET", path="/", body=None, query=""):
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.7", 5000),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    response = asyncio.run(handler(Request(scope, receive)))
    return response.status_code, response


@pytest.fixture
def demo(executor, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SAFESQL_DEMO_QUERIES_PER_MINUTE", "5")
    settings = Settings(database_url=executor.pool.conninfo)
    return DemoApp(settings, executor, ["spider_concert_singer", "inject_demo", "not_a_schema"])


def body(response):
    return json.loads(response.body)


def test_config_lists_granted_schemas_and_the_attack_corpus(demo):
    status, resp = call(demo.config)
    data = body(resp)
    assert status == 200
    assert [s["name"] for s in data["schemas"]] == ["spider_concert_singer", "inject_demo"]
    assert len(data["attacks"]) == 132 and data["tricky"]
    assert data["agent"]["enabled"] is False


def test_query_runs_through_the_guard(demo):
    _, ok = call(
        demo.query,
        "POST",
        "/api/query",
        {"schema": "spider_concert_singer", "sql": "SELECT count(*) FROM singer"},
    )
    assert body(ok)["ok"] and body(ok)["rows"] == [[6]]
    _, bad = call(
        demo.query, "POST", "/api/query", {"schema": "spider_concert_singer", "sql": "DROP TABLE singer"}
    )
    assert body(bad)["error"]["code"] == "statement_not_allowed" and body(bad)["layer"] == "guard"
    _, secret = call(
        demo.query, "POST", "/api/query", {"schema": "inject_demo", "sql": "SELECT * FROM secrets.api_keys"}
    )
    assert body(secret)["layer"] == "guard"


def test_unknown_schema_and_bad_body(demo):
    assert call(demo.query, "POST", "/api/query", {"schema": "secrets", "sql": "SELECT 1"})[0] == 404
    assert call(demo.schema, query="schema=nope")[0] == 404


def test_query_rate_limit(demo):
    statuses = [
        call(demo.query, "POST", "/api/query", {"schema": "inject_demo", "sql": "SELECT 1"})[0]
        for _ in range(7)
    ]
    assert statuses[:5] == [200] * 5 and statuses[5:] == [429, 429]


def test_english_mode_is_off_without_a_key(demo):
    assert call(demo.ask, "POST", "/api/ask", {"schema": "inject_demo", "question": "hi"})[0] == 403


def test_page_is_served_with_a_strict_csp(demo):
    status, resp = call(demo.index)
    assert status == 200 and b"SafeSQL-MCP" in resp.body
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_daily_budget_and_rate_limiter():
    budget = DailyBudget(2)
    assert [budget.take(), budget.take(), budget.take()] == [True, True, False] and budget.remaining() == 0
    limiter = RateLimiter(2, window_s=60)
    assert [limiter.allow("a"), limiter.allow("a"), limiter.allow("a"), limiter.allow("b")] == [
        True,
        True,
        False,
        True,
    ]
