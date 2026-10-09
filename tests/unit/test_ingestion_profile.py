import pandas as pd
import pytest
import io

from app.ingestion.readers import IngestionError, read_dataset, validate_frame
from app.profiling.profiler import profile_dataset


def test_csv_detects_semicolon_and_hashes_source():
    result = read_dataset(b"a;b\n1;2\n", "data.csv")
    assert result.delimiter == ";"
    assert result.frame.iloc[0].tolist() == ["1", "2"]
    assert len(result.sha256) == 64


@pytest.mark.parametrize("content", [b"a,,b\n1,2,3\n", b"a,a\n1,2\n"])
def test_rejects_empty_or_duplicate_csv_headers(content):
    with pytest.raises(IngestionError):
        read_dataset(content, "data.csv")


def test_validation_limits_and_profile_facts():
    frame = pd.DataFrame({" code ": [" A ", "B"], "amount": [1, None]})
    with pytest.raises(IngestionError):
        validate_frame(frame, max_rows=1)
    result = profile_dataset(frame)
    assert result["row_count"] == 2
    assert result["columns"][0]["whitespace_count"] == 1
    assert result["columns"][1]["null_count"] == 1


def test_identifier_name_is_not_inferred_as_quantity():
    result = profile_dataset(pd.DataFrame({"customer_id": ["001", "002"]}))
    assert result["columns"][0]["inferred_type"] == "identifier"


def test_xlsx_sheet_selection_and_signature():
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, index=False, sheet_name="One")
        pd.DataFrame({"b": [2]}).to_excel(writer, index=False, sheet_name="Two")
    raw = buffer.getvalue()
    result = read_dataset(raw, "book.xlsx", sheet="Two")
    assert result.selected_sheet == "Two"
    assert result.frame.columns.tolist() == ["b"]
    with pytest.raises(IngestionError, match="valid worksheet"):
        read_dataset(raw, "book.xlsx", sheet="Missing")
