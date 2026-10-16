"""Unit tests for the static SQL guard (no database needed)."""

import json
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from safesql.catalog import Catalog, Column, Table
from safesql.guard import FORBIDDEN_NODES, Blocked, SqlGuard

SCHEMA = "spider_concert_singer"


def _table(name, *cols):
    return Table(SCHEMA, name, tuple(Column(c, "text", True) for c in cols))


CATALOG = Catalog.from_tables(
    [SCHEMA],
    [
        _table("singer", "singer_id", "name", "country", "song_name", "song_release_year", "age", "is_male"),
        _table("concert", "concert_id", "concert_name", "theme", "stadium_id", "year"),
        _table("stadium", "stadium_id", "location", "name", "capacity", "highest", "lowest", "average"),
        _table("singer_in_concert", "concert_id", "singer_id"),
    ],
)


@pytest.fixture
def guard():
    return SqlGuard(CATALOG, max_rows=200)


def blocked(guard, sql) -> str:
    with pytest.raises(Blocked) as info:
        guard.check(sql)
    return info.value.code


def test_forbidden_node_names_exist_in_sqlglot():
    missing = [n for n in FORBIDDEN_NODES if not hasattr(exp, n)]
    assert not missing, f"sqlglot no longer defines {missing}; update FORBIDDEN_NODES"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM singer",
        "select s.name, c.concert_name from singer s join singer_in_concert sc on s.singer_id = sc.singer_id "
        "join concert c on c.concert_id = sc.concert_id",
        "WITH t AS (SELECT * FROM singer) SELECT count(*) FROM t",
        "SELECT name FROM singer UNION SELECT name FROM stadium",
        "SELECT name, rank() OVER (ORDER BY age) FROM singer",
        "SELECT country, count(*) FROM singer GROUP BY country HAVING count(*) > 1",
        "SELECT name FROM singer WHERE age > (SELECT avg(age) FROM singer)",
        "SELECT * FROM generate_series(1, 3) g",
        "SELECT name FROM singer WHERE name ILIKE '%a%'",
        "SELECT date_trunc('year', now()), extract(year FROM now()), coalesce(name, '') FROM singer",
        "SELECT CASE WHEN age > 30 THEN 'old' ELSE 'young' END FROM singer",
    ],
)
def test_allows_read_only_queries(guard, sql):
    g = guard.check(sql)
    assert sqlglot.parse_one(g.sql, read="postgres") is not None


def test_injects_limit_when_missing(guard):
    assert guard.check("SELECT name FROM singer").sql.endswith("LIMIT 201")


def test_keeps_small_limit(guard):
    g = guard.check("SELECT name FROM singer LIMIT 5")
    assert g.sql.endswith("LIMIT 5") and g.limit_applied is None


def test_tightens_large_limit(guard):
    assert guard.check("SELECT name FROM singer LIMIT 100000").sql.endswith("LIMIT 201")


def test_limits_set_operations_and_fetch(guard):
    assert guard.check("SELECT name FROM singer UNION SELECT name FROM stadium").sql.endswith("LIMIT 201")
    assert "201" in guard.check("SELECT name FROM singer FETCH FIRST 5000 ROWS ONLY").sql


def test_wraps_parenthesised_root(guard):
    g = guard.check("(SELECT name FROM singer)")
    assert g.sql.startswith("SELECT * FROM") and g.sql.endswith("LIMIT 201")


def test_strips_comments_from_executed_sql(guard):
    g = guard.check("SELECT 1 -- */ ; DROP TABLE singer")
    assert "DROP" not in g.sql and "--" not in g.sql and "/*" not in g.sql


def test_records_referenced_tables(guard):
    g = guard.check("SELECT * FROM singer s JOIN concert c ON true")
    assert g.tables == {f"{SCHEMA}.singer", f"{SCHEMA}.concert"}


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT 1; DROP TABLE singer", "multiple_statements"),
        ("DELETE FROM singer", "statement_not_allowed"),
        ("DROP TABLE singer", "statement_not_allowed"),
        ("WITH d AS (DELETE FROM singer RETURNING *) SELECT * FROM d", "forbidden_operation"),
        ("SELECT * INTO stolen FROM singer", "forbidden_operation"),
        ("SELECT * FROM singer FOR UPDATE", "forbidden_operation"),
        ("DO $$ BEGIN DELETE FROM singer; END $$", "statement_not_allowed"),
        ("SELECT pg_sleep(10)", "dangerous_function"),
        ("SELECT pg_catalog.pg_sleep(10)", "dangerous_function"),
        ("SELECT set_config('statement_timeout', '0', false)", "dangerous_function"),
        ("SELECT version()", "dangerous_function"),
        ("SELECT current_user", "dangerous_function"),
        ("SELECT repeat('x', 100000000)", "function_not_allowed"),
        ("SELECT format('%100000000s', '')", "function_not_allowed"),
        ("SELECT format('%*s', 100000000, '')", "function_not_allowed"),
        ("SELECT my_udf(1)", "function_not_allowed"),
        ("SELECT * FROM pg_user", "unknown_table"),
        ("SELECT * FROM information_schema.tables", "unknown_table"),
        ("SELECT * FROM secrets.api_keys", "unknown_table"),
        ("SELECT * FROM otherdb.secrets.api_keys", "cross_database"),
        (
            "SELECT * FROM (WITH pg_user AS (SELECT 1 AS a) SELECT * FROM pg_user) a, pg_user b",
            "unknown_table",
        ),
        ("SELEC name FROM singer", "parse_error"),
        ("", "empty_query"),
        ("SELECT 1\x00", "invalid_characters"),
    ],
)
def test_blocks(guard, sql, code):
    assert blocked(guard, sql) == code


def test_cte_name_is_not_a_table(guard):
    guard.check("WITH pg_user AS (SELECT name FROM singer) SELECT * FROM pg_user")


def test_rejects_overlong_sql():
    g = SqlGuard(CATALOG, max_sql_chars=50)
    assert blocked(g, "SELECT name FROM singer WHERE name = '" + "x" * 100 + "'") == "too_long"


def test_unknown_table_hint_lists_tables(guard):
    with pytest.raises(Blocked) as info:
        guard.check("SELECT * FROM singers")
    assert "singer" in info.value.hint


CORPUS = json.loads(
    (Path(__file__).parents[1] / "evals/data/adversarial_sql.json").read_text(encoding="utf-8")
)
RUNTIME_ONLY = {"resource_exhaustion"}  # caught by the cost cap / timeout, which need a database


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["id"])
def test_adversarial_corpus_static(guard, case):
    if case["category"] in RUNTIME_ONLY:
        pytest.skip("needs the database (cost cap / timeout)")
    if case["expect"] == "block":
        blocked(guard, case["sql"])
    else:
        guard.check(case["sql"])
