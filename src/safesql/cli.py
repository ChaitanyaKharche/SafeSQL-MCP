"""Command-line entry point: `safesql <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _settings(args):
    from .config import Settings

    s = Settings.from_env()
    if getattr(args, "schemas", None):
        s = s.with_schemas(*[x.strip() for x in args.schemas.split(",") if x.strip()])
    return s


def _leaf_message(group: BaseExceptionGroup) -> str:
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return str(exc)


def cmd_serve(args) -> None:
    from .server import build_server
    from .service import SafeSQLService
    from .telemetry import setup_telemetry

    setup_telemetry("safesql-mcp")
    settings = _settings(args)
    service = SafeSQLService.from_settings(settings)
    server = build_server(service, log_level="INFO")
    if args.transport == "stdio":
        server.run("stdio")
        return
    if args.demo or os.environ.get("SAFESQL_DEMO") == "1":
        from .web import DemoApp

        DemoApp.from_env(settings, service.executor).mount(server)
    security = None
    allowed = [h.strip() for h in os.environ.get("SAFESQL_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed:  # DNS-rebinding protection for public deployments (the SDK only defaults it on localhost)
        from mcp.server.transport_security import TransportSecuritySettings

        security = TransportSecuritySettings(
            allowed_hosts=allowed, allowed_origins=[f"https://{h.split(':')[0]}" for h in allowed]
        )
    server.run(
        "streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True,
        transport_security=security,
    )  # fmt: skip


def cmd_query(args) -> None:
    from .service import SafeSQLService

    service = SafeSQLService.from_settings(_settings(args))
    print(json.dumps(service.run_query(args.sql), indent=2, default=str))


def cmd_ask(args) -> None:
    from .agent import MissingCredentialsError, ask_in_process
    from .telemetry import setup_telemetry

    setup_telemetry("safesql-agent")
    try:
        result = asyncio.run(ask_in_process(args.question, _settings(args), verbose=True))
    except* MissingCredentialsError as group:  # may arrive wrapped by the MCP client's task group
        sys.exit(_leaf_message(group))
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_setup_db(args) -> None:
    from .setup_db import ensure_database, setup

    admin_url = args.admin_url or os.environ.get("SAFESQL_ADMIN_URL")
    if not admin_url:
        sys.exit("Pass --admin-url or set SAFESQL_ADMIN_URL")
    password = args.reader_password or os.environ.get("SAFESQL_READER_PASSWORD")
    if not password:
        sys.exit("Pass --reader-password or set SAFESQL_READER_PASSWORD")
    from psycopg.conninfo import conninfo_to_dict

    ensure_database(admin_url, conninfo_to_dict(admin_url).get("dbname") or "postgres")
    if args.load:
        from pathlib import Path

        from .setup_db import load_dump

        loaded = load_dump(admin_url, Path(args.load), force=args.force_load)
        print(f"Loaded {args.load}" if loaded else "Spider schemas already present; skipped load")
    granted = setup(
        admin_url, password, args.grant or ["spider_*", "inject_demo"], fixtures=not args.no_fixtures
    )
    print(f"Reader role ready; SELECT granted on {len(granted)} schemas: {', '.join(granted)}")


def cmd_eval(args) -> None:
    from .agent import MissingCredentialsError
    from .evals import main as evals_main

    try:
        sys.exit(evals_main(args.eval_args))
    except* MissingCredentialsError as group:  # may arrive wrapped by the MCP client's task group
        sys.exit(_leaf_message(group))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="safesql", description="Safe text-to-SQL MCP server")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--schemas", help="comma-separated schemas (overrides SAFESQL_SCHEMAS)")
    s.add_argument("--demo", action="store_true", help="also serve the web playground at / (HTTP only)")
    s.set_defaults(func=cmd_serve)

    q = sub.add_parser("query", help="run one SQL query through the guard")
    q.add_argument("sql")
    q.add_argument("--schemas")
    q.set_defaults(func=cmd_query)

    a = sub.add_parser("ask", help="ask the agent a question in plain English")
    a.add_argument("question")
    a.add_argument("--schemas")
    a.set_defaults(func=cmd_ask)

    d = sub.add_parser("setup-db", help="create the read-only role, fixtures and grants")
    d.add_argument("--admin-url")
    d.add_argument("--reader-password")
    d.add_argument("--grant", action="append", help="schema glob to grant SELECT on (repeatable)")
    d.add_argument("--no-fixtures", action="store_true", help="skip the prompt-injection fixtures")
    d.add_argument(
        "--load", help="gzipped SQL dump to load first with psql (e.g. evals/fixtures/spider_dev.sql.gz)"
    )
    d.add_argument("--force-load", action="store_true", help="load the dump even if spider_* schemas exist")
    d.set_defaults(func=cmd_setup_db)

    e = sub.add_parser("eval", help="run evals (see `safesql eval --help`)", add_help=False)
    e.add_argument("eval_args", nargs=argparse.REMAINDER)
    e.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
