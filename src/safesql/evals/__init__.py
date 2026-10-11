"""Eval harness: `safesql eval <command>`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

DATA = Path("evals/data")
REPORTS = Path("evals/reports")


def _write(report: dict, out: Path, md: str, summary: str | None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="safesql eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    summary_help = "append the markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)"

    b = sub.add_parser("build-spider", help="load Spider into Postgres and build the golden set")
    b.add_argument("--spider-dir", type=Path, required=True)
    b.add_argument("--admin-url", default=os.environ.get("SAFESQL_ADMIN_URL"))
    b.add_argument("--out", type=Path, default=DATA / "spider_dev_pg.jsonl")

    s = sub.add_parser("safety", help="deterministic safety eval (no LLM)")
    s.add_argument("--admin-url", default=os.environ.get("SAFESQL_ADMIN_URL"))
    s.add_argument("--corpus", type=Path, default=DATA / "adversarial_sql.json")
    s.add_argument("--golden", type=Path, default=DATA / "spider_dev_pg.jsonl")
    s.add_argument("--out", type=Path, default=REPORTS / "safety.json")
    s.add_argument("--no-db-layer", action="store_true", help="skip the guard-off database-layer replay")
    s.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"), help=summary_help)

    a = sub.add_parser(
        "accuracy", help="agent execution accuracy on the golden set (needs ANTHROPIC_API_KEY)"
    )
    a.add_argument("--golden", type=Path, default=DATA / "spider_dev_pg.jsonl")
    a.add_argument("--limit", type=int, default=50, help="questions to sample (0 = all)")
    a.add_argument("--seed", type=int, default=7)
    a.add_argument("--concurrency", type=int, default=4)
    a.add_argument("--out", type=Path, default=REPORTS / "accuracy.json")
    a.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"), help=summary_help)

    g = sub.add_parser("agent-safety", help="adversarial prompts and prompt injection against the agent")
    g.add_argument("--admin-url", default=os.environ.get("SAFESQL_ADMIN_URL"))
    g.add_argument("--prompts", type=Path, default=DATA / "adversarial_prompts.json")
    g.add_argument("--concurrency", type=int, default=4)
    g.add_argument("--out", type=Path, default=REPORTS / "agent_safety.json")
    g.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"), help=summary_help)

    gate = sub.add_parser("gate", help="fail if reports fall below thresholds or regress from the baseline")
    gate.add_argument("--thresholds", type=Path, default=Path("evals/thresholds.json"))
    gate.add_argument("--baseline", type=Path, default=Path("evals/baseline.json"))
    gate.add_argument("--reports", type=Path, default=REPORTS)
    gate.add_argument("--require", nargs="*", default=["safety"], help="reports that must be present")
    gate.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"), help=summary_help)

    args = p.parse_args(argv)
    from ..config import Settings

    settings = Settings.from_env()

    if args.cmd == "build-spider":
        from .spider import build

        print(json.dumps(build(args.spider_dir, args.admin_url, args.out), indent=2))
        return 0
    if args.cmd == "safety":
        from . import safety

        report = safety.run(settings, args.admin_url, args.corpus, args.golden, db_layer=not args.no_db_layer)
        _write(report, args.out, safety.markdown(report), args.summary)
        return 0
    if args.cmd == "accuracy":
        from . import accuracy

        report = accuracy.run(
            settings, args.golden, limit=args.limit, seed=args.seed, concurrency=args.concurrency
        )
        _write(report, args.out, accuracy.markdown(report), args.summary)
        return 0
    if args.cmd == "agent-safety":
        from . import agent_safety

        report = agent_safety.run(settings, args.admin_url, args.prompts, concurrency=args.concurrency)
        _write(report, args.out, agent_safety.markdown(report), args.summary)
        return 0
    if args.cmd == "gate":
        from .gate import check

        return check(args.thresholds, args.baseline, args.reports, args.require, args.summary)
    return 2
