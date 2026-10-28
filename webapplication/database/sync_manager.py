"""Export / external edit / re-import workflow with three-way conflict detection.

Export writes an editable copy plus an untouched base snapshot and a manifest of file hashes.
Import compares  base (at export time)  vs  edited  vs  live (now):
  * rows changed only in the edited files   -> applied
  * rows changed only in live data          -> kept
  * rows changed differently in both        -> CONFLICT, user must choose (never silent)
  * deleted rows, and edits to append-only tables (audit log, allocation ledger) -> rejected
Live workbooks are only touched by apply_import(), after validation, a backup, and
explicit confirmation by the caller.
"""
import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from .backup_manager import sha256_file
from .excel_io import find_formula_cells
from .ids import next_ids, append_rows
from .schemas import SCHEMAS, FILENAME_MAP, PRIMARY_KEYS, IMMUTABLE_TABLES, ID_PREFIX
from .validation import validate_dataset

EDIT_DIR = "EDIT_THESE_FILES"
BASE_DIR = "_base_do_not_edit"
NO_IMPORT = {"settings"}


class ImportPlan:
    def __init__(self):
        self.export_id = ""
        self.exported_at = ""
        self.stale_tables = []
        self.errors = []
        self.warnings = []
        self.changes = []          # dicts: table, pk, kind(add/modify), cols, row
        self.conflicts = []        # dicts: table, pk, cols, live, imported
        self.content_hash = ""
        self.folder = None

    @property
    def counts(self):
        out = {}
        for c in self.changes:
            out.setdefault(c["table"], {"added": 0, "modified": 0})
            out[c["table"]]["added" if c["kind"] == "add" else "modified"] += 1
        return out

    @property
    def has_changes(self):
        return bool(self.changes or self.conflicts)

    def describe(self):
        lines = [f"Export {self.export_id} created {self.exported_at}"]
        if self.stale_tables:
            lines.append("NOTE: live data changed since the export in: " + ", ".join(self.stale_tables))
        for t, c in self.counts.items():
            lines.append(f"{t}: {c['added']} to add, {c['modified']} to modify")
        for c in self.conflicts:
            lines.append(f"CONFLICT {c['table']} {c['pk']}: columns {', '.join(c['cols'])}")
        lines += [f"WARNING: {w}" for w in self.warnings]
        lines += [f"ERROR: {e}" for e in self.errors]
        if not self.has_changes and not self.errors:
            lines.append("No differences found: nothing to import.")
        return "\n".join(lines)


