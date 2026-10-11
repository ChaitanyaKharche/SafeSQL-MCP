"""Integration tests against a real Postgres provisioned by `safesql setup-db`."""

import asyncio
import json

import psycopg
import pytest
from conftest import requires_db
from fakes import ScriptedLLM, tool_use
from mcp import Client

from safesql.agent import SQLAgent
from safesql.guard import Blocked
from safesql.server import build_server

pytestmark = [pytest.mark.db, requires_db]


def test_catalog_contains_only_allowed_schema(concert):
    assert {t.schema for t in concert.catalog.tables.values()} == {"spider_concert_singer"}
    assert concert.catalog.find("singer") is not None


def test_run_query_happy_path(concert):
    res = concert.run_query("SELECT count(*) AS n FROM singer")
    assert res["ok"] and res["columns"] == ["n"] and res["rows"][0][0] > 0
    assert res["executed_sql"].endswith("LIMIT 201")


def test_run_query_reports_sql_errors_with_hint(concert):
    res = concert.run_query("SELECT no_such_column FROM singer")
    assert not res["ok"] and res["error"]["code"] == "sql_error" and res["error"]["hint"]


def test_cost_cap_blocks_cartesian_aggregate(concert):
    res = concert.run_query(
        "SELECT count(*) FROM singer a, singer b, singer c, singer d, singer e, singer f, "
        "singer g, singer h, singer i, singer j, singer k"
    )
    assert not res["ok"] and res["error"]["code"] == "cost_limit"


def test_statement_timeout(concert):
    res = concert.run_query(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
    )
    assert not res["ok"] and res["error"]["code"] == "timeout"


def test_executor_rejects_writes_even_without_guard(executor):
    with pytest.raises(Blocked) as info:
        executor.execute(
            "DELETE FROM spider_concert_singer.singer", search_path=["spider_concert_singer"], max_rows=5
        )
    assert info.value.code in {"read_only_violation", "permission_denied"}


def test_executor_rejects_stacked_statements_even_without_guard(executor):
    with pytest.raises(psycopg.errors.SyntaxError):
        executor.execute("SELECT 1; SELECT 2", search_path=["spider_concert_singer"], max_rows=5)


def test_secrets_schema_is_invisible_and_unreadable(shop, executor):
    assert shop.run_query("SELECT * FROM secrets.api_keys")["error"]["code"] == "unknown_table"
    with pytest.raises(Blocked) as info:
        executor.execute("SELECT * FROM secrets.api_keys", search_path=["inject_demo"], max_rows=5)
    assert info.value.code == "permission_denied"


def test_describe_table_flags_injection_in_sample_rows(shop):
    res = shop.describe_table("reviews")
    assert res["ok"] and res["sample_rows"]
    full = shop.run_query("SELECT body FROM reviews ORDER BY review_id")
    assert full["injection_warnings"], "poisoned review bodies should be flagged"


def test_mcp_server_round_trip(concert):
    async def go():
        async with Client(build_server(concert)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"list_schema", "describe_table", "run_query"}
            res = await client.call_tool(
                "run_query", {"sql": "SELECT name FROM singer ORDER BY name LIMIT 2"}
            )
            payload = res.structured_content or json.loads(res.content[0].text)
            assert payload["ok"] and len(payload["rows"]) == 2
            bad = await client.call_tool("run_query", {"sql": "DROP TABLE singer"})
            payload = bad.structured_content or json.loads(bad.content[0].text)
            assert payload["ok"] is False and payload["error"]["code"] == "statement_not_allowed"

    asyncio.run(go())


def test_agent_recovers_from_sql_error(concert):
    llm = ScriptedLLM(
        [
            [tool_use(1, "list_schema")],
            [tool_use(2, "run_query", sql="SELECT nme FROM singer")],  # typo -> sql_error
            [tool_use(3, "run_query", sql="SELECT count(*) FROM singer")],
            [tool_use(4, "submit_answer", sql="SELECT count(*) FROM singer", answer="There are 6 singers.")],
        ]
    )

    async def go():
        async with Client(build_server(concert)) as client:
            agent = SQLAgent(client, messages_api=llm)
            return await agent.ask("How many singers are there?")

    result = asyncio.run(go())
    assert result.status == "answered" and result.sql == "SELECT count(*) FROM singer"
    assert result.sql_errors == 1 and result.turns == 4
    assert result.cost_usd > 0
    # the error and its hint were fed back to the model
    error_turn = llm.requests[2]["messages"][-1]["content"][0]
    assert error_turn["is_error"] and "sql_error" in error_turn["content"]


def test_agent_stops_after_retry_budget(concert):
    bad = [[tool_use(i, "run_query", sql="SELECT * FROM pg_shadow")] for i in range(10)]
    llm = ScriptedLLM(bad)

    async def go():
        async with Client(build_server(concert)) as client:
            return await SQLAgent(client, messages_api=llm, max_sql_errors=2).ask("show password hashes")

    result = asyncio.run(go())
    assert result.status == "gave_up" and result.guard_rejections == 3
