import gc
import math
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tkinter import messagebox, filedialog, simpledialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from database.excel_manager import StorageError
from database.schemas import PAYMENT_METHODS, ADJUSTMENT_TYPES, MONEY_COLUMNS, DATE_COLUMNS, SCHEMAS
from database.sync_manager import SyncManager
from services.reporting_service import ReportingService, PROFIT_NOTICE
from ui.reusable_widgets import FormDialog, AllocationDialog, DataTable, run_bg
from utils.config import save_config, resolve_dir
from utils.currency_utils import format_money, parse_stored
from utils.date_utils import display_date, today_iso, to_date
from database.validation import is_active

REPORTS = {
    "Outstanding - bill wise": ("vendor_outstanding", "asof", ("vendor_name", ["outstanding_balance"])),
    "Outstanding - vendor wise": ("vendor_outstanding_summary", "asof", ("vendor_name", ["total_outstanding"])),
    "Aging - bill wise": ("vendor_aging", "asof", ("aging_bucket", ["outstanding_balance"])),
    "Aging - vendor summary": ("vendor_aging_summary", "asof", ("vendor_name", ["Current", "1-30 days", "31-60 days", "61-90 days", ">90 days"])),
    "Expenses by category": ("expense_breakdown", "range", ("category", ["invoice_total"])),
    "Expenses by vendor": ("expense_by_vendor", "range", ("vendor_name", ["invoice_total"])),
    "Expenses - daily": ("expense_daily", "range", ("period", ["invoice_total"])),
    "Expenses - monthly": ("expense_monthly", "range", ("period", ["invoice_total"])),
    "Expenses - quarterly": ("expense_quarterly", "range", ("period", ["invoice_total"])),
    "Expenses - annual": ("expense_annual", "range", ("period", ["invoice_total"])),
    "Cash flow - daily": ("cash_daily", "range", ("period", ["sales_receipts", "supplier_payments"])),
    "Cash flow - monthly": ("cash_monthly", "range", ("period", ["sales_receipts", "supplier_payments"])),
    "Cash flow - by method": ("cash_flow_by_method", "range", ("method", ["receipts", "supplier_payments"])),
    "Sales & collections": ("sales_collections", "range", ("business_date", ["net_sales", "receipts"])),
    "Sales reconciliation": ("reconciliation", "range", ("business_date", ["difference"])),
    "Tips & service charges": ("tips_report", "range", ("entry_type", ["collected", "distributed"])),
    "Tips summary": ("tips_summary", "range", ("entry_type", ["collected", "distributed", "retained_or_payable"])),
}


