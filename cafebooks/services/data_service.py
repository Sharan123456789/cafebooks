"""Bulk data operations: import a data file, import a legacy Excel data folder, reset.

Every operation validates the complete resulting dataset first, refuses on accounting errors,
takes a safety backup, replaces the tables in one database transaction and is audited.
"""
import csv
import io
import json
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from cafebooks.database.excel_io import find_formula_cells
from cafebooks.database.migrations import migrate_frames
from cafebooks.database.schemas import SCHEMAS, FILENAME_MAP
from cafebooks.database.sql_store import settings_with_schema_version
from cafebooks.database.validation import validate_dataset
from cafebooks.database.workbooks import empty_frame, frame_from_rows, load_workbook_folder

MAX_ROWS_PER_TABLE = 500_000


def _merge_settings(current, incoming):
    """Imported settings rows override matching keys; other current keys are kept."""
    if incoming is None or incoming.empty:
        return current
    keys = set(incoming["key"].astype(str))
    keep = current[~current["key"].astype(str).isin(keys)]
    return pd.concat([keep, incoming], ignore_index=True) if not keep.empty else incoming.copy()


def read_data_file(path, filename=None):
    """Read a workbook with one sheet per table (sheet names = table names), or a CSV/TXT
    file with a 'table' column.  Returns (frames, problems)."""
    path = Path(path)
    suffix = Path(filename or path.name).suffix.lower()
    frames, problems = {}, []
    if suffix in (".csv", ".txt"):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if not rows or "table" not in [h.strip() for h in rows[0]]:
            raise ValueError("TXT/CSV files must include a 'table' column (for example vendors or customer_sales).")
        header = [h.strip() for h in rows[0]]
        ti = header.index("table")
        grouped = {}
        for r in rows[1:]:
            if not any(str(x).strip() for x in r):
                continue
            r = (r + [""] * len(header))[:len(header)]
            grouped.setdefault(r[ti].strip(), []).append([v for i, v in enumerate(r) if i != ti])
        cols = [h for i, h in enumerate(header) if i != ti]
        for table, trows in grouped.items():
            if table not in SCHEMAS:
                raise ValueError(f"Unknown table name: {table}")
            df, issues = frame_from_rows(cols, trows, table, f"{table} rows")
            frames[table], problems = df, problems + issues
    elif suffix == ".xlsx":
        formulas = find_formula_cells(path, 3)
        if formulas:
            raise ValueError(f"The workbook contains formulas ({', '.join(formulas)}). Replace them with plain values.")
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                table = ws.title.strip().lower()
                if table not in SCHEMAS:
                    continue
                it = ws.iter_rows(values_only=True)
                header = next(it, None)
                if not header or all(h in (None, "") for h in header):
                    continue
                keep = [i for i, h in enumerate(header) if h not in (None, "")]
                names = [str(header[i]).strip() for i in keep]
                rows = []
                for r in it:
                    vals = [r[i] if i < len(r) else None for i in keep]
                    if all(v is None or (isinstance(v, str) and not v.strip()) for v in vals):
                        continue
                    rows.append(vals)
                    if len(rows) > MAX_ROWS_PER_TABLE:
                        raise ValueError(f"Sheet '{ws.title}' has too many rows.")
                missing = [c for c in SCHEMAS[table] if c not in names]
                if missing:
                    raise ValueError(f"Sheet '{ws.title}' is missing columns: {', '.join(missing)}")
                df, issues = frame_from_rows(names, rows, table, f"sheet '{ws.title}'")
                frames[table], problems = df, problems + issues
        finally:
            wb.close()
    else:
        raise ValueError("Choose an .xlsx workbook or a .csv / .txt file.")
    if not frames:
        raise ValueError("No CafeBooks tables found. Sheet names must match table names "
                         f"({', '.join(SCHEMAS)}).")
    return frames, problems


class DataService:
    def __init__(self, accounting):
        self.a = accounting
        self.db = accounting.db

    def _result_frames(self, incoming):
        current = self.db.snapshot()
        merged = dict(current)
        for t, df in incoming.items():
            merged[t] = _merge_settings(current["settings"], df) if t == "settings" else df
        merged = migrate_frames({t: df.copy() for t, df in merged.items()})
        merged["settings"] = settings_with_schema_version(merged["settings"])
        return current, merged

    def preview_data_file(self, path, filename=None):
        incoming, problems = read_data_file(path, filename)
        current, merged = self._result_frames(incoming)
        issues = validate_dataset(merged)
        errors = problems[:30] + [f"[{i.table}] {i.ref}: {i.message}" for i in issues if i.severity == "error"][:30]
        warnings = [f"[{i.table}] {i.ref}: {i.message}" for i in issues if i.severity == "warning"][:30]
        return {"tables": {t: {"incoming": len(incoming[t]), "current": len(current[t])} for t in incoming},
                "errors": errors, "warnings": warnings, "frames": merged}

    def apply_data_file(self, path, filename=None):
        info = self.preview_data_file(path, filename)
        if info["errors"]:
            raise ValueError("Import refused; nothing was changed:\n" + "\n".join(info["errors"][:15]))
        backup = self.db.backup("before_file_import")
        self.db.replace_all(info["frames"], "direct_file_import")
        details = {t: v["incoming"] for t, v in info["tables"].items()}
        self.a.log_event("import", Path(filename or path).name[:120], "import_file",
                         "Replaced tables: " + ", ".join(f"{t} ({n} rows)" for t, n in details.items()),
                         after={"tables": details, "safety_backup": backup})
        self.a.reload_settings()
        return {"backup": backup, "tables": info["tables"]}

    def import_legacy_folder(self, folder, force=False):
        """Bring an earlier CafeBooks Excel data folder (vendors.xlsx, bills.xlsx, ...) into the database."""
        folder = Path(folder)
        frames, problems = load_workbook_folder(folder)
        missing = [FILENAME_MAP[t] for t in SCHEMAS if t not in frames and t != "settings"]
        if missing:
            raise ValueError(f"{folder} is not a CafeBooks data folder (missing {', '.join(missing)}).")
        frames.setdefault("settings", empty_frame("settings"))
        frames = migrate_frames(frames)
        frames["settings"] = settings_with_schema_version(
            _merge_settings(self.db.read("settings"), frames["settings"][frames["settings"]["key"] != "schema_version"]))
        issues = [f"[{i.table}] {i.ref}: {i.message}" for i in validate_dataset(frames) if i.severity == "error"]
        errors = problems + issues
        if errors and not force:
            raise ValueError(f"{len(errors)} problem(s) found; nothing was imported:\n" + "\n".join(errors[:15]))
        backup = self.db.backup("before_legacy_import")
        self.db.replace_all(frames, "legacy_excel_import")
        counts = {t: len(frames[t]) for t in SCHEMAS}
        self.a.log_event("import", folder.name[:120], "import_legacy", json.dumps(counts),
                         after={"safety_backup": backup})
        return {"backup": backup, "tables": counts, "problems": errors}

    def reset_all(self):
        """Clear every business record (settings are kept) after a safety backup."""
        backup = self.db.backup("before_reset")
        frames = {t: empty_frame(t) for t in SCHEMAS}
        frames["settings"] = self.db.read("settings")
        self.db.replace_all(frames, "reset_app_data")
        self.a.log_event("data", "all", "reset", "All business records cleared", after={"safety_backup": backup})
        return backup
