"""Public demo: a "try to break it" SQL playground, plus an optional plain-English mode.

`safesql serve --transport http --demo` mounts these routes next to the MCP endpoint.
Visitor SQL goes through exactly the same guard and executor as the MCP `run_query`
tool, so the playground costs nothing to run. The English mode runs the agent on
Claude Haiku 5.5 and is switched on only when ANTHROPIC_API_KEY is set, behind a
per-visitor rate limit and a global daily cap.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import anthropic
from mcp import Client
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .agent import SQLAgent, cache_hit_rate
from .config import Settings
from .executor import Executor
from .server import build_server
from .service import SafeSQLService, layer_of

# Labels and starter examples for the schemas the demo knows about. Other schemas listed in
# SAFESQL_DEMO_SCHEMAS still work; they just get their raw name and no starters.
KNOWN_SCHEMAS: dict[str, dict[str, str]] = {
    "spider_concert_singer": {
        "label": "Concerts and singers",
        "sql": "SELECT name, country, age FROM singer ORDER BY age DESC",
        "question": "Which singers are older than the average singer, and where are they from?",
    },
    "spider_world_1": {
        "label": "World countries, cities and languages",
        "sql": "SELECT name, population FROM city ORDER BY population DESC LIMIT 5",
        "question": "Which five countries in Asia have the largest populations?",
    },
    "spider_car_1": {
        "label": "Cars, makers and models",
        "sql": "SELECT maker, fullname FROM car_makers ORDER BY maker LIMIT 10",
        "question": "Which car maker produces the most models?",
    },
    "inject_demo": {
        "label": "Shop reviews with planted prompt injections",
        "sql": "SELECT p.name, r.rating, r.body FROM reviews r JOIN products p USING (product_id) ORDER BY r.review_id",
        "question": "What is the average rating of each product?",
    },
}
DEFAULT_DEMO_SCHEMAS = ",".join(KNOWN_SCHEMAS)
MAX_QUESTION_CHARS = 400


class RateLimiter:
    """Sliding-window limit per key (a visitor's address)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 50_000:  # drop idle keys rather than grow without bound
                self._hits = defaultdict(
                    deque, {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window}
                )
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


class DailyBudget:
    """A global cap on English-mode questions per UTC day, so a public page can't run up a bill."""

    def __init__(self, limit: int):
        self.limit = limit
        self._day = None
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day, self._used = today, 0

    def take(self) -> bool:
        with self._lock:
            self._roll()
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(self.limit - self._used, 0)


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded or (request.client.host if request.client else "unknown")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class DemoApp:
    def __init__(self, settings: Settings, executor: Executor, schemas: list[str]):
        self.settings = settings
        self.services = {s: SafeSQLService(settings.with_schemas(s), executor) for s in schemas}
        self.services = {s: svc for s, svc in self.services.items() if svc.catalog.tables}
        self.mcp_servers = {s: build_server(svc) for s, svc in self.services.items()}
        self.query_limiter = RateLimiter(int(os.environ.get("SAFESQL_DEMO_QUERIES_PER_MINUTE", "60")), 60)
        self.ask_limiter = RateLimiter(int(os.environ.get("SAFESQL_DEMO_ASKS_PER_HOUR", "10")), 3600)
        self.budget = DailyBudget(int(os.environ.get("SAFESQL_DEMO_ASK_DAILY_LIMIT", "50")))
        self.agent_enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._messages = anthropic.AsyncAnthropic(max_retries=3).messages if self.agent_enabled else None
        self._ask_slots = asyncio.Semaphore(2)
        root = Path(os.environ.get("SAFESQL_DEMO_ROOT", "."))
        self.corpus = _load_json(root / "evals/data/adversarial_sql.json") or {"cases": []}
        self.reports = {
            n: _load_json(root / f"evals/reports/{n}.json") for n in ("safety", "accuracy", "agent_safety")
        }
        self.page = resources.files("safesql").joinpath("static/index.html").read_text(encoding="utf-8")

    @classmethod
    def from_env(cls, settings: Settings, executor: Executor) -> DemoApp:
        raw = os.environ.get("SAFESQL_DEMO_SCHEMAS", DEFAULT_DEMO_SCHEMAS)
        return cls(settings, executor, [s.strip() for s in raw.split(",") if s.strip()])

    def mount(self, server: MCPServer) -> None:
        server.custom_route("/", methods=["GET"])(self.index)
        server.custom_route("/api/config", methods=["GET"])(self.config)
        server.custom_route("/api/schema", methods=["GET"])(self.schema)
        server.custom_route("/api/query", methods=["POST"])(self.query)
        server.custom_route("/api/ask", methods=["POST"])(self.ask)

    # -- routes ------------------------------------------------------------------

    async def index(self, _: Request) -> HTMLResponse:
        return HTMLResponse(
            self.page, headers={"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff"}
        )

    async def config(self, _: Request) -> JSONResponse:
        attacks = [
            {"id": c["id"], "category": c["category"], "sql": c["sql"]}
            for c in self.corpus["cases"]
            if c["expect"] == "block"
        ]
        tricky = [
            {"id": c["id"], "sql": c["sql"], "why": c.get("why", "")}
            for c in self.corpus["cases"]
            if c["expect"] == "allow"
        ]
        return JSONResponse(
            {
                "schemas": [
                    {
                        "name": s,
                        "label": KNOWN_SCHEMAS.get(s, {}).get("label", s),
                        "sql": KNOWN_SCHEMAS.get(s, {}).get("sql", ""),
                        "question": KNOWN_SCHEMAS.get(s, {}).get("question", ""),
                        "tables": len(svc.catalog.tables),
                    }
                    for s, svc in self.services.items()
                ],
                "attack_schema": self.corpus.get("schema"),
                "attacks": attacks,
                "tricky": tricky,
                "agent": {
                    "enabled": self.agent_enabled,
                    "model": self.settings.model,
                    "remaining_today": self.budget.remaining() if self.agent_enabled else 0,
                },
                "max_rows": self.settings.max_rows,
                "stats": self._stats(),
            }
        )

    async def schema(self, request: Request) -> JSONResponse:
        svc = self.services.get(request.query_params.get("schema", ""))
        if svc is None:
            return JSONResponse({"error": "unknown schema"}, status_code=404)
        return JSONResponse(svc.list_schema())

    async def query(self, request: Request) -> JSONResponse:
        if not self.query_limiter.allow(_client_key(request)):
            return JSONResponse({"error": "Too many queries; wait a minute and try again."}, status_code=429)
        body = await _json_body(request)
        svc = self.services.get(str(body.get("schema", "")))
        if svc is None:
            return JSONResponse({"error": "unknown schema"}, status_code=404)
        result = await asyncio.to_thread(svc.run_query, str(body.get("sql", "")))
        if not result["ok"]:
            result["layer"] = layer_of(result["error"]["code"])
        return JSONResponse(result)

    async def ask(self, request: Request) -> JSONResponse:
        if not self.agent_enabled:
            return JSONResponse({"error": "Plain-English mode is off on this deployment."}, status_code=403)
        body = await _json_body(request)
        schema, question = str(body.get("schema", "")), str(body.get("question", "")).strip()
        if schema not in self.services:
            return JSONResponse({"error": "unknown schema"}, status_code=404)
        if not question or len(question) > MAX_QUESTION_CHARS:
            return JSONResponse(
                {"error": f"Ask a question of 1-{MAX_QUESTION_CHARS} characters."}, status_code=400
            )
        if not self.ask_limiter.allow(_client_key(request)):
            return JSONResponse(
                {"error": "You've hit the hourly question limit; try again later."}, status_code=429
            )
        if not self.budget.take():
            return JSONResponse(
                {
                    "error": "Today's question budget for this demo is used up. Try the SQL "
                    "playground, which has no limit."
                },
                status_code=429,
            )
        async with self._ask_slots, Client(self.mcp_servers[schema]) as client:
            agent = SQLAgent(
                client,
                model=self.settings.model,
                effort=self.settings.effort,
                max_turns=8,
                max_sql_errors=3,
                messages_api=self._messages,
            )
            result = await agent.ask(question)
        trace = [
            {
                "tool": c.name,
                "input": c.input,
                "ok": c.ok,
                "error_code": c.error_code,
                "layer": layer_of(c.error_code) if c.error_code else None,
            }
            for c in result.tool_calls
        ]
        return JSONResponse(
            {
                "status": result.status,
                "answer": result.answer,
                "sql": result.sql,
                "turns": result.turns,
                "trace": trace,
                "cost_usd": round(result.cost_usd, 6),
                "cache_hit_rate": round(cache_hit_rate(result.usage), 3),
                "latency_ms": round(result.latency_ms),
                "remaining_today": self.budget.remaining(),
                "error": result.error,
            }
        )

    def _stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if s := self.reports.get("safety"):
            out.update(
                attacks_blocked=s.get("unsafe_blocked"),
                attacks=s.get("unsafe_cases"),
                benign_rate=s.get("benign_pass_rate"),
                benign=s.get("benign_cases"),
            )
        if a := self.reports.get("accuracy"):
            out.update(
                accuracy=a.get("exec_accuracy"),
                accuracy_n=a.get("questions"),
                model=a.get("model"),
                cost_per_question=a.get("cost_usd", {}).get("per_question"),
            )
        if g := self.reports.get("agent_safety"):
            out.update(canary_leaks=g.get("canary_leaks"), injection_cases=g.get("cases"))
        return out


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


# The page is self-contained: inline script and styles, no third-party origins.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors https://huggingface.co 'self'"
)