class CafeBooksApp(ctk.CTk):
    def __init__(self, config, db, accounting, reports, sync):
        super().__init__()
        self.cfg, self.db, self.accounting, self.reports, self.sync = config, db, accounting, reports, sync
        self.current_report = None
        # Warm, low-glare charcoal-and-gold visual system inspired by the supplied reference.
        ctk.set_appearance_mode("Dark")
        self.configure(fg_color="#171610")
        self.title(f"CafeBooks - {config.get('cafe_name', 'My Cafe')}")
        self.geometry("1360x860")
        self.minsize(1100, 700)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=190, corner_radius=0, fg_color="#12120F")
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)
        self.content = ctk.CTkFrame(self, fg_color="#171610", corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew", padx=(18, 18), pady=14)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(self.sidebar, text="◉  CafeBooks", font=ctk.CTkFont(size=21, weight="bold"),
                     text_color="#F4B323").pack(anchor="w", padx=16, pady=(25, 3))
        self.side_sub = ctk.CTkLabel(self.sidebar, text=config.get("cafe_name", ""), text_color="#A7A18F", wraplength=158,
                                     font=ctk.CTkFont(size=11))
        self.side_sub.pack(anchor="w", padx=18, pady=(0, 24))
        pages = [("▦   Dashboard", self.show_dashboard), ("＋   Quick Entry", self.show_quick_entry),
                 ("▤   Purchase Bills", self.show_bills), ("↗   Supplier Payments", self.show_payments),
                 ("◷   Daily Sales", self.show_sales), ("◉   Reports", self.show_reports),
                 ("♙   Vendors", self.show_vendors), ("▣   Tips & Charges", self.show_tips),
                 ("⚙   Settings", self.show_settings)]
        self.nav_buttons = []
        for i, (label, cmd) in enumerate(pages):
            b = ctk.CTkButton(self.sidebar, text=label, anchor="w", command=cmd, height=36,
                              corner_radius=7, fg_color="transparent", hover_color="#302C22",
                              text_color="#D7D1C2", font=ctk.CTkFont(size=12))
            b.pack(fill="x", padx=9, pady=3)
            self.nav_buttons.append((b, cmd))
            self.bind(f"<Control-Key-{i + 1}>", lambda e, c=cmd: c())
        ctk.CTkFrame(self.sidebar, height=1, fg_color="#393529").pack(fill="x", padx=13, pady=(16, 10))
        ctk.CTkButton(self.sidebar, text="?   Welcome & Tutorial", anchor="w", command=self.show_welcome,
                      height=34, corner_radius=7, fg_color="#242219", hover_color="#393529",
                      text_color="#E8C46C").pack(fill="x", padx=9, pady=4)
        self.status = ctk.CTkLabel(self, text="", anchor="w", text_color="#8D8879", fg_color="#12120F", font=ctk.CTkFont(size=10))
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=0)
        self.refresh_status()
        # Tk objects must never be finalised on worker threads: run the cyclic GC on the UI thread only.
        gc.disable()
        self._gc_tick()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_dashboard()
        if not self.cfg.get("welcome_seen", False):
            self.after(450, self.show_welcome)

    def _gc_tick(self):
        gc.collect()
        self.after(2000, self._gc_tick)

    # ------------------------------------------------------------------ helpers
    @property
    def symbol(self):
        return self.cfg.get("currency_symbol", "\u20b9")

    def money(self, v):
        return format_money(v, self.symbol, self.cfg.get("indian_number_grouping", True))

    def dstr(self, iso):
        return display_date(iso, self.cfg.get("date_display", "DD-MM-YYYY"))

    def today_in(self):
        return self.dstr(today_iso())

    def refresh_status(self, extra=""):
        saved = self.db.last_saved.strftime("%d-%m-%Y %H:%M:%S") if self.db.last_saved else "no changes this session"
        self.status.configure(text=f"Data folder: {self.db.data_dir}   |   Last successful save: {saved}   {extra}")

    def done(self, msg, refresh=None):
        self.refresh_status(f"|  {msg}")
        if refresh:
            refresh()

    def clear(self):
        for w in self.content.winfo_children():
            w.destroy()

    def header(self, title, subtitle=""):
        self.clear()
        ctk.CTkLabel(self.content, text=title, font=ctk.CTkFont(size=25, weight="bold"),
                     text_color="#F3F0E7").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))
        ctk.CTkLabel(self.content, text=subtitle, text_color="#A7A18F", anchor="w",
                     wraplength=1050, justify="left").grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 7))

    def bar(self, buttons):
        # A balanced two-row action grid prevents Settings controls from colliding or
        # pushing the final buttons (especially Excel Snapshot) off the window edge.
        b = ctk.CTkFrame(self.content, fg_color="transparent")
        b.grid(row=2, column=0, sticky="ew", padx=8, pady=(5, 9))
        columns = 5
        for col in range(columns):
            b.grid_columnconfigure(col, weight=1, uniform="action_button")
        for i, (label, cmd) in enumerate(buttons):
            row, col = divmod(i, columns)
            button = ctk.CTkButton(
                b, text=label, command=cmd, height=34, corner_radius=8,
                fg_color="#302C22", hover_color="#48402B", text_color="#E8C46C",
                font=ctk.CTkFont(size=11),
            )
            button.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
        return b

    def table(self, columns, rows, **kw):
        t = DataTable(self.content, columns, rows, symbol=self.symbol, indian=self.cfg.get("indian_number_grouping", True),
                      style=self.cfg.get("date_display", "DD-MM-YYYY"), **kw)
        t.grid(row=3, column=0, sticky="nsew", padx=7, pady=7)
        return t

    def need(self, table):
        row = table.selected() if table else None
        if not row:
            messagebox.showinfo("Select a record", "Select a row first.", parent=self)
        return row

    def bg(self, fn, ok, title="Working"):
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("360x110")
        win.transient(self)
        ctk.CTkLabel(win, text=f"{title}... please wait").pack(pady=(20, 8))
        bar = ctk.CTkProgressBar(win, mode="indeterminate")
        bar.pack(padx=30, fill="x")
        bar.start()
        win.after(150, win.grab_set)

        def finished(result, err):
            win.destroy()
            if err:
                messagebox.showerror(title + " failed", str(err), parent=self)
            else:
                ok(result)
        run_bg(self, fn, finished)

    def open_path(self, path):
        path = str(path)
        if not os.path.exists(path):
            messagebox.showwarning("File not found", f"Could not find:\n{path}", parent=self)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Open failed", str(exc), parent=self)

    def vendor_choices(self, active_only=True):
        return [f"{vid} | {name}" for vid, name in self.accounting.vendor_options(active_only)]

    @staticmethod
    def vid_of(choice):
        return choice.split(" | ", 1)[0]

    # ------------------------------------------------------------------ welcome and guided tutorial
    def show_welcome(self):
        """A friendly welcome panel that launches a short, step-by-step in-app tour."""
        win = ctk.CTkToplevel(self)
        win.title("Welcome to CafeBooks")
        win.geometry("650x470")
        win.minsize(560, 420)
        win.transient(self)
        win.grab_set()
        outer = ctk.CTkFrame(win, corner_radius=18)
        outer.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(outer, text="☕  Welcome to CafeBooks", font=ctk.CTkFont(size=28, weight="bold")).pack(anchor="w", padx=24, pady=(24, 8))
        ctk.CTkLabel(outer, text=f"Your cafe's everyday records, brought together.\n{self.cfg.get('cafe_name', 'My Cafe')}",
                     font=ctk.CTkFont(size=16), justify="left", text_color=("#43536A", "#C8D4E5")).pack(anchor="w", padx=24, pady=(0, 16))
        ctk.CTkLabel(outer, text="Use CafeBooks to record daily sales, manage vendors and purchase bills, track supplier payments, and review reports. Your records are stored in Excel workbooks in the configured data folder.",
                     wraplength=560, justify="left", anchor="w").pack(fill="x", padx=24, pady=5)
        ctk.CTkLabel(outer, text="A quick note: cash movement is not the same as profit. CafeBooks does not calculate profit because inventory cost, payroll and other costs are not fully tracked.",
                     wraplength=560, justify="left", anchor="w", text_color=("#8A4B10", "#F0BD76")).pack(fill="x", padx=24, pady=(10, 14))
        remember = ctk.BooleanVar(value=bool(self.cfg.get("welcome_seen", False)))
        ctk.CTkCheckBox(outer, text="Don't show this welcome note automatically again", variable=remember).pack(anchor="w", padx=24, pady=(2, 14))
        actions = ctk.CTkFrame(outer, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 20))
        def close_welcome():
            if remember.get() and not self.cfg.get("welcome_seen", False):
                self.cfg["welcome_seen"] = True
                try:
                    self.cfg.update(save_config(self.cfg))
                except Exception as exc:  # noqa: BLE001
                    messagebox.showwarning("Preferences", f"Could not save this preference: {exc}", parent=win)
            win.destroy()
        ctk.CTkButton(actions, text="Start guided tour", command=lambda: (close_welcome(), self.after(100, self.show_tutorial))).pack(side="left", padx=4)
        ctk.CTkButton(actions, text="Maybe later", fg_color="gray", command=close_welcome).pack(side="right", padx=4)

    def show_tutorial(self):
        """Game-like guided tour with one focused step at a time and visible progress."""
        steps = [
            ("01  |  Start here: Quick Entry", "Think of Quick Entry as your mission hub. Choose Daily Sales, Purchase Bill, Supplier Payment, New Vendor, or Tips / Service Charge to open the matching form. Each form validates the entry before saving.", "Open Quick Entry", self.show_quick_entry),
            ("02  |  Set up your vendors", "Add a vendor before recording their bills or payments. Keep names and invoice numbers consistent so duplicate invoices can be detected. Vendors with transaction history are deactivated rather than deleted.", "Open Vendors", self.show_vendors),
            ("03  |  Record the day's sales", "Enter the business date, sales totals, payment methods, discounts and cash count in Daily Sales. Check the reconciliation values before moving on; differences are useful signals to investigate.", "Open Daily Sales", self.show_sales),
            ("04  |  Bills first, payments second", "A purchase bill records the expense. Supplier payments record cash leaving the cafe and can be allocated to bills. An unallocated payment does not settle a bill; allocate it later from Supplier Payments.", "Open Purchase Bills", self.show_bills),
            ("05  |  Read the dashboard", "Use the date range to focus the summary. Large figures highlight sales, outstanding supplier balances and cash movement. Review reconciliation differences and integrity status when something looks unusual. Cash movement is not profit.", "Open Dashboard", self.show_dashboard),
            ("06  |  Reports, backups and safe recovery", "Reports help review a period. In Data Management and Settings, run backups regularly and use the guided export/edit/import workflow for spreadsheet changes. Avoid editing live workbook files directly, and close Excel before saving from CafeBooks.", "Open Reports", self.show_reports),
        ]
        win = ctk.CTkToplevel(self)
        win.title("CafeBooks | Guided Tutorial")
        win.geometry("700x450")
        win.minsize(600, 400)
        win.transient(self)
        win.grab_set()
        current = {"index": 0}
        outer = ctk.CTkFrame(win, corner_radius=18)
        outer.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(outer, text="YOUR FIRST SHIFT  /  GUIDED TOUR", font=ctk.CTkFont(size=12, weight="bold"), text_color=("#2463A6", "#80B9F0")).pack(anchor="w", padx=24, pady=(22, 8))
        title = ctk.CTkLabel(outer, text="", font=ctk.CTkFont(size=24, weight="bold"), anchor="w", justify="left", wraplength=590)
        title.pack(fill="x", padx=24, pady=(2, 12))
        progress_text = ctk.CTkLabel(outer, text="", text_color="gray")
        progress_text.pack(anchor="w", padx=24)
        progress = ctk.CTkProgressBar(outer, height=8)
        progress.pack(fill="x", padx=24, pady=(5, 18))
        body = ctk.CTkLabel(outer, text="", anchor="nw", justify="left", wraplength=590, font=ctk.CTkFont(size=15))
        body.pack(fill="both", expand=True, padx=24, pady=4)
        footer = ctk.CTkFrame(outer, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(16, 20))
        back = ctk.CTkButton(footer, text="← Back", width=95, fg_color="gray")
        back.pack(side="left", padx=4)
        action = ctk.CTkButton(footer, text="Go to this section", width=160)
        action.pack(side="left", padx=4)
        nxt = ctk.CTkButton(footer, text="Next →", width=100)
        nxt.pack(side="right", padx=4)
        def render():
            i = current["index"]
            step = steps[i]
            title.configure(text=step[0])
            body.configure(text=step[1])
            progress_text.configure(text=f"STEP {i + 1} OF {len(steps)}")
            progress.set((i + 1) / len(steps))
            back.configure(state="normal" if i else "disabled")
            nxt.configure(text="Finish ✓" if i == len(steps) - 1 else "Next →")
            action.configure(text=step[2])
        def go_back():
            current["index"] = max(0, current["index"] - 1)
            render()
        def go_next():
            if current["index"] >= len(steps) - 1:
                win.destroy()
                return
            current["index"] += 1
            render()
        def go_section():
            fn = steps[current["index"]][3]
            win.destroy()
            self.after(100, fn)
        back.configure(command=go_back)
        nxt.configure(command=go_next)
        action.configure(command=go_section)
        render()

    # ------------------------------------------------------------------ quick entry
    def show_quick_entry(self):
        """Central launchpad for common entries; each action uses the validated ledger workflow."""
        self.header("Quick Entry", "Enter a cafe transaction from one place. Choose the matching entry type below; saved records flow into the existing tables, dashboard and reports automatically.")
        body = ctk.CTkScrollableFrame(self.content)
        body.grid(row=3, column=0, sticky="nsew", padx=8, pady=8)
        body.grid_columnconfigure((0, 1), weight=1)

        choices = [
            ("Daily Sales", "Record one day's sales, payment methods, discounts, tips and cash count.", self.add_sales),
            ("Purchase Bill", "Enter a supplier invoice; expense and outstanding balance are calculated from the bill and payment ledger.", self.add_bill),
            ("Supplier Payment", "Record a supplier payment and allocate it to open bills where applicable.", self.add_payment),
            ("New Vendor", "Add a supplier once so it can be selected for bills and payments.", self.add_vendor),
            ("Tips / Service Charge", "Record a manual tip or service-charge ledger entry.", self.add_tip),
        ]
        for i, (title, description, action) in enumerate(choices):
            card = ctk.CTkFrame(body, corner_radius=12)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=8, pady=8)
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=14, pady=(14, 4))
            ctk.CTkLabel(card, text=description, wraplength=430, justify="left", anchor="w", text_color="gray").pack(fill="x", padx=14, pady=(0, 12))
            ctk.CTkButton(card, text=f"Enter {title}", command=action).pack(anchor="w", padx=14, pady=(0, 14))

        summary = ctk.CTkFrame(body)
        summary.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 8))
        ctk.CTkLabel(summary, text="Current records", font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=14, pady=(12, 6))
        try:
            stats = [
                ("Sales entries", len(self.db.read("customer_sales"))),
                ("Purchase bills", len(self.db.read("bills"))),
                ("Supplier payments", len(self.db.read("payments"))),
                ("Vendors", len(self.db.read("vendors"))),
            ]
            row = ctk.CTkFrame(summary, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=(0, 12))
            for label, count in stats:
                item = ctk.CTkFrame(row)
                item.pack(side="left", expand=True, fill="x", padx=5)
                ctk.CTkLabel(item, text=str(count), font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(8, 0))
                ctk.CTkLabel(item, text=label, text_color="gray").pack(pady=(0, 8))
        except Exception as exc:
            ctk.CTkLabel(summary, text=f"Record counts are temporarily unavailable: {exc}", text_color="gray").pack(anchor="w", padx=14, pady=10)

        note = ("Automatic flow: saved entries update their relevant records and are reflected in Dashboard and Reports. "
                "Supplier payments remain cash movements, while the purchase bill is the expense. Use the dedicated tabs for editing, reviewing, allocations and corrections.")
        ctk.CTkLabel(body, text=note, wraplength=950, justify="left", text_color="gray").grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=12)

    def _open_dashboard_calendar_for_values(self, start="", end=""):
        # Date selection lives in this dialog; no duplicate date boxes remain on the dashboard.
        win = ctk.CTkToplevel(self)
        win.title("Select date range")
        win.geometry("380x250")
        win.configure(fg_color="#171610")
        win.transient(self)
        ctk.CTkLabel(win, text="SELECT DATE RANGE", font=ctk.CTkFont(size=17, weight="bold"),
                     text_color="#F4B323").pack(anchor="w", padx=18, pady=(18, 8))
        row = ctk.CTkFrame(win, fg_color="transparent"); row.pack(fill="x", padx=14, pady=6)
        a = ctk.CTkEntry(row, placeholder_text="Start date · DD-MM-YYYY", width=165)
        b = ctk.CTkEntry(row, placeholder_text="End date · DD-MM-YYYY", width=165)
        a.pack(side="left", padx=4); b.pack(side="left", padx=4)
        for entry, value in ((a, start), (b, end)):
            if value: entry.insert(0, value)
        ctk.CTkLabel(win, text="Choose the start and end dates, then apply.",
                     text_color="#A7A18F").pack(anchor="w", padx=18, pady=5)
        def apply():
            if not a.get().strip() or not b.get().strip():
                return messagebox.showwarning("Dates required", "Enter both dates to apply a custom range.", parent=win)
            try:
                self.show_dashboard(a.get().strip(), b.get().strip())
                win.destroy()
            except Exception as exc:
                messagebox.showerror("Invalid date range", str(exc), parent=win)
        ctk.CTkButton(win, text="Apply range", command=apply, fg_color="#F4B323",
                      hover_color="#D99A19", text_color="#171610").pack(anchor="e", padx=18, pady=12)

    def _open_dashboard_calendar(self, start_entry, end_entry):
        win = ctk.CTkToplevel(self)
        win.title("Select date range")
        win.geometry("360x230")
        win.configure(fg_color="#171610")
        win.transient(self)
        ctk.CTkLabel(win, text="Choose your reporting period", font=ctk.CTkFont(size=18, weight="bold"), text_color="#F4B323").pack(anchor="w", padx=18, pady=(18, 8))
        row = ctk.CTkFrame(win, fg_color="transparent"); row.pack(fill="x", padx=16, pady=6)
        a = ctk.CTkEntry(row, placeholder_text="Start · DD-MM-YYYY", width=150); a.pack(side="left", padx=4)
        b = ctk.CTkEntry(row, placeholder_text="End · DD-MM-YYYY", width=150); b.pack(side="left", padx=4)
        for entry, old in ((a, start_entry.get()), (b, end_entry.get())):
            if old: entry.insert(0, old)
        ctk.CTkLabel(win, text="Enter dates in your configured date format.", text_color="#A7A18F").pack(anchor="w", padx=20, pady=4)
        def apply():
            start_entry.delete(0, "end"); start_entry.insert(0, a.get().strip())
            end_entry.delete(0, "end"); end_entry.insert(0, b.get().strip())
            win.destroy()
            self.show_dashboard(a.get().strip(), b.get().strip())
        ctk.CTkButton(win, text="Apply date range", command=apply, fg_color="#F4B323", hover_color="#D99A19", text_color="#171610").pack(anchor="e", padx=18, pady=12)

    # ------------------------------------------------------------------ dashboard
    def show_dashboard(self, start="", end=""):
        self.header("Dashboard", PROFIT_NOTICE)
        # Keep the dashboard's filter controls visually quiet so the data is the focus.
        bar = ctk.CTkFrame(self.content, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 6))
        ctk.CTkButton(
            bar, text="▦  Select range", width=132,
            command=lambda: self._open_dashboard_calendar_for_values(start, end),
            fg_color="#302C22", hover_color="#48402B", text_color="#E8C46C"
        ).pack(side="left", padx=5)
        quick = ctk.CTkFrame(bar, fg_color="transparent")
        quick.pack(side="left", padx=8)
        for label, days in (("Yesterday", 1), ("Last week", 7), ("Last month", 30), ("Last year", 365), ("All time", None)):
            def choose_range(n=days):
                if n is None:
                    self.show_dashboard()
                else:
                    end_date = datetime.now().date() - timedelta(days=1) if n == 1 else datetime.now().date()
                    start_date = end_date - timedelta(days=n-1)
                    self.show_dashboard(self.dstr(start_date.strftime("%Y-%m-%d")), self.dstr(end_date.strftime("%Y-%m-%d")))
            ctk.CTkButton(quick, text=label, width=86, height=30, fg_color="#302C22", hover_color="#48402B", text_color="#E8C46C", command=choose_range).pack(side="left", padx=3)

        body = ctk.CTkScrollableFrame(self.content, fg_color="#171610", corner_radius=14)
        body.grid(row=3, column=0, sticky="nsew", padx=6, pady=(3, 6))
        for i in range(4):
            body.grid_columnconfigure(i, weight=1, uniform="dash")

        def calc():
            return self.accounting.dashboard_summary(start=start or None, end=end or None), self.accounting.integrity_check()

        def show(result):
            if not body.winfo_exists():
                return
            d, issues = result
            by = d["collections_by_method"]
            accent_teal = "#F4B323"
            accent_blue = "#D99A19"
            accent_purple = "#C8BFA8"
            accent_amber = "#F2D06B"
            muted = "#A7A18F"

            # A small welcome strip gives the dashboard a polished analytics-app feel.
            welcome = ctk.CTkFrame(body, corner_radius=14, fg_color="#302C22", border_width=1,
                                   border_color="#48402B")
            welcome.grid(row=0, column=0, columnspan=4, sticky="ew", padx=7, pady=(7, 10))
            ctk.CTkLabel(welcome, text="Your café at a glance", font=ctk.CTkFont(size=19, weight="bold"),
                         text_color="#F4B323").pack(anchor="w", padx=17, pady=(12, 1))
            period = "All recorded dates" if not start and not end else f"Period: {start or 'beginning'}  →  {end or 'today'}"
            ctk.CTkLabel(welcome, text=f"{period}  ·  Key figures and collection patterns", text_color="#C4BCA8").pack(anchor="w", padx=17, pady=(0, 12))

            # Four primary KPI cards; secondary figures are grouped below to reduce clutter.
            primary = [("NET SALES", d["net_sales"], accent_teal, "Sales after adjustments"),
                       ("GROSS SALES", d["gross_sales"], accent_blue, "Before adjustments"),
                       ("CASH MOVEMENT", d["net_cash_movement"], accent_purple, "Not the same as profit"),
                       ("SUPPLIER OUTSTANDING", d["supplier_outstanding"], accent_amber, "Still payable to suppliers")]
            for i, (label, val, color, sub) in enumerate(primary):
                card = ctk.CTkFrame(body, corner_radius=13, border_width=1,
                                    border_color="#393529", fg_color="#242219")
                card.grid(row=1, column=i, sticky="nsew", padx=7, pady=5)
                ctk.CTkFrame(card, height=4, corner_radius=2, fg_color=color).pack(fill="x", padx=12, pady=(11, 8))
                ctk.CTkLabel(card, text=label, font=ctk.CTkFont(size=10, weight="bold"), text_color=muted).pack(anchor="w", padx=13, pady=(2, 3))
                ctk.CTkLabel(card, text=self.money(val), font=ctk.CTkFont(size=20, weight="bold"), text_color=color,
                             wraplength=245).pack(anchor="w", padx=13, pady=(0, 2))
                ctk.CTkLabel(card, text=sub, font=ctk.CTkFont(size=10), text_color=muted).pack(anchor="w", padx=13, pady=(0, 12))

            # Main chart panel: high-resolution anti-aliased donut with strongly contrasting colors.
            chart_row = 2
            panel = ctk.CTkFrame(body, corner_radius=14, border_width=1, border_color="#393529",
                                 fg_color="#242219")
            panel.grid(row=chart_row, column=0, columnspan=2, sticky="nsew", padx=7, pady=(12, 7))
            ctk.CTkLabel(panel, text="Collection mix", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=16, pady=(14, 1))
            ctk.CTkLabel(panel, text="How customers paid in this period", text_color=muted).pack(anchor="w", padx=16, pady=(0, 4))
            inner = ctk.CTkFrame(panel, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=10, pady=(0, 10))

            # Render at 3x size then let CTk scale it down for crisp, smooth edges.
            chart_size, scale = 240, 3
            image = Image.new("RGBA", (chart_size * scale, chart_size * scale), (36, 34, 25, 255))
            draw = ImageDraw.Draw(image)
            methods = [
                ("Cash", by["Cash"], "#20C997"),   # teal-green
                ("UPI", by["UPI"], "#3B82F6"),     # vivid blue
                ("Card", by["Card"], "#A855F7"),   # purple
                ("Other", by["Other"], "#F97316"), # orange
            ]
            total_collected = sum((Decimal(str(v or 0)) for _, v, _ in methods), Decimal("0"))
            margin = 16 * scale
            bbox = (margin, margin, chart_size * scale - margin, chart_size * scale - margin)
            if total_collected <= 0:
                draw.ellipse(bbox, fill="#484333")
                draw.ellipse((58*scale, 58*scale, 182*scale, 182*scale), fill="#242219")
                draw.text((120*scale, 105*scale), "NO DATA", fill="#C4BCA8", anchor="mm")
            else:
                angle = -90.0
                # Use a bold, high-resolution label placed at the midpoint of
                # each slice, centered within the donut ring.
                try:
                    percent_font = ImageFont.truetype("arial.ttf", 14 * scale)
                except OSError:
                    percent_font = ImageFont.load_default()
                center = (120 * scale, 120 * scale)
                label_radius = 79 * scale
                for method, value, color in methods:
                    amount = max(0.0, float(value or 0))
                    if amount <= 0:
                        continue
                    extent = 360.0 * amount / float(total_collected)
                    draw.pieslice(bbox, start=angle, end=angle + extent, fill=color,
                                  outline="#242219", width=3 * scale)

                    mid_angle = math.radians(angle + extent / 2.0)
                    label_x = center[0] + math.cos(mid_angle) * label_radius
                    label_y = center[1] + math.sin(mid_angle) * label_radius
                    percentage = f"{100.0 * amount / float(total_collected):.0f}%"
                    draw.text(
                        (label_x, label_y), percentage, font=percent_font,
                        fill="#FFFFFF", anchor="mm",
                        stroke_width=1 * scale, stroke_fill="#242219",
                    )
                    angle += extent
                hole = (65*scale, 65*scale, 175*scale, 175*scale)
                draw.ellipse(hole, fill="#242219")
                draw.text((120*scale, 99*scale), "TOTAL", fill="#A7A18F", anchor="mm")
                total_text = self.money(total_collected)
                draw.text((120*scale, 123*scale), total_text, fill="#F3F0E7", anchor="mm")

            chart_image = ctk.CTkImage(light_image=image, dark_image=image,
                                       size=(chart_size, chart_size))
            chart_label = ctk.CTkLabel(inner, text="", image=chart_image)
            chart_label.image = chart_image  # Keep a reference so Tk does not discard the image.
            chart_label.pack(side="left", padx=(0, 8), pady=3)

            legend = ctk.CTkFrame(inner, fg_color="transparent")
            legend.pack(side="left", fill="both", expand=True, padx=(4, 3), pady=10)
            for method, value, color in methods:
                item = ctk.CTkFrame(legend, fg_color="transparent")
                item.pack(fill="x", pady=7)
                ctk.CTkFrame(item, width=12, height=12, corner_radius=3, fg_color=color).pack(side="left", padx=(0, 8))
                ctk.CTkLabel(item, text=method, text_color="#E8E2D2", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
                ctk.CTkLabel(item, text=self.money(value), text_color="#F3F0E7",
                             font=ctk.CTkFont(size=12, weight="bold")).pack(side="right")

            # Companion chart uses horizontal bars for an at-a-glance comparison.
            compare_panel = ctk.CTkFrame(body, corner_radius=14, border_width=1, border_color="#393529",
                                          fg_color="#242219")
            compare_panel.grid(row=chart_row, column=2, columnspan=2, sticky="nsew", padx=7, pady=(12, 7))
            ctk.CTkLabel(compare_panel, text="Money snapshot", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=16, pady=(14, 1))
            ctk.CTkLabel(compare_panel, text="Compare sales and supplier obligations", text_color=muted).pack(anchor="w", padx=16, pady=(0, 10))
            compare = [("Net sales", d["net_sales"], accent_teal), ("Supplier payments", d["supplier_payments"], accent_blue),
                       ("Outstanding", d["supplier_outstanding"], accent_amber)]
            max_compare = max([float(v or 0) for _, v, _ in compare] + [1.0])
            for label, value, color in compare:
                line = ctk.CTkFrame(compare_panel, fg_color="transparent")
                line.pack(fill="x", padx=16, pady=7)
                top = ctk.CTkFrame(line, fg_color="transparent")
                top.pack(fill="x", pady=(0, 5))
                ctk.CTkLabel(top, text=label, font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
                ctk.CTkLabel(top, text=self.money(value), text_color=color, font=ctk.CTkFont(size=12, weight="bold")).pack(side="right")
                track = ctk.CTkFrame(line, height=12, corner_radius=6, fg_color="#484333")
                track.pack(fill="x")
                width = max(0.0, min(1.0, float(value or 0) / max_compare))
                if width > 0:
                    ctk.CTkFrame(track, height=12, corner_radius=6, fg_color=color).place(relx=0, rely=0, relwidth=width, relheight=1)

            # Operational detail cards remain available without overwhelming the top of the page.
            secondary = [("CAFE REVENUE", d["cafe_revenue"], accent_teal), ("SUPPLIER BILLS", d["bills_entered"], "#64748B"),
                         ("SUPPLIER PAYMENTS", d["supplier_payments"], accent_blue), ("OVERDUE LIABILITIES", d["overdue_outstanding"], "#D9485F"),
                         ("UNALLOCATED PAYMENTS", d["unallocated_payments"], accent_purple), ("CASH COLLECTED", by["Cash"], "#64748B"),
                         ("UPI COLLECTED", by["UPI"], accent_blue), ("CARD COLLECTED", by["Card"], accent_purple),
                         ("OTHER COLLECTIONS", by["Other"], accent_amber), ("RECONCILIATION DIFFERENCES", d["recon_difference_total"], "#D9485F")]
            ctk.CTkLabel(body, text="Operational details", font=ctk.CTkFont(size=16, weight="bold")).grid(
                row=3, column=0, columnspan=4, sticky="w", padx=10, pady=(13, 2))
            for i, (label, val, color) in enumerate(secondary):
                r, col = 4 + i // 4, i % 4
                card = ctk.CTkFrame(body, corner_radius=11, border_width=1, border_color=("#E1E8F0", "#29364A"),
                                    fg_color="#242219")
                card.grid(row=r, column=col, sticky="nsew", padx=7, pady=5)
                ctk.CTkLabel(card, text=label, font=ctk.CTkFont(size=10, weight="bold"), text_color=muted,
                             wraplength=200).pack(anchor="w", padx=12, pady=(11, 4))
                ctk.CTkLabel(card, text=self.money(val), font=ctk.CTkFont(size=17, weight="bold"), text_color=color,
                             wraplength=220).pack(anchor="w", padx=12, pady=(0, 11))

            errs = [x for x in issues if x.startswith("ERROR")]
            msg = "DATA INTEGRITY  ·  OK" if not issues else (
                f"DATA INTEGRITY  ·  {len(errs)} error(s), {len(issues) - len(errs)} warning(s) — see Data Management > Integrity Check")
            status_card = ctk.CTkFrame(body, corner_radius=12, border_width=1,
                                       border_color="#D9485F" if errs else "#168568",
                                       fg_color="#242219")
            status_card.grid(row=7, column=0, columnspan=4, sticky="ew", padx=7, pady=(12, 9))
            ctk.CTkLabel(status_card, text=msg, font=ctk.CTkFont(size=13, weight="bold"),
                         text_color="#D9485F" if errs else "#168568").pack(anchor="w", padx=15, pady=(11, 3))
            ctk.CTkLabel(status_card, text=f"{d['bill_count']} bills  •  {d['sales_days']} sales records  •  Tips and service charges are tracked separately.",
                         text_color=muted).pack(anchor="w", padx=15, pady=(0, 11))
        try:
            self.bg(calc, show, "Loading dashboard")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Dashboard", str(exc))

    # ------------------------------------------------------------------ vendors
    def show_vendors(self):
        self.header("Vendors", "Vendors with history are deactivated, never deleted.")
        self.bar([("+ Add", self.add_vendor), ("Edit", self.edit_vendor), ("Deactivate", self.deactivate_vendor),
                  ("Reactivate", self.reactivate_vendor), ("Delete (unused only)", self.delete_vendor), ("Refresh", self.show_vendors)])
        rows = self.db.read("vendors").to_dict("records")
        cats = sorted({r["category"] for r in rows})
        self.vendor_tbl = self.table(["vendor_id", "name", "category", "contact_person", "phone", "email", "gstin",
                                      "payment_terms_days", "default_payment_method", "active", "created_at", "modified_at"],
                                     rows, choice_filters={"category": cats})

    def _vendor_fields(self, r=None):
        r = r or {}
        cats = list(self.cfg["vendor_categories"])
        cur = r.get("category")
        if cur and cur not in cats:
            cats.append(cur)
        if cur:
            cats.remove(cur)
            cats.insert(0, cur)
        methods = [r.get("default_payment_method", "")] + [m for m in [""] + PAYMENT_METHODS if m != r.get("default_payment_method", "")]
        return [("name", "Vendor name *", "text", r.get("name", "")), ("category", "Category", "choice", cats),
                ("contact_person", "Contact person", "text", r.get("contact_person", "")),
                ("phone", "Phone", "text", r.get("phone", "")), ("email", "Email", "text", r.get("email", "")),
                ("address", "Address", "text", r.get("address", "")), ("gstin", "GSTIN", "text", r.get("gstin", "")),
                ("payment_terms_days", "Payment terms (days)", "text", r.get("payment_terms_days", self.cfg["default_payment_terms_days"])),
                ("default_payment_method", "Default payment method", "choice", methods),
                ("notes", "Notes", "multiline", r.get("notes", ""))]

    def add_vendor(self):
        def save(d):
            rid, dup = d.pop("_request_id"), d.pop("_allow_duplicate", False)
            d.pop("_allow_missing", None)
            self.accounting.add_vendor(d.pop("name"), d.pop("category"), request_id=rid, allow_duplicate=dup, **d)
            return "Vendor saved", self.show_vendors
        FormDialog(self, "Add Vendor", self._vendor_fields(), save)

    def edit_vendor(self):
        r = self.need(self.vendor_tbl)
        if r:
            def save(d):
                for k in ("_request_id", "_allow_duplicate", "_allow_missing"):
                    d.pop(k, None)
                self.accounting.update_vendor(r["vendor_id"], **d)
                return "Vendor updated", self.show_vendors
            FormDialog(self, "Edit Vendor", self._vendor_fields(r), save)

    def _vendor_action(self, label, fn, confirm):
        r = self.need(self.vendor_tbl)
        if r and messagebox.askyesno("Confirm", confirm.format(r["name"]), parent=self):
            try:
                fn(r["vendor_id"])
                self.done(label, self.show_vendors)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(label + " failed", str(exc), parent=self)

    def deactivate_vendor(self):
        self._vendor_action("Vendor deactivated", self.accounting.deactivate_vendor, "Deactivate {}? Historical transactions remain.")

    def reactivate_vendor(self):
        self._vendor_action("Vendor reactivated", self.accounting.reactivate_vendor, "Reactivate {}?")

    def delete_vendor(self):
        self._vendor_action("Vendor deleted", self.accounting.delete_vendor,
                            "Permanently delete '{}'? Only possible when the vendor has no bills or payments.")

    # ------------------------------------------------------------------ bills
    def show_bills(self):
        self.header("Purchase Bills", "Paid, outstanding and status are calculated from the payment ledger.")
        self.bar([("+ Add Bill", self.add_bill), ("Edit", self.edit_bill), ("Cancel Bill", self.cancel_bill),
                  ("Open Attachment", self.open_attachment), ("Refresh", self.show_bills)])
        rows = self.accounting.bill_balances()
        self.bill_rows = rows
        self.bill_tbl = self.table(["bill_id", "vendor_name", "invoice_number", "invoice_date", "due_date", "category",
                                    "invoice_total", "amount_paid", "outstanding_balance", "calculated_status", "days_overdue"],
                                   rows, money_cols=["invoice_total", "amount_paid", "outstanding_balance"],
                                   date_cols=["invoice_date", "due_date"], date_col="invoice_date",
                                   choice_filters={"vendor_name": sorted({r["vendor_name"] for r in rows}),
                                                   "category": self.cfg["expense_categories"],
                                                   "calculated_status": ["Unpaid", "Partially Paid", "Paid", "Overdue", "Cancelled"]})

    def add_bill(self):
        opts = self.vendor_choices()
        if not opts:
            return messagebox.showwarning("No vendors", "Create an active vendor first.", parent=self)
        fields = [("vendor", "Vendor", "choice", opts), ("invoice_number", "Supplier invoice number *", "text", ""),
                  ("invoice_date", "Invoice date", "text", self.today_in()), ("due_date", "Due date", "text", self.today_in()),
                  ("category", "Expense category", "choice", self.cfg["expense_categories"]),
                  ("subtotal", f"Subtotal {self.symbol}", "text", "0.00"), ("tax_amount", f"Tax {self.symbol}", "text", "0.00"),
                  ("other_charges", f"Other charges {self.symbol}", "text", "0.00"),
                  ("invoice_total", f"Invoice total {self.symbol} (optional check)", "text", ""),
                  ("description", "Description", "text", ""), ("attachment_path", "Attachment path (PDF/JPG/PNG)", "text", ""),
                  ("notes", "Notes", "multiline", "")]

        def save(d):
            rid, miss = d.pop("_request_id"), d.pop("_allow_missing", False)
            d.pop("_allow_duplicate", None)
            vid = self.vid_of(d.pop("vendor"))
            self.accounting.add_bill(vid, d.pop("invoice_number"), d.pop("invoice_date"), d.pop("due_date"), d.pop("category"),
                                     d.pop("subtotal"), d.pop("tax_amount"), d.pop("other_charges"), request_id=rid,
                                     allow_missing_attachment=miss, **d)
            return "Bill saved", self.show_bills
        FormDialog(self, "Add Purchase Bill", fields, save)

    def edit_bill(self):
        r = self.need(self.bill_tbl)
        if not r:
            return
        cats = [r["category"]] + [c for c in self.cfg["expense_categories"] if c != r["category"]]
        fields = [("invoice_number", "Invoice number", "text", r["invoice_number"]),
                  ("invoice_date", "Invoice date", "text", self.dstr(r["invoice_date"])),
                  ("due_date", "Due date", "text", self.dstr(r["due_date"])), ("category", "Category", "choice", cats),
                  ("subtotal", "Subtotal", "text", r["subtotal"]), ("tax_amount", "Tax", "text", r["tax_amount"]),
                  ("other_charges", "Other charges", "text", r["other_charges"]),
                  ("description", "Description", "text", r["description"]), ("attachment_path", "Attachment path", "text", r["attachment_path"]),
                  ("notes", "Notes", "multiline", r["notes"]), ("reason", "Reason for change", "text", "")]

        def save(d):
            for k in ("_request_id", "_allow_duplicate"):
                d.pop(k, None)
            miss = d.pop("_allow_missing", False)
            reason = d.pop("reason")
            self.accounting.update_bill(r["bill_id"], reason=reason, allow_missing_attachment=miss, **d)
            return "Bill updated", self.show_bills
        FormDialog(self, "Edit Bill (audited)", fields, save)

    def cancel_bill(self):
        r = self.need(self.bill_tbl)
        if r:
            def save(d):
                self.accounting.cancel_bill(r["bill_id"], d["reason"])
                return "Bill cancelled", self.show_bills
            FormDialog(self, "Cancel Bill", [("reason", "Reason *", "multiline", "")], save)

    def open_attachment(self):
        r = self.need(self.bill_tbl)
        if r:
            p = r.get("attachment_path", "")
            if not p:
                return messagebox.showinfo("No attachment", "This bill has no attachment path.", parent=self)
            if not os.path.exists(p):
                return messagebox.showwarning("Attachment missing", f"The linked file is not available:\n{p}", parent=self)
            self.open_path(p)

    # ------------------------------------------------------------------ payments
    def show_payments(self):
        self.header("Supplier Payments", "Payments are never edited or deleted. Reverse a payment or one allocation instead; history is kept.")
        self.bar([("+ Payment", self.add_payment), ("Allocate Unallocated", self.allocate_existing),
                  ("Reverse Allocation", self.reverse_allocation), ("Reverse Payment", self.reverse_payment), ("Refresh", self.show_payments)])
        rows = self.accounting.payment_rows()
        self.pay_tbl = self.table(["payment_id", "vendor_name", "payment_date", "amount", "allocated", "unallocated", "method",
                                   "reference", "status", "reversal_reason"], rows,
                                  money_cols=["amount", "allocated", "unallocated"], date_cols=["payment_date"], date_col="payment_date",
                                  choice_filters={"vendor_name": sorted({r["vendor_name"] for r in rows}), "method": PAYMENT_METHODS,
                                                  "status": ["Active", "Reversed"]})

    def _open_bills(self, vendor_id, as_of):
        return [b for b in self.accounting.bill_balances(as_of) if str(b["vendor_id"]) == str(vendor_id)
                and b["calculated_status"] not in ("Paid", "Cancelled") and parse_stored(b["outstanding_balance"]) > 0]

    def add_payment(self):
        opts = self.vendor_choices(active_only=False)
        if not opts:
            return messagebox.showwarning("No vendors", "Create a vendor first.", parent=self)
        fields = [("vendor", "Vendor", "choice", opts), ("payment_date", "Payment date", "text", self.today_in()),
                  ("amount", f"Payment amount {self.symbol}", "text", ""), ("method", "Payment method", "choice", PAYMENT_METHODS),
                  ("reference", "Reference / transaction ID", "text", ""), ("notes", "Notes", "multiline", "")]
        dlg_ref = {}

        def first(d):
            # runs in a worker thread: only reads, never touches widgets
            vid = self.vid_of(d["vendor"])
            return ("alloc", d, vid, self._open_bills(vid, d["payment_date"]))

        def next_step(d, vid, bills):
            from utils.currency_utils import money
            try:
                amt = money(d["amount"], "Payment amount", allow_zero=False)
            except ValueError as exc:
                return messagebox.showerror("Invalid amount", str(exc), parent=self)
            rid = d["_request_id"]
            if not bills:
                if not messagebox.askyesno("No open bills", "This vendor has no open bills. Save as a fully UNALLOCATED payment?", parent=self):
                    return
                return self._save_payment(d, vid, amt, [], rid)
            AllocationDialog(self, bills, amt, lambda alloc: self._save_payment(d, vid, amt, alloc, rid),
                             self.symbol, self.cfg.get("indian_number_grouping", True), self.cfg.get("date_display", "DD-MM-YYYY"))

        class Step(FormDialog):
            def _finished(s, result, err):
                s.save_btn.configure(state="normal")
                s.status.configure(text="")
                if err:
                    return messagebox.showerror("Error", str(err), parent=s)
                _, d, vid, bills = result
                s.destroy()
                next_step(d, vid, bills)
        Step(self, "Record Supplier Payment", fields, first)

    def _save_payment(self, d, vid, amt, alloc, rid):
        self.bg(lambda: self.accounting.record_payment(vid, d["payment_date"], amt, d["method"], alloc, d["reference"], d["notes"], request_id=rid),
                lambda _: self.done("Payment saved", self.show_payments), "Saving payment")

    def allocate_existing(self):
        r = self.need(self.pay_tbl)
        if not r:
            return
        free = parse_stored(r["unallocated"])
        if r["status"] != "Active" or free <= 0:
            return messagebox.showinfo("Nothing to allocate", "This payment has no unallocated balance.", parent=self)
        bills = self._open_bills(r["vendor_id"], today_iso())
        if not bills:
            return messagebox.showinfo("No open bills", "The vendor has no open bills.", parent=self)

        def go(alloc):
            if alloc:
                self.bg(lambda: self.accounting.allocate_payment(r["payment_id"], alloc),
                        lambda _: self.done("Allocation saved", self.show_payments), "Saving allocation")
        AllocationDialog(self, bills, free, go, self.symbol, self.cfg.get("indian_number_grouping", True), self.cfg.get("date_display", "DD-MM-YYYY"))

    def reverse_payment(self):
        r = self.need(self.pay_tbl)
        if r and messagebox.askyesno("Confirm reversal", "Reverse this payment? All its allocations are reversed; the records are kept.", parent=self):
            def save(d):
                self.accounting.reverse_payment(r["payment_id"], d["reason"])
                return "Payment reversed", self.show_payments
            FormDialog(self, "Reverse Payment", [("reason", "Reason *", "multiline", "")], save)

    def reverse_allocation(self):
        r = self.need(self.pay_tbl)
        if not r:
            return
        alloc = self.db.read("payment_allocations").to_dict("records")
        done = {}
        for a in alloc:
            if a["entry_type"] == "REVERSE":
                done[str(a["reversal_of"])] = done.get(str(a["reversal_of"]), Decimal(0)) + parse_stored(a["amount"])
        choices = [f"{a['allocation_id']} | bill {a['bill_id']} | {self.money(parse_stored(a['amount']) - done.get(str(a['allocation_id']), 0))} left"
                   for a in alloc if a["entry_type"] == "ALLOCATE" and str(a["payment_id"]) == str(r["payment_id"])
                   and parse_stored(a["amount"]) - done.get(str(a["allocation_id"]), 0) > 0]
        if not choices:
            return messagebox.showinfo("Nothing to reverse", "This payment has no live allocations.", parent=self)

        def save(d):
            self.accounting.reverse_allocation(self.vid_of(d["allocation"]), d["reason"], d["amount"] or None)
            return "Allocation reversed", self.show_payments
        FormDialog(self, "Reverse Allocation (correction)", [("allocation", "Allocation", "choice", choices),
                   ("amount", "Amount to reverse (blank = all)", "text", ""), ("reason", "Reason *", "multiline", "")], save)

    # ------------------------------------------------------------------ sales
    def show_sales(self):
        self.header("Daily Sales", "Register totals. Credit collections are not new revenue. Corrections are audited; later refunds/discounts are separate adjustments.")
        self.bar([("+ Sales Entry", self.add_sales), ("Edit", self.edit_sales), ("+ Refund/Discount", self.add_adjustment),
                  ("Void", self.void_sales), ("Refresh", self.show_sales)])
        rows = self.db.read("customer_sales").to_dict("records")
        self.sales_tbl = self.table(["sales_id", "business_date", "shift", "gross_sales", "discounts", "refunds", "net_sales",
                                     "cash_collected", "upi_collected", "card_collected", "other_collected", "credit_sales",
                                     "credit_collections", "tips_collected", "service_charges_collected", "taxes_collected",
                                     "cash_difference", "status"], rows, money_cols=MONEY_COLUMNS["customer_sales"],
                                    date_cols=["business_date"], date_col="business_date", choice_filters={"status": ["Active", "Voided"]})

    def _sales_form(self, r=None):
        g = (lambda k, d="": (r or {}).get(k, d))
        f = []
        if r is None:
            f.append(("business_date", "Business date", "text", self.today_in()))
        if self.cfg["allow_multiple_shifts"]:
            f.append(("shift", "Shift", "choice", [g("shift")] + [s for s in self.cfg["shift_names"] if s != g("shift")] if r else self.cfg["shift_names"]))
        else:
            f.append(("shift", "Shift (optional)", "text", g("shift")))
        s = self.symbol
        f += [("gross_sales", f"Gross sales {s}", "text", g("gross_sales", "0.00")), ("discounts", f"Discounts {s}", "text", g("discounts", "0.00")),
              ("refunds", f"Refunds {s}", "text", g("refunds", "0.00")), ("cash_collected", f"Cash collected {s}", "text", g("cash_collected", "0.00")),
              ("upi_collected", f"UPI collected {s}", "text", g("upi_collected", "0.00")), ("card_collected", f"Card collected {s}", "text", g("card_collected", "0.00")),
              ("other_collected", f"Other collected {s}", "text", g("other_collected", "0.00"))]
        if self.cfg["track_customer_credit"]:
            f += [("credit_sales", f"Customer credit sales {s} (summary only)", "text", g("credit_sales", "0.00")),
                  ("credit_collections", f"Credit collections {s} (not revenue)", "text", g("credit_collections", "0.00"))]
        f += [("tips_collected", f"Tips collected {s}", "text", g("tips_collected", "0.00")),
              ("service_charges_collected", f"Service charges collected {s}", "text", g("service_charges_collected", "0.00"))]
        if self.cfg["track_taxes"]:
            f.append(("taxes_collected", f"Taxes collected {s}", "text", g("taxes_collected", "0.00")))
        f += [("expected_cash", f"Expected cash {s} (blank = cash collected)", "text", g("expected_cash") if r else ""),
              ("actual_cash", f"Actual cash count {s} (blank = not counted)", "text", g("actual_cash") if r else ""),
              ("notes", "Notes", "multiline", g("notes"))]
        return f

    def add_sales(self):
        def save(d):
            rid, dup = d.pop("_request_id"), d.pop("_allow_duplicate", False)
            d.pop("_allow_missing", None)
            self.accounting.add_sales(d["business_date"], d["gross_sales"], d["discounts"], d["refunds"], d["cash_collected"],
                                      d["upi_collected"], d["card_collected"], d["other_collected"], d.get("credit_sales", 0),
                                      d.get("credit_collections", 0), d["tips_collected"], d["service_charges_collected"],
                                      d.get("taxes_collected", 0), d["shift"], d["expected_cash"] or None, d["actual_cash"] or None,
                                      d["notes"], request_id=rid, allow_duplicate=dup)
            return "Sales saved", self.show_sales
        FormDialog(self, "Add Daily Sales", self._sales_form(), save, width=700)

    def edit_sales(self):
        r = self.need(self.sales_tbl)
        if r:
            def save(d):
                dup = d.pop("_allow_duplicate", False)
                for k in ("_request_id", "_allow_missing"):
                    d.pop(k, None)
                reason = d.pop("reason", "")
                self.accounting.update_sales(r["sales_id"], reason=reason, allow_duplicate=dup, **d)
                return "Sales updated", self.show_sales
            FormDialog(self, "Edit Sales (audited)", self._sales_form(r) + [("reason", "Reason for change", "text", "")], save, width=700)

    def add_adjustment(self):
        r = self.need(self.sales_tbl)
        if r:
            def save(d):
                self.accounting.add_sales_adjustment(r["sales_id"], d["adjustment_type"], d["amount"], d["adjustment_date"], d["notes"], request_id=d["_request_id"])
                return "Adjustment saved", self.show_sales
            FormDialog(self, "Later Refund / Discount", [("adjustment_type", "Type", "choice", ADJUSTMENT_TYPES),
                       ("amount", f"Amount {self.symbol}", "text", ""), ("adjustment_date", "Date", "text", self.today_in()),
                       ("notes", "Notes", "multiline", "")], save)

    def void_sales(self):
        r = self.need(self.sales_tbl)
        if r:
            def save(d):
                self.accounting.void_sales(r["sales_id"], d["reason"])
                return "Sales voided", self.show_sales
            FormDialog(self, "Void Sales Record", [("reason", "Reason *", "multiline", "")], save)

    # ------------------------------------------------------------------ tips
    def show_tips(self):
        self.header("Tips and Service Charges", "Tips and service charges are not cafe revenue unless Settings say so. Collected amounts stay intact; record distribution below.")
        self.bar([("Edit Distribution", self.edit_tip), ("+ Manual Entry", self.add_tip), ("Void Entry", self.void_tip), ("Refresh", self.show_tips)])
        rows = self.db.read("tips_ledger").to_dict("records")
        self.tip_tbl = self.table(["entry_id", "sales_id", "entry_type", "collected", "distributed", "retained_or_payable", "entry_date",
                                   "distribution_date", "notes", "status"], rows, money_cols=MONEY_COLUMNS["tips_ledger"],
                                  date_cols=["entry_date", "distribution_date"], date_col="entry_date",
                                  choice_filters={"entry_type": ["tip", "service_charge"], "status": ["Active", "Voided"]})

    def edit_tip(self):
        r = self.need(self.tip_tbl)
        if r:
            def save(d):
                self.accounting.update_tip(r["entry_id"], d["distributed"], d["retained"], d["notes"], d["distribution_date"])
                return "Ledger updated", self.show_tips
            FormDialog(self, "Update Distribution", [("distributed", f"Distributed {self.symbol}", "text", r["distributed"]),
                       ("retained", f"Retained / payable {self.symbol}", "text", r["retained_or_payable"]),
                       ("distribution_date", "Distribution date", "text", self.dstr(r["distribution_date"]) if r["distribution_date"] else ""),
                       ("notes", "Notes", "multiline", r["notes"])], save)

    def add_tip(self):
        def save(d):
            self.accounting.add_tip_entry(d["entry_type"], d["collected"], d["entry_date"], d["sales_id"].strip(), d["notes"], request_id=d["_request_id"])
            return "Entry saved", self.show_tips
        FormDialog(self, "Manual Ledger Entry", [("entry_type", "Type", "choice", ["tip", "service_charge"]),
                   ("collected", f"Collected {self.symbol}", "text", ""), ("entry_date", "Date", "text", self.today_in()),
                   ("sales_id", "Related sales ID (optional)", "text", ""), ("notes", "Notes", "multiline", "")], save)

    def void_tip(self):
        r = self.need(self.tip_tbl)
        if r:
            def save(d):
                self.accounting.void_tip_entry(r["entry_id"], d["reason"])
                return "Entry voided", self.show_tips
            FormDialog(self, "Void Ledger Entry", [("reason", "Reason *", "multiline", "")], save)

    # ------------------------------------------------------------------ reports
    def show_reports(self):
        self.header("Reports", PROFIT_NOTICE)
        ctrl = ctk.CTkFrame(self.content, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        self.rep_choice = ctk.CTkOptionMenu(ctrl, values=list(REPORTS), width=240)
        self.rep_choice.pack(side="left", padx=3)
        self.rep_start = ctk.CTkEntry(ctrl, width=130, placeholder_text="From · DD-MM-YYYY")
        self.rep_end = ctk.CTkEntry(ctrl, width=130, placeholder_text="To · DD-MM-YYYY")
        self.rep_start.pack(side="left", padx=3)
        self.rep_end.pack(side="left", padx=3)
        days = self.cfg.get("report_default_range_days", 30)
        self.rep_start.insert(0, self.dstr((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")))
        self.rep_end.insert(0, self.today_in())
        self.rep_cat = ctk.CTkComboBox(ctrl, values=["All"] + self.cfg["expense_categories"], width=170)
        self.rep_cat.set("All")
        self.rep_cat.pack(side="left", padx=3)
        self.rep_ven = ctk.CTkComboBox(ctrl, values=["All"] + self.vendor_choices(False), width=190)
        self.rep_ven.set("All")
        self.rep_ven.pack(side="left", padx=3)
        for label, cmd in (("Run", self.run_report), ("Chart", self.chart_report), ("Export Excel", self.export_report)):
            ctk.CTkButton(ctrl, text=label, width=90, command=cmd).pack(side="left", padx=3)
        self.rep_holder = ctk.CTkFrame(self.content, fg_color="#242219", corner_radius=12, border_width=1, border_color="#393529")
        self.rep_holder.grid(row=3, column=0, sticky="nsew", padx=6, pady=6)
        self.rep_holder.grid_rowconfigure(0, weight=1)
        self.rep_holder.grid_columnconfigure(0, weight=1)
        self.rep_note = ctk.CTkLabel(self.content, text="Outstanding/aging reports use the 'To / As-of' date, not today.", text_color="gray")
        self.rep_note.grid(row=4, column=0, sticky="w", padx=10)

    def _compute_report(self, name, start, end, cat, ven):
        meth, kind, _ = REPORTS[name]
        r = self.reports
        if kind == "asof":
            return getattr(r, meth)(end or None)
        if meth.startswith("expense_") and meth.split("_")[1] in ("daily", "monthly", "quarterly", "annual"):
            f = {"daily": "D", "monthly": "M", "quarterly": "Q", "annual": "Y"}[meth.split("_")[1]]
            return r.expense_periodic(f, start or None, end or None, cat, ven)
        if meth in ("expense_breakdown", "expense_by_vendor"):
            return getattr(r, meth)(start or None, end or None, cat, ven)
        if meth in ("cash_daily", "cash_monthly"):
            return r.cash_flow_summary(start or None, end or None, "M" if meth == "cash_monthly" else "D")
        return getattr(r, meth)(start or None, end or None)

    def run_report(self):
        name = self.rep_choice.get()
        cat = None if self.rep_cat.get() == "All" else self.rep_cat.get()
        ven = None if self.rep_ven.get() == "All" else self.vid_of(self.rep_ven.get())
        args = (name, self.rep_start.get().strip(), self.rep_end.get().strip(), cat, ven)

        def show(df):
            if not self.rep_holder.winfo_exists():
                return
            self.current_report = (name, df)
            for w in self.rep_holder.winfo_children():
                w.destroy()
            rows = df.to_dict("records") if not df.empty else []
            cols = list(df.columns)
            if df.empty:
                self.rep_note.configure(text="No records for this selection.")
            t = DataTable(self.rep_holder, cols, rows, symbol=self.symbol, indian=self.cfg.get("indian_number_grouping", True),
                          style=self.cfg.get("date_display", "DD-MM-YYYY"),
                          date_cols=[c for c in cols if c.endswith("date") or c == "business_date"])
            t.grid(row=0, column=0, sticky="nsew")
            if rows:
                self.rep_note.configure(text=PROFIT_NOTICE if name.startswith("Cash") else f"{len(rows)} rows.")
        self.bg(lambda: self._compute_report(*args), show, "Running report")

    def export_report(self):
        if not self.current_report:
            return messagebox.showinfo("No report", "Run a report first.", parent=self)
        name, df = self.current_report
        init = resolve_dir(self.cfg, "export_dir")
        init.mkdir(parents=True, exist_ok=True)
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".xlsx", initialdir=str(init),
                                         initialfile=name.replace(" ", "_").replace("&", "and") + ".xlsx", filetypes=[("Excel workbook", "*.xlsx")])
        if p:
            self.bg(lambda: ReportingService.export(df, p, name[:31]), lambda _: messagebox.showinfo("Export complete", p, parent=self), "Exporting")

    def chart_report(self):
        if not self.current_report:
            return messagebox.showinfo("No report", "Run a report first.", parent=self)
        name, df = self.current_report
        label_col, value_cols = REPORTS[name][2]
        value_cols = [c for c in value_cols if c in df.columns]
        if df.empty or not value_cols:
            return messagebox.showinfo("Chart", "Nothing to chart for this report.", parent=self)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        data = df[df[label_col].astype(str) != "TOTAL"] if label_col in df else df
        win = ctk.CTkToplevel(self)
        win.title(name)
        win.geometry("900x560")
        fig = Figure(figsize=(9, 5), dpi=100)
        ax = fig.add_subplot(111)
        labels = [str(x) for x in data[label_col]]
        width = 0.8 / len(value_cols)
        for i, c in enumerate(value_cols):
            ax.bar([x + i * width for x in range(len(labels))], [float(v) for v in data[c]], width=width, label=c)
        ax.set_xticks([x + width * (len(value_cols) - 1) / 2 for x in range(len(labels))])
        ax.set_xticklabels(labels, rotation=35, ha="right")
        ax.set_ylabel(self.cfg.get("currency_code", "INR"))
        ax.set_title(name)
        if len(value_cols) > 1:
            ax.legend()
        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

    # ------------------------------------------------------------------ settings & data management
    SETTING_FIELDS = [
        ("cafe_name", "Cafe name", "text"), ("business_details", "Business details (address, GSTIN)", "text"),
        ("currency_code", "Currency code", "text"), ("currency_symbol", "Currency symbol", "text"),
        ("date_display", "Date display (DD-MM-YYYY / YYYY-MM-DD / DD/MM/YYYY)", "text"), ("operator_name", "Operator name (audit log)", "text"),
        ("vendor_categories", "Vendor category", "managed"), ("expense_categories", "Expense category", "managed"),
        ("default_payment_terms_days", "Default vendor payment terms (days)", "text"),
        ("allow_multiple_shifts", "Allow multiple shifts per day", "bool"), ("shift_names", "Shift name", "managed"),
        ("tips_excluded_from_revenue", "Exclude TIPS from cafe revenue", "bool"),
        ("service_charges_excluded_from_revenue", "Exclude SERVICE CHARGES from cafe revenue (accountant decision)", "bool"),
        ("taxes_excluded_from_revenue", "Exclude TAXES from cafe revenue (accountant decision)", "bool"),
        ("gross_sales_includes_tax", "POS 'gross sales' already includes tax", "bool"),
        ("track_taxes", "Track taxes collected", "bool"), ("track_customer_credit", "Track customer credit (summary only)", "bool"),
        ("opening_cash", "Opening cash on hand (blank = not set)", "text"), ("opening_cash_date", "Opening cash date", "text"),
        ("copy_attachments", "Copy invoice attachments into the managed folder", "bool"),
        ("auto_backup_daily", "Automatic daily backup", "bool"), ("backup_retention", "Backups to keep", "text"),
        ("data_dir", "Data folder (restart required)", "text"), ("backup_dir", "Backup folder (restart required)", "text"),
        ("export_dir", "Export folder", "text"), ("staging_dir", "External-edit staging folder", "text"),
        ("report_default_range_days", "Default report range (days)", "text"), ("appearance_mode", "Appearance (System/Light/Dark)", "text"),
    ]

    def show_settings(self):
        self.header("Data Management and Settings", "Excel files are the data store. They are NOT access-controlled or encrypted - protect the folder and keep backups. Single user only.")
        self.bar([("Save Settings", self.save_settings), ("Backup Now", self.backup_now), ("Restore Backup", self.restore_backup),
                  ("Integrity Check", self.integrity), ("Export for Edit", self.export_for_edit), ("Import / Preview", self.import_preview),
                  ("Import Data File", self.import_data_file), ("🔴 Reset All Data", self.reset_app_data),
                  ("Excel Snapshot", self.snapshot), ("Open Data Folder", lambda: self.open_path(self.db.data_dir))])
        box = ctk.CTkScrollableFrame(self.content, fg_color="#1D1C16", corner_radius=12, border_width=1, border_color="#393529")
        box.grid(row=3, column=0, sticky="nsew", padx=8, pady=8)
        box.grid_columnconfigure(0, weight=0, minsize=270)
        box.grid_columnconfigure(1, weight=1, minsize=240)
        box.grid_columnconfigure(2, weight=0, minsize=110)
        self.set_w = {}
        for i, (key, label, kind) in enumerate(self.SETTING_FIELDS):
            ctk.CTkLabel(box, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=12, pady=9)
            v = self.cfg.get(key)
            if kind == "bool":
                w = ctk.CTkSwitch(box, text="")
                (w.select() if v else w.deselect())
            elif kind == "managed":
                vals = list(v) if isinstance(v, (list, tuple)) else []
                w = ctk.CTkOptionMenu(box, values=vals or ["(none)"])
                w.set(vals[0] if vals else "(none)")
                w.grid(row=i, column=1, sticky="ew", padx=12, pady=9)
                def add_item(k=key, menu=w):
                    name = simpledialog.askstring("Add new item", "Enter a new name:", parent=self)
                    if not name or not name.strip(): return
                    name = name.strip()
                    current = list(self.cfg.get(k, []))
                    if name not in current: current.append(name)
                    self.cfg[k] = current
                    menu.configure(values=current); menu.set(name)
                    self.set_w[k] = ("managed", menu)
                ctk.CTkButton(box, text="＋ Add new", width=95, command=add_item, fg_color="#302C22", hover_color="#48402B", text_color="#E8C46C").grid(row=i, column=2, padx=12, pady=9)
            elif kind == "multi":
                w = ctk.CTkTextbox(box, height=90)
                w.insert("1.0", "\n".join(v))
                w.grid(row=i, column=1, sticky="ew", padx=12, pady=9)
                self.set_w[key] = (kind, w)
            else:
                w = ctk.CTkEntry(box)
                w.insert(0, str(v))
                w.grid(row=i, column=1, sticky="ew", padx=12, pady=9)
            if kind != "managed" and kind != "multi": w.grid(row=i, column=1, sticky="ew", padx=12, pady=9)
            if kind != "managed": self.set_w[key] = (kind, w)

    def save_settings(self):
        new = dict(self.cfg)
        for key, (kind, w) in self.set_w.items():
            new[key] = bool(w.get()) if kind == "bool" else [x.strip() for x in w.get("1.0", "end-1c").splitlines() if x.strip()] if kind == "multi" else list(self.cfg.get(key, [])) if kind == "managed" else w.get().strip()
        try:
            clean = save_config(new)
        except Exception as exc:  # noqa: BLE001
            return messagebox.showerror("Settings not saved", str(exc), parent=self)
        self.cfg.clear()
        self.cfg.update(clean)     # services share this dict, so policy changes apply immediately
        self.db.backups.retention = clean["backup_retention"]
        ctk.set_appearance_mode(clean["appearance_mode"])
        self.side_sub.configure(text=clean["cafe_name"])
        self.title(f"CafeBooks - {clean['cafe_name']}")
        messagebox.showinfo("Saved", "Settings saved. Folder changes need a restart.", parent=self)

    def backup_now(self):
        self.bg(lambda: self.db.backup("manual"), lambda p: messagebox.showinfo("Backup complete", f"Created and verified:\n{p}", parent=self), "Creating backup")

    def snapshot(self):
        self.bg(lambda: self.db.export_database_snapshot(resolve_dir(self.cfg, "export_dir")), lambda p: self.open_path(p), "Exporting snapshot")

    def integrity(self):
        # Keep the result window alive while the check runs; the old generic progress
        # dialog was destroyed immediately after the worker finished, which looked as
        # if Integrity Check had closed without showing useful results.
        win = ctk.CTkToplevel(self)
        win.title("Data Integrity Check")
        win.geometry("820x500")
        win.minsize(640, 360)
        win.configure(fg_color="#171610")
        win.transient(self)
        ctk.CTkLabel(win, text="DATA INTEGRITY", text_color="#F4B323",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=16, pady=(14, 2))
        status = ctk.CTkLabel(win, text="Checking records and relationships…", text_color="#C8BFA8")
        status.pack(anchor="w", padx=16, pady=(0, 8))
        t = ctk.CTkTextbox(win, fg_color="#1D1C16", text_color="#E8E1D0",
                           border_width=1, border_color="#393529", corner_radius=10)
        t.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        t.insert("1.0", "Please wait while CafeBooks checks your data…")
        t.configure(state="disabled")
        def show(issues):
            if not win.winfo_exists():
                return
            errors = [x for x in issues if str(x).startswith("ERROR:")]
            warnings = [x for x in issues if not str(x).startswith("ERROR:")]
            status.configure(text=f"{len(errors)} error(s)  •  {len(warnings)} warning(s)" if issues else "All checks passed — no integrity problems found.",
                             text_color="#F4B323" if errors else "#8CCB9A")
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.insert("1.0", "\n".join(issues) if issues else "No integrity problems found.\n\nYour records passed the dataset checks.")
            t.configure(state="disabled")
        def failed(exc):
            if not win.winfo_exists():
                return
            status.configure(text="The check could not finish.", text_color="#F28B82")
            t.configure(state="normal"); t.delete("1.0", "end")
            t.insert("1.0", f"Integrity check failed with an unexpected error:\n\n{exc}\n\nNo records were changed.")
            t.configure(state="disabled")
        run_bg(self, self.accounting.integrity_check, lambda result, err: failed(err) if err else show(result))

    def restore_backup(self):
        p = filedialog.askopenfilename(parent=self, initialdir=str(self.db.backup_dir), filetypes=[("CafeBooks backup", "*.zip")])
        if not p:
            return

        def confirm(info):
            lines = [f"Backup created: {info['manifest'].get('created_at') or 'unknown (legacy backup, integrity not provable)'}", ""]
            lines += [f"{t}: backup {v['backup']} rows / current {v['current']} rows" for t, v in info["tables"].items()]
            errs = [i for i in info["issues"] if i.severity == "error"]
            if errs:
                lines += ["", f"{len(errs)} integrity errors in this backup - restore will be refused."]
            lines += ["", "A safety backup of the current data is made first. Restore now?"]
            if messagebox.askyesno("Restore preview", "\n".join(lines), parent=self):
                self.bg(lambda: self.db.restore(p), lambda s: (self.done("Backup restored"), messagebox.showinfo("Restored", f"Restored. Safety copy of previous data:\n{s}", parent=self), self.show_dashboard()), "Restoring")
        self.bg(lambda: self.db.restore_preview(p), confirm, "Verifying backup")

    def export_for_edit(self):
        def ok(info):
            messagebox.showinfo("Export ready", f"Exported at {info['exported_at']}\n\nEdit ONLY the files in:\n{info['edit_dir']}\n\n"
                                "Do not delete rows or add formulas. When finished, close Excel and use Import / Preview. "
                                "Live data is not changed by exporting or opening these files.", parent=self)
            self.open_path(info["edit_dir"])
        self.bg(self.sync.export_for_edit, ok, "Exporting for edit")

    def import_data_file(self):
        path = filedialog.askopenfilename(parent=self, title="Import CafeBooks data",
            filetypes=[("CafeBooks Excel workbook", "*.xlsx"), ("Text / CSV files", "*.txt *.csv"), ("All supported files", "*.xlsx *.txt *.csv")])
        if not path:
            return
        source = Path(path)
        try:
            import pandas as pd
            from database.schemas import SCHEMAS
            if source.suffix.lower() in (".txt", ".csv"):
                # Text imports must include a table column identifying the destination table.
                df = pd.read_csv(source, sep=None, engine="python")
                if "table" not in df.columns:
                    raise ValueError("TXT/CSV files must include a 'table' column (for example vendors or customer_sales).")
                imported = {}
                for table, part in df.groupby("table"):
                    if table not in SCHEMAS:
                        raise ValueError(f"Unknown table name: {table}")
                    part = part.drop(columns=["table"])
                    missing = [c for c in SCHEMAS[table] if c not in part.columns]
                    if missing: raise ValueError(f"{table}: missing columns: {', '.join(missing)}")
                    imported[table] = part[SCHEMAS[table]]
            else:
                book = pd.ExcelFile(source)
                imported = {}
                for table in SCHEMAS:
                    sheet = next((n for n in book.sheet_names if n.strip().lower() == table.lower()), None)
                    if sheet:
                        df = pd.read_excel(source, sheet_name=sheet)
                        if len(df.columns) == 0: continue
                        missing = [c for c in SCHEMAS[table] if c not in df.columns]
                        if missing: raise ValueError(f"Sheet '{sheet}' is missing columns: {', '.join(missing)}")
                        imported[table] = df[SCHEMAS[table]]
                if not imported:
                    raise ValueError("No recognized CafeBooks sheets found. Use the supplied demo workbook or a workbook with sheet names matching the app tables.")
            frames = {t: self.db.read(t).copy() for t in SCHEMAS}
            # Demo/import files intentionally replace only tables present in the file. Confirm first and back up.
            details = "\n".join(f"{t}: {len(df)} rows" for t, df in imported.items())
            if not messagebox.askyesno("Confirm import", f"Selected file: {source.name}\n\n{details}\n\nImport will REPLACE these tables in the app. A safety backup is created first. Your Excel source file will not be modified. Continue?", parent=self):
                return
            for table, df in imported.items(): frames[table] = df
            # Create a safety backup before touching live records.
            backup = self.db.backup("before_file_import")
            self.db.replace_all(frames, "direct_file_import")
            self.refresh_status("| Imported data file")
            messagebox.showinfo("Import complete", f"Imported successfully.\nSafety backup: {backup}\nSource Excel/text file was left unchanged.", parent=self)
            self.show_dashboard()
        except Exception as exc:
            messagebox.showerror("Import failed", f"No data was imported.\n\n{exc}", parent=self)

    def reset_app_data(self):
        if not messagebox.askyesno("Reset all app data", "This removes all transaction and vendor records from CafeBooks, but keeps the Excel files safe by creating a verified backup first. This cannot be undone from the app except by restoring that backup. Continue?", icon="warning", parent=self):
            return
        if not messagebox.askyesno("Confirm permanent reset", "Really clear all app records? Your backup will be preserved.", icon="warning", parent=self):
            return
        try:
            backup = self.db.backup("before_reset")
            from database.schemas import SCHEMAS
            frames = {t: self.db._empty(t) for t in SCHEMAS}
            # Keep settings, including theme and data paths, but clear all operational records.
            frames["settings"] = self.db.read("settings").copy()
            self.db.replace_all(frames, "reset_app_data")
            self.refresh_status("| App data reset")
            messagebox.showinfo("Reset complete", f"All operational records were cleared. Settings remain.\nBackup saved at:\n{backup}", parent=self)
            self.show_dashboard()
        except Exception as exc:
            messagebox.showerror("Reset failed", f"No reset was completed.\n\n{exc}", parent=self)

    def import_preview(self):
        folder = filedialog.askdirectory(parent=self, title="Select the export folder (contains manifest.json)", initialdir=str(resolve_dir(self.cfg, "staging_dir")))
        if not folder:
            return

        def show(plan):
            text = plan.describe()
            if plan.errors or not plan.has_changes:
                return messagebox.showinfo("Import preview", text + ("\n\nNothing was changed." if plan.errors else ""), parent=self)
            resolutions = {}
            if plan.conflicts:
                ans = messagebox.askyesnocancel("Conflicts", text + "\n\nConflicting rows were changed both here and in the files.\n"
                                                "YES = keep CURRENT app values for conflicts\nNO = use IMPORTED values for conflicts\nCANCEL = abort", parent=self)
                if ans is None:
                    return
                choice = "live" if ans else "import"
                resolutions = {(c["table"], c["pk"]): choice for c in plan.conflicts}
            elif not messagebox.askyesno("Apply import?", text + "\n\nA backup is created first. Apply these changes?", parent=self):
                return
            self.bg(lambda: self.sync.apply_import(folder, resolutions),
                    lambda r: (messagebox.showinfo("Import", r["message"], parent=self), self.refresh_status(), self.show_dashboard()), "Applying import")
        self.bg(lambda: self.sync.preview_import(folder), show, "Validating import")

    def on_close(self):
        try:
            if self.cfg.get("auto_backup_daily", True):
                self.db.maybe_daily_backup()
        except Exception:  # noqa: BLE001
            pass
        self.db.close()
        self.destroy()
