from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass

import pandas as pd


class IngestionError(ValueError):
    pass


@dataclass
class ReadResult:
    frame: pd.DataFrame
    source_file_id: str
    sha256: str
    format: str
    selected_sheet: str | None
    delimiter: str | None
    encoding: str | None
    warnings: list[str]


def inspect_excel(data: bytes, max_sheets: int = 30) -> list[dict[str, object]]:
    try:
        book = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
    except Exception as exc:
        raise IngestionError("The XLSX file could not be opened.") from exc
    if len(book.sheet_names) > max_sheets:
        raise IngestionError(f"Workbook exceeds the {max_sheets}-sheet limit.")
    result = []
    for name in book.sheet_names:
        preview = pd.read_excel(book, sheet_name=name, nrows=5, header=None)
        result.append({"name": name, "preview_rows": len(preview), "preview_columns": len(preview.columns)})
    return result


def read_dataset(data: bytes, filename: str, sheet: str | None = None, max_sheets: int = 30) -> ReadResult:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    digest = hashlib.sha256(data).hexdigest()
    if suffix == "csv":
        if not data:
            raise IngestionError("The uploaded CSV is empty.")
        decoded = None
        encoding = None
        for candidate in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                decoded = data.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise IngestionError("CSV encoding is unsupported; use UTF-8 or Windows-1252.")
        try:
            dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
        try:
            header = next(csv.reader(io.StringIO(decoded), delimiter=delimiter))
        except (csv.Error, StopIteration) as exc:
            raise IngestionError("CSV header could not be read.") from exc
        if any(not str(value).strip() for value in header):
            raise IngestionError("CSV contains an empty column header.")
        if len(set(header)) != len(header):
            raise IngestionError("CSV contains duplicate column headers.")
        try:
            frame = pd.read_csv(io.StringIO(decoded), sep=delimiter, dtype=object)
        except Exception as exc:
            raise IngestionError("CSV parsing failed. Check the delimiter and row structure.") from exc
        warnings = []
        if len(set(line.count(delimiter) for line in decoded.splitlines()[:50])) > 1:
            warnings.append("CSV delimiter detection may be uncertain because row widths vary.")
        return ReadResult(frame, uuid.uuid4().hex, digest, "csv", None, delimiter, encoding, warnings)
    if suffix == "xlsx":
        if not data.startswith(b"PK\x03\x04"):
            raise IngestionError("The file extension is XLSX but its signature is invalid.")
        try:
            book = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
            if len(book.sheet_names) > max_sheets:
                raise IngestionError(f"Workbook exceeds the {max_sheets}-sheet limit.")
            chosen = sheet or (book.sheet_names[0] if book.sheet_names else None)
            if chosen not in book.sheet_names:
                raise IngestionError("Select a valid worksheet from this workbook.")
            header = pd.read_excel(book, sheet_name=chosen, nrows=0).columns.tolist()
            if not header or any(str(value).strip() == "" or str(value).startswith("Unnamed:") for value in header):
                raise IngestionError("The selected worksheet has empty or unnamed column headers.")
            if len(set(header)) != len(header):
                raise IngestionError("The selected worksheet has duplicate column headers.")
            frame = pd.read_excel(book, sheet_name=chosen, dtype=object)
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError("XLSX parsing failed.") from exc
        return ReadResult(frame, uuid.uuid4().hex, digest, "xlsx", chosen, None, None, [])
    raise IngestionError("Only CSV and XLSX files are supported.")


def validate_frame(frame: pd.DataFrame, max_rows: int = 250_000, max_columns: int = 500) -> None:
    if frame.empty and len(frame.columns) == 0:
        raise IngestionError("The selected dataset has no headers or rows.")
    if len(frame) > max_rows:
        raise IngestionError(f"Dataset exceeds the {max_rows:,}-row limit.")
    if len(frame.columns) > max_columns:
        raise IngestionError(f"Dataset exceeds the {max_columns}-column limit.")
    if any(str(column).strip() == "" or str(column).startswith("Unnamed:") for column in frame.columns):
        raise IngestionError("Empty or unnamed column headers need to be corrected before processing.")
    if frame.columns.duplicated().any():
        raise IngestionError("Duplicate column headers are not supported; rename them in the source file.")
