"""Execution accuracy of the agent on the golden Spider set (needs ANTHROPIC_API_KEY).

Each question runs through the full stack: agent -> MCP (in-process) -> guard ->
executor. The SQL the agent submits is scored by executing it and comparing its
result with the gold query's result (see compare.py).
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcp import Client

from ..agent import SQLAgent
from ..config import Settings
from ..executor import Executor
from ..server import build_server
from ..service import SafeSQLService
from .compare import results_match

SCORING_MAX_ROWS = 10_000


def sample(golden: list[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    """Stratified by database so a small sample still covers every schema."""
    if limit <= 0 or limit >= len(golden):
        return golden
    rng = random.Random(seed)
    by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in golden:
        by_db[g["db_id"]].append(g)
    for rows in by_db.values():
        rng.shuffle(rows)
    picked: list[dict[str, Any]] = []
    while len(picked) < limit:
        for db in sorted(by_db):
            if by_db[db] and len(picked) < limit:
                picked.append(by_db[db].pop())
    return sorted(picked, key=lambda g: g["id"])


def run(
    settings: Settings, golden_path: Path, limit: int = 50, seed: int = 7, concurrency: int = 4
) -> dict[str, Any]:
    golden = [json.loads(x) for x in golden_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    questions = sample(golden, limit, seed)
    return asyncio.run(_run(settings, questions, concurrency, total=len(golden), seed=seed))


async def _run(settings: Settings, questions, concurrency: int, total: int, seed: int) -> dict[str, Any]:
    started = time.time()
    executor = Executor(
        settings.database_url,
        statement_timeout_ms=settings.statement_timeout_ms,
        lock_timeout_ms=settings.lock_timeout_ms,
        max_cost=settings.max_cost,
        pool_size=max(4, concurrency + 2),
    )
    agent_services: dict[str, SafeSQLService] = {}
    scoring_services: dict[str, SafeSQLService] = {}
    for schema in sorted({q["schema"] for q in questions}):
        s = settings.with_schemas(schema)
        agent_services[schema] = SafeSQLService(s, executor)
        scoring_services[schema] = SafeSQLService(
            replace(s, max_rows=SCORING_MAX_ROWS), executor, catalog=agent_services[schema].catalog
        )
    sem = asyncio.Semaphore(concurrency)

    async def one(q: dict[str, Any]) -> dict[str, Any]:
        async with sem, Client(build_server(agent_services[q["schema"]])) as client:
            agent = SQLAgent(
                client,
                model=settings.model,
                effort=settings.effort,
                max_turns=settings.max_turns,
                max_sql_errors=settings.max_sql_errors,
            )
            result = await agent.ask(q["question"])
        scorer = scoring_services[q["schema"]]
        correct, reason = False, result.status
        if result.sql:
            pred = await asyncio.to_thread(scorer.run_query, result.sql)
            gold = await asyncio.to_thread(scorer.run_query, q["gold_sql"])
            if not pred["ok"]:
                reason = f"pred_failed:{pred['error']['code']}"
            elif not gold["ok"]:
                reason = f"gold_failed:{gold['error']['code']}"
            else:
                correct = results_match(gold["rows"], pred["rows"], q["order_matters"])
                reason = "correct" if correct else "wrong_result"
        print(
            f"{'PASS' if correct else 'FAIL'} {q['id']} [{reason}] {result.turns} turns ${result.cost_usd:.4f}",
            flush=True,
        )
        return {
            "id": q["id"],
            "db_id": q["db_id"],
            "question": q["question"],
            "gold_sql": q["gold_sql"],
            "pred_sql": result.sql,
            "correct": correct,
            "reason": reason,
            **{k: v for k, v in result.summary().items() if k not in ("question", "sql")},
        }

    rows = await asyncio.gather(*(one(q) for q in questions))
    executor.close()
    return _report(settings, rows, total, seed, time.time() - started)


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p * len(s)))], 1)


def _report(
    settings: Settings, rows: list[dict[str, Any]], total: int, seed: int, duration: float
) -> dict[str, Any]:
    n = len(rows)
    correct = sum(r["correct"] for r in rows)
    with_errors = [r for r in rows if r["sql_errors"] > 0]
    per_db: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        per_db[r["db_id"]][0] += int(r["correct"])
        per_db[r["db_id"]][1] += 1
    costs = [r["cost_usd"] for r in rows]
    latencies = [r["latency_ms"] for r in rows]
    return {
        "eval": "accuracy",
        "model": settings.model,
        "effort": settings.effort,
        "questions": n,
        "golden_pool": total,
        "seed": seed,
        "exec_accuracy": round(correct / n, 4) if n else 0.0,
        "correct": correct,
        "answered_rate": round(sum(r["status"] == "answered" for r in rows) / n, 4) if n else 0.0,
        "avg_turns": round(statistics.fmean(r["turns"] for r in rows), 2) if n else 0,
        "avg_tool_calls": round(statistics.fmean(len(r["tool_calls"]) for r in rows), 2) if n else 0,
        "questions_with_sql_errors": len(with_errors),
        "recovered_after_error": sum(r["correct"] for r in with_errors),
        "guard_rejections": sum(r["guard_rejections"] for r in rows),
        "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95)},
        "cost_usd": {"total": round(sum(costs), 4), "per_question": round(sum(costs) / n, 5) if n else 0},
        "tokens": {k: sum(r["usage"][k] for r in rows) for k in rows[0]["usage"]} if rows else {},
        "status_counts": {
            s: sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})
        },
        "per_database": {db: {"correct": c, "total": t} for db, (c, t) in sorted(per_db.items())},
        "duration_s": round(duration, 1),
        "results": rows,
    }


def markdown(report: dict[str, Any]) -> str:
    lat, cost = report["latency_ms"], report["cost_usd"]
    lines = [
        "### Accuracy eval (Spider dev, Postgres port)",
        "",
        "| metric | value |",
        "|---|---|",
        f"| model | `{report['model']}` (effort {report['effort']}) |",
        f"| execution accuracy | **{report['exec_accuracy']:.1%}** ({report['correct']}/{report['questions']}) |",
        f"| answered | {report['answered_rate']:.1%} |",
        f"| avg turns / tool calls | {report['avg_turns']} / {report['avg_tool_calls']} |",
        f"| questions that hit a SQL error | {report['questions_with_sql_errors']} "
        f"(recovered and correct: {report['recovered_after_error']}) |",
        f"| latency p50 / p95 | {lat['p50']} ms / {lat['p95']} ms |",
        f"| cost per question | ${cost['per_question']:.4f} (total ${cost['total']:.2f}) |",
    ]
    return "\n".join(lines) + "\n"
