"""Shared SQL storage: SQLite (desktop app / single server) or PostgreSQL (web / Vercel).

The services see the interface earlier Excel-based versions offered: read(table) returns a
DataFrame copy and transaction(tables) yields mutable copies that are committed atomically.

Multi-user safety
  * Every write transaction first takes one database-wide write lock (a PostgreSQL
    transaction-scoped advisory lock, or SQLite BEGIN IMMEDIATE), re-reads its tables under
    that lock and commits only the rows that changed.  Sequential IDs, duplicate checks and
    ledger limits therefore stay correct across browser tabs, users, worker processes and
    serverless instances.  If anything fails, the database transaction is rolled back and
    nothing is changed.
  * A data_version counter, bumped in the same transaction, lets every process keep an
    in-memory cache and notice changes made by other processes.
"""
import contextvars
import io
import logging
import tempfile
import threading
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.types import TypeDecorator

from cafebooks.utils.date_utils import now_local
from .backup_manager import (BackupError, FileBackupStore, DatabaseBackupStore, build_backup_zip, extract_backup,
                             new_backup_name, verify_backup)
from .errors import StorageError, StartupError, BusyError
from .excel_io import write_xlsx_multi
from .migrations import migrate_frames
from .schemas import (SCHEMAS, FILENAME_MAP, PRIMARY_KEYS, MONEY_COLUMNS, DATE_COLUMNS, BOOL_COLUMNS,
                      SCHEMA_VERSION)
from .validation import validate_dataset
from .workbooks import empty_frame, finalize_frame, load_workbook_folder, write_table_file

log = logging.getLogger(__name__)

WRITE_LOCK_KEY = 72910001          # arbitrary constant shared by every CafeBooks process
LOCK_WAIT_SECONDS = 25
_VERSION_MEMO = contextvars.ContextVar("cafebooks_version_memo", default=None)


# ---------------------------------------------------------------------- column types
class Money(TypeDecorator):
    """NUMERIC(14,2) on PostgreSQL; exact decimal text on SQLite (which has no decimal type)."""
    impl = sa.Numeric(14, 2)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(sa.String(24))
        return dialect.type_descriptor(sa.Numeric(14, 2, asdecimal=True))

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return None
        d = Decimal(str(value))
        return f"{d:.2f}" if dialect.name == "sqlite" else d

    def process_result_value(self, value, dialect):
        return "" if value is None or value == "" else f"{Decimal(str(value)):.2f}"


class IsoDate(TypeDecorator):
    impl = sa.Date
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return None
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return ""
        return value.isoformat() if isinstance(value, date) else str(value)


class Txt(TypeDecorator):
    impl = sa.Text
    cache_ok = True

    def process_result_value(self, value, dialect):
        return "" if value is None else value


class Flag(TypeDecorator):
    impl = sa.Boolean
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return bool(value)

    def process_result_value(self, value, dialect):
        return bool(value)


class Days(TypeDecorator):
    impl = sa.Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return int(float(value)) if str(value or "").strip() else 0

    def process_result_value(self, value, dialect):
        return str(int(value or 0))


def build_tables(metadata):
    tables = {}
    for name, cols in SCHEMAS.items():
        pk = PRIMARY_KEYS[name]
        money_cols, date_cols = set(MONEY_COLUMNS.get(name, [])), set(DATE_COLUMNS.get(name, []))
        bool_cols = set(BOOL_COLUMNS.get(name, []))
        columns = [sa.Column("row_seq", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False,
                             index=True)]
        for c in cols:
            if c in money_cols:
                typ = Money()
            elif c in date_cols:
                typ = IsoDate()
            elif c in bool_cols:
                typ = Flag()
            elif name == "vendors" and c == "payment_terms_days":
                typ = Days()
            else:
                typ = Txt()
            columns.append(sa.Column(c, typ, primary_key=(c == pk), nullable=(c != pk)))
        tables[name] = sa.Table(name, metadata, *columns)
    meta = sa.Table("cafebooks_meta", metadata,
                    sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("value", sa.Text, nullable=False))
    backups = sa.Table("cafebooks_backups", metadata,
                       sa.Column("name", sa.String(200), primary_key=True),
                       sa.Column("label", sa.String(80), nullable=False),
                       sa.Column("created_at", sa.String(32), nullable=False, index=True),
                       sa.Column("size", sa.Integer, nullable=False),
                       sa.Column("sha256", sa.String(64), nullable=False),
                       sa.Column("data", sa.LargeBinary, nullable=False))
    return tables, meta, backups


