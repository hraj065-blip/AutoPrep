from __future__ import annotations

import pandas as pd


def exact_cell_accuracy(actual: pd.DataFrame, expected: pd.DataFrame) -> float:
    left, right = actual.align(expected, join="outer", axis=None)
    equal = left.eq(right) | (left.isna() & right.isna())
    return float(equal.to_numpy().mean()) if equal.size else 1.0


def changed_cell_counts(source: pd.DataFrame, actual: pd.DataFrame, expected: pd.DataFrame) -> dict[str, int | float]:
    common = source.columns.intersection(actual.columns).intersection(expected.columns)
    s, a, e = source[common].reset_index(drop=True), actual[common].reset_index(drop=True), expected[common].reset_index(drop=True)
    max_rows = max(len(s), len(a), len(e))
    s, a, e = (x.reindex(range(max_rows)) for x in (s, a, e))
    source_changed = ~(s.eq(a) | (s.isna() & a.isna()))
    expected_changed = ~(s.eq(e) | (s.isna() & e.isna()))
    correct_changed = source_changed & expected_changed & (a.eq(e) | (a.isna() & e.isna()))
    tp = int(correct_changed.to_numpy().sum())
    predicted = int(source_changed.to_numpy().sum())
    truth = int(expected_changed.to_numpy().sum())
    return {"changed_cells": predicted, "expected_changed_cells": truth,
            "correct_changes": tp,
            "changed_cell_precision": tp / predicted if predicted else (1.0 if truth == 0 else 0.0),
            "changed_cell_recall": tp / truth if truth else 1.0,
            "incorrect_change_rate": (predicted - tp) / max(1, predicted)}
