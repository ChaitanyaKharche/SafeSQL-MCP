"""Execution-match comparison, in the spirit of Spider's test-suite evaluation.

Two result sets match when they have the same rows, allowing the predicted columns
to be a permutation of the gold columns (or, optionally, a superset of them). Row order only matters when the gold
query has a top-level ORDER BY. Numbers are compared after rounding so 1, 1.0
and Decimal('1.00') are equal.
"""

from __future__ import annotations

import decimal
from collections import Counter
from collections.abc import Iterator, Sequence
from typing import Any

import sqlglot


def normalize(v: Any) -> Any:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        return round(f, 4) if f == f else "NaN"
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, (list, dict)):
        return repr(v)
    return v


def order_matters(gold_sql: str, dialect: str = "postgres") -> bool:
    try:
        tree = sqlglot.parse_one(gold_sql, read=dialect)
    except Exception:
        return "order by" in gold_sql.lower()
    return tree.args.get("order") is not None


def _column_permutations(
    gold: list[tuple], pred: list[tuple], limit: int = 2000
) -> Iterator[tuple[int, ...]]:
    """Injective maps from gold columns to pred columns holding the same multiset of values."""
    gcols = [Counter(r[j] for r in gold) for j in range(len(gold[0]))]
    pcols = [Counter(r[i] for r in pred) for i in range(len(pred[0]))]
    candidates = [[i for i, pc in enumerate(pcols) if pc == gc] for gc in gcols]
    produced = 0

    def backtrack(j: int, used: set[int], acc: list[int]) -> Iterator[tuple[int, ...]]:
        nonlocal produced
        if produced >= limit:
            return
        if j == len(gcols):
            produced += 1
            yield tuple(acc)
            return
        for i in candidates[j]:
            if i not in used:
                used.add(i)
                acc.append(i)
                yield from backtrack(j + 1, used, acc)
                acc.pop()
                used.discard(i)

    yield from backtrack(0, set(), [])


def results_match(
    gold_rows: Sequence[Sequence[Any]],
    pred_rows: Sequence[Sequence[Any]],
    ordered: bool,
    allow_extra_columns: bool = False,
) -> bool:
    """Spider-style execution match. With allow_extra_columns, pred may carry columns gold lacks."""
    gold = [tuple(normalize(v) for v in r) for r in gold_rows]
    pred = [tuple(normalize(v) for v in r) for r in pred_rows]
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    width_ok = len(pred[0]) >= len(gold[0]) if allow_extra_columns else len(pred[0]) == len(gold[0])
    if not width_ok:
        return False
    gold_counter = None if ordered else Counter(gold)
    for perm in _column_permutations(gold, pred):
        permuted = [tuple(r[i] for i in perm) for r in pred]
        if ordered:
            if permuted == gold:
                return True
        elif Counter(permuted) == gold_counter:
            return True
    return False
