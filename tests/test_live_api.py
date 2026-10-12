"""Live Claude API checks. Skipped without ANTHROPIC_API_KEY; together they cost well under a cent.

These are the standing check that prompt caching works: requests keep succeeding when a
change to prompt assembly breaks caching, so only the usage fields can catch it.
"""

import asyncio
import os

import pytest
from conftest import requires_db
from mcp import Client

from safesql.agent import MODEL, SQLAgent
from safesql.server import build_server

pytestmark = [
    pytest.mark.llm,
    requires_db,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"),
]


def test_shared_prefix_is_cached(concert):
    async def go():
        async with Client(build_server(concert)) as client:
            agent = SQLAgent(client)
            first, _ = await agent.prewarm()
            second, _ = await agent.prewarm()
            return first, second

    first, second = asyncio.run(go())
    # The first call writes the tools + system prefix (or reads it, if a recent run wrote it).
    assert first["cache_creation_input_tokens"] + first["cache_read_input_tokens"] > 0, first
    assert second["cache_read_input_tokens"] > 0, second


def test_agent_answers_and_reads_its_own_cache(concert):
    async def go():
        async with Client(build_server(concert)) as client:
            return await SQLAgent(client).ask("How many singers are there?")

    result = asyncio.run(go())
    assert result.model == MODEL
    assert result.status == "answered", result.summary()
    assert concert.run_query(result.sql)["rows"] == [[6]]
    assert result.turns >= 2 and result.usage["cache_read_input_tokens"] > 0, result.usage
