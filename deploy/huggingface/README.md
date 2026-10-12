---
title: SafeSQL-MCP
emoji: 🛡️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: A text-to-SQL MCP server an AI agent can't break
---

# SafeSQL-MCP: try to break it

A text-to-SQL MCP server for Postgres. Every query an agent writes is parsed into a syntax
tree, checked against allowlists, cost-checked with `EXPLAIN` and run read-only under a
read-only role. On this page you can type any SQL, or fire one of the 132 attacks from the
CI suite, and see which layer stops it.

- **Playground:** the page above. It runs your SQL through the same guard as the MCP tools.
- **MCP endpoint:** `https://ckharche-safesql.hf.space/mcp` (streamable HTTP). Connect Claude Code,
  Claude Desktop or Cursor and ask questions about the Spider `world_1` database.
- **Source, evals and CI gate:** https://github.com/ChaitanyaKharche/SafeSQL-MCP

Data: [Spider](https://yale-lily.github.io/spider) (Yu et al., 2018; CC BY-SA 4.0), ported to
Postgres, plus a synthetic shop table with planted prompt injections.
