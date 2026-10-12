"""Runtime settings, read from environment variables (all prefixed SAFESQL_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SAFESQL_{name}", default)


@dataclass(frozen=True)
class Settings:
    # DSN for the *read-only* role. The server never needs anything stronger.
    database_url: str = "postgresql://safesql_reader:safesql_reader@localhost:5432/safesql"
    # Schemas the agent may see. Everything else is invisible and blocked.
    schemas: tuple[str, ...] = ("public",)

    # Guardrails
    max_rows: int = 200  # rows returned to the client; LIMIT is injected above this
    max_cost: float = 50_000.0  # EXPLAIN total-cost ceiling (planner units)
    statement_timeout_ms: int = 5_000
    lock_timeout_ms: int = 1_000
    max_sql_chars: int = 8_000
    max_cell_chars: int = 300  # long text cells are truncated in tool output
    sample_rows: int = 3  # sample rows shown by describe_table

    # Agent
    model: str = "claude-haiku-5-5"  # pinned; see agent.PRICING
    effort: str = "medium"
    max_turns: int = 12
    max_sql_errors: int = 4

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        return cls(
            database_url=_env("DATABASE_URL", d.database_url),
            schemas=tuple(s.strip() for s in _env("SCHEMAS", ",".join(d.schemas)).split(",") if s.strip()),
            max_rows=int(_env("MAX_ROWS", str(d.max_rows))),
            max_cost=float(_env("MAX_COST", str(d.max_cost))),
            statement_timeout_ms=int(_env("STATEMENT_TIMEOUT_MS", str(d.statement_timeout_ms))),
            lock_timeout_ms=int(_env("LOCK_TIMEOUT_MS", str(d.lock_timeout_ms))),
            max_sql_chars=int(_env("MAX_SQL_CHARS", str(d.max_sql_chars))),
            max_cell_chars=int(_env("MAX_CELL_CHARS", str(d.max_cell_chars))),
            sample_rows=int(_env("SAMPLE_ROWS", str(d.sample_rows))),
            model=_env("MODEL", d.model),
            effort=_env("EFFORT", d.effort),
            max_turns=int(_env("MAX_TURNS", str(d.max_turns))),
            max_sql_errors=int(_env("MAX_SQL_ERRORS", str(d.max_sql_errors))),
        )

    def with_schemas(self, *schemas: str) -> Settings:
        return replace(self, schemas=tuple(schemas))
