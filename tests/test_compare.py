from decimal import Decimal

from safesql.evals.compare import order_matters, results_match


def test_identical_rows_match():
    assert results_match([(1, "a"), (2, "b")], [(1, "a"), (2, "b")], ordered=False)


def test_row_order_ignored_without_order_by():
    assert results_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=False)
    assert not results_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], ordered=True)


def test_column_permutation_allowed():
    assert results_match([(1, "a"), (2, "b")], [("a", 1), ("b", 2)], ordered=False)


def test_numbers_normalised():
    assert results_match([(1,)], [(Decimal("1.00"),)], ordered=False)
    assert results_match([(3.666666667,)], [(Decimal("3.6666666666"),)], ordered=False)


def test_different_values_or_shapes_do_not_match():
    assert not results_match([(1,)], [(2,)], ordered=False)
    assert not results_match([(1, 2)], [(1,)], ordered=False)
    assert not results_match([(1,)], [(1,), (1,)], ordered=False)


def test_duplicates_count():
    assert not results_match([(1,), (1,), (2,)], [(1,), (2,), (2,)], ordered=False)


def test_empty_results_match():
    assert results_match([], [], ordered=False)


def test_order_matters_detects_top_level_order_by():
    assert order_matters("SELECT a FROM t ORDER BY a")
    assert not order_matters("SELECT a FROM (SELECT a FROM t ORDER BY a) x")


def test_extra_columns_only_when_allowed():
    gold, pred = [(3.66667,)], [("Aurora Blender", 3.666666, 3)]
    assert not results_match(gold, pred, ordered=False)
    assert results_match(gold, pred, ordered=False, allow_extra_columns=True)
    assert not results_match([(1, "a")], [(1,)], ordered=False, allow_extra_columns=True)
