"""File locks, corruption, crash recovery, backups/restore, import workflow, integrity, audit."""
import json
import os
import shutil
from pathlib import Path
import pandas as pd
import pytest
from database import excel_manager as em
from database.backup_manager import BackupError
from database.excel_manager import (ExcelManager, FileLockedError, StartupError, AlreadyRunningError)
from database.schemas import SCHEMAS
from database.sync_manager import SyncManager, EDIT_DIR
from services.accounting_service import AccountingService
from database.excel_io import write_xlsx
from database.schemas import MONEY_COLUMNS


def test_excel_lock_file_blocks_write_and_preserves_data(svc, db, vendor):
    (db.data_dir / "~$vendors.xlsx").write_text("lock")
    with pytest.raises(FileLockedError):
        svc.add_vendor("Blocked", "Other")
    (db.data_dir / "~$vendors.xlsx").unlink()
    assert len(db.read("vendors")) == 1
    svc.add_vendor("Now ok", "Other")


def test_replace_permission_error_rolls_back_all_files(svc, db, vendor, monkeypatch):
    before = {t: (db.data_dir / f"{t}.xlsx").read_bytes() for t in ("bills", "audit_log")}
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        if ".stage_" in str(src):
            calls["n"] += 1
            if calls["n"] == 2:                    # second table of the transaction fails mid-swap
                raise PermissionError("locked")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(em.os, "replace", flaky)
    with pytest.raises(FileLockedError):
        svc.add_bill(vendor, "L1", "01-03-2026", "31-03-2026", "Other", 100)
    monkeypatch.undo()
    for t, data in before.items():
        assert (db.data_dir / f"{t}.xlsx").read_bytes() == data, t
    assert not db.journal_path.exists()
    assert db.read("bills").empty and db.read("audit_log").shape[0] == 1


def test_corrupted_workbook_refuses_start_and_keeps_files(tmp_path):
    db = ExcelManager(tmp_path / "d", tmp_path / "b")
    AccountingService(db).add_vendor("V", "Other")
    bad = tmp_path / "d" / "bills.xlsx"
    bad.write_bytes(b"not an excel file")
    with pytest.raises(StartupError, match="bills.xlsx"):
        ExcelManager(tmp_path / "d", tmp_path / "b")
    assert bad.read_bytes() == b"not an excel file"           # nothing overwritten


def test_missing_workbook_is_not_silently_recreated(tmp_path):
    ExcelManager(tmp_path / "d", tmp_path / "b")
    (tmp_path / "d" / "payments.xlsx").unlink()
    with pytest.raises(StartupError, match="payments.xlsx"):
        ExcelManager(tmp_path / "d", tmp_path / "b")


def test_unexpected_columns_rejected(tmp_path):
    ExcelManager(tmp_path / "d", tmp_path / "b")
    write_xlsx(tmp_path / "d" / "vendors.xlsx", "vendors", SCHEMAS["vendors"] + ["surprise"], [])
    with pytest.raises(StartupError, match="surprise"):
        ExcelManager(tmp_path / "d", tmp_path / "b")


def _crash_after_n_replaces(monkeypatch, n):
    real = os.replace
    count = {"n": 0}

    class Crash(BaseException):
        pass

    def crashing(src, dst, *a, **k):
        if ".stage_" in str(src):
            if count["n"] >= n:
                raise Crash()
            count["n"] += 1
        return real(src, dst, *a, **k)
    monkeypatch.setattr(em.os, "replace", crashing)
    return Crash