# ---------------------------------------------------------------------- engine
def normalize_url(url):
    url = str(url).strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url):
    url = normalize_url(url)
    if url.startswith("sqlite"):
        db_path = sa.engine.make_url(url).database
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = sa.create_engine(url, connect_args={"check_same_thread": False, "timeout": LOCK_WAIT_SECONDS})

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):
            dbapi_conn.isolation_level = None          # SQLAlchemy emits BEGIN itself (below)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
            cur.execute("PRAGMA synchronous=FULL")      # a committed save survives a power cut
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            write = conn.get_execution_options().get("cafebooks_write")
            conn.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
        return engine
    # prepare_threshold=None keeps psycopg compatible with transaction-mode connection
    # poolers (Neon / Supabase / PgBouncer), which serverless deployments should use.
    return sa.create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=3, max_overflow=7,
                            connect_args={"prepare_threshold": None, "connect_timeout": 15})


def settings_with_schema_version(df):
    row = {"key": "schema_version", "value": str(SCHEMA_VERSION),
           "modified_at": now_local().isoformat(timespec="seconds")}
    keep = df[df["key"] != "schema_version"]
    new = pd.DataFrame([row], columns=SCHEMAS["settings"], dtype=object)
    return new if keep.empty else pd.concat([keep, new], ignore_index=True)


