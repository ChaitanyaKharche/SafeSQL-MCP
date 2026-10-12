#!/bin/sh
# Start the baked-in Postgres, then the MCP server with the web playground on port 7860.
set -e
pg_ctl -o "-c listen_addresses=localhost -k $PGSOCKETS" -l "$HOME/postgres.log" -w start
export SAFESQL_DATABASE_URL="postgresql://safesql_reader:$(cat "$HOME/.reader_pw")@localhost:5432/safesql"
export SAFESQL_SCHEMAS="${SAFESQL_SCHEMAS:-spider_world_1}"
cd "$HOME/app"
exec safesql serve --transport http --host 0.0.0.0 --port "${PORT:-7860}" --demo
