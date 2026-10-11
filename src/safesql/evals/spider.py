"""Port the Spider dev set to Postgres and build the golden question -> SQL file.

Spider ships SQLite databases and SQLite-flavoured gold SQL. This module:

1. loads each dev database into its own Postgres schema (spider_<db_id>), with
   column types inferred from SQLite's actual storage classes;
2. translates every gold query to PostgreSQL with sqlglot, fixing two SQLite
   quirks Spider relies on: double-quoted string literals and case-insensitive
   LIKE;
3. runs the original on SQLite and the translation on Postgres, and keeps only
   questions whose results match. Nothing is hand-edited.

Spider is (c) Yu et al. 2018, CC BY-SA 4.0: https://yale-lily.github.io/spider
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import sqlglot
from psycopg import sql
from sqlglot import exp

from .compare import order_matters, results_match

BIGINT_MAX = 2**63 - 1


def schema_name(db_id: str) -> str:
    return "spider_" + db_id.lower()


def _infer_type(declared: str, values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    kinds = {type(v) for v in present}
    if not present:
        d = (declared or "").upper()
        if "INT" in d:
            return "bigint"
        if any(k in d for k in ("REAL", "FLOA", "DOUB")):
            return "double precision"
        return "text"
    if kinds <= {str}:
        # Spider declares some numeric columns as text (e.g. concert.year); SQLite's type
        # affinity hides that, Postgres would not. Promote text that is always a canonical
        # number. If that changes a query's meaning, cross-engine validation drops it.
        if all(_canonical_int(v) for v in present):
            kinds = {int}
            present = [int(v) for v in present]
        elif all(_canonical_int(v) or _canonical_float(v) for v in present):
            return "double precision"
    if kinds <= {int}:
        return "bigint" if all(abs(v) <= BIGINT_MAX for v in present) else "numeric"
    if kinds <= {int, float}:
        return "double precision"
    return "text"


def _canonical_int(s: str) -> bool:
    try:
        return str(int(s)) == s
    except ValueError:
        return False


def _canonical_float(s: str) -> bool:
    try:
        f = float(s)
    except ValueError:
        return False
    return f == f and abs(f) != float("inf") and s.strip() == s and "e" not in s.lower() and repr(f) == s


def _coerce(v: Any, pg_type: str) -> Any:
    if v is None:
        return None
    if pg_type == "text":
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="replace")
        return v if isinstance(v, str) else str(v)
    if isinstance(v, str):
        return int(v) if pg_type in ("bigint", "numeric") else float(v)
    return v


def load_database(pg: psycopg.Connection, sqlite_path: Path, schema: str) -> dict[str, list[str]]:
    """Copy one SQLite database into a fresh Postgres schema. Returns {table: [columns]}."""
    src = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    src.text_factory = lambda b: b.decode("utf-8", errors="replace")
    s = sql.Identifier(schema)
    pg.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(s))
    pg.execute(sql.SQL("CREATE SCHEMA {}").format(s))
    tables = [
        r[0]
        for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    layout: dict[str, list[str]] = {}
    for t in tables:
        info = src.execute(f'PRAGMA table_info("{t}")').fetchall()  # cid, name, type, notnull, dflt, pk
        rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
        cols = [c[1].lower() for c in info]
        types = [_infer_type(c[2], [r[i] for r in rows]) for i, c in enumerate(info)]
        tname = sql.Identifier(schema, t.lower())
        pg.execute(
            sql.SQL("CREATE TABLE {} ({})").format(
                tname,
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(ty))
                    for c, ty in zip(cols, types, strict=True)
                ),
            )
        )
        with pg.cursor().copy(sql.SQL("COPY {} FROM STDIN").format(tname)) as copy:
            for r in rows:
                copy.write_row([_coerce(v, ty) for v, ty in zip(r, types, strict=True)])
        pk = [c[1].lower() for c in sorted(info, key=lambda c: c[5]) if c[5] > 0]
        if pk:
            _try(
                pg,
                sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
                    tname, sql.SQL(", ").join(map(sql.Identifier, pk))
                ),
            )
        layout[t.lower()] = cols
    for t in tables:
        fks: dict[int, list[tuple]] = {}
        for fk in src.execute(f'PRAGMA foreign_key_list("{t}")').fetchall():  # id, seq, table, from, to, ...
            fks.setdefault(fk[0], []).append(fk)
        for parts in fks.values():
            parts.sort(key=lambda p: p[1])
            ref_table = parts[0][2].lower()
            from_cols = [p[3].lower() for p in parts]
            to_cols = [(p[4] or p[3]).lower() for p in parts]
            if ref_table not in layout:
                continue
            _try(
                pg,
                sql.SQL("ALTER TABLE {} ADD FOREIGN KEY ({}) REFERENCES {} ({}) NOT VALID").format(
                    sql.Identifier(schema, t.lower()),
                    sql.SQL(", ").join(map(sql.Identifier, from_cols)),
                    sql.Identifier(schema, ref_table),
                    sql.SQL(", ").join(map(sql.Identifier, to_cols)),
                ),
            )
    pg.execute(sql.SQL("ANALYZE"))  # fresh stats so EXPLAIN costs are meaningful
    src.close()
    return layout


def _try(pg: psycopg.Connection, stmt) -> bool:
    try:
        with pg.transaction():
            pg.execute(stmt)
        return True
    except psycopg.Error:
        return False


def translate(gold_sql: str, columns: set[str]) -> str:
    tree = sqlglot.parse_one(gold_sql, read="sqlite")
    # SQLite accepts "double quotes" for string literals when no such column exists.
    for col in list(tree.find_all(exp.Column)):
        ident = col.this
        if (
            isinstance(ident, exp.Identifier)
            and ident.quoted
            and not col.table
            and ident.name.lower() not in columns
        ):
            col.replace(exp.Literal.string(ident.name))
    # Tables and columns were created lowercase.
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
    # SQLite's LIKE is case-insensitive for ASCII; Postgres needs ILIKE for the same meaning.
    for like in list(tree.find_all(exp.Like)):
        like.replace(exp.ILike(this=like.this, expression=like.expression))
    return tree.sql(dialect="postgres")


def build(spider_dir: Path, admin_url: str, out_path: Path, split: str = "dev") -> dict[str, Any]:
    examples = json.loads((spider_dir / f"{split}.json").read_text(encoding="utf-8"))
    db_ids = sorted({e["db_id"] for e in examples})
    reasons: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    with psycopg.connect(admin_url, autocommit=True) as pg:
        layouts = {}
        for db_id in db_ids:
            path = spider_dir / "database" / db_id / f"{db_id}.sqlite"
            layouts[db_id] = load_database(pg, path, schema_name(db_id))
            print(f"loaded {db_id}: {len(layouts[db_id])} tables")

        sqlite_conns: dict[str, sqlite3.Connection] = {}
        for idx, ex in enumerate(examples):
            db_id, gold = ex["db_id"], ex["query"].strip().rstrip(";")
            schema = schema_name(db_id)
            columns = {c for cols in layouts[db_id].values() for c in cols} | set(layouts[db_id])
            if db_id not in sqlite_conns:
                p = spider_dir / "database" / db_id / f"{db_id}.sqlite"
                sqlite_conns[db_id] = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
                sqlite_conns[db_id].text_factory = lambda b: b.decode("utf-8", errors="replace")
            try:
                expected = sqlite_conns[db_id].execute(gold).fetchall()
            except sqlite3.Error:
                reasons["sqlite_error"] += 1
                continue
            try:
                pg_sql = translate(gold, columns)
            except Exception:
                reasons["translation_error"] += 1
                continue
            try:
                with pg.transaction():
                    pg.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
                    pg.execute("SET LOCAL statement_timeout = '30s'")
                    got = pg.execute(pg_sql).fetchall()
            except psycopg.Error:
                reasons["postgres_error"] += 1
                continue
            ordered = order_matters(pg_sql)
            if not results_match(expected, got, ordered):
                reasons["result_mismatch"] += 1
                continue
            if not got:
                reasons["empty_result"] += 1
                continue
            kept.append(
                {
                    "id": f"spider-{split}-{idx:04d}",
                    "db_id": db_id,
                    "schema": schema,
                    "question": ex["question"],
                    "gold_sql": pg_sql,
                    "gold_sql_sqlite": gold,
                    "order_matters": ordered,
                    "gold_rows": len(got),
                }
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "split": split,
        "source_examples": len(examples),
        "kept": len(kept),
        "dropped": dict(reasons),
        "databases": len(db_ids),
        "kept_per_database": dict(Counter(r["db_id"] for r in kept)),
    }
    out_path.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
