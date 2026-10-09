import pandas as pd


def noop_baseline(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.copy(deep=True)


def fixed_pandas_baseline(frame: pd.DataFrame) -> pd.DataFrame:
    """Fixed, non-agentic baseline; trims surrounding whitespace in object/string cells."""
    result = frame.copy(deep=True)
    for name in result.select_dtypes(include=["object", "string"]).columns:
        result[name] = result[name].map(lambda value: value.strip() if isinstance(value, str) else value)
    return result
