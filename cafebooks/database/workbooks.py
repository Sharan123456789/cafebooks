"""Table <-> DataFrame <-> .xlsx conversion shared by the SQL store, backups and imports.

In-memory convention (unchanged from the Excel-only versions, so the services did not change):
every frame has object dtype; money is a 2-place string ("123.50", "" only for blankable
cash-count columns); dates are ISO text or ""; vendors.active is a bool; every other value
is text ("" for missing).
"""
import logging
import math
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from cafebooks.utils.currency_utils import money
from cafebooks.utils.date_utils import parse_date
from .errors import StorageError, WorkbookCorruptError, WorkbookSchemaError
from .excel_io import read_xlsx, write_xlsx
from .schemas import (SCHEMAS, FILENAME_MAP, MONEY_COLUMNS, DATE_COLUMNS, BOOL_COLUMNS, SIGNED_MONEY,
                      BLANKABLE_MONEY)
from .validation import is_active

log = logging.getLogger(__name__)


def empty_frame(table):
    return pd.DataFrame(columns=SCHEMAS[table], dtype=object)


def strict_money_cell(v, col):
    if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and math.isnan(v)):
        return "" if col in BLANKABLE_MONEY else "0.00"
    return f"{money(v, col, allow_negative=col in SIGNED_MONEY):.2f}"


def text_cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def finalize_frame(df, table):
    """Strictly normalise a frame about to be stored (raises ValueError on garbage)."""
    cols = SCHEMAS[table]
    if list(df.columns) != cols:
        missing = [c for c in cols if c not in df.columns]
        extra = [c for c in df.columns if c not in cols]
        if missing or extra:
            raise StorageError(f"{table}: unexpected columns (missing {missing}, extra {extra}).")
        df = df[cols]
    df = df.reset_index(drop=True).copy()
    money_cols = set(MONEY_COLUMNS.get(table, []))
    date_cols = set(DATE_COLUMNS.get(table, []))
    bool_cols = set(BOOL_COLUMNS.get(table, []))
    for col in cols:
        if col in money_cols:
            df[col] = df[col].map(lambda v, c=col: strict_money_cell(v, c))
        elif col in date_cols:
            df[col] = df[col].map(lambda v: parse_date(v, required=False))
        elif col in bool_cols:
            df[col] = df[col].map(lambda v: bool(is_active(v))).astype(object)
        elif table == "vendors" and col == "payment_terms_days":
            df[col] = df[col].map(lambda v: str(int(float(v or 0))) if text_cell(v).strip() else "0")
        else:
            df[col] = df[col].map(text_cell)
    return df.astype(object)


def normalize_loaded(df, table, issues):
    """Lenient normalisation for data read from a workbook; problems are recorded, not raised."""
    for col in df.columns:
        out = []
        for idx, v in enumerate(df[col].tolist()):
            if col in MONEY_COLUMNS.get(table, []):
                try:
                    out.append(strict_money_cell(v, col))
                except ValueError as exc:
                    issues.append(f"{table} row {idx + 2}, {col}: {exc}")
                    out.append("0.00")
            elif col in DATE_COLUMNS.get(table, []):
                try:
                    out.append(parse_date(v, required=False))
                except ValueError as exc:
                    issues.append(f"{table} row {idx + 2}, {col}: {exc}")
                    out.append(text_cell(v))
            elif col in BOOL_COLUMNS.get(table, []):
                out.append(bool(is_active(v)))
            elif isinstance(v, bool):
                out.append(v)
            else:
                out.append(text_cell(v))
        df[col] = pd.Series(out, index=df.index, dtype=object)
    return df


def frame_from_rows(header, rows, table, source_name):
    """Build a schema-ordered frame from raw header/rows (missing columns become blank)."""
    cols = SCHEMAS[table]
    if not header:
        raise WorkbookCorruptError(f"{source_name} has no header row.")
    extra = [h for h in header if h not in cols]
    if extra:
        raise WorkbookSchemaError(f"{source_name} has unexpected columns: {extra}.")
    df = pd.DataFrame(rows, columns=header, dtype=object)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    issues = []
    df = normalize_loaded(df[cols].copy(), table, issues)
    if issues:
        log.warning("%s: %d data problems on load (first: %s)", table, len(issues), issues[0])
    return df, issues


def load_table_file(path, table):
    try:
        header, rows = read_xlsx(path)
    except Exception as exc:  # noqa: BLE001
        raise WorkbookCorruptError(f"{Path(path).name} is unreadable or corrupted: {exc}") from exc
    return frame_from_rows(header, rows, table, Path(path).name)


def load_workbook_folder(folder):
    """Load every CafeBooks table workbook present in a folder (backups, edit packs, legacy data)."""
    folder = Path(folder)
    frames, issues = {}, []
    for t in SCHEMAS:
        p = folder / FILENAME_MAP[t]
        if p.exists():
            df, iss = load_table_file(p, t)
            frames[t] = df
            issues.extend(iss)
    return frames, issues


def write_table_file(path, table, df):
    write_xlsx(path, table, SCHEMAS[table], df[SCHEMAS[table]].itertuples(index=False, name=None),
               money_columns=MONEY_COLUMNS.get(table, []))


def is_zip(path):
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False