def _chunks(items, size=500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SqlStore:
    def __init__(self, url, backup_dir=None, retention=30, backup_store="auto"):
        self.url = normalize_url(url)
        self.engine = make_engine(self.url)
        self.dialect = self.engine.dialect.name
        self.metadata = sa.MetaData()
        self.tables, self.meta_table, self.backup_table = build_tables(self.metadata)
        self._lock = threading.RLock()          # serialises writers inside this process
        self._local = threading.local()         # the write connection of the current thread
        self._cache = {}                        # table -> (data_version, frame, max_row_seq)
        self.last_saved = None
        self.recovery_message = ""
        if backup_store == "auto":
            backup_store = "files" if (self.dialect == "sqlite" and backup_dir) else "database"
        if backup_store == "files":
            if not backup_dir:
                raise StartupError("A backup folder is required for file backups.")
            self.backups = FileBackupStore(backup_dir, retention)
        else:
            self.backups = DatabaseBackupStore(self.engine, self.backup_table, retention)
        self.initialize()

    # ------------------------------------------------------------------ description
    @property
    def data_dir(self):
        if self.dialect == "sqlite":
            db_path = sa.engine.make_url(self.url).database
            return Path(db_path).resolve().parent if db_path and db_path != ":memory:" else None
        return None

    def describe(self):
        u = sa.engine.make_url(self.url)
        if self.dialect == "sqlite":
            return {"engine": "SQLite", "location": str(Path(u.database).resolve()) if u.database else ":memory:",
                    "backups": self.backups.kind}
        return {"engine": "PostgreSQL", "location": f"{u.host or ''}/{u.database or ''}", "backups": self.backups.kind}

    # ------------------------------------------------------------------ connections
    def _tx_conn(self):
        return getattr(self._local, "conn", None)

    @contextmanager
    def _write_conn(self):
        """Open a transaction holding the database-wide write lock; yield (conn, data_version)."""
        conn = self.engine.connect()
        conn.execution_options(cafebooks_write=True)
        try:
            try:
                if self.dialect == "postgresql":
                    conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_SECONDS}s'"))
                    conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": WRITE_LOCK_KEY})
                version = self._read_version(conn)
            except sa.exc.OperationalError as exc:
                raise BusyError("The database is busy with another save. Nothing was changed; please try "
                                "again in a moment.") from exc
            self._local.conn, self._local.version = conn, version
            yield conn, version
            if conn.in_transaction():
                conn.rollback()
        except BaseException:
            if conn.in_transaction():
                conn.rollback()
            raise
        finally:
            self._local.conn = None
            conn.close()

    def _read_version(self, conn):
        value = conn.execute(sa.select(self.meta_table.c.value)
                             .where(self.meta_table.c.key == "data_version")).scalar()
        return int(value or 0)

    def _write_version(self, conn, version):
        conn.execute(self.meta_table.update().where(self.meta_table.c.key == "data_version")
                     .values(value=str(version)))

    @contextmanager
    def request_scope(self):
        """Within one web request, check the shared data_version only once."""
        token = _VERSION_MEMO.set({})
        try:
            yield
        finally:
            _VERSION_MEMO.reset(token)

    def _current_version(self):
        memo = _VERSION_MEMO.get()
        if memo is not None and id(self) in memo:
            return memo[id(self)]
        with self.engine.connect() as conn:
            version = self._read_version(conn)
        if memo is not None:
            memo[id(self)] = version
        return version

    @property
    def version(self):
        """Changes whenever any process commits data (used as a cache key by the services)."""
        if self._tx_conn() is not None:
            return self._local.version
        return self._current_version()

    # ------------------------------------------------------------------ initialisation
    def initialize(self):
        """Create missing tables (idempotent; safe when several instances start at once)."""
        try:
            with self.engine.connect() as conn:
                conn.execution_options(cafebooks_write=True)
                if self.dialect == "postgresql":
                    conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_SECONDS}s'"))
                    conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": WRITE_LOCK_KEY})
                else:
                    conn.execute(sa.text("SELECT 1"))       # starts BEGIN IMMEDIATE
                self.metadata.create_all(conn)
                self._check_columns(conn)
                if conn.execute(sa.select(self.meta_table.c.value)
                                .where(self.meta_table.c.key == "data_version")).first() is None:
                    conn.execute(self.meta_table.insert(), [{"key": "data_version", "value": "0"},
                                                           {"key": "storage_layout", "value": "1"}])
                st = self.tables["settings"]
                stored = conn.execute(sa.select(st.c.value).where(st.c.key == "schema_version")).scalar()
                if stored is None:
                    seq = conn.execute(sa.select(sa.func.max(st.c.row_seq))).scalar() or 0
                    conn.execute(st.insert(), {"row_seq": seq + 1, "key": "schema_version",
                                               "value": str(SCHEMA_VERSION),
                                               "modified_at": now_local().isoformat(timespec="seconds")})
                    self._write_version(conn, self._read_version(conn) + 1)
                elif int(float(stored or 0)) > SCHEMA_VERSION:
                    raise StartupError("The data was created by a newer CafeBooks version; refusing to open it.")
                conn.commit()
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001  (driver errors, bad URL, unreachable host)
            raise StartupError(f"Cannot open the database: {getattr(exc, 'orig', None) or exc}") from exc
        self._cache.clear()

    def _check_columns(self, conn):
        insp = sa.inspect(conn)
        problems = []
        for name, table in self.tables.items():
            have = {c["name"] for c in insp.get_columns(name)}
            want = {c.name for c in table.columns}
            if have != want:
                problems.append(f"table '{name}' has columns {sorted(have ^ want)} that CafeBooks does not expect")
        if problems:
            raise StartupError("This database already contains tables that are not CafeBooks tables ("
                               + "; ".join(problems) + "). Use a separate, empty database for CafeBooks.")

    # ------------------------------------------------------------------ reading
    def _load(self, conn, table, version):
        hit = self._cache.get(table)
        if hit is not None and hit[0] == version:
            return hit[1]
        t = self.tables[table]
        cols = SCHEMAS[table]
        rows = conn.execute(sa.select(t.c.row_seq, *[t.c[c] for c in cols]).order_by(t.c.row_seq)).all()
        if rows:
            df = pd.DataFrame([tuple(r)[1:] for r in rows], columns=cols, dtype=object)
            max_seq = max(r[0] for r in rows)
        else:
            df, max_seq = empty_frame(table), 0
        self._cache[table] = (version, df, max_seq)
        return df

    def read_all(self, tables=None):
        """Consistent copies of several tables (one snapshot, no write lock)."""
        tables = list(tables or SCHEMAS)
        for t in tables:
            if t not in SCHEMAS:
                raise KeyError(t)
        conn = self._tx_conn()
        if conn is not None:
            return {t: self._load(conn, t, self._local.version).copy() for t in tables}
        version = self._current_version()
        cached = {t: self._cache.get(t) for t in tables}
        if all(hit is not None and hit[0] == version for hit in cached.values()):
            return {t: hit[1].copy() for t, hit in cached.items()}
        with self.engine.connect() as conn:
            if self.dialect == "postgresql":
                conn.execution_options(isolation_level="REPEATABLE READ")
            version = self._read_version(conn)      # read the version first, then the rows
            out = {t: self._load(conn, t, version).copy() for t in tables}
        memo = _VERSION_MEMO.get()
        if memo is not None:
            memo[id(self)] = version
        return out

    def read(self, table):
        return self.read_all([table])[table]

    def snapshot(self):
        return self.read_all()

    def invalidate(self):
        self._cache.clear()

    def warm_cache(self):
        self.read_all()

    def row_counts(self):
        return {t: len(df) for t, df in self.read_all().items()}

    # ------------------------------------------------------------------ writing
    @contextmanager
    def transaction(self, tables, operation="update"):
        """Yield mutable copies of the named tables.  On normal exit every changed table is
        committed in one database transaction; on exception nothing is written."""
        tables = list(dict.fromkeys(tables))
        for t in tables:
            if t not in SCHEMAS:
                raise KeyError(t)
        with self._lock:
            if self._tx_conn() is not None:
                raise StorageError("Nested write transactions are not supported.")
            with self._write_conn() as (conn, version):
                originals = {t: self._load(conn, t, version) for t in tables}
                frames = {t: originals[t].copy() for t in tables}
                yield frames
                changed = {}
                for t in tables:
                    df = finalize_frame(frames[t], t)
                    if not df.equals(originals[t]):
                        changed[t] = df
                if changed:
                    self._commit(conn, version, changed, operation, originals)

    def replace_all(self, frames, operation):
        """Replace every table with the given frames (restore / import / reset)."""
        final = {t: finalize_frame(frames[t] if t in frames else empty_frame(t), t) for t in SCHEMAS}
        with self._lock:
            if self._tx_conn() is not None:
                raise StorageError("Nested write transactions are not supported.")
            with self._write_conn() as (conn, version):
                self._commit(conn, version, final, operation, originals=None)

    def _commit(self, conn, version, changed, operation, originals):
        try:
            seqs = {}
            for t, df in changed.items():
                self._check_keys(t, df)
                if originals is None:
                    seqs[t] = self._rewrite(conn, t, df)
                else:
                    seqs[t] = self._apply_diff(conn, t, originals[t], df)
            new_version = version + 1
            self._write_version(conn, new_version)
            conn.commit()
        except sa.exc.IntegrityError as exc:
            raise StorageError(f"The save was refused by the database and nothing was changed: "
                               f"{getattr(exc, 'orig', exc)}") from exc
        except sa.exc.OperationalError as exc:
            raise StorageError(f"The database could not complete the save; nothing was changed: "
                               f"{getattr(exc, 'orig', exc)}") from exc
        for t, hit in list(self._cache.items()):
            if hit[0] == version and t not in changed:
                self._cache[t] = (new_version, hit[1], hit[2])
        for t, df in changed.items():
            self._cache[t] = (new_version, df, seqs[t])
        memo = _VERSION_MEMO.get()
        if memo is not None:
            memo[id(self)] = new_version
        self.last_saved = now_local()
        log.info("Saved %s (data version %d): %s", operation, new_version, ", ".join(changed))

    @staticmethod
    def _check_keys(table, df):
        pk = PRIMARY_KEYS[table]
        keys = df[pk].astype(str)
        if (keys.str.strip() == "").any():
            raise StorageError(f"{table}: a row has an empty {pk}; nothing was saved.")
        dup = keys[keys.duplicated()].unique().tolist()
        if dup:
            raise StorageError(f"{table}: duplicate {pk} values {dup[:5]}; nothing was saved.")

    def _rewrite(self, conn, table, df):
        t = self.tables[table]
        conn.execute(t.delete())
        rows = [{"row_seq": i + 1, **rec} for i, rec in enumerate(df.to_dict("records"))]
        if rows:
            conn.execute(t.insert(), rows)
        return len(rows)

    def _apply_diff(self, conn, table, old, new):
        t = self.tables[table]
        pk = PRIMARY_KEYS[table]
        cols = SCHEMAS[table]
        old_rows = dict(zip(old[pk].tolist(), old.to_dict("records")))
        max_seq = conn.execute(sa.select(sa.func.max(t.c.row_seq))).scalar() or 0
        inserts, updates = [], []
        for rec in new.to_dict("records"):
            before = old_rows.pop(rec[pk], None)
            if before is None:
                max_seq += 1
                inserts.append({"row_seq": max_seq, **rec})
            elif before != rec:
                updates.append(rec)
        for chunk in _chunks(list(old_rows)):
            conn.execute(t.delete().where(t.c[pk].in_(chunk)))
        if updates:
            stmt = (t.update().where(t.c[pk] == sa.bindparam("k_pk"))
                    .values({c: sa.bindparam(f"v_{c}") for c in cols if c != pk}))
            conn.execute(stmt, [{"k_pk": r[pk], **{f"v_{c}": r[c] for c in cols if c != pk}} for r in updates])
        if inserts:
            conn.execute(t.insert(), inserts)
        return max_seq

    # ------------------------------------------------------------------ settings table
    def get_setting(self, key, default=""):
        df = self.read("settings")
        hit = df[df["key"] == key]
        return str(hit["value"].iloc[0]) if not hit.empty else default

    def set_setting(self, key, value):
        with self.transaction(["settings"], "set_setting") as f:
            df = f["settings"]
            now = now_local().isoformat(timespec="seconds")
            if (df["key"] == key).any():
                df.loc[df["key"] == key, ["value", "modified_at"]] = [str(value), now]
            else:
                df.loc[len(df)] = [key, str(value), now]

    # ------------------------------------------------------------------ backups / restore
    def backup(self, label="manual"):
        """Create a verified backup of every table; return its name."""
        frames = self.snapshot()
        with tempfile.TemporaryDirectory(prefix="cafebooks_backup_") as tmp:
            for t, df in frames.items():
                write_table_file(Path(tmp) / FILENAME_MAP[t], t, df)
            data = build_backup_zip(tmp, label)
        return self.backups.save(new_backup_name(label), data)

    def maybe_daily_backup(self):
        stamp = now_local().strftime("%Y%m%d")
        if any(b["name"].startswith(f"daily_{stamp}") for b in self.backups.list()):
            return None
        return self.backup(f"daily_{stamp}")

    @contextmanager
    def _backup_path(self, source):
        """source: a backup name in the store, or a Path to an uploaded zip."""
        if isinstance(source, Path):
            if not source.exists():
                raise BackupError("Backup file not found.")
            yield source
        else:
            with self.backups.as_path(source) as p:
                yield p

    def restore_preview(self, source):
        with self._backup_path(source) as path:
            manifest = verify_backup(path)
            with tempfile.TemporaryDirectory(prefix="cafebooks_restore_") as tmp:
                extract_backup(path, tmp)
                frames, load_issues = load_workbook_folder(tmp)
        missing = [t for t in SCHEMAS if t not in frames and t != "settings"]
        if missing:
            raise BackupError("Backup is missing: " + ", ".join(FILENAME_MAP[t] for t in missing))
        for t in SCHEMAS:
            frames.setdefault(t, empty_frame(t))
        frames = migrate_frames(frames)
        current = self.row_counts()
        return {"manifest": manifest,
                "tables": {t: {"backup": len(frames[t]), "current": current[t]} for t in SCHEMAS},
                "load_issues": load_issues, "issues": validate_dataset(frames), "frames": frames}

    def restore(self, source):
        info = self.restore_preview(source)
        if info["load_issues"]:
            raise BackupError(f"Backup contains {len(info['load_issues'])} unreadable values "
                              f"(first: {info['load_issues'][0]}). Restore refused.")
        errors = [i for i in info["issues"] if i.severity == "error"]
        if errors:
            raise BackupError(f"Backup contains {len(errors)} accounting integrity errors "
                              f"(first: {errors[0].message}). Restore refused.")
        safety = self.backup("pre_restore")
        frames = info["frames"]
        frames["settings"] = settings_with_schema_version(frames["settings"])
        self.replace_all(frames, "restore")
        return safety

    # ------------------------------------------------------------------ exports / checks
    def export_database_snapshot(self):
        """Every table as one sheet of a single .xlsx (bytes)."""
        frames = self.snapshot()
        buf = io.BytesIO()
        write_xlsx_multi(buf, [(t, SCHEMAS[t], frames[t].itertuples(index=False, name=None),
                                MONEY_COLUMNS.get(t, [])) for t in SCHEMAS])
        return buf.getvalue()

    def load_workbook_folder(self, folder):
        return load_workbook_folder(folder)

    @staticmethod
    def _empty(table):
        return empty_frame(table)

    def validate_all(self):
        return validate_dataset(self.read_all())

    def close(self):
        self.engine.dispose()
