import sys, time, tempfile
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import demo_data
from pathlib import Path
from tkinter import messagebox
log=[]
messagebox.showerror=lambda *a,**k: log.append(("ERR",a[1]))
messagebox.showinfo=lambda *a,**k: log.append(("INFO",a[1]))
messagebox.askyesno=lambda *a,**k: True
from services.accounting_service import AccountingService
from services.reporting_service import ReportingService
from database.sync_manager import SyncManager
from utils.config import load_config
from ui import main_window as mw, reusable_widgets as rw
import customtkinter as ctk
tmp=Path(tempfile.mkdtemp()); db=demo_data.build(tmp/"d",tmp/"b"); cfg=load_config("/x.json"); acc=AccountingService(db,cfg)
app=mw.CafeBooksApp(cfg,db,acc,ReportingService(acc),SyncManager(db,tmp/"s"))
errs=[]; app.report_callback_exception=lambda *a: errs.append(a[1])
def pump(s=1.2):
    e=time.time()+s
    while time.time()<e: app.update(); time.sleep(0.02)
def top(): return [w for w in app.winfo_children() if isinstance(w, ctk.CTkToplevel)]
def fill(dlg, **kv):
    for k,v in kv.items():
        w=dlg.widgets[k]
        if isinstance(w, ctk.CTkTextbox): w.delete("1.0","end"); w.insert("1.0",v)
        elif isinstance(w, ctk.CTkComboBox): w.set(v)
        else: w.delete(0,"end"); w.insert(0,v)
# 1 add vendor
app.show_vendors(); pump(.3); app.add_vendor(); pump(.5); d=top()[-1]
fill(d, name="Smoke Vendor", category="Beverages", phone="9876500000"); d.save(); pump(1.5)
print("vendor added:", "Smoke Vendor" in db.read("vendors")["name"].tolist())
# 2 duplicate name -> confirm path (askyesno patched True) 
app.add_vendor(); pump(.5); d=top()[-1]; fill(d, name="smoke vendor", category="Beverages"); d.save(); pump(2.5)
print("duplicate confirmed & saved:", (db.read("vendors")["name"].str.lower()=="smoke vendor").sum()==2)
# 3 add bill
app.show_bills(); pump(.3); app.add_bill(); pump(.5); d=top()[-1]; vid=db.read("vendors").iloc[0]["vendor_id"]
fill(d, vendor=app.vendor_choices()[0], invoice_number="GUI-1", invoice_date="01-10-2026", due_date="20-10-2026", category="Beverages", subtotal="250.50", tax_amount="12.50"); d.save(); pump(1.5)
b=db.read("bills"); print("bill added:", "GUI-1" in b["invoice_number"].tolist(), b[b.invoice_number=="GUI-1"].iloc[0]["invoice_total"])
# 4 payment with allocation dialog
app.show_payments(); pump(.3); app.add_payment(); pump(.5); d=top()[-1]
fill(d, vendor=[c for c in app.vendor_choices(False) if "Fresh Milk" in c][0], payment_date="10-10-2026", amount="1000", method="Cash"); d.save(); pump(1.5)
al=[w for w in top() if isinstance(w, rw.AllocationDialog)]
print("allocation dialog shown:", len(al)==1)
al[0].auto(); al[0].finish(); pump(2.5)
print("payments:", [(r["payment_id"], r["allocated"], r["unallocated"]) for r in acc.payment_rows()])
# 5 sales form with duplicate warning
app.show_sales(); pump(.3); app.add_sales(); pump(.5); d=top()[-1]
fill(d, business_date="05-10-2026", gross_sales="1000", cash_collected="1000"); d.save(); pump(1.5)
print("sales rows:", len(db.read("customer_sales")))
print("errors:", errs, [l for l in log if l[0]=="ERR"])
app.destroy()
