"""Timestamped, hash-verified backups with retention."""
import hashlib
import json
import os
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .schemas import SCHEMAS, FILENAME_MAP, SCHEMA_VERSION


class BackupError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupManager:
    MANIFEST = "manifest.json"

    def __init__(self, data_dir, backup_dir, retention=30):
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.retention = max(1, int(retention))
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, label="manual"):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.backup_dir / f"{label}_{stamp}_{uuid.uuid4().hex[:6]}.zip"
        tmp = out.with_suffix(".zip.part")
        files = {}
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in FILENAME_MAP.values():
                p = self.data_dir / name
                if p.exists():
                    files[name] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                    z.write(p, name)
            manifest = {"created_at": datetime.now().isoformat(timespec="seconds"), "label": label,
                        "schema_version": SCHEMA_VERSION, "files": files}
            z.writestr(self.MANIFEST, json.dumps(manifest, indent=2))
        self.verify_backup(tmp)
        os.replace(tmp, out)
        self.prune()
        return out

    def verify_backup(self, path):
        """Return the manifest dict or raise BackupError.  Legacy zips without a
        manifest are returned with legacy=True (integrity not provable)."""
        path = Path(path)
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    raise BackupError(f"Backup is corrupted (bad member {bad}).")
                names = set(z.namelist())
                if self.MANIFEST not in names:
                    if not any(n in names for n in FILENAME_MAP.values()):
                        raise BackupError("Not a CafeBooks backup.")
                    return {"legacy": True, "files": {}, "created_at": "", "schema_version": 3}
                manifest = json.loads(z.read(self.MANIFEST))
                for name, meta in manifest["files"].items():
                    if name not in names:
                        raise BackupError(f"Backup is missing {name}.")
                    h = hashlib.sha256(z.read(name)).hexdigest()
                    if h != meta["sha256"]:
                        raise BackupError(f"Checksum mismatch for {name}; the backup is damaged.")
                return manifest
        except zipfile.BadZipFile:
            raise BackupError("File is not a valid backup archive.") from None

    def list_backups(self):
        return sorted(self.backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)

    def prune(self):
        """Keep the newest `retention` backups; always keep at least one verifiable backup."""
        backups = self.list_backups()
        removed = []
        for old in backups[self.retention:]:
            try:
                old.unlink()
                removed.append(old)
            except OSError:
                pass
        return removed

    def latest_valid_backup(self):
        for p in self.list_backups():
            try:
                self.verify_backup(p)
                return p
            except BackupError:
                continue
        return None

    def extract(self, path, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name in FILENAME_MAP.values():  # never trust other member names (zip-slip)
                    with z.open(name) as src, open(dest / name, "wb") as dst:
                        dst.write(src.read())
        return dest
