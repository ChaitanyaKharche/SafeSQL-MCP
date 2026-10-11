"""Query execution with database-side guardrails.

Even after the static guard approves a query, it runs under four more layers:

- a role that only has SELECT grants and defaults to read-only transactions
- an explicit READ ONLY transaction that is always rolled back
- transaction-local statement_timeout / lock_timeout and a pinned search_path
- an EXPLAIN cost ceiling checked before the query runs

Queries are sent as prepared statements (extended query protocol), which
Postgres refuses to run with more than one statement, so stacked queries fail
here too even if the guard were bypassed.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import errors, sql
from psycopg_pool import ConnectionPool

from .catalog import Catalog, load_catalog
from .guard import Blocked
from .telemetry import tracer


@dataclass(frozen=True)
class ExecResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    cost: float
    elapsed_ms: float


class Executor:
    def __init__(
        self,
        dsn: str,
        *,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 1_000,
        max_cost: float = 50_000.0,
        pool_size: int = 4,
    ):
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms
        self.max_cost = max_cost
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=pool_size,
            kwargs={"autocommit": False, "application_name": "safesql-mcp"},
            configure=_configure,
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    def load_catalog(self, schemas: Sequence[str]) -> Catalog:
        with self.pool.connection() as conn:
            return load_catalog(conn, schemas)

    def execute(self, query: str, *, search_path: Sequence[str], max_rows: int) -> ExecResult:
        """Run an already-guarded query. Raises Blocked for guardrail hits, psycopg.Error otherwise."""
        with self.pool.connection() as conn:
            try:
                with conn.cursor() as cur:
                    self._set_locals(cur, conn, search_path)
                    cost = self._explain_cost(cur, query)
                    if cost > self.max_cost:
                        raise Blocked(
                            "cost_limit",
                            f"Estimated query cost {cost:,.0f} exceeds the limit of {self.max_cost:,.0f}.",
                            "Add filters, avoid cross joins, or aggregate before joining.",
                        )
                    with tracer.start_as_current_span("db.execute") as span:
                        start = time.perf_counter()
                        cur.execute(query, prepare=True)
                        rows = cur.fetchmany(max_rows + 1)
                        elapsed = (time.perf_counter() - start) * 1000
                        span.set_attribute("db.response.returned_rows", min(len(rows), max_rows))
                    columns = [d.name for d in cur.description or []]
                truncated = len(rows) > max_rows
                return ExecResult(columns, rows[:max_rows], truncated, cost, elapsed)
            except errors.QueryCanceled:
                raise Blocked(
                    "timeout",
                    f"Query exceeded the {self.statement_timeout_ms} ms statement timeout.",
                    "Simplify the query or filter earlier.",
                ) from None
            except errors.ReadOnlySqlTransaction:
                raise Blocked(
                    "read_only_violation", "The database rejected a write in a read-only transaction."
                ) from None
            except errors.InsufficientPrivilege:
                raise Blocked(
                    "permission_denied", "The read-only role is not permitted to do that."
                ) from None
            finally:
                conn.rollback()

    def _set_locals(self, cur: psycopg.Cursor, conn: psycopg.Connection, search_path: Sequence[str]) -> None:
        path = sql.SQL(", ").join(sql.Identifier(s) for s in search_path).as_string(conn)
        cur.execute(
            "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true),"
            " set_config('search_path', %s, true)",
            (str(self.statement_timeout_ms), str(self.lock_timeout_ms), path),
        )

    def _explain_cost(self, cur: psycopg.Cursor, query: str) -> float:
        with tracer.start_as_current_span("db.explain") as span:
            cur.execute("EXPLAIN (FORMAT JSON) " + query, prepare=True)
            plan = cur.fetchone()[0][0]["Plan"]
            cost = float(plan["Total Cost"])
            span.set_attribute("db.plan.total_cost", cost)
            span.set_attribute("db.plan.rows", float(plan.get("Plan Rows", 0)))
            return cost


def _configure(conn: psycopg.Connection) -> None:
    # Every transaction psycopg opens on this connection starts as BEGIN READ ONLY.
    conn.read_only = True
    conn.commit()
