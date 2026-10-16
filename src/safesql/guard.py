"""Static SQL guard: parse, allowlist, rewrite.

Every query an agent sends goes through `SqlGuard.check` before it reaches the
database. The guard never pattern-matches raw SQL text. It parses the query into
a syntax tree with sqlglot and makes decisions on the tree:

1. Exactly one statement, and its root must be a SELECT or a set operation.
2. No node anywhere in the tree may write, lock, change session state or be
   something the parser could not understand (sqlglot's `Command` fallback).
   This catches data-modifying CTEs, SELECT INTO and FOR UPDATE.
3. Every function call must be on an allowlist, judged by the name it renders
   to in PostgreSQL, and a denylist of dangerous functions (pg_sleep,
   pg_read_file, dblink, set_config, query_to_xml, ...) gives those a clearer
   error.
4. Every real table reference (CTE references are resolved per scope) must be
   in the catalog of allowed tables. System catalogs and other schemas are
   invisible.
5. A LIMIT is injected or tightened so results stay bounded.

What gets executed is SQL *regenerated from the checked tree*, with comments
stripped, and then re-parsed to confirm it is still a single SELECT. Text the
guard did not see can never reach the database.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from .catalog import Catalog

logging.getLogger("sqlglot").setLevel(logging.ERROR)

DIALECT = "postgres"

ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

# Node types that must not appear anywhere in the tree. Names rather than classes so
# the set survives sqlglot renames; tests assert each name still exists.
FORBIDDEN_NODES = frozenset(
    {
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
        "Copy", "Grant", "Revoke", "Set", "Transaction", "Commit", "Rollback", "Use",
        "Pragma", "LoadData", "Into", "Lock", "Analyze", "Cache", "Uncache", "Refresh",
        "Kill", "Comment", "Describe", "Show", "Command",
    }
)  # fmt: skip

# Functions that are dangerous whatever the context: sleeping, file and network
# access, session/config changes, dynamic SQL, sequence writes, backend control,
# and environment fingerprinting.
DENIED_FUNCTIONS = frozenset(
    {
        "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
        "pg_ls_dir", "pg_stat_file", "pg_file_write", "lo_import", "lo_export", "lo_get",
        "lo_put", "lo_from_bytea", "dblink", "dblink_exec", "dblink_connect", "set_config",
        "current_setting", "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
        "cursor_to_xml", "table_to_xml", "schema_to_xml", "database_to_xml",
        "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
        "pg_advisory_lock", "pg_try_advisory_lock", "nextval", "setval", "currval", "lastval",
        "txid_current", "pg_current_xact_id", "pg_notify", "pg_logical_emit_message",
        "pg_switch_wal", "pg_create_restore_point", "version", "inet_server_addr",
        "inet_client_addr", "current_user", "session_user", "current_database",
        "current_role", "current_schema", "current_schemas", "current_catalog",
        "current_version", "user", "pg_backend_pid", "has_table_privilege", "to_regclass",
        "xpath",
    }
)  # fmt: skip
DENIED_PREFIXES = ("pg_", "lo_", "dblink", "xpath", "txid_")

# Every function call must be on this allowlist, checked by the name the call renders
# to in PostgreSQL (so sqlglot's typed nodes and unknown functions are treated alike).
ALLOWED_FUNCTIONS = frozenset(
    {
        # syntax that renders as a call
        "cast", "extract", "row", "exists", "any", "all", "some", "array",
        # string (repeat/lpad/rpad/format are left out: one cheap call can build a huge
        # string, and format pads to any width, e.g. format('%100000000s', ''))
        "initcap", "btrim", "ltrim", "rtrim", "split_part", "strpos",
        "translate", "replace", "reverse", "left", "right", "concat", "concat_ws",
        "char_length", "character_length", "octet_length", "length", "ascii",
        "chr", "md5", "to_hex", "regexp_replace", "regexp_match", "regexp_matches",
        "regexp_split_to_array", "regexp_count", "regexp_like", "starts_with", "overlay",
        "position", "substr", "substring", "lower", "upper", "trim",
        # numeric
        "abs", "ceil", "ceiling", "floor", "round", "trunc", "mod", "power", "pow", "sqrt",
        "cbrt", "exp", "ln", "log", "log10", "sign", "div", "width_bucket", "greatest",
        "least", "pi", "degrees", "radians", "random",
        # date / time
        "now", "age", "date_part", "date_trunc", "make_date", "make_timestamp",
        "make_interval", "to_char", "to_date", "to_timestamp", "to_number", "justify_days",
        "justify_hours", "justify_interval", "isfinite", "date_bin",
        # aggregates and window functions
        "count", "sum", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp",
        "variance", "var_pop", "var_samp", "bool_and", "bool_or", "every", "array_agg",
        "string_agg", "json_agg", "jsonb_agg", "percentile_cont", "percentile_disc", "mode",
        "corr", "covar_pop", "covar_samp", "regr_slope", "regr_intercept", "row_number",
        "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
        "first_value", "last_value", "nth_value",
        # conditional
        "coalesce", "nullif", "num_nonnulls", "num_nulls",
        # arrays and json (read-only)
        "array_length", "cardinality", "unnest", "array_position", "array_to_string",
        "string_to_array", "json_build_object", "jsonb_build_object", "json_extract_path",
        "jsonb_extract_path", "json_extract_path_text", "jsonb_extract_path_text", "to_json",
        "to_jsonb", "json_typeof", "jsonb_typeof", "json_array_length", "jsonb_array_length",
        "generate_series",
    }
)  # fmt: skip


# Matches the function name at the start of a rendered call, e.g. `DATE_TRUNC(` or `pg_catalog.x(`.
_CALL = re.compile(r'^\s*(?:"?[A-Za-z_][\w$]*"?\s*\.\s*)?"?([A-Za-z_][\w$]*)"?\s*\(')


class Blocked(Exception):
    """A query was rejected. `code` is stable and machine-readable; `hint` helps the agent retry."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


