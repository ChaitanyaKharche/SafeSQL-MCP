"""CI gate: compare eval reports with thresholds and the committed baseline.

thresholds.json maps report -> metric -> rule, where a rule can use:
  min / max      absolute bounds
  equals         exact value
  max_drop       largest allowed drop below the baseline value (baseline.json)
Exit code 1 if any rule fails or a required report is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def evaluate(thresholds: dict, baseline: dict, reports: dict[str, dict], required: list[str]) -> list[dict]:
    rows = []
    for name in required:
        if name not in reports:
            rows.append({"report": name, "metric": "-", "value": "missing", "rule": "required", "ok": False})
    for name, rules in thresholds.items():
        report = reports.get(name)
        if report is None:
            continue
        for metric, rule in rules.items():
            value = report.get(metric)
            checks: list[tuple[str, bool]] = []
            if "min" in rule:
                checks.append((f">= {rule['min']}", value is not None and value >= rule["min"]))
            if "max" in rule:
                checks.append((f"<= {rule['max']}", value is not None and value <= rule["max"]))
            if "equals" in rule:
                checks.append((f"== {rule['equals']}", value == rule["equals"]))
            base = baseline.get(name, {}).get(metric)
            if "max_drop" in rule and isinstance(base, (int, float)):
                floor = round(base - rule["max_drop"], 4)
                checks.append(
                    (f">= baseline {base} - {rule['max_drop']}", value is not None and value >= floor)
                )
            for desc, ok in checks:
                rows.append({"report": name, "metric": metric, "value": value, "rule": desc, "ok": ok})
    return rows


def check(
    thresholds_path: Path, baseline_path: Path, reports_dir: Path, required: list[str], summary: str | None
) -> int:
    thresholds, baseline = _load(thresholds_path), _load(baseline_path)
    reports = {}
    for name in thresholds:
        path = reports_dir / f"{name}.json"
        if path.exists():
            reports[name] = _load(path)
    rows = evaluate(thresholds, baseline, reports, required)
    failed = [r for r in rows if not r["ok"]]
    md = [
        "### Eval gate: " + ("PASS" if not failed else f"FAIL ({len(failed)} checks)"),
        "",
        "| report | metric | value | rule | result |",
        "|---|---|---|---|---|",
    ]
    md += [
        f"| {r['report']} | {r['metric']} | {r['value']} | {r['rule']} | {'pass' if r['ok'] else '**FAIL**'} |"
        for r in rows
    ]
    text = "\n".join(md) + "\n"
    print(text)
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if failed else 0
