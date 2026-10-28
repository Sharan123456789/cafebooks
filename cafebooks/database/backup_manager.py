"""Timestamped, hash-verified backups with retention.

A backup is a zip of one workbook per table plus manifest.json (SHA-256 of every file), the
same format earlier CafeBooks versions wrote, so their backup zips can still be restored.

Where backups live depends on the deployment:
  * FileBackupStore     - a folder on disk (desktop app, VPS with a persistent disk)
  * DatabaseBackupStore - a table inside the shared database (Vercel and other platforms
                          whose disk is wiped between requests)
"""
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from cafebooks.utils.date_utils import now_local, local_from_timestamp
from .schemas import SCHEMAS, FILENAME_MAP, SCHEMA_VERSION

MANIFEST = "manifest.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.zip$")


class BackupError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_name(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise BackupError("Invalid backup name.")
    return name


def new_backup_name(label):
    label = re.sub(r"[^A-Za-z0-9_-]", "_", str(label or "backup"))[:40] or "backup"
    return f"{label}_{now_local().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.zip"


def label_of(name):
    """'daily_20261029_20261029_013600_ab12cd.zip' -> 'daily_20261029'."""
    parts = name[:-4].split("_")
    return "_".join(parts[:-3]) if len(parts) > 3 else name[:-4]


def build_backup_zip(folder, label):
    """Zip the table workbooks in `folder` with a manifest and return the bytes."""
    folder = Path(folder)
    buf = io.BytesIO()
    files = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILENAME_MAP.values():
            p = folder / name
            if p.exists():
                files[name] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                z.write(p, name)
        manifest = {"created_at": now_local().isoformat(timespec="seconds"), "label": label,
                    "schema_version": SCHEMA_VERSION, "files": files}
        z.writestr(MANIFEST, json.dumps(manifest, indent=2))
    data = buf.getvalue()
    verify_backup(io.BytesIO(data))
    return data


def verify_backup(src):
    """src: path or binary file object.  Return the manifest dict or raise BackupError.
    Legacy zips without a manifest are returned with legacy=True (integrity not provable)."""
    try:
        with zipfile.ZipFile(src) as z:
            bad = z.testzip()
            if bad:
                raise BackupError(f"Backup is corrupted (bad member {bad}).")
            names = set(z.namelist())
            if MANIFEST not in names:
                if not any(n in names for n in FILENAME_MAP.values()):
                    raise BackupError("Not a CafeBooks backup.")
                return {"legacy": True, "files": {}, "created_at": "", "schema_version": 3}
            manifest = json.loads(z.read(MANIFEST))
            for name, meta in manifest["files"].items():
                if name not in names:
                    raise BackupError(f"Backup is missing {name}.")
                if hashlib.sha256(z.read(name)).hexdigest() != meta["sha256"]:
                    raise BackupError(f"Checksum mismatch for {name}; the backup is damaged.")
            return manifest
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError):
        raise BackupError("File is not a valid CafeBooks backup archive.") from None


def extract_backup(src, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        for name in z.namelist():
            if name in FILENAME_MAP.values():  # never trust other member names (zip-slip)
                with z.open(name) as fin, open(dest / name, "wb") as fout:
                    fout.write(fin.read())
    return dest


class FileBackupStore:
    kind = "files"

    def __init__(self, backup_dir, retention=30):
        self.backup_dir = Path(backup_dir)
        self.retention = max(1, int(retention))
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        return self.backup_dir / check_name(name)

    def save(self, name, data):
        out = self._path(name)
        tmp = out.with_suffix(".zip.part")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
        self.prune()
        return name

    def list(self):
        items = sorted(self.backup_dir.glob("*.zip"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        return [{"name": p.name, "label": label_of(p.name), "size": p.stat().st_size,
                 "created_at": local_from_timestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
                for p in items if _NAME.match(p.name)]

    def exists(self, name):
        return self._path(name).exists()

    def read_bytes(self, name):
        p = self._path(name)
        if not p.exists():
            raise BackupError("Backup not found.")
        return p.read_bytes()

    @contextmanager
    def as_path(self, name):
        p = self._path(name)
        if not p.exists():
            raise BackupError("Backup not found.")
        yield p

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)

    def prune(self):
        """Keep the newest `retention` backups."""
        removed = []
        for item in self.list()[self.retention:]:
            try:
                self.delete(item["name"])
                removed.append(item["name"])
            except OSError:
                pass
        return removed


class DatabaseBackupStore:
    kind = "database"

    def __init__(self, engine, table, retention=30):
        self.engine, self.table = engine, table
        self.retention = max(1, int(retention))

    def save(self, name, data):
        check_name(name)
        row = {"name": name, "label": label_of(name), "created_at": now_local().isoformat(timespec="seconds"),
               "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "data": data}
        with self.engine.begin() as conn:
            conn.execute(self.table.insert(), row)
        self.prune()
        return name

    def list(self):
        t = self.table
        with self.engine.connect() as conn:
            rows = conn.execute(t.select().with_only_columns(t.c.name, t.c.label, t.c.size, t.c.created_at)
                                .order_by(t.c.created_at.desc(), t.c.name.desc())).all()
        return [{"name": r.name, "label": r.label, "size": r.size, "created_at": r.created_at} for r in rows]

    def exists(self, name):
        t = self.table
        with self.engine.connect() as conn:
            return conn.execute(t.select().with_only_columns(t.c.name).where(t.c.name == check_name(name))).first() is not None

    def read_bytes(self, name):
        t = self.table
        with self.engine.connect() as conn:
            row = conn.execute(t.select().with_only_columns(t.c.data).where(t.c.name == check_name(name))).first()
        if row is None:
            raise BackupError("Backup not found.")
        return bytes(row.data)

    @contextmanager
    def as_path(self, name):
        data = self.read_bytes(name)
        fd, tmp = tempfile.mkstemp(prefix="cafebooks_backup_", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            yield Path(tmp)
        finally:
            os.unlink(tmp)

    def delete(self, name):
        with self.engine.begin() as conn:
            conn.execute(self.table.delete().where(self.table.c.name == check_name(name)))

    def prune(self):
        removed = [item["name"] for item in self.list()[self.retention:]]
        if removed:
            with self.engine.begin() as conn:
                conn.execute(self.table.delete().where(self.table.c.name.in_(removed)))
        return removed
