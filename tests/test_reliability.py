"""Atomic saves, concurrent users, backups/restore, import workflows, integrity, audit."""
import io
import threading
import zipfile
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

from cafebooks.database.backup_manager import BackupError
from cafebooks.database.errors import StartupError, StorageError
from cafebooks.database.excel_io import write_xlsx
from cafebooks.database.schemas import SCHEMAS, MONEY_COLUMNS
from cafebooks.database.sql_store import SqlStore
from cafebooks.database.sync_manager import SyncManager
from cafebooks.services.accounting_service import AccountingService
from cafebooks.services.data_service import DataService

DEMO = Path(__file__).resolve().parents[1] / "demo_data" / "CafeBooks_Demo_Data.xlsx"


# ------------------------------------------------------------------ atomic saves
def test_failed_operation_writes_nothing(svc, db, vendor):
    before = {t: db.read(t) for t in SCHEMAS}
    with pytest.raises(RuntimeError):
        with db.transaction(["bills", "audit_log"]) as f:
            f["bills"].loc[len(f["bills"])] = ["BIL-1", vendor, "Z", "2026-01-01", "2026-01-02", "Other", "", "1.00",
                                               "0.00", "0.00", "1.00", "", "", "Active", "", ""]
            raise RuntimeError("power cut")
    for t in SCHEMAS:
        assert db.read(t).equals(before[t]), t


def test_database_error_mid_commit_rolls_back_every_table(svc, db, vendor, monkeypatch):
    before = {t: db.read(t) for t in SCHEMAS}
    real = db._apply_diff
    calls = {"n": 0}

    def flaky(conn, table, old, new):
        calls["n"] += 1
        if calls["n"] == 2:                       # second table of a three-table save fails
            raise sa.exc.OperationalError("INSERT", {}, Exception("disk full"))
        return real(conn, table, old, new)
    monkeypatch.setattr(db, "_apply_diff", flaky)
    with pytest.raises(StorageError, match="nothing was changed"):
        svc.add_sales("01-10-2026", 100, cash=90, tips=10)
    monkeypatch.undo()
    fresh = SqlStore(db.url, backup_dir=None, backup_store="database")
    for t in SCHEMAS:
        assert fresh.read(t).equals(before[t]), t       # committed state: unchanged
        assert db.read(t).equals(before[t]), t          # this process's cache: unchanged
    fresh.close()


def test_duplicate_primary_key_is_refused(db):
    with pytest.raises(StorageError, match="duplicate"):
        with db.transaction(["vendors"]) as f:
            row = ["VEN-1", "A", "", "", "", "", "Other", "", "7", "", "", True, "", ""]
            f["vendors"].loc[0] = row
            f["vendors"].loc[1] = row
    assert db.read("vendors").empty


def test_concurrent_users_never_get_duplicate_ids(svc, db):
    errors = []

    def worker(n):
        try:
            for i in range(6):
                svc.add_vendor(f"Vendor {n}-{i}", "Other")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = db.read("vendors")["vendor_id"].tolist()
    assert len(ids) == 36 and len(set(ids)) == 36
    assert db.read("audit_log")["audit_id"].is_unique


def test_two_servers_share_one_database(make_db):
    """Two app instances (e.g. two Vercel functions) see each other's saves and never clash."""
    a_db, b_db = make_db(), make_db()
    a, b = AccountingService(a_db, actor="till-1"), AccountingService(b_db, actor="till-2")
    v1 = a.add_vendor("From A", "Other")
    assert "From A" in b_db.read("vendors")["name"].tolist()        # B's cache noticed A's save
    v2 = b.add_vendor("From B", "Other")
    v3 = a.add_vendor("From A again", "Other")
    assert len({v1, v2, v3}) == 3
    bill = b.add_bill(v1, "INV-1", "01-03-2026", "31-03-2026", "Other", 100)
    from cafebooks.services.accounting_service import DuplicateError
    with pytest.raises(DuplicateError):                              # duplicate check sees B's bill
        a.add_bill(v1, "inv 1", "01-03-2026", "31-03-2026", "Other", 100)
    a.record_payment(v1, "02-03-2026", 100, "Cash", [(bill, 100)])
    with pytest.raises(ValueError, match="outstanding"):           # B cannot over-pay the bill A paid
        b.record_payment(v1, "02-03-2026", 100, "Cash", [(bill, 100)])
    actors = set(a_db.read("audit_log")["actor"])
    assert actors == {"till-1", "till-2"}


def test_foreign_tables_are_refused(db_url, tmp_path):
    from cafebooks.database.sql_store import normalize_url
    engine = sa.create_engine(normalize_url(db_url))
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE vendors (id INTEGER PRIMARY KEY, title TEXT)"))
    engine.dispose()
    with pytest.raises(StartupError, match="separate, empty database"):
        SqlStore(db_url, backup_dir=tmp_path / "b")


