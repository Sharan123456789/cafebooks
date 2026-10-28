"""CafeBooks web server: a JSON API under /api plus the single-page UI.

The same app serves every deployment:
  * Vercel / any host  - `app.py` at the project root exposes `app`; DATABASE_URL points at PostgreSQL.
  * Desktop app        - `desktop.py` runs it on 127.0.0.1 against a local SQLite file.
  * LAN / VPS          - `python -m cafebooks serve`.
Expensive objects are created on the first request, so importing this module needs no database.
"""
import io
import logging
import math
import os
import re
import sys
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from cafebooks import __version__
from cafebooks.database.backup_manager import BackupError
from cafebooks.database.errors import BusyError, StartupError, StorageError
from cafebooks.database.schemas import ADJUSTMENT_TYPES, PAYMENT_METHODS, TIP_TYPES
from cafebooks.database.sql_store import SqlStore
from cafebooks.database.sync_manager import SyncManager
from cafebooks.services.accounting_service import (AccountingService, AttachmentMissing, BILL_EDITABLE, DuplicateError,
                                                   DuplicateWarning, SALES_EDITABLE, VENDOR_EDITABLE)
from cafebooks.services.data_service import DataService
from cafebooks.services.reporting_service import PROFIT_NOTICE, REPORTS, ReportingService
from cafebooks.utils.actor import acting_as, clean_actor
from cafebooks.utils.config import ConfigError, DEFAULTS, ServerSettings
from cafebooks.utils.currency_utils import parse_stored, ZERO
from cafebooks.utils.date_utils import today_iso

log = logging.getLogger("cafebooks.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_WORKBOOK = Path(__file__).resolve().parents[2] / "demo_data" / "CafeBooks_Demo_Data.xlsx"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
OPENABLE = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tif", ".tiff", ".bmp", ".txt", ".csv",
            ".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods"}
SALES_ARGS = {"cash_collected": "cash", "upi_collected": "upi", "card_collected": "card",
              "other_collected": "other", "tips_collected": "tips",
              "service_charges_collected": "service_charges", "taxes_collected": "taxes"}


class RequestRefused(Exception):
    """A request that did not come from the CafeBooks user interface (HTTP 403)."""


# ---------------------------------------------------------------------- runtime (lazy)
class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.db = SqlStore(settings.database_url, backup_dir=settings.backup_dir, backup_store=settings.backup_store)
        self.config = dict(DEFAULTS)
        self.accounting = AccountingService(self.db, self.config,
                                            actor="desktop user" if settings.mode == "desktop" else "web user",
                                            local_files=settings.mode == "desktop",
                                            attachment_dir=settings.data_dir / "attachments")
        self.accounting.reload_settings()
        self.reports = ReportingService(self.accounting)
        self.sync = SyncManager(self.db)
        self.data = DataService(self.accounting)


_runtime = None
_runtime_lock = threading.Lock()


def get_runtime():
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = Runtime(ServerSettings.from_env())
                log.info("CafeBooks %s ready (%s)", __version__, _runtime.db.describe())
    return _runtime


def reset_runtime():
    """Forget the runtime (tests, desktop shutdown)."""
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.db.close()
        _runtime = None


# ---------------------------------------------------------------------- helpers
def jsonable(obj):
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        return jsonable(obj.item())             # numpy scalars
    return obj


def ok(data=None, **extra):
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return JSONResponse(jsonable(body))


def frame_payload(df):
    cols = list(df.columns)
    rows = df.to_dict("records") if not df.empty else []
    money = [c for c in cols if rows and any(isinstance(r[c], Decimal) for r in rows)]
    dates = [c for c in cols if c.endswith("date") or c == "date"]
    return {"columns": cols, "rows": [[r[c] for c in cols] for r in rows], "money_columns": money,
            "date_columns": dates}


