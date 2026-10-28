import sys, time, tempfile
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import demo_data
from pathlib import Path
from database.excel_manager import ExcelManager
from database.sync_manager import SyncManager
from services.accounting_service import AccountingService
from services.reporting_service import ReportingService
from utils.config import load_config
from ui import main_window as mw

tmp = Path(tempfile.mkdtemp())
db = demo_data.build(tmp / "d", tmp / "b")
cfg = load_config("/nonexistent.json")
acc = AccountingService(db, cfg); rep = ReportingService(acc); sync = SyncManager(db, tmp / "s")
app = mw.CafeBooksApp(cfg, db, acc, rep, sync)
def pump(sec=1.5):
    end = time.time() + sec
    while time.time() < end:
        app.update(); time.sleep(0.02)
errors = []
from tkinter import messagebox
messagebox.showerror=lambda *a,**k: errors.append(('MSGBOX',a))
messagebox.showinfo=lambda *a,**k: errors.append(('INFO',a))
import tkinter
app.report_callback_exception = lambda *a: errors.append(a[1])
for name in ["show_dashboard", "show_vendors", "show_bills", "show_payments", "show_sales", "show_tips", "show_settings", "show_reports"]:
    getattr(app, name)(); pump(1.5)
    print("opened", name)
app.show_reports(); pump(0.5)
for rname in mw.REPORTS:
    app.rep_choice.set(rname); app.rep_start.delete(0, "end"); app.rep_end.delete(0, "end")
    app.rep_end.insert(0, "30-11-2026"); app.run_report(); pump(2.5)
    assert app.current_report and app.current_report[0] == rname, (rname, errors)
print("all", len(mw.REPORTS), "reports ran")
app.show_bills(); pump(0.5)
t = app.bill_tbl; print("bill rows shown:", len(t.visible)); t.tree.selection_set("0"); print("selected:", t.selected()["invoice_number"])
t.search.insert(0, "milk"); t.apply(); print("search ->", len(t.visible)); t.sort_by("invoice_total"); print("sorted ok")
app.show_payments(); pump(0.5); print("payments:", [(r["payment_id"], r["unallocated"]) for r in app.pay_tbl.visible])
print("callback errors:", errors)
app.destroy()
sys.exit(1 if errors else 0)