@dataclass(frozen=True)
class GuardedQuery:
    original_sql: str
    sql: str  # regenerated, comment-free SQL that will actually run
    tables: frozenset[str]  # qualified names of every table referenced
    limit_applied: int | None  # the LIMIT injected or tightened by the guard, if any
    functions: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SqlGuard:
    catalog: Catalog
    max_rows: int = 200
    max_sql_chars: int = 8_000

    def check(self, sql: str) -> GuardedQuery:
        if not isinstance(sql, str) or not sql.strip():
            raise Blocked("empty_query", "No SQL was provided.")
        if len(sql) > self.max_sql_chars:
            raise Blocked("too_long", f"Query is longer than {self.max_sql_chars} characters.")
        if "\x00" in sql:
            raise Blocked("invalid_characters", "Query contains a NUL byte.")

        tree = self._parse_single(sql)
        if not isinstance(tree, ALLOWED_ROOTS):
            raise Blocked(
                "statement_not_allowed",
                f"Only SELECT queries are allowed; got {_describe(tree)}.",
                "Rewrite the request as a single read-only SELECT statement.",
            )
        tree = normalize_identifiers(tree, dialect=DIALECT)

        self._check_nodes(tree)
        functions = self._check_functions(tree)
        tables = self._check_tables(tree)
        tree, limit = self._apply_limit(tree)

        final_sql = tree.sql(dialect=DIALECT, comments=False)
        self._verify_roundtrip(final_sql)
        return GuardedQuery(sql, final_sql, frozenset(tables), limit, frozenset(functions))

    # -- steps -----------------------------------------------------------------

    def _parse_single(self, sql: str) -> exp.Expression:
        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except (ParseError, TokenError) as e:
            raise Blocked(
                "parse_error",
                f"Could not parse the query: {_first_line(e)}",
                "Check the SQL syntax (PostgreSQL dialect).",
            ) from None
        if not statements:
            raise Blocked("empty_query", "No SQL statement found.")
        if len(statements) > 1:
            raise Blocked(
                "multiple_statements",
                f"Exactly one statement is allowed; got {len(statements)}.",
                "Send a single SELECT statement without semicolon-separated extras.",
            )
        return statements[0]

    def _check_nodes(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            name = type(node).__name__
            if name in FORBIDDEN_NODES:
                raise Blocked(
                    "forbidden_operation",
                    f"{_describe(node)} is not allowed; the server only runs read-only SELECTs.",
                    "Remove any data-modifying, locking or session-changing clause.",
                )

    def _check_functions(self, tree: exp.Expression) -> set[str]:
        seen: set[str] = set()
        for fn in tree.find_all(exp.Func):
            if isinstance(fn, (exp.Binary, exp.Unary)):
                continue  # operators such as AND, ||, NOT that sqlglot models as functions
            name, is_call = _function_name(fn)
            seen.add(name)
            if name in DENIED_FUNCTIONS or name.startswith(DENIED_PREFIXES):
                raise Blocked(
                    "dangerous_function",
                    f"Function {name}() is blocked.",
                    "System, file, network, sleep and session functions are not available.",
                )
            if is_call and name not in ALLOWED_FUNCTIONS:
                raise Blocked(
                    "function_not_allowed",
                    f"Function {name}() is not on the allowlist.",
                    "Use standard SQL/PostgreSQL scalar, aggregate or window functions.",
                )
        return seen

    def _check_tables(self, tree: exp.Expression) -> set[str]:
        # Work out which Table nodes are references to CTEs (resolved per scope, so a CTE
        # name cannot shadow a real table outside the CTE's scope). Everything else is a real
        # table and must be in the catalog. If scope analysis fails, treat all as real tables.
        cte_refs: set[int] = set()
        try:
            for scope in traverse_scope(tree):
                for t in scope.tables:
                    if isinstance(scope.sources.get(t.alias_or_name), Scope) and not t.args.get("db"):
                        cte_refs.add(id(t))
        except Exception:
            cte_refs.clear()

        referenced: set[str] = set()
        for t in tree.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                continue  # a table function such as generate_series(); checked as a function
            if id(t) in cte_refs:
                continue
            if t.args.get("catalog"):
                raise Blocked("cross_database", "Cross-database references are not allowed.")
            name = t.name
            schema = t.db or None
            table = self.catalog.resolve(name, schema)
            if table is None:
                where = f"{schema}.{name}" if schema else name
                raise Blocked(
                    "unknown_table",
                    f"Table {where!r} does not exist or is not accessible.",
                    _available_hint(self.catalog),
                )
            referenced.add(table.qualified)
        return referenced

    def _apply_limit(self, tree: exp.Expression) -> tuple[exp.Expression, int | None]:
        """Bound the result size. We fetch max_rows + 1 so callers can tell when rows were cut."""
        cap = self.max_rows + 1
        limit = None if isinstance(tree, exp.Subquery) else tree.args.get("limit")
        count = _limit_count(limit) if limit is not None else None
        if limit is None and not isinstance(tree, exp.Subquery):
            return tree.limit(cap), cap
        if count is not None and count <= self.max_rows:
            return tree, None  # user's own limit is already within bounds
        if count is not None:
            _set_limit_count(limit, cap)
            return tree, cap
        # A parenthesised root, or a non-literal LIMIT/FETCH: wrap the whole query.
        wrapped = exp.select("*").from_(tree.subquery("_safesql_q"))
        return wrapped.limit(cap), cap

    def _verify_roundtrip(self, final_sql: str) -> None:
        try:
            again = [s for s in sqlglot.parse(final_sql, read=DIALECT) if s is not None]
        except (ParseError, TokenError):
            again = []
        if len(again) != 1 or not isinstance(again[0], ALLOWED_ROOTS):
            raise Blocked("internal_rewrite_error", "The rewritten query failed verification.")
        for node in again[0].walk():
            if type(node).__name__ in FORBIDDEN_NODES:
                raise Blocked("internal_rewrite_error", "The rewritten query failed verification.")
        self._check_functions(again[0])


def _function_name(fn: exp.Func) -> tuple[str, bool]:
    """Return (lowercase name, renders_as_call) for a function node."""
    if isinstance(fn, exp.Anonymous):
        return fn.name.lower(), True
    m = _CALL.match(fn.sql(dialect=DIALECT, comments=False))
    if m:
        return m.group(1).lower(), True
    return fn.sql_name().lower(), False  # keyword forms: CASE, CURRENT_DATE, CURRENT_USER, ...


def _limit_count(limit: exp.Expression) -> int | None:
    value = limit.args.get("count") if isinstance(limit, exp.Fetch) else limit.expression
    if isinstance(value, exp.Literal) and not value.is_string:
        try:
            return int(value.this)
        except ValueError:
            return None
    return None


def _set_limit_count(limit: exp.Expression, n: int) -> None:
    key = "count" if isinstance(limit, exp.Fetch) else "expression"
    limit.set(key, exp.Literal.number(n))


def _describe(node: exp.Expression) -> str:
    if isinstance(node, exp.Command):
        return f"a {str(node.this).upper()} command"
    return (
        f"a {type(node).__name__.upper()} statement" if node.parent is None else type(node).__name__.upper()
    )


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__


def _available_hint(catalog: Catalog, limit: int = 40) -> str:
    names = catalog.names()
    shown = ", ".join(names[:limit]) + (" ..." if len(names) > limit else "")
    return f"Available tables: {shown}. Call list_schema for columns."