def download(data, filename, media_type):
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                             "Cache-Control": "no-store"})


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _flag(payload, key):
    return payload.get(key) in (True, "true", "1", 1, "yes", "on")


def _allocations(raw):
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            bill_id, amount = item.get("bill_id"), item.get("amount")
        else:
            bill_id, amount = item
        if str(amount or "").strip() not in ("", "0", "0.0", "0.00"):
            out.append((str(bill_id), amount))
    return out


@contextmanager
def uploaded(file: UploadFile):
    suffix = Path(file.filename or "upload").suffix.lower()[:10]
    fd, tmp = tempfile.mkstemp(prefix="cafebooks_upload_", suffix=suffix)
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError("The uploaded file is too large.")
                out.write(chunk)
        yield Path(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------- app
def create_app():
    app = FastAPI(title="CafeBooks", version=__version__, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")

    # ---------------------------------------------------------------- errors
    def err(status, message, **extra):
        return JSONResponse({"ok": False, "error": str(message), **extra}, status_code=status)

    @app.exception_handler(RequestRefused)
    async def _refused(_req, exc):
        return err(403, exc)

    @app.exception_handler(DuplicateWarning)
    async def _dup_warn(_req, exc):
        return err(409, exc, confirm="allow_duplicate")

    @app.exception_handler(AttachmentMissing)
    async def _att_missing(_req, exc):
        return err(409, exc, confirm="allow_missing_attachment")

    @app.exception_handler(DuplicateError)
    async def _dup(_req, exc):
        return err(409, exc)

    @app.exception_handler(ConfigError)
    async def _config(_req, exc):
        return err(503, exc, setup=True)

    @app.exception_handler(StartupError)
    async def _startup(_req, exc):
        return err(503, exc, setup=True)

    @app.exception_handler(BusyError)
    async def _busy(_req, exc):
        return err(503, exc)

    @app.exception_handler(StorageError)
    async def _storage(_req, exc):
        return err(500, exc)

    @app.exception_handler(BackupError)
    async def _backup(_req, exc):
        return err(400, exc)

    @app.exception_handler(ValueError)
    async def _value(_req, exc):
        return err(400, exc)

    @app.exception_handler(Exception)
    async def _unexpected(_req, exc):
        log.exception("Unexpected error")
        return err(500, "Unexpected server error. If you were saving, nothing was changed; please try again.")

    def run(request, fn):
        """Per request: shared settings refresh, actor for the audit log, one version check."""
        rt = get_runtime()
        actor = clean_actor(unquote(request.headers.get("x-cafebooks-user", "")),
                            fallback=rt.accounting.actor)
        guard(request, rt)
        with rt.db.request_scope(), acting_as(actor):
            rt.accounting.reload_settings()
            return fn(rt)

    def guard(request, rt):
        """Refuse requests a third-party web page could forge.  Saving requests must carry a custom
        header (cross-site pages cannot add one without CORS approval, which is never given), and
        the desktop app only answers to its own local address (blocks DNS rebinding)."""
        if rt.settings.mode == "desktop":
            host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
            if host not in LOCAL_HOSTS:
                raise RequestRefused("The desktop app only accepts requests from this computer.")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "CafeBooks":
            raise RequestRefused("This request did not come from the CafeBooks app, so it was refused.")

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health():
        try:
            rt = get_runtime()
            return {"ok": True, "app": "CafeBooks", "version": __version__, "database": rt.db.describe()["engine"],
                    "mode": rt.settings.mode}
        except (ConfigError, StartupError) as exc:
            return JSONResponse({"ok": False, "app": "CafeBooks", "error": str(exc), "setup": True}, status_code=503)

    @app.get("/api/version")
    def version(request: Request):
        return run(request, lambda rt: ok({"version": rt.db.version, "today": today_iso()}))

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        def go(rt):
            counts = rt.db.row_counts()
            return ok({
                "app_version": __version__, "mode": rt.settings.mode, "database": rt.db.describe(),
                "config": rt.config, "today": today_iso(), "version": rt.db.version,
                "payment_methods": PAYMENT_METHODS, "adjustment_types": ADJUSTMENT_TYPES, "tip_types": TIP_TYPES,
                "reports": [{"name": n, "kind": kind, "chart": {"label": label, "values": values}}
                            for n, (_, kind, (label, values)) in REPORTS.items()],
                "profit_notice": PROFIT_NOTICE,
                "counts": {"sales": counts["customer_sales"], "bills": counts["bills"],
                           "payments": counts["payments"], "vendors": counts["vendors"]},
                "on_vercel": rt.settings.on_vercel,
            })
        return run(request, go)

    # ---------------------------------------------------------------- vendors
    @app.get("/api/vendors")
    def vendors(request: Request):
        def go(rt):
            owed = {}
            for b in rt.accounting.bill_balances():
                owed[str(b["vendor_id"])] = owed.get(str(b["vendor_id"]), ZERO) + parse_stored(b["outstanding_balance"])
            rows = rt.db.read("vendors").to_dict("records")
            for r in rows:
                r["outstanding"] = owed.get(str(r["vendor_id"]), ZERO)
            return ok(rows)
        return run(request, go)

    @app.post("/api/vendors")
    def add_vendor(request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        fields = {k: p[k] for k in VENDOR_EDITABLE - {"name", "category"} if k in p}
        return run(request, lambda rt: ok({"vendor_id": rt.accounting.add_vendor(
            p.get("name"), p.get("category"), request_id=str(p.get("request_id") or ""),
            allow_duplicate=_flag(p, "allow_duplicate"), **fields)}))

    @app.patch("/api/vendors/{vendor_id}")
    def update_vendor(vendor_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        fields = {k: p[k] for k in VENDOR_EDITABLE if k in p}
        return run(request, lambda rt: ok(rt.accounting.update_vendor(vendor_id, **fields)))

    @app.post("/api/vendors/{vendor_id}/deactivate")
    def deactivate_vendor(vendor_id: str, request: Request):
        return run(request, lambda rt: ok(rt.accounting.deactivate_vendor(vendor_id)))

    @app.post("/api/vendors/{vendor_id}/reactivate")
    def reactivate_vendor(vendor_id: str, request: Request):
        return run(request, lambda rt: ok(rt.accounting.reactivate_vendor(vendor_id)))

    @app.delete("/api/vendors/{vendor_id}")
    def delete_vendor(vendor_id: str, request: Request):
        return run(request, lambda rt: ok(rt.accounting.delete_vendor(vendor_id)))

    # ---------------------------------------------------------------- bills
    @app.get("/api/bills")
    def bills(request: Request, as_of: str = Query(default="")):
        return run(request, lambda rt: ok(rt.accounting.bill_balances(as_of or None)))

    @app.get("/api/bills/open")
    def open_bills(request: Request, vendor_id: str, as_of: str = Query(default="")):
        def go(rt):
            rows = [b for b in rt.accounting.bill_balances(as_of or None)
                    if str(b["vendor_id"]) == str(vendor_id) and b["calculated_status"] not in ("Paid", "Cancelled")
                    and parse_stored(b["outstanding_balance"]) > 0]
            rows.sort(key=lambda b: (b["due_date"], b["invoice_date"]))
            return ok(rows)
        return run(request, go)

    @app.post("/api/bills")
    def add_bill(request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok({"bill_id": rt.accounting.add_bill(
            p.get("vendor_id"), p.get("invoice_number"), p.get("invoice_date"), p.get("due_date"), p.get("category"),
            p.get("subtotal"), p.get("tax_amount", 0), p.get("other_charges", 0),
            description=p.get("description", ""), attachment_path=p.get("attachment_path", ""),
            notes=p.get("notes", ""), request_id=str(p.get("request_id") or ""),
            invoice_total=p.get("invoice_total") or None,
            allow_missing_attachment=_flag(p, "allow_missing_attachment"))}))

    @app.patch("/api/bills/{bill_id}")
    def update_bill(bill_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        fields = {k: p[k] for k in BILL_EDITABLE if k in p}
        return run(request, lambda rt: ok(rt.accounting.update_bill(
            bill_id, reason=p.get("reason", ""), allow_missing_attachment=_flag(p, "allow_missing_attachment"),
            **fields)))

    @app.post("/api/bills/{bill_id}/cancel")
    def cancel_bill(bill_id: str, request: Request, payload: dict = Body(default=None)):
        return run(request, lambda rt: ok(rt.accounting.cancel_bill(bill_id, (payload or {}).get("reason"))))

    @app.post("/api/bills/{bill_id}/open-attachment")
    def open_attachment(bill_id: str, request: Request):
        """Desktop app only: open a linked local file with the default program."""
        def go(rt):
            if rt.settings.mode != "desktop":
                raise ValueError("Files on your computer can only be opened from the desktop app.")
            bills_df = rt.db.read("bills")
            hit = bills_df[bills_df["bill_id"] == bill_id]
            path = str(hit["attachment_path"].iloc[0]) if not hit.empty else ""
            if not path or not os.path.isfile(path):
                raise ValueError(f"The linked file is not available: {path or '(none)'}")
            if Path(path).suffix.lower() not in OPENABLE:
                raise ValueError("Only documents and images (PDF, JPG, PNG, Word, Excel…) can be opened from CafeBooks.")
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
            return ok()
        return run(request, go)

    # ---------------------------------------------------------------- payments
    @app.get("/api/payments")
    def payments(request: Request):
        return run(request, lambda rt: ok(rt.accounting.payment_rows()))

    @app.post("/api/payments")
    def record_payment(request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok({"payment_id": rt.accounting.record_payment(
            p.get("vendor_id"), p.get("payment_date"), p.get("amount"), p.get("method"),
            _allocations(p.get("allocations")), p.get("reference", ""), p.get("notes", ""),
            request_id=str(p.get("request_id") or ""))}))

    @app.patch("/api/payments/{payment_id}")
    def update_payment(payment_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok(rt.accounting.update_payment_details(
            payment_id, reference=p.get("reference"), notes=p.get("notes"))))

    @app.post("/api/payments/{payment_id}/allocate")
    def allocate(payment_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok(rt.accounting.allocate_payment(
            payment_id, _allocations(p.get("allocations")), p.get("allocation_date") or None,
            request_id=str(p.get("request_id") or ""))))

    @app.post("/api/payments/{payment_id}/reverse")
    def reverse_payment(payment_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok(rt.accounting.reverse_payment(
            payment_id, p.get("reason"), p.get("reversal_date") or None)))

    @app.get("/api/payments/{payment_id}/allocations")
    def payment_allocations(payment_id: str, request: Request):
        def go(rt):
            alloc = rt.db.read("payment_allocations").to_dict("records")
            invoices = {b["bill_id"]: b["invoice_number"] for b in rt.db.read("bills").to_dict("records")}
            reversed_by = {}
            for a in alloc:
                if a["entry_type"] == "REVERSE":
                    reversed_by[a["reversal_of"]] = reversed_by.get(a["reversal_of"], ZERO) + parse_stored(a["amount"])
            out = []
            for a in alloc:
                if str(a["payment_id"]) != payment_id:
                    continue
                remaining = parse_stored(a["amount"]) - reversed_by.get(a["allocation_id"], ZERO) \
                    if a["entry_type"] == "ALLOCATE" else ZERO
                out.append({**a, "invoice_number": invoices.get(a["bill_id"], ""), "remaining": remaining})
            return ok(out)
        return run(request, go)

    @app.post("/api/allocations/{allocation_id}/reverse")
    def reverse_allocation(allocation_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok({"allocation_id": rt.accounting.reverse_allocation(
            allocation_id, p.get("reason"), p.get("amount") or None, p.get("reversal_date") or None)}))

    # ---------------------------------------------------------------- sales
    @app.get("/api/sales")
    def sales(request: Request):
        return run(request, lambda rt: ok(rt.db.read("customer_sales").to_dict("records")))

    @app.post("/api/sales")
    def add_sales(request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        kw = {SALES_ARGS.get(k, k): p[k] for k in ("discounts", "refunds", "credit_sales", "credit_collections",
                                                   *SALES_ARGS) if k in p}
        return run(request, lambda rt: ok({"sales_id": rt.accounting.add_sales(
            p.get("business_date"), p.get("gross_sales"), shift=p.get("shift", ""),
            expected_cash=p.get("expected_cash") or None, actual_cash=p.get("actual_cash") or None,
            notes=p.get("notes", ""), request_id=str(p.get("request_id") or ""),
            allow_duplicate=_flag(p, "allow_duplicate"), **kw)}))

    @app.patch("/api/sales/{sales_id}")
    def update_sales(sales_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        fields = {k: p[k] for k in SALES_EDITABLE if k in p}
        return run(request, lambda rt: ok(rt.accounting.update_sales(
            sales_id, reason=p.get("reason", ""), allow_duplicate=_flag(p, "allow_duplicate"), **fields)))

    @app.post("/api/sales/{sales_id}/void")
    def void_sales(sales_id: str, request: Request, payload: dict = Body(default=None)):
        return run(request, lambda rt: ok(rt.accounting.void_sales(sales_id, (payload or {}).get("reason"))))

    @app.post("/api/sales/{sales_id}/adjustments")
    def add_adjustment(sales_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok({"adjustment_id": rt.accounting.add_sales_adjustment(
            sales_id, p.get("adjustment_type"), p.get("amount"), p.get("adjustment_date") or None,
            p.get("notes", ""), request_id=str(p.get("request_id") or ""))}))

    @app.get("/api/adjustments")
    def adjustments(request: Request):
        return run(request, lambda rt: ok(rt.db.read("sales_adjustments").to_dict("records")))

    @app.post("/api/adjustments/{adjustment_id}/void")
    def void_adjustment(adjustment_id: str, request: Request, payload: dict = Body(default=None)):
        return run(request, lambda rt: ok(rt.accounting.void_sales_adjustment(
            adjustment_id, (payload or {}).get("reason"))))

    # ---------------------------------------------------------------- tips
    @app.get("/api/tips")
    def tips(request: Request):
        return run(request, lambda rt: ok(rt.db.read("tips_ledger").to_dict("records")))

    @app.post("/api/tips")
    def add_tip(request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok({"entry_id": rt.accounting.add_tip_entry(
            p.get("entry_type"), p.get("collected"), p.get("entry_date"), str(p.get("sales_id") or "").strip(),
            p.get("notes", ""), request_id=str(p.get("request_id") or ""))}))

    @app.patch("/api/tips/{entry_id}")
    def update_tip(entry_id: str, request: Request, payload: dict = Body(default=None)):
        p = payload or {}
        return run(request, lambda rt: ok(rt.accounting.update_tip(
            entry_id, p.get("distributed"), p.get("retained"), p.get("notes", ""), p.get("distribution_date", ""))))

    @app.post("/api/tips/{entry_id}/void")
    def void_tip(entry_id: str, request: Request, payload: dict = Body(default=None)):
        return run(request, lambda rt: ok(rt.accounting.void_tip_entry(entry_id, (payload or {}).get("reason"))))

    # ---------------------------------------------------------------- dashboard & reports
    @app.get("/api/dashboard")
    def dashboard(request: Request, start: str = "", end: str = ""):
        def go(rt):
            summary = rt.accounting.dashboard_summary(start=start or None, end=end or None)
            issues = rt.accounting.integrity_check()
            errors = sum(1 for i in issues if i.startswith("ERROR"))
            return ok({"summary": summary, "integrity": {"errors": errors, "warnings": len(issues) - errors}})
        return run(request, go)

    @app.get("/api/reports/run")
    def run_report(request: Request, name: str, start: str = "", end: str = "", category: str = "",
                   vendor_id: str = ""):
        return run(request, lambda rt: ok(frame_payload(rt.reports.run(name, start, end, category or None,
                                                                       vendor_id or None))))

    @app.get("/api/reports/export")
    def export_report(request: Request, name: str, start: str = "", end: str = "", category: str = "",
                      vendor_id: str = ""):
        def go(rt):
            df = rt.reports.run(name, start, end, category or None, vendor_id or None)
            buf = io.BytesIO()
            ReportingService.export(df, buf, name[:31])
            fname = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") + f"_{today_iso()}.xlsx"
            return download(buf.getvalue(), fname, XLSX)
        return run(request, go)

    # ---------------------------------------------------------------- settings, integrity, activity
    @app.get("/api/settings")
    def get_settings(request: Request):
        return run(request, lambda rt: ok(rt.config))

    @app.put("/api/settings")
    def put_settings(request: Request, payload: dict = Body(default=None)):
        return run(request, lambda rt: ok(rt.accounting.update_settings(payload or {})))

    @app.get("/api/integrity")
    def integrity(request: Request):
        return run(request, lambda rt: ok(rt.accounting.integrity_check()))

    @app.get("/api/activity")
    def activity(request: Request, limit: int = 300):
        return run(request, lambda rt: ok(rt.accounting.activity(min(max(limit, 1), 5000))))

    # ---------------------------------------------------------------- backups
    @app.get("/api/backups")
    def backups(request: Request):
        return run(request, lambda rt: ok({"items": rt.db.backups.list(), "store": rt.db.backups.kind,
                                           "retention": rt.db.backups.retention}))

    @app.post("/api/backups")
    def create_backup(request: Request):
        return run(request, lambda rt: ok({"name": rt.db.backup("manual")}))

    @app.get("/api/backups/{name}/download")
    def download_backup(name: str, request: Request):
        return run(request, lambda rt: download(rt.db.backups.read_bytes(name), name, "application/zip"))

    def _restore_info(info):
        errors = [i.message for i in info["issues"] if i.severity == "error"]
        return {"created_at": info["manifest"].get("created_at") or "", "legacy": bool(info["manifest"].get("legacy")),
                "tables": info["tables"], "errors": (info["load_issues"] + errors)[:30],
                "warnings": [i.message for i in info["issues"] if i.severity == "warning"][:30]}

    @app.post("/api/backups/{name}/preview")
    def preview_backup(name: str, request: Request):
        return run(request, lambda rt: ok(_restore_info(rt.db.restore_preview(name))))

    @app.post("/api/backups/{name}/restore")
    def restore_backup(name: str, request: Request):
        def go(rt):
            safety = rt.db.restore(name)
            rt.accounting.log_event("backup", name, "restore", f"Restored backup {name}",
                                    after={"safety_backup": safety})
            return ok({"safety_backup": safety})
        return run(request, go)

    @app.post("/api/restore-upload/preview")
    def preview_uploaded_backup(request: Request, file: UploadFile = File(...)):
        def go(rt):
            with uploaded(file) as path:
                return ok(_restore_info(rt.db.restore_preview(path)))
        return run(request, go)

    @app.post("/api/restore-upload/apply")
    def restore_uploaded_backup(request: Request, file: UploadFile = File(...)):
        def go(rt):
            with uploaded(file) as path:
                safety = rt.db.restore(path)
            rt.accounting.log_event("backup", (file.filename or "upload")[:120], "restore",
                                    "Restored an uploaded backup", after={"safety_backup": safety})
            return ok({"safety_backup": safety})
        return run(request, go)

    @app.get("/api/cron/daily-backup")
    def cron_daily_backup(request: Request):
        """Called once a day by Vercel Cron (see vercel.json)."""
        rt = get_runtime()
        secret = rt.settings.cron_secret
        if secret and request.headers.get("authorization", "") != f"Bearer {secret}":
            return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

        def go(rt):
            if not rt.config["auto_backup_daily"]:
                return ok({"skipped": "Automatic daily backup is turned off in Settings."})
            return ok({"backup": rt.db.maybe_daily_backup()})
        return run(request, go)

    # ---------------------------------------------------------------- data tools
    @app.get("/api/data/snapshot")
    def snapshot(request: Request):
        return run(request, lambda rt: download(rt.db.export_database_snapshot(),
                                                f"CafeBooks_Database_{today_iso()}.xlsx", XLSX))

    @app.get("/api/data/demo-workbook")
    def demo_workbook():
        path = DEMO_WORKBOOK
        if not path.exists():
            raise ValueError("The demo workbook is not included in this installation.")
        return download(path.read_bytes(), path.name, XLSX)

    @app.get("/api/data/edit-pack")
    def edit_pack(request: Request):
        def go(rt):
            data, filename = rt.sync.export_pack_zip()
            return download(data, filename, "application/zip")
        return run(request, go)

    def _plan_payload(plan):
        return {"export_id": plan.export_id, "exported_at": plan.exported_at, "errors": plan.errors[:40],
                "warnings": plan.warnings[:40], "stale_tables": plan.stale_tables, "counts": plan.counts,
                "has_changes": plan.has_changes, "summary": plan.describe(),
                "conflicts": [{"table": c["table"], "pk": c["pk"], "cols": c["cols"],
                               "live": {k: c["live"].get(k, "") for k in c["cols"]},
                               "imported": {k: c["imported"].get(k, "") for k in c["cols"]}}
                              for c in plan.conflicts[:200]]}

    @app.post("/api/data/edit-pack/preview")
    def preview_pack(request: Request, file: UploadFile = File(...)):
        def go(rt):
            with uploaded(file) as path, rt.sync.unpacked(path) as folder:
                return ok(_plan_payload(rt.sync.preview_import(folder)))
        return run(request, go)

    @app.post("/api/data/edit-pack/apply")
    def apply_pack(request: Request, file: UploadFile = File(...), resolutions: str = Form(default="[]")):
        import json
        try:
            chosen = {(r["table"], str(r["pk"])): r["choice"] for r in json.loads(resolutions or "[]")}
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid conflict decisions.") from None

        def go(rt):
            with uploaded(file) as path, rt.sync.unpacked(path) as folder:
                return ok(rt.sync.apply_import(folder, chosen))
        return run(request, go)

    def _file_info(info):
        return {"tables": info["tables"], "errors": info["errors"], "warnings": info["warnings"]}

    @app.post("/api/data/import-file/preview")
    def preview_file(request: Request, file: UploadFile = File(...)):
        def go(rt):
            with uploaded(file) as path:
                return ok(_file_info(rt.data.preview_data_file(path, file.filename)))
        return run(request, go)

    @app.post("/api/data/import-file/apply")
    def apply_file(request: Request, file: UploadFile = File(...)):
        def go(rt):
            with uploaded(file) as path:
                return ok(rt.data.apply_data_file(path, file.filename))
        return run(request, go)

    @app.post("/api/data/reset")
    def reset(request: Request, payload: dict = Body(default=None)):
        if (payload or {}).get("confirm") != "RESET":
            raise ValueError("Type RESET to confirm.")
        return run(request, lambda rt: ok({"backup": rt.data.reset_all()}))

    # ---------------------------------------------------------------- user interface
    # Declared last so every /api route wins.  On Vercel these files are served by the CDN.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