def test_interrupted_write_is_rolled_back_on_restart(tmp_path, monkeypatch):
    db = ExcelManager(tmp_path / "d", tmp_path / "b")
    s = AccountingService(db)
    v = s.add_vendor("V", "Other")
    snapshot = {t: db.read(t) for t in SCHEMAS}
    Crash = _crash_after_n_replaces(monkeypatch, 1)       # crash with one of three files swapped
    with pytest.raises(Crash):
        s.add_sales("01-10-2026", 100, cash=100, tips=0)
    monkeypatch.undo()
    assert db.journal_path.exists()
    db2 = ExcelManager(tmp_path / "d", tmp_path / "b")
    assert "rolled back" in db2.recovery_message
    for t in SCHEMAS:
        assert db2.read(t).equals(snapshot[t]), t
    assert not db2.journal_path.exists()


def test_fully_committed_write_survives_missing_journal_cleanup(tmp_path, monkeypatch):
    db = ExcelManager(tmp_path / "d", tmp_path / "b")
    s = AccountingService(db)
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "transaction_journal.json":
            raise RuntimeError("crash before cleanup")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(RuntimeError):
        s.add_vendor("Committed", "Other")
    monkeypatch.undo()
    db2 = ExcelManager(tmp_path / "d", tmp_path / "b")
    assert "already completed" in db2.recovery_message
    assert list(db2.read("vendors")["name"]) == ["Committed"]       # committed data NOT rolled back


def test_backup_verify_restore_and_preview(svc, db, vendor):
    svc.add_bill(vendor, "BK1", "01-03-2026", "31-03-2026", "Other", 100)
    zp = db.backup("manual")
    assert db.backups.verify_backup(zp)["files"]
    svc.add_vendor("After backup", "Other")
    prev = db.restore_preview(zp)
    assert prev["tables"]["vendors"] == {"backup": 1, "current": 2}
    safety = db.restore(zp)
    assert safety.exists()
    assert list(db.read("vendors")["name"]) == ["Fresh Milk Co"]
    assert len(db.read("bills")) == 1


def test_tampered_backup_is_rejected(svc, db, vendor):
    import zipfile
    zp = db.backup("manual")
    bad = zp.with_name("tampered.zip")
    with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "vendors.xlsx":
                data = data + b"x"
            zout.writestr(item, data)
    with pytest.raises(BackupError):
        db.restore(bad)
    assert len(db.read("vendors")) == 1


def test_backup_retention_prunes_oldest(tmp_path):
    db = ExcelManager(tmp_path / "d", tmp_path / "b", retention=3)
    for i in range(6):
        db.backup(f"t{i}")
    assert len(db.backups.list_backups()) == 3


def test_legacy_v3_data_is_migrated(tmp_path):
    """v3 workbooks: mutable allocation status, no entry_type."""
    d = tmp_path / "d"
    ExcelManager(d, tmp_path / "b")
    old_cols = ["allocation_id", "payment_id", "bill_id", "amount", "allocated_at", "reversal_of", "status"]
    write_xlsx(d / "payment_allocations.xlsx", "x", old_cols,
               [["A1", "P1", "B1", "100.00", "2026-03-02T10:00:00", "", "Reversed"]], money_columns=["amount"])
    write_xlsx(d / "settings.xlsx", "settings", SCHEMAS["settings"], [])
    db = ExcelManager(d, tmp_path / "b")
    a = db.read("payment_allocations")
    assert sorted(a["entry_type"]) == ["ALLOCATE", "REVERSE"]
    assert any("pre_migration" in p.name for p in db.backups.list_backups())
    assert db.get_setting("schema_version") == "4"


def test_instance_lock(tmp_path):
    a = ExcelManager(tmp_path / "d", tmp_path / "b")
    a.acquire_instance_lock()
    b = ExcelManager(tmp_path / "d", tmp_path / "b")
    (tmp_path / "d" / ".cafebooks.lock").write_text(str(os.getppid()))   # another live process
    with pytest.raises(AlreadyRunningError):
        b.acquire_instance_lock()
    (tmp_path / "d" / ".cafebooks.lock").write_text("999999")           # stale pid is reclaimed
    b.acquire_instance_lock()
    b.close()


