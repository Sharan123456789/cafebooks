"""Excel persistence layer for single-user CafeBooks.

Guarantees (and limits): Excel is NOT a transactional database.  This layer adds, at
application level: an in-memory cache with explicit invalidation, staged writes, a
journal with hash-verified crash recovery (rollback), file-lock detection, schema
validation, backups and an instance lock.  Two people editing the same files at the
same time is unsupported.
"""
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from utils.currency_utils import money
from utils.date_utils import parse_date
from .backup_manager import BackupManager, BackupError, sha256_file
from .excel_io import read_xlsx, write_xlsx, write_xlsx_multi, quick_verify
from .migrations import migrate_frames
from .schemas import (SCHEMAS, FILENAME_MAP, MONEY_COLUMNS, DATE_COLUMNS, BOOL_COLUMNS, SIGNED_MONEY,
                      BLANKABLE_MONEY, SCHEMA_VERSION)
from .validation import is_active, validate_dataset

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Base class for storage problems that are safe to show to the user."""


class FileLockedError(StorageError):
    pass


class WorkbookCorruptError(StorageError):
    pass


class WorkbookSchemaError(StorageError):
    pass


class StartupError(StorageError):
    pass


class RecoveryError(StorageError):
    pass


class AlreadyRunningError(StorageError):
    pass


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
    except Exception:
        return True


class ExcelManager:
    def __init__(self, data_dir="data", backup_dir="backups", retention=30):
        self.data_dir = Path(data_dir).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.backups = BackupManager(self.data_dir, self.backup_dir, retention)
        self._lock = threading.RLock()
        self._cache = {}
        self.version = 0                      # bumps on every cache change
        self.last_saved = None                # datetime of last successful commit
        self.recovery_message = ""
        self.journal_path = self.data_dir / "transaction_journal.json"
        self._instance_lock = None
        self.initialize()

    # ------------------------------------------------------------------ paths / helpers
    def _path(self, table):
        return self.data_dir / FILENAME_MAP[table]

    @staticmethod
    def _sig(path):
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)

    @staticmethod
    def _write_json_atomic(path, obj):
        fd, tmp = tempfile.mkstemp(prefix=".j_", suffix=".tmp", dir=Path(path).parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _fsync_file(path):
        try:
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass

    # ------------------------------------------------------------------ instance lock
    def acquire_instance_lock(self):
        lock = self.data_dir / ".cafebooks.lock"
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._instance_lock = lock
                return
            except FileExistsError:
                try:
                    pid = int(lock.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    raise AlreadyRunningError(
                        f"CafeBooks is already running (process {pid}) on this data folder. "
                        "Close the other window first.") from None
                try:
                    lock.unlink()
                except OSError:
                    raise AlreadyRunningError(f"Could not clear stale lock file {lock}.") from None
        raise AlreadyRunningError("Could not acquire the data-folder lock.")

    def close(self):
        if self._instance_lock and Path(self._instance_lock).exists():
            try:
                Path(self._instance_lock).unlink()
            except OSError:
                pass
        self._instance_lock = None

    # ------------------------------------------------------------------ normalisation
    @staticmethod
    def _strict_money_cell(v, col):
        if v is None or (isinstance(v, str) and not v.strip()):
            return "" if col in BLANKABLE_MONEY else "0.00"
        return f"{money(v, col, allow_negative=col in SIGNED_MONEY):.2f}"

    def _finalize(self, df, table):
        """Strictly normalise a frame about to be written (raises ValueError on garbage)."""
        cols = SCHEMAS[table]
        if list(df.columns) != cols:
            missing = [c for c in cols if c not in df.columns]
            extra = [c for c in df.columns if c not in cols]
            if missing or extra:
                raise StorageError(f"{table}: unexpected columns (missing {missing}, extra {extra}).")
            df = df[cols]
        df = df.reset_index(drop=True).copy()
        for col in MONEY_COLUMNS.get(table, []):
            df[col] = df[col].map(lambda v, c=col: self._strict_money_cell(v, c))
        for col in DATE_COLUMNS.get(table, []):
            df[col] = df[col].map(lambda v: parse_date(v, required=False))
        for col in BOOL_COLUMNS.get(table, []):
            df[col] = df[col].map(lambda v: bool(is_active(v))).astype(object)
        if table == "vendors":
            df["payment_terms_days"] = df["payment_terms_days"].map(lambda v: str(int(float(v or 0))))
        return df.astype(object)

    def _normalize_loaded(self, df, table, issues):
        """Lenient normalisation for data read from disk; problems are recorded, not raised."""
        for col in df.columns:
            vals = df[col].tolist()
            out = []
            for idx, v in enumerate(vals):
                if col in MONEY_COLUMNS.get(table, []):
                    try:
                        out.append(self._strict_money_cell(v, col))
                    except ValueError as exc:
                        issues.append(f"{table} row {idx + 2}, {col}: {exc}")
                        out.append("0.00")
                elif col in DATE_COLUMNS.get(table, []):
                    try:
                        out.append(parse_date(v, required=False))
                    except ValueError as exc:
                        issues.append(f"{table} row {idx + 2}, {col}: {exc}")
                        out.append("" if v is None else str(v))
                elif col in BOOL_COLUMNS.get(table, []):
                    out.append(bool(is_active(v)))
                elif v is None or (isinstance(v, float) and v != v):
                    out.append("")
                elif isinstance(v, bool):
                    out.append(v)
                elif isinstance(v, datetime):
                    out.append(v.isoformat(timespec="seconds"))
                elif isinstance(v, float) and v.is_integer():
                    out.append(str(int(v)))
                else:
                    out.append(v if isinstance(v, str) else str(v))
            df[col] = pd.Series(out, index=df.index, dtype=object)
        return df

    # ------------------------------------------------------------------ reading
    def _load_file(self, path, table):
        cols = SCHEMAS[table]
        try:
            header, rows = read_xlsx(path)
        except (zipfile.BadZipFile, KeyError, OSError, ValueError, Exception) as exc:  # noqa: BLE001
            raise WorkbookCorruptError(f"{Path(path).name} is unreadable or corrupted: {exc}") from exc
        if not header:
            raise WorkbookCorruptError(f"{Path(path).name} has no header row.")
        extra = [h for h in header if h not in cols]
        if extra:
            raise WorkbookSchemaError(f"{Path(path).name} has unexpected columns: {extra}.")
        df = pd.DataFrame(rows, columns=header, dtype=object)
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df = df[cols]
        issues = []
        df = self._normalize_loaded(df, table, issues)
        if issues:
            log.warning("%s: %d data problems on load (first: %s)", table, len(issues), issues[0])
        return df, issues

    def read(self, table):
        if table not in SCHEMAS:
            raise KeyError(table)
        with self._lock:
            path = self._path(table)
            if not path.exists():
                raise StorageError(f"{path.name} is missing from {self.data_dir}.")
            sig = self._sig(path)
            hit = self._cache.get(table)
            if hit and hit[0] == sig:
                return hit[1].copy()
            df, _ = self._load_file(path, table)
            self._cache[table] = (sig, df)
            self.version += 1
            return df.copy()

    def invalidate(self):
        with self._lock:
            self._cache.clear()
            self.version += 1

    def row_counts(self):
        return {t: len(self.read(t)) for t in SCHEMAS}

    # ------------------------------------------------------------------ initialisation
    def _create_empty(self, table):
        write_xlsx(self._path(table), table, SCHEMAS[table], [])

    def initialize(self):
        with self._lock:
            self.recovery_message = self.recover_interrupted_write()
            self._cleanup_leftovers()
            existing = [t for t in SCHEMAS if self._path(t).exists()]
            if not existing:
                for t in SCHEMAS:
                    self._create_empty(t)
                self._commit({"settings": self._settings_frame(self._empty("settings"))}, "init")
                return
            missing = [t for t in SCHEMAS if t not in existing]
            if missing:
                raise StartupError(
                    "These workbooks are missing from the data folder: "
                    + ", ".join(FILENAME_MAP[t] for t in missing)
                    + ". Nothing was changed. Restore them from a backup or copy them back.")
            problems = []
            for t in SCHEMAS:
                try:
                    header, _ = read_xlsx(self._path(t))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{FILENAME_MAP[t]}: unreadable/corrupted ({exc})")
                    continue
                if not header:
                    problems.append(f"{FILENAME_MAP[t]}: no header row")
                    continue
                extra = [h for h in header if h not in SCHEMAS[t]]
                if extra:
                    problems.append(f"{FILENAME_MAP[t]}: unexpected columns {extra}")
            if problems:
                raise StartupError("The data folder cannot be opened safely. Nothing was changed.\n- "
                                   + "\n- ".join(problems)
                                   + "\nRestore the latest backup (Data & Settings) or fix the files.")
            version = self._stored_version()
            if version > SCHEMA_VERSION:
                raise StartupError("The data was created by a newer CafeBooks version; refusing to open it.")
            if version < SCHEMA_VERSION:
                self._migrate()

    @staticmethod
    def _empty(table):
        return pd.DataFrame(columns=SCHEMAS[table], dtype=object)

    @staticmethod
    def _settings_frame(df):
        row = {"key": "schema_version", "value": str(SCHEMA_VERSION),
               "modified_at": datetime.now().isoformat(timespec="seconds")}
        keep = df[df["key"] != "schema_version"]
        new = pd.DataFrame([row], columns=SCHEMAS["settings"], dtype=object)
        return new if keep.empty else pd.concat([keep, new], ignore_index=True)

    def _stored_version(self):
        df = self.read("settings")
        hit = df[df["key"] == "schema_version"]
        if hit.empty:
            return 0
        try:
            return int(hit["value"].iloc[0])
        except ValueError:
            return 0

    def _migrate(self):
        total = sum(len(self.read(t)) for t in SCHEMAS if t != "settings")
        if total:
            self.backup("pre_migration")
        frames = {t: self.read(t) for t in SCHEMAS}
        frames = migrate_frames(frames)
        frames["settings"] = self._settings_frame(frames["settings"])
        frames = {t: self._finalize(f, t) for t, f in frames.items()}
        self._commit(frames, "migrate_to_v4")
        log.info("Migrated data to schema v%s", SCHEMA_VERSION)

    def _cleanup_leftovers(self):
        for p in self.data_dir.glob(".tmp_*"):
            p.unlink(missing_ok=True)
        for p in self.data_dir.glob(".j_*.tmp"):
            p.unlink(missing_ok=True)
        if not self.journal_path.exists():
            for d in self.data_dir.glob(".stage_*"):
                shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ writing
    def _check_writable(self, tables):
        for t in tables:
            path = self._path(t)
            lock_file = path.parent / f"~${path.name}"
            if lock_file.exists():
                raise FileLockedError(f"{path.name} appears to be open in Excel ({lock_file.name} exists). "
                                      "Close it and try again. Your data was not changed.")
            if os.name == "nt":
                try:
                    with open(path, "r+b"):
                        pass
                except PermissionError:
                    raise FileLockedError(f"{path.name} is locked by another program. Close it and try again. "
                                          "Your data was not changed.") from None

    def _write_frame(self, path, table, df):
        write_xlsx(path, table, SCHEMAS[table], df[SCHEMAS[table]].itertuples(index=False, name=None),
                   money_columns=MONEY_COLUMNS.get(table, []))
        self._fsync_file(path)

    def _verify_written(self, path, table, nrows):
        problem = quick_verify(path, SCHEMAS[table], nrows)
        if problem:
            raise StorageError(f"Verification of staged {table} workbook failed ({problem}); nothing was changed.")

    @contextmanager
    def transaction(self, tables, operation="update"):
        """Yield mutable copies of the named tables.  On normal exit all changed tables are
        committed through the journal; on exception nothing is written."""
        tables = list(dict.fromkeys(tables))
        with self._lock:
            self._check_writable(tables)
            frames = {t: self.read(t) for t in tables}
            yield frames
            changed = {}
            for t in tables:
                df = self._finalize(frames[t], t)
                if df.equals(self._cache[t][1]):
                    continue
                changed[t] = df
            if changed:
                self._commit(changed, operation)

    def replace_all(self, frames, operation):
        with self.transaction(list(SCHEMAS), operation) as f:
            for t in SCHEMAS:
                f[t] = frames[t]

    def _commit(self, frames, operation):
        txid = uuid.uuid4().hex
        stage = self.data_dir / f".stage_{txid}"
        stage.mkdir()
        manifest = {"txid": txid, "operation": operation, "state": "PREPARING", "stage_dir": str(stage), "tables": {}}
        try:
            for t, df in frames.items():
                live = self._path(t)
                staged = stage / FILENAME_MAP[t]
                self._write_frame(staged, t, df)
                self._verify_written(staged, t, len(df))
                orig = stage / ("orig_" + FILENAME_MAP[t])
                shutil.copy2(live, orig)
                manifest["tables"][t] = {"staged": str(staged), "orig": str(orig),
                                         "staged_sha": sha256_file(staged), "orig_sha": sha256_file(live)}
            manifest["state"] = "PREPARED"
            self._write_json_atomic(self.journal_path, manifest)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            self.journal_path.unlink(missing_ok=True)
            raise
        try:
            for t in frames:
                os.replace(manifest["tables"][t]["staged"], self._path(t))
        except Exception as exc:  # noqa: BLE001
            self._rollback(manifest)
            if isinstance(exc, PermissionError):
                raise FileLockedError("A workbook is locked by another program (probably Excel). "
                                      "The change was rolled back; your data is unchanged.") from exc
            raise StorageError(f"Could not write workbooks; the change was rolled back: {exc}") from exc
        self.journal_path.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        for t, df in frames.items():
            self._cache[t] = (self._sig(self._path(t)), df)
        self.version += 1
        self.last_saved = datetime.now()

    # ------------------------------------------------------------------ recovery
    def _restore_file(self, src, dst):
        tmp = Path(dst).parent / f".tmp_{uuid.uuid4().hex}.xlsx"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)

    def _rollback(self, manifest):
        """Put every workbook back to its pre-transaction content.  If that fails, the journal
        and stage folder are kept so recovery can retry on the next start."""
        failures = []
        for t, info in manifest["tables"].items():
            live = self._path(t)
            try:
                if not live.exists() or sha256_file(live) != info["orig_sha"]:
                    self._restore_file(info["orig"], live)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{live.name}: {exc}")
        if failures:
            raise RecoveryError("Rollback incomplete (" + "; ".join(failures)
                                + "). Restart CafeBooks to retry automatic recovery.")
        self.journal_path.unlink(missing_ok=True)
        shutil.rmtree(manifest["stage_dir"], ignore_errors=True)
        self._cache.clear()
        self.version += 1

    def recover_interrupted_write(self):
        """Finish or undo a transaction that was interrupted.  Never deletes the only valid copy."""
        if not self.journal_path.exists():
            return ""
        try:
            manifest = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            quarantine = self.data_dir / "recovery_quarantine"
            quarantine.mkdir(exist_ok=True)
            shutil.move(str(self.journal_path), quarantine / f"bad_journal_{uuid.uuid4().hex[:6]}.json")
            return "A damaged transaction journal was quarantined; data files were left untouched."
        tables = manifest.get("tables", {})
        if manifest.get("state") != "PREPARED":
            shutil.rmtree(manifest.get("stage_dir", ""), ignore_errors=True)
            self.journal_path.unlink(missing_ok=True)
            return "An unfinished transaction (never applied) was discarded."
        states = {}
        for t, info in tables.items():
            live = self._path(t)
            sha = sha256_file(live) if live.exists() else None
            states[t] = "new" if sha == info["staged_sha"] else "old" if sha == info["orig_sha"] else "unknown"
        if all(s == "new" for s in states.values()):
            msg = "An interrupted save had already completed; cleaned up."
        elif all(s == "old" for s in states.values()):
            msg = "An interrupted save never reached your data files; it was discarded."
        else:
            orig_ok = all(Path(i["orig"]).exists() and sha256_file(i["orig"]) == i["orig_sha"] for i in tables.values())
            staged_ok = all(Path(i["staged"]).exists() and sha256_file(i["staged"]) == i["staged_sha"]
                            for i in tables.values())
            quarantine = self.data_dir / "recovery_quarantine"
            for t, state in states.items():
                if state == "unknown" and self._path(t).exists():
                    quarantine.mkdir(exist_ok=True)
                    shutil.copy2(self._path(t), quarantine / f"{manifest['txid'][:8]}_{self._path(t).name}")
            if orig_ok:
                for t, info in tables.items():
                    if states[t] != "old":
                        self._restore_file(info["orig"], self._path(t))
                msg = "An interrupted multi-file save was rolled back to the last consistent state."
            elif staged_ok:
                for t, info in tables.items():
                    if states[t] != "new":
                        self._restore_file(info["staged"], self._path(t))
                msg = "An interrupted multi-file save was completed from its verified staged copies."
            else:
                raise RecoveryError("Interrupted save could not be recovered automatically (no verified copy "
                                    f"available). Files in {manifest['stage_dir']} were kept; restore a backup.")
        shutil.rmtree(manifest.get("stage_dir", ""), ignore_errors=True)
        self.journal_path.unlink(missing_ok=True)
        self._cache.clear()
        self.version += 1
        log.warning(msg)
        return msg

    # ------------------------------------------------------------------ settings table
    def get_setting(self, key, default=""):
        df = self.read("settings")
        hit = df[df["key"] == key]
        return str(hit["value"].iloc[0]) if not hit.empty else default

    def set_setting(self, key, value):
        with self.transaction(["settings"], "set_setting") as f:
            df = f["settings"]
            now = datetime.now().isoformat(timespec="seconds")
            if (df["key"] == key).any():
                df.loc[df["key"] == key, ["value", "modified_at"]] = [str(value), now]
            else:
                df.loc[len(df)] = [key, str(value), now]

    # ------------------------------------------------------------------ backups / restore
    def backup(self, label="manual"):
        with self._lock:
            return self.backups.create_backup(label)

    def maybe_daily_backup(self):
        stamp = datetime.now().strftime("%Y%m%d")
        with self._lock:
            if any(p.name.startswith(f"daily_{stamp}") for p in self.backup_dir.glob("daily_*.zip")):
                return None
            return self.backups.create_backup(f"daily_{stamp}")

    def load_workbook_folder(self, folder):
        """Load every CafeBooks workbook present in a folder (used by restore and import)."""
        folder = Path(folder)
        frames, issues = {}, []
        for t in SCHEMAS:
            p = folder / FILENAME_MAP[t]
            if p.exists():
                df, iss = self._load_file(p, t)
                frames[t] = df
                issues.extend(iss)
        return frames, issues

    def restore_preview(self, zip_path):
        manifest = self.backups.verify_backup(zip_path)
        tmp = Path(tempfile.mkdtemp(prefix="restore_preview_", dir=self.backup_dir))
        try:
            self.backups.extract(zip_path, tmp)
            frames, load_issues = self.load_workbook_folder(tmp)
            missing = [t for t in SCHEMAS if t not in frames and t != "settings"]
            if missing:
                raise BackupError("Backup is missing: " + ", ".join(FILENAME_MAP[t] for t in missing))
            for t in SCHEMAS:
                frames.setdefault(t, self._empty(t))
            frames = migrate_frames(frames)
            issues = [i for i in validate_dataset(frames)]
            current = self.row_counts()
            return {"manifest": manifest,
                    "tables": {t: {"backup": len(frames[t]), "current": current[t]} for t in SCHEMAS},
                    "load_issues": load_issues, "issues": issues, "frames": frames}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def restore(self, zip_path):
        info = self.restore_preview(zip_path)
        errors = [i for i in info["issues"] if i.severity == "error"]
        if errors:
            raise BackupError(f"Backup contains {len(errors)} accounting integrity errors "
                              f"(first: {errors[0].message}). Restore refused.")
        safety = self.backup("pre_restore")
        frames = info["frames"]
        frames["settings"] = self._settings_frame(frames["settings"])
        frames = {t: self._finalize(f, t) for t, f in frames.items()}
        self.replace_all(frames, "restore")
        return safety

    # ------------------------------------------------------------------ snapshot export
    def export_database_snapshot(self, output_dir=None):
        out_dir = Path(output_dir) if output_dir else self.data_dir.parent / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"CafeBooks_Database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        with self._lock:
            sheets = []
            for t in SCHEMAS:
                df = self.read(t)
                sheets.append((t, SCHEMAS[t], df.itertuples(index=False, name=None), MONEY_COLUMNS.get(t, [])))
            write_xlsx_multi(out, sheets)
        return out

    def validate_all(self):
        """Dataset-level validation of live data (used by the integrity checker)."""
        with self._lock:
            return validate_dataset({t: self.read(t) for t in SCHEMAS})

    def warm_cache(self):
        """Load all tables into the cache (call from a background thread at startup)."""
        for t in SCHEMAS:
            self.read(t)
