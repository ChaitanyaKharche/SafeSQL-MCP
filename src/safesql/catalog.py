"""Schema catalog: the set of tables the agent is allowed to see.

The catalog is loaded with the read-only role, so it only contains tables that
role has been granted SELECT on, filtered further to the configured schemas.
The guard uses it as the table allowlist.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import psycopg


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    schema: str
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    row_estimate: int | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class Catalog:
    """Tables visible to the agent, keyed by (schema, table)."""

    schemas: tuple[str, ...]
    tables: dict[tuple[str, str], Table] = field(default_factory=dict)

    @classmethod
    def from_tables(cls, schemas: Iterable[str], tables: Iterable[Table]) -> Catalog:
        schemas = tuple(schemas)
        return cls(schemas, {(t.schema, t.name): t for t in tables if t.schema in schemas})

    def resolve(self, name: str, schema: str | None = None) -> Table | None:
        """Resolve a table reference the way Postgres would, but only within the allowlist.

        Unqualified names are looked up along the configured schemas in order, mirroring the
        search_path the executor sets.
        """
        if schema is not None:
            return self.tables.get((schema, name))
        for s in self.schemas:
            t = self.tables.get((s, name))
            if t is not None:
                return t
        return None

    def find(self, ref: str) -> Table | None:
        """Look up a user-supplied 'table' or 'schema.table' string (case-insensitive)."""
        ref = ref.strip().strip('"')
        if "." in ref:
            schema, _, name = ref.partition(".")
            return self.resolve(name.strip('"'), schema.strip('"')) or self.resolve(
                name.strip('"').lower(), schema.strip('"').lower()
            )
        return self.resolve(ref) or self.resolve(ref.lower())

    def display_name(self, t: Table) -> str:
        return t.name if len(self.schemas) == 1 else t.qualified

    def names(self) -> list[str]:
        return sorted(self.display_name(t) for t in self.tables.values())


_COLUMNS_SQL = """
SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable = 'YES'
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = ANY(%(schemas)s) AND t.table_type IN ('BASE TABLE', 'VIEW')
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

_CONSTRAINTS_SQL = """
SELECT n.nspname, cl.relname, con.contype,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             ORDER BY k.ord)::text[],
       fn.nspname, fcl.relname,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
             ORDER BY k.ord)::text[]
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
WHERE con.contype IN ('p', 'f') AND n.nspname = ANY(%(schemas)s)
"""

_ROWS_SQL = """
SELECT n.nspname, c.relname, c.reltuples::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%(schemas)s) AND c.relkind IN ('r', 'v', 'm', 'p')
"""


def load_catalog(conn: psycopg.Connection, schemas: Iterable[str]) -> Catalog:
    schemas = list(schemas)
    params = {"schemas": schemas}
    cols: dict[tuple[str, str], list[Column]] = {}
    pks: dict[tuple[str, str], tuple[str, ...]] = {}
    fks: dict[tuple[str, str], list[ForeignKey]] = {}
    rows: dict[tuple[str, str], int] = {}
    with conn.cursor() as cur:
        cur.execute(_COLUMNS_SQL, params)
        for schema, table, col, dtype, nullable in cur.fetchall():
            cols.setdefault((schema, table), []).append(Column(col, dtype, nullable))
        cur.execute(_CONSTRAINTS_SQL, params)
        for schema, table, kind, keys, fschema, ftable, fkeys in cur.fetchall():
            if kind == "p":
                pks[(schema, table)] = tuple(keys)
            else:
                fks.setdefault((schema, table), []).append(
                    ForeignKey(tuple(keys), fschema, ftable, tuple(fkeys))
                )
        cur.execute(_ROWS_SQL, params)
        for schema, table, n in cur.fetchall():
            rows[(schema, table)] = max(int(n), 0) if n is not None and n >= 0 else None
    conn.rollback()
    tables = [
        Table(s, t, tuple(c), pks.get((s, t), ()), tuple(fks.get((s, t), [])), rows.get((s, t)))
        for (s, t), c in cols.items()
    ]
    return Catalog.from_tables(schemas, tables)