class SyncManager:
    def __init__(self, db, staging_dir):
        self.db = db
        self.staging_dir = Path(staging_dir)

    # ------------------------------------------------------------------ export
    def export_for_edit(self):
        errors = [i for i in self.db.validate_all() if i.severity == "error"]
        if errors:
            raise ValueError("Data has integrity errors; fix them before exporting:\n"
                             + "\n".join(f"- [{e.table}] {e.ref}: {e.message}" for e in errors[:15]))
        self.db.backup("pre_export")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = self.staging_dir / f"export_{stamp}_{uuid.uuid4().hex[:4]}"
        edit, base = folder / EDIT_DIR, folder / BASE_DIR
        edit.mkdir(parents=True)
        base.mkdir()
        files = {}
        with self.db._lock:
            for t in SCHEMAS:
                src = self.db.data_dir / FILENAME_MAP[t]
                shutil.copy2(src, base / src.name)
                shutil.copy2(src, edit / src.name)
                files[src.name] = sha256_file(src)
        manifest = {"export_id": folder.name, "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "files": files}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (folder / "README.txt").write_text(
            "CafeBooks external edit folder\n"
            "1. Edit ONLY the files inside EDIT_THESE_FILES (keep column names; do not add formulas).\n"
            "2. Do not edit _base_do_not_edit or manifest.json.\n"
            "3. Do not delete rows - void/deactivate inside CafeBooks instead.\n"
            "4. Close Excel, then in CafeBooks use Data & Settings > Import / Preview.\n"
            "The live data is never changed by this export or by opening these files.\n", encoding="utf-8")
        return {"folder": folder, "edit_dir": edit, "exported_at": manifest["exported_at"],
                "export_id": manifest["export_id"]}

    # ------------------------------------------------------------------ preview
    @staticmethod
    def _locate(folder):
        folder = Path(folder)
        for cand in (folder, folder.parent):
            if (cand / "manifest.json").exists():
                return cand
        return None

    def preview_import(self, folder):
        plan = ImportPlan()
        root = self._locate(folder)
        if root is None:
            plan.errors.append("This is not a CafeBooks export folder (manifest.json not found).")
            return plan
        plan.folder = root
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan.errors.append("manifest.json is unreadable.")
            return plan
        plan.export_id, plan.exported_at = manifest.get("export_id", ""), manifest.get("exported_at", "")
        edit, base = root / EDIT_DIR, root / BASE_DIR
        for name, sha in manifest.get("files", {}).items():
            bp = base / name
            if not bp.exists() or sha256_file(bp) != sha:
                plan.errors.append(f"The base snapshot {name} was modified or is missing; re-export.")
        for t in SCHEMAS:
            if not (edit / FILENAME_MAP[t]).exists():
                plan.errors.append(f"{FILENAME_MAP[t]} is missing from the edited files.")
        if plan.errors:
            return plan
        hasher = hashlib.sha256()
        for t in SCHEMAS:
            p = edit / FILENAME_MAP[t]
            hasher.update(sha256_file(p).encode())
            formulas = find_formula_cells(p, 3)
            if formulas:
                plan.errors.append(f"{p.name} contains formulas ({', '.join(formulas)}). Formulas are never "
                                   "imported; replace them with plain values.")
        plan.content_hash = hasher.hexdigest()[:16]
        if plan.errors:
            return plan
        try:
            edited, load_issues = self.db.load_workbook_folder(edit)
            base_frames, _ = self.db.load_workbook_folder(base)
        except Exception as exc:  # noqa: BLE001
            plan.errors.append(f"Edited workbook could not be read: {exc}")
            return plan
        plan.errors += [f"Invalid value: {i}" for i in load_issues[:30]]
        with self.db._lock:
            live = {t: self.db.read(t) for t in SCHEMAS}
        for t in SCHEMAS:
            if t in NO_IMPORT:
                if not self._rows(edited[t], t) == self._rows(base_frames[t], t):
                    plan.errors.append(f"Changes to '{t}' are not accepted through import.")
                continue
            self._diff_table(plan, t, base_frames[t], live[t], edited[t])
            if self._rows(live[t], t) != self._rows(base_frames[t], t):
                plan.stale_tables.append(t)
        if not plan.errors:
            merged = self._merge(live, plan, resolutions={})
            plan.errors += [f"[{i.table}] {i.ref}: {i.message}" for i in
                            validate_dataset({**live, **merged}) if i.severity == "error"][:30]
        return plan

    @staticmethod
    def _rows(df, table):
        pk = PRIMARY_KEYS[table]
        return {str(r[pk]): {k: str(v) for k, v in r.items()} for r in df.to_dict("records")}

    def _diff_table(self, plan, t, base_df, live_df, edit_df):
        b, l, e = self._rows(base_df, t), self._rows(live_df, t), self._rows(edit_df, t)
        if len(e) != len(edit_df):
            plan.errors.append(f"{t}: duplicate or empty {PRIMARY_KEYS[t]} values in the edited file.")
            return
        for pk in b:
            if pk not in e:
                plan.errors.append(f"{t} {pk}: row was deleted. Deleting is not allowed; void/deactivate in the app.")
        for pk, row in e.items():
            old = b.get(pk)
            if old is not None and row == old:
                continue
            if old is not None and t in IMMUTABLE_TABLES:
                plan.errors.append(f"{t} {pk}: existing rows of this append-only ledger cannot be edited.")
                continue
            if old is None:
                if pk in l:
                    if l[pk] != row:
                        plan.conflicts.append({"table": t, "pk": pk, "cols": [c for c in row if row[c] != l[pk].get(c)],
                                               "live": l[pk], "imported": row})
                else:
                    plan.changes.append({"table": t, "pk": pk, "kind": "add", "cols": list(row), "row": row})
                continue
            cols = [c for c in row if row[c] != old.get(c)]
            cur = l.get(pk)
            if cur is None:
                plan.errors.append(f"{t} {pk}: edited row no longer exists in live data.")
                continue
            live_cols = [c for c in cur if cur[c] != old.get(c)]
            clash = [c for c in cols if c in live_cols and row[c] != cur[c]]
            if clash:
                plan.conflicts.append({"table": t, "pk": pk, "cols": clash, "live": cur, "imported": row})
            apply_cols = [c for c in cols if c not in clash and row[c] != cur[c]]
            if apply_cols:
                plan.changes.append({"table": t, "pk": pk, "kind": "modify", "cols": apply_cols, "row": row})

    # ------------------------------------------------------------------ merge / apply
    def _merge(self, live, plan, resolutions):
        out = {}
        by_table = {}
        for c in plan.changes:
            by_table.setdefault(c["table"], []).append(c)
        for t, changes in by_table.items():
            df = live[t].copy()
            pk = PRIMARY_KEYS[t]
            pos = {str(v): i for i, v in zip(df.index, df[pk])}
            adds = []
            for c in changes:
                if c["kind"] == "add":
                    adds.append({col: c["row"][col] for col in SCHEMAS[t]})
                else:
                    i = pos[c["pk"]]
                    for col in c["cols"]:
                        df.at[i, col] = c["row"][col]
            out[t] = append_rows(df, adds)
        for c in plan.conflicts:
            if resolutions.get((c["table"], c["pk"])) == "import":
                t = c["table"]
                df = out.get(t, live[t].copy())
                pk = PRIMARY_KEYS[t]
                idx = df.index[df[pk].astype(str) == c["pk"]]
                if len(idx):
                    for col in c["cols"]:
                        df.at[idx[0], col] = c["imported"][col]
                else:
                    df = append_rows(df, [{col: c["imported"][col] for col in SCHEMAS[t]}])
                out[t] = df
        return out

    def apply_import(self, folder, resolutions=None):
        """resolutions: {(table, pk): 'live' | 'import'} for every conflict.  Returns a summary dict."""
        resolutions = resolutions or {}
        with self.db._lock:
            plan = self.preview_import(folder)
            if plan.errors:
                raise ValueError("Import rejected:\n" + "\n".join(plan.errors[:20]))
            if not plan.has_changes:
                return {"applied": False, "message": "No differences: nothing to import.", "counts": {}}
            missing = [c for c in plan.conflicts if resolutions.get((c["table"], c["pk"])) not in ("live", "import")]
            if missing:
                raise ValueError(f"{len(missing)} conflict(s) need an explicit decision (keep live or use import).")
            request_id = f"import:{plan.export_id}:{plan.content_hash}"
            a = self.db.read("audit_log")
            if ((a["request_id"] == request_id) & (a["action"] == "import")).any():
                return {"applied": False, "message": "This exact import was already applied.", "counts": {}}
            live = {t: self.db.read(t) for t in SCHEMAS}
            merged = self._merge(live, plan, resolutions)
            final = {**live, **merged}
            errors = [i for i in validate_dataset(final) if i.severity == "error"]
            if errors:
                raise ValueError("Import rejected after applying decisions:\n"
                                 + "\n".join(f"[{e.table}] {e.ref}: {e.message}" for e in errors[:15]))
            if not merged:
                return {"applied": False, "message": "Decisions kept all live values: nothing changed.", "counts": {}}
            self.db.backup("pre_import")
            with self.db.transaction(list(merged) + ["audit_log"], "external_import") as f:
                for t, df in merged.items():
                    f[t] = df
                counts = plan.counts
                aud = f["audit_log"]
                row = {"audit_id": next_ids(aud["audit_id"], ID_PREFIX["audit_log"])[0],
                       "timestamp": datetime.now().isoformat(timespec="microseconds"), "actor": "import",
                       "request_id": request_id, "entity_type": "import", "entity_id": plan.export_id,
                       "action": "import", "details": json.dumps(counts), "before": "",
                       "after": json.dumps({"conflicts_resolved": len(plan.conflicts)})}
                f["audit_log"] = append_rows(aud, [row])
            return {"applied": True, "message": "Import applied.", "counts": plan.counts}