def test_unreachable_database_reports_startup_error(tmp_path):
    with pytest.raises(StartupError):
        SqlStore("postgresql://nobody:x@127.0.0.1:1/none", backup_store="database")


# ------------------------------------------------------------------ backups
def test_backup_verify_restore_and_preview(svc, db, vendor):
    svc.add_bill(vendor, "BK1", "01-03-2026", "31-03-2026", "Other", 100)
    name = db.backup("manual")
    assert name in [b["name"] for b in db.backups.list()]
    svc.add_vendor("After backup", "Other")
    prev = db.restore_preview(name)
    assert prev["tables"]["vendors"] == {"backup": 1, "current": 2}
    safety = db.restore(name)
    assert db.backups.exists(safety)
    assert list(db.read("vendors")["name"]) == ["Fresh Milk Co"]
    assert len(db.read("bills")) == 1


def test_backups_can_live_inside_the_database(make_db, vendor, svc):
    store = make_db(backup_store="database")
    name = store.backup("manual")
    assert store.backups.kind == "database" and store.backups.list()[0]["name"] == name
    other = make_db(backup_store="database")                          # visible to every server
    assert name in [b["name"] for b in other.backups.list()]
    AccountingService(store).add_vendor("Later", "Other")
    other.restore(name)
    assert list(store.read("vendors")["name"]) == ["Fresh Milk Co"]


def test_tampered_backup_is_rejected(svc, db, vendor, tmp_path):
    good = io.BytesIO(db.backups.read_bytes(db.backup("manual")))
    bad = tmp_path / "tampered.zip"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "vendors.xlsx":
                data = data + b"x"
            zout.writestr(item, data)
    with pytest.raises(BackupError):
        db.restore(bad)
    with pytest.raises(BackupError):
        db.restore(tmp_path / "missing.zip")
    with pytest.raises(BackupError):
        db.restore("../../etc/passwd.zip")
    assert len(db.read("vendors")) == 1


def test_backup_retention_prunes_oldest(make_db):
    db = make_db(retention=3)
    names = [db.backup(f"t{i}") for i in range(6)]
    kept = [b["name"] for b in db.backups.list()]
    assert len(kept) == 3 and set(kept) <= set(names)


def test_daily_backup_once_per_day(db):
    assert db.maybe_daily_backup() is not None
    assert db.maybe_daily_backup() is None


def test_restore_of_earlier_excel_version_backup(make_db, tmp_path):
    """Backups written by the earlier Excel-based CafeBooks restore into the database."""
    folder = tmp_path / "old"
    folder.mkdir()
    for t, cols in SCHEMAS.items():
        rows = []
        if t == "vendors":
            rows = [["VEN-000001", "Old Supplier", "", "", "", "", "Other", "", "7", "", "", True, "", ""]]
        if t == "settings":
            rows = [["schema_version", "4", ""]]
        write_xlsx(folder / f"{t}.xlsx", t, cols, rows)
    zp = tmp_path / "legacy_backup.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for p in folder.iterdir():
            z.write(p, p.name)
    db = make_db()
    db.restore(zp)
    assert db.read("vendors").iloc[0]["name"] == "Old Supplier"


# ------------------------------------------------------------------ legacy Excel data + data files
def test_legacy_v3_excel_folder_is_migrated_on_import(svc, db, tmp_path):
    d = tmp_path / "legacy"
    d.mkdir()
    for t, cols in SCHEMAS.items():
        write_xlsx(d / f"{t}.xlsx", t, cols, [])
    write_xlsx(d / "vendors.xlsx", "vendors", SCHEMAS["vendors"],
               [["VEN-1", "V", "", "", "", "", "Other", "", "7", "", "", True, "", ""]])
    write_xlsx(d / "bills.xlsx", "bills", SCHEMAS["bills"],
               [["B1", "VEN-1", "I1", "2026-03-01", "2026-03-31", "Other", "", "100", "0", "0", "100", "", "",
                 "Unpaid", "", ""]], money_columns=MONEY_COLUMNS["bills"])
    write_xlsx(d / "payments.xlsx", "payments", SCHEMAS["payments"],
               [["P1", "VEN-1", "2026-03-02", "100", "Cash", "", "", "Active", "", "", "", "2026-03-05T10:00:00"]],
               money_columns=["amount"])
    old_cols = ["allocation_id", "payment_id", "bill_id", "amount", "allocated_at", "reversal_of", "status"]
    write_xlsx(d / "payment_allocations.xlsx", "x", old_cols,
               [["A1", "P1", "B1", "100.00", "2026-03-02T10:00:00", "", "Reversed"]], money_columns=["amount"])
    res = DataService(svc).import_legacy_folder(d)
    a = db.read("payment_allocations")
    assert sorted(a["entry_type"]) == ["ALLOCATE", "REVERSE"]
    assert db.backups.exists(res["backup"])
    assert db.get_setting("schema_version") == "4"
    assert svc.bill_balances("10-03-2026")[0]["outstanding_balance"] == "100.00"