# ------------------------------------------------------------------ import workflow
def _edit(path, fn):
    df = pd.read_excel(path, dtype=object).fillna("")
    fn(df)
    cols = list(df.columns)
    money = [c for t, m in MONEY_COLUMNS.items() for c in m]
    write_xlsx(path, path.stem, cols, df.itertuples(index=False, name=None), money_columns=money)


def test_export_edit_import_applies_validated_changes(svc, db, vendor, tmp_path):
    sync = SyncManager(db, tmp_path / "staging")
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "vendors.xlsx", lambda df: df.__setitem__("name", ["Renamed Co"]))
    plan = sync.preview_import(ex["folder"])
    assert not plan.errors and plan.counts["vendors"]["modified"] == 1
    assert db.read("vendors").iloc[0]["name"] == "Fresh Milk Co"           # preview changes nothing
    res = sync.apply_import(ex["folder"])
    assert res["applied"] and db.read("vendors").iloc[0]["name"] == "Renamed Co"
    assert any("pre_import" in p.name for p in db.backups.list_backups())
    again = sync.apply_import(ex["folder"])                                   # replay is a no-op
    assert not again["applied"]
    assert len(db.read("audit_log")) == len(db.read("audit_log").drop_duplicates("audit_id"))


def test_import_conflict_requires_decision(svc, db, vendor, tmp_path):
    sync = SyncManager(db, tmp_path / "staging")
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "vendors.xlsx", lambda df: df.__setitem__("name", ["Edited Externally"]))
    svc.update_vendor(vendor, name="Edited In App")                            # live changes meanwhile
    plan = sync.preview_import(ex["folder"])
    assert len(plan.conflicts) == 1 and "vendors" in plan.stale_tables
    with pytest.raises(ValueError, match="explicit decision"):
        sync.apply_import(ex["folder"])
    assert db.read("vendors").iloc[0]["name"] == "Edited In App"
    sync.apply_import(ex["folder"], {("vendors", vendor): "live"})
    assert db.read("vendors").iloc[0]["name"] == "Edited In App"
    ex2_plan = sync.preview_import(ex["folder"])
    sync.apply_import(ex["folder"], {("vendors", vendor): "import"})
    assert db.read("vendors").iloc[0]["name"] == "Edited Externally"


def test_import_rejects_bad_edits(svc, db, vendor, tmp_path):
    b = svc.add_bill(vendor, "I1", "01-03-2026", "31-03-2026", "Other", 100)
    svc.record_payment(vendor, "02-03-2026", 50, "Cash", [(b, 50)])
    sync = SyncManager(db, tmp_path / "staging")
    # deleting rows
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "bills.xlsx", lambda df: df.drop(df.index, inplace=True))
    assert any("deleted" in e for e in sync.preview_import(ex["folder"]).errors)
    # editing the append-only allocation ledger
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "payment_allocations.xlsx", lambda df: df.__setitem__("amount", ["10.00"]))
    assert any("append-only" in e for e in sync.preview_import(ex["folder"]).errors)
    # accounting invariant: total no longer equals components
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "bills.xlsx", lambda df: df.__setitem__("invoice_total", ["999.00"]))
    plan = sync.preview_import(ex["folder"])
    assert any("does not equal" in e for e in plan.errors)
    with pytest.raises(ValueError):
        sync.apply_import(ex["folder"])
    assert db.read("bills").iloc[0]["invoice_total"] == "100.00"
    # formulas are refused
    ex = sync.export_for_edit()
    from openpyxl import load_workbook
    p = ex["edit_dir"] / "vendors.xlsx"
    wb = load_workbook(p); wb.active["B2"] = "=1+1"; wb.save(p)
    assert any("formulas" in e for e in sync.preview_import(ex["folder"]).errors)
    # not an export folder
    assert sync.preview_import(tmp_path).errors


