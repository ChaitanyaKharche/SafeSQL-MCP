"""One agent question should produce one connected trace across agent, MCP and database spans."""

import asyncio

import pytest
from conftest import requires_db
from fakes import ScriptedLLM, tool_use
from mcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from safesql.agent import SQLAgent
from safesql.server import build_server

pytestmark = [pytest.mark.db, requires_db]
EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        current = TracerProvider()
        trace.set_tracer_provider(current)
    current.add_span_processor(SimpleSpanProcessor(EXPORTER))
    return current


def test_agent_question_is_one_trace(concert):
    EXPORTER.clear()
    llm = ScriptedLLM(
        [
            [tool_use(1, "run_query", sql="SELECT count(*) FROM singer")],
            [tool_use(2, "submit_answer", sql="SELECT count(*) FROM singer", answer="6")],
        ]
    )

    async def go():
        async with Client(build_server(concert)) as client:
            await SQLAgent(client, messages_api=llm).ask("How many singers?")

    asyncio.run(go())
    spans = EXPORTER.get_finished_spans()
    names = {s.name for s in spans}
    assert {
        "agent.ask",
        "llm.call",
        "mcp.tool run_query",
        "safesql.run_query",
        "safesql.guard",
        "db.explain",
        "db.execute",
    } <= names
    root = next(s for s in spans if s.name == "agent.ask")
    run_query = next(s for s in spans if s.name == "safesql.run_query")
    assert run_query.context.trace_id == root.context.trace_id, "server spans should join the agent's trace"
    assert run_query.attributes["safesql.decision"] == "allowed"