def test_demo_workbook_imports_cleanly(svc, db):
    ds = DataService(svc)
    preview = ds.preview_data_file(DEMO)
    assert preview["errors"] == [] and preview["tables"]["vendors"]["incoming"] == 2
    ds.apply_data_file(DEMO)
    assert len(db.read("vendors")) == 2 and svc.integrity_check() == []
    assert "import_file" in db.read("audit_log")["action"].tolist()


def test_data_file_with_broken_references_is_refused(svc, db, tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("table,bill_id,vendor_id,invoice_number,invoice_date,due_date,category,subtotal,invoice_total,status\n"
                 "bills,BIL-1,VEN-NOPE,X1,2026-01-01,2026-01-05,Other,10,10,Active\n", encoding="utf-8")
    preview = DataService(svc).preview_data_file(p)
    assert any("missing vendor" in e for e in preview["errors"])
    with pytest.raises(ValueError, match="refused"):
        DataService(svc).apply_data_file(p)
    assert db.read("bills").empty


def test_snapshot_round_trips_through_data_import(svc, db, vendor, tmp_path):
    svc.add_bill(vendor, "S1", "01-03-2026", "31-03-2026", "Other", "100.50")
    p = tmp_path / "snap.xlsx"
    p.write_bytes(db.export_database_snapshot())
    before = {t: db.read(t) for t in SCHEMAS if t not in ("audit_log", "settings")}
    DataService(svc).apply_data_file(p)
    for t, df in before.items():
        assert db.read(t).equals(df), t


def test_reset_keeps_settings_and_a_backup(svc, db, vendor):
    svc.update_settings({"cafe_name": "Sunrise Bakery"})
    backup = DataService(svc).reset_all()
    assert db.read("vendors").empty and db.backups.exists(backup)
    assert svc.reload_settings()["cafe_name"] == "Sunrise Bakery"


# ------------------------------------------------------------------ shared settings
def test_settings_are_shared_validated_and_audited(make_db):
    a_db, b_db = make_db(), make_db()
    a = AccountingService(a_db, actor="owner")
    b = AccountingService(b_db, actor="cashier")
    a.update_settings({"cafe_name": "Sunrise Bakery", "tips_excluded_from_revenue": False})
    assert b.reload_settings()["cafe_name"] == "Sunrise Bakery"
    assert b.config["tips_excluded_from_revenue"] is False
    with pytest.raises(ValueError):
        a.update_settings({"timezone": "Mars/Base"})
    with pytest.raises(ValueError):
        a.update_settings({"unknown": 1})
    row = a_db.read("audit_log").iloc[-1]
    assert row["entity_type"] == "settings" and row["actor"] == "owner" and "cafe_name" in row["details"]


def test_today_follows_the_business_timezone(svc):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from cafebooks.utils.date_utils import today_iso, set_timezone
    try:
        set_timezone("Pacific/Kiritimati")       # UTC+14
        assert today_iso() == datetime.now(timezone.utc).astimezone(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d")
        set_timezone("Pacific/Pago_Pago")        # UTC-11
        assert today_iso() == datetime.now(timezone.utc).astimezone(ZoneInfo("Pacific/Pago_Pago")).strftime("%Y-%m-%d")
    finally:
        set_timezone("Asia/Kolkata")


# ------------------------------------------------------------------ export / edit / import workflow
def _edit(path, fn):
    df = pd.read_excel(path, dtype=object).fillna("")
    df = fn(df)
    cols = list(df.columns)
    money = [c for t, m in MONEY_COLUMNS.items() for c in m]
    write_xlsx(path, path.stem, cols, df.itertuples(index=False, name=None), money_columns=money)


def _set(col, values):
    def fn(df):
        df[col] = values
        return df
    return fn


def test_export_edit_import_applies_validated_changes(svc, db, vendor, tmp_path):
    sync = SyncManager(db, tmp_path / "staging")
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "vendors.xlsx", _set("name", ["Renamed Co"]))
    plan = sync.preview_import(ex["folder"])
    assert not plan.errors and plan.counts["vendors"]["modified"] == 1
    assert db.read("vendors").iloc[0]["name"] == "Fresh Milk Co"           # preview changes nothing
    res = sync.apply_import(ex["folder"])
    assert res["applied"] and db.read("vendors").iloc[0]["name"] == "Renamed Co"
    assert any(b["label"] == "pre_import" for b in db.backups.list())
    again = sync.apply_import(ex["folder"])                                   # replay is a no-op
    assert not again["applied"]
    assert db.read("audit_log")["audit_id"].is_unique


def test_edit_pack_zip_round_trip(svc, db, vendor, tmp_path):
    sync = SyncManager(db)
    data, filename = sync.export_pack_zip()
    assert filename.endswith(".zip")
    folder = tmp_path / "unzipped"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(folder)
    edit_dir = next(folder.glob("*/EDIT_THESE_FILES"))
    _edit(edit_dir / "vendors.xlsx", _set("phone", ["9876543210"]))
    rezipped = tmp_path / "edited.zip"
    with zipfile.ZipFile(rezipped, "w") as z:                 # user re-zips the folder in Explorer
        for p in folder.rglob("*"):
            z.write(p, p.relative_to(folder).as_posix())
    with sync.unpacked(rezipped) as tmp:
        assert not sync.preview_import(tmp).errors
        assert sync.apply_import(tmp)["applied"]
    assert db.read("vendors").iloc[0]["phone"] == "9876543210"


def test_unsafe_zip_is_rejected(db, tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../outside.txt", "x")
    with pytest.raises(ValueError, match="Unsafe"):
        with SyncManager.unpacked(p):
            pass


def test_import_conflict_requires_decision(svc, db, vendor, tmp_path):
    sync = SyncManager(db, tmp_path / "staging")
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "vendors.xlsx", _set("name", ["Edited Externally"]))
    svc.update_vendor(vendor, name="Edited In App")                            # live changes meanwhile
    plan = sync.preview_import(ex["folder"])
    assert len(plan.conflicts) == 1 and "vendors" in plan.stale_tables
    with pytest.raises(ValueError, match="explicit decision"):
        sync.apply_import(ex["folder"])
    assert db.read("vendors").iloc[0]["name"] == "Edited In App"
    sync.apply_import(ex["folder"], {("vendors", vendor): "live"})
    assert db.read("vendors").iloc[0]["name"] == "Edited In App"
    sync.apply_import(ex["folder"], {("vendors", vendor): "import"})
    assert db.read("vendors").iloc[0]["name"] == "Edited Externally"


def test_import_rejects_bad_edits(svc, db, vendor, tmp_path):
    b = svc.add_bill(vendor, "I1", "01-03-2026", "31-03-2026", "Other", 100)
    svc.record_payment(vendor, "02-03-2026", 50, "Cash", [(b, 50)])
    sync = SyncManager(db, tmp_path / "staging")
    # deleting rows
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "bills.xlsx", lambda df: df.iloc[0:0])
    assert any("deleted" in e for e in sync.preview_import(ex["folder"]).errors)
    # editing the append-only allocation ledger
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "payment_allocations.xlsx", _set("amount", ["10.00"]))
    assert any("append-only" in e for e in sync.preview_import(ex["folder"]).errors)
    # accounting invariant: total no longer equals components
    ex = sync.export_for_edit()
    _edit(ex["edit_dir"] / "bills.xlsx", _set("invoice_total", ["999.00"]))
    plan = sync.preview_import(ex["folder"])
    assert any("does not equal" in e for e in plan.errors)
    with pytest.raises(ValueError):
        sync.apply_import(ex["folder"])
    assert db.read("bills").iloc[0]["invoice_total"] == "100.00"
    # formulas are refused
    ex = sync.export_for_edit()
    from openpyxl import load_workbook
    p = ex["edit_dir"] / "vendors.xlsx"
    wb = load_workbook(p)
    wb.active["B2"] = "=1+1"
    wb.save(p)
    assert any("formulas" in e for e in sync.preview_import(ex["folder"]).errors)
    # not an export folder
    assert sync.preview_import(tmp_path / "nothing_here").errors


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


def test_web_actor_is_recorded(svc):
    from cafebooks.utils.actor import acting_as
    with acting_as("  Priya  (counter) "):
        svc.add_vendor("By Priya", "Other")
    assert svc.db.read("audit_log").iloc[-1]["actor"] == "Priya (counter)"


def test_performance_10k_bills(make_db):
    import time
    from cafebooks.database.ids import append_rows
    db = make_db()
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
    db2 = make_db()
    s2 = AccountingService(db2)
    t = time.time(); s2.bill_balances("2026-03-01"); load_and_calc = time.time() - t
    t = time.time(); s2.bill_balances("2026-03-02"); warm = time.time() - t
    t = time.time(); s2.add_bill(v, "NEW", "01-03-2026", "31-03-2026", "Other", 10); write = time.time() - t
    print(f"\nperf: cold load+balances {load_and_calc:.1f}s, warm balances {warm:.2f}s, add_bill write {write:.2f}s")
    assert warm < 3 and write < 5
