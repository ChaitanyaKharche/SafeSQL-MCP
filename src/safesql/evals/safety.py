"""Deterministic safety eval (no LLM).

1. Adversarial corpus: every 'block' case must be rejected, every 'allow' case must run.
2. Benign set: every golden Spider query must pass the guard and execute (false-positive rate).
3. DB layer only: the attack corpus is replayed straight against the read-only role with no
   guard, to measure what the database layer catches on its own (reported, not gated).
4. Integrity: table checksums before and after must be identical.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from ..config import Settings
from ..executor import Executor
from ..service import SafeSQLService, layer_of

INTEGRITY_SCHEMAS = ("spider_concert_singer", "inject_demo", "secrets")


def snapshot(admin_url: str) -> dict[str, str]:
    """md5 of every row of every table in the integrity schemas, plus role attributes."""
    out: dict[str, str] = {}
    with psycopg.connect(admin_url, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = ANY(%s) "
            "ORDER BY 1, 2",
            (list(INTEGRITY_SCHEMAS),),
        ).fetchall()
        for schema, table in tables:
            q = sql.SQL(
                "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {} t"
            ).format(sql.Identifier(schema, table))
            out[f"{schema}.{table}"] = conn.execute(q).fetchone()[0]
        role = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolconfig::text FROM pg_roles "
            "WHERE rolname = 'safesql_reader'"
        ).fetchone()
        out["role:safesql_reader"] = repr(role)
    return out


def _db_layer_only(reader_url: str, cases: list[dict], schema: str) -> dict[str, Any]:
    outcomes: Counter[str] = Counter()
    executed: list[str] = []
    logging.getLogger("psycopg").setLevel(logging.ERROR)  # COPY TO STDOUT leaves noisy cleanup logs
    for case in cases:
        try:
            with psycopg.connect(reader_url, autocommit=True, options="-c statement_timeout=2000") as conn:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                conn.execute(case["sql"])  # raw, simple protocol, no guard
            outcomes["executed"] += 1
            executed.append(case["id"])
        except psycopg.Error as e:
            if e.sqlstate is None:
                # Raised client-side (e.g. psycopg refusing a COPY the server already started):
                # the database itself did not block it.
                outcomes["executed"] += 1
                executed.append(case["id"])
            else:
                outcomes[type(e).__name__] += 1
    n = len(cases)
    return {
        "cases": n,
        "blocked": n - outcomes["executed"],
        "block_rate": round((n - outcomes["executed"]) / n, 4) if n else 1.0,
        "outcomes": dict(outcomes),
        "executed_ids": executed,
    }


def run(
    settings: Settings, admin_url: str, corpus_path: Path, golden_path: Path, db_layer: bool = True
) -> dict[str, Any]:
    started = time.time()
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    golden = [
        json.loads(line) for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    executor = Executor(
        settings.database_url,
        statement_timeout_ms=settings.statement_timeout_ms,
        lock_timeout_ms=settings.lock_timeout_ms,
        max_cost=settings.max_cost,
    )
    before = snapshot(admin_url)

    # 1. adversarial corpus
    svc = SafeSQLService(settings.with_schemas(corpus["schema"]), executor)
    results, by_category = [], defaultdict(lambda: {"cases": 0, "correct": 0})
    for case in corpus["cases"]:
        res = svc.run_query(case["sql"])
        code = "ok" if res["ok"] else res["error"]["code"]
        correct = (not res["ok"]) if case["expect"] == "block" else res["ok"]
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expect": case["expect"],
                "outcome": "allowed" if res["ok"] else "blocked",
                "code": code,
                "layer": None if res["ok"] else layer_of(code),
                "correct": correct,
            }
        )
        by_category[case["category"]]["cases"] += 1
        by_category[case["category"]]["correct"] += int(correct)
    unsafe = [r for r in results if r["expect"] == "block"]
    tricky = [r for r in results if r["expect"] == "allow"]

    # 2. benign golden queries, scoped to each question's own schema
    services: dict[str, SafeSQLService] = {}
    benign_failures, costs = [], []
    for g in golden:
        if g["schema"] not in services:
            services[g["schema"]] = SafeSQLService(settings.with_schemas(g["schema"]), executor)
        res = services[g["schema"]].run_query(g["gold_sql"])
        if res["ok"]:
            costs.append(res["estimated_cost"])
        else:
            benign_failures.append(
                {
                    "id": g["id"],
                    "code": res["error"]["code"],
                    "message": res["error"]["message"][:200],
                    "sql": g["gold_sql"],
                }
            )

    # 3. database layer alone
    db_only = (
        _db_layer_only(
            settings.database_url, [c for c in corpus["cases"] if c["expect"] == "block"], corpus["schema"]
        )
        if db_layer
        else None
    )

    after = snapshot(admin_url)
    executor.close()
    changed = sorted(k for k in before if before.get(k) != after.get(k)) + sorted(set(after) - set(before))

    costs.sort()

    def pct(p: float) -> float | None:
        return round(costs[min(len(costs) - 1, int(p * len(costs)))], 1) if costs else None

    report = {
        "eval": "safety",
        "unsafe_cases": len(unsafe),
        "unsafe_blocked": sum(r["correct"] for r in unsafe),
        "unsafe_block_rate": round(sum(r["correct"] for r in unsafe) / len(unsafe), 4),
        "tricky_benign_cases": len(tricky),
        "tricky_benign_pass_rate": round(sum(r["correct"] for r in tricky) / len(tricky), 4)
        if tricky
        else 1.0,
        "benign_cases": len(golden),
        "benign_pass_rate": round((len(golden) - len(benign_failures)) / len(golden), 4) if golden else 1.0,
        "integrity_ok": not changed,
        "integrity_changed": changed,
        "blocked_by_layer": dict(Counter(r["layer"] for r in unsafe if r["layer"])),
        "blocked_by_code": dict(Counter(r["code"] for r in unsafe if r["outcome"] == "blocked")),
        "by_category": dict(by_category),
        "misses": [r for r in results if not r["correct"]],
        "benign_failures": benign_failures[:50],
        "benign_failure_codes": dict(Counter(f["code"] for f in benign_failures)),
        "benign_cost": {
            "p50": pct(0.5),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": costs[-1] if costs else None,
            "mean": round(statistics.fmean(costs), 1) if costs else None,
        },
        "max_cost_setting": settings.max_cost,
        "db_layer_only": db_only,
        "duration_s": round(time.time() - started, 1),
        "results": results,
    }
    return report


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "### Safety eval",
        "",
        "| metric | value |",
        "|---|---|",
        f"| unsafe queries blocked | {report['unsafe_blocked']}/{report['unsafe_cases']} "
        f"({report['unsafe_block_rate']:.1%}) |",
        f"| tricky-but-benign allowed | {report['tricky_benign_pass_rate']:.1%} of {report['tricky_benign_cases']} |",
        f"| golden Spider queries allowed (false-positive check) | {report['benign_pass_rate']:.2%} "
        f"of {report['benign_cases']} |",
        f"| database unchanged after the run | {'yes' if report['integrity_ok'] else 'NO'} |",
    ]
    if report.get("db_layer_only"):
        d = report["db_layer_only"]
        lines.append(
            f"| DB layer alone (guard off) blocks | {d['blocked']}/{d['cases']} ({d['block_rate']:.1%}) |"
        )
    lines += ["", "| category | correct |", "|---|---|"]
    for cat, v in report["by_category"].items():
        lines.append(f"| {cat} | {v['correct']}/{v['cases']} |")
    lines += ["", "Blocked by layer: " + ", ".join(f"{k} {v}" for k, v in report["blocked_by_layer"].items())]
    if report["misses"]:
        lines += ["", "**Misses:** " + ", ".join(m["id"] for m in report["misses"])]
    return "\n".join(lines) + "\n"
