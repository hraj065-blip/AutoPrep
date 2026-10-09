from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Case:
    case_id: str
    family: str
    frame: pd.DataFrame
    expected: pd.DataFrame
    expected_action: str


FAMILIES = ["whitespace", "missing", "sentinel", "duplicate", "ambiguous_date",
            "category_variant", "mixed_numeric", "numeric_identifier", "outlier", "invalid_numeric"]


def generate_cases(count: int = 100, seed: int = 4242) -> list[Case]:
    rng = np.random.default_rng(seed)
    cases = []
    for i in range(count):
        family = FAMILIES[i % len(FAMILIES)]
        ids = [f"C{(i * 100 + n):05d}" for n in range(8)]
        clean = pd.DataFrame({"customer_id": ids, "category": ["Food", "Travel"] * 4,
                              "amount": [12.0, 24.5, 9.0, 80.0, 14.0, 25.0, 31.0, 18.0]})
        dirty = clean.copy(deep=True)
        expected = clean.copy(deep=True)
        action = "preserve_and_review"
        if family == "whitespace":
            dirty.loc[1, "category"] = " Travel "
            expected.loc[1, "category"] = "Travel"
            action = "trim_whitespace"
        elif family == "missing":
            dirty.loc[2, "amount"] = np.nan
            expected.loc[2, "amount"] = np.nan
        elif family == "sentinel":
            dirty.loc[3, "category"] = "unknown"
            expected.loc[3, "category"] = "unknown"
        elif family == "duplicate":
            dirty = pd.concat([dirty, dirty.iloc[[0]]], ignore_index=True)
            expected = dirty.copy(deep=True)
        elif family == "ambiguous_date":
            dirty["date"] = ["03/04/2025", "04/05/2025"] * 4
            expected["date"] = dirty["date"]
        elif family == "category_variant":
            dirty.loc[0, "category"] = "FOOD"
            expected.loc[0, "category"] = "FOOD"
        elif family == "mixed_numeric":
            dirty["amount"] = ["1,200.00", "2,300.00", "9.00", "80.00", "14.00", "25.00", "31.00", "18.00"]
            expected["amount"] = dirty["amount"]
        elif family == "numeric_identifier":
            dirty["customer_id"] = [f"00{rng.integers(1, 999):03d}" for _ in range(8)]
            expected["customer_id"] = dirty["customer_id"]
        elif family == "outlier":
            dirty.loc[7, "amount"] = 1000000.0
            expected.loc[7, "amount"] = 1000000.0
        elif family == "invalid_numeric":
            dirty["amount"] = dirty["amount"].astype(object)
            expected["amount"] = expected["amount"].astype(object)
            dirty.loc[4, "amount"] = "not available"
            expected.loc[4, "amount"] = "not available"
        cases.append(Case(f"case-{i:04d}", family, dirty, expected, action))
    return cases