def test_export_refused_when_data_has_errors(svc, db, vendor, tmp_path):
    with db.transaction(["bills"]) as f:
        f["bills"].loc[len(f["bills"])] = ["BIL-9", "VEN-MISSING", "Z", "2026-01-01", "2026-01-02", "Other", "", "1.00",
                                         "0.00", "0.00", "1.00", "", "", "Active", "", ""]
    with pytest.raises(ValueError, match="integrity errors"):
        SyncManager(db, tmp_path / "s").export_for_edit()


# ------------------------------------------------------------------ integrity & audit
def test_integrity_checker_finds_orphans_and_overallocation(svc, db, vendor):
    b = svc.add_bill(vendor, "N1", "01-03-2026", "31-03-2026", "Other", 100)
    p = svc.record_payment(vendor, "02-03-2026", 100, "Cash", [(b, 100)])
    assert svc.integrity_check() == []
    with db.transaction(["payment_allocations"]) as f:
        a = f["payment_allocations"]
        a.loc[len(a)] = ["ALC-X", p, "BIL-NOPE", "ALLOCATE", "10.00", "2026-03-03", "", "", "Active"]
        a.loc[len(a)] = ["ALC-Y", p, b, "ALLOCATE", "50.00", "2026-03-03", "", "", "Active"]
    msgs = "\n".join(svc.integrity_check())
    assert "missing bill" in msgs and "exceed" in msgs


def test_audit_log_consistency(svc, vendor):
    b = svc.add_bill(vendor, "AU1", "01-03-2026", "31-03-2026", "Other", 100)
    p = svc.record_payment(vendor, "02-03-2026", 100, "Cash", [(b, 100)])
    svc.reverse_payment(p, "undo")
    a = svc.db.read("audit_log")
    assert a["audit_id"].is_unique
    assert list(a["action"]) == ["create", "create", "create", "reverse"]
    assert (a["actor"] == "tester").all()
    assert list(a["entity_type"]) == ["vendor", "bill", "payment", "payment"]
    assert list(a["timestamp"]) == sorted(a["timestamp"])


def test_performance_10k_bills(tmp_path):
    import time
    from database.ids import append_rows
    db = ExcelManager(tmp_path / "d", tmp_path / "b")
    s = AccountingService(db)
    v = s.add_vendor("Big", "Other")
    with db.transaction(["bills", "payment_allocations"]) as f:
        rows = [{"bill_id": f"BIL-{i:06d}", "vendor_id": v, "invoice_number": f"I{i}", "invoice_date": "2026-01-01",
                 "due_date": "2026-02-01", "category": "Other", "description": "", "subtotal": "100.00",
                 "tax_amount": "0.00", "other_charges": "0.00", "invoice_total": "100.00", "attachment_path": "",
                 "notes": "", "status": "Active", "created_at": "", "modified_at": ""} for i in range(1, 10001)]
        f["bills"] = append_rows(f["bills"], rows)
        arows = [{"allocation_id": f"ALC-{i:06d}", "payment_id": "PAY-X", "bill_id": f"BIL-{(i % 10000) + 1:06d}",
                  "entry_type": "ALLOCATE", "amount": "1.00", "effective_date": "2026-01-05", "allocated_at": "",
                  "reversal_of": "", "status": "Active"} for i in range(1, 50001)]
        f["payment_allocations"] = append_rows(f["payment_allocations"], arows)
    db2 = ExcelManager(tmp_path / "d", tmp_path / "b")
    s2 = AccountingService(db2)
    t = time.time(); s2.bill_balances("2026-03-01"); load_and_calc = time.time() - t
    t = time.time(); s2.bill_balances("2026-03-02"); warm = time.time() - t
    t = time.time(); s2.add_bill(v, "NEW", "01-03-2026", "31-03-2026", "Other", 10); write = time.time() - t
    print(f"\nperf: cold load+balances {load_and_calc:.1f}s, warm balances {warm:.2f}s, add_bill write {write:.1f}s")
    assert warm < 3 and write < 15
