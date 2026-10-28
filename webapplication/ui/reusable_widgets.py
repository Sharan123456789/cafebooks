"""Reusable GUI pieces.  Worker threads never touch widgets: results travel through a queue
that the main thread polls."""
import queue
import threading
import uuid
from decimal import Decimal
from tkinter import ttk, messagebox

import customtkinter as ctk

from services.accounting_service import DuplicateWarning, AttachmentMissing
from utils.currency_utils import format_money, parse_stored
from utils.date_utils import display_date, parse_date, today_iso


def run_bg(widget, fn, on_done, poll_ms=80):
    """Run fn() in a thread; call on_done(result, error) on the main thread."""
    q = queue.Queue()

    def work():
        try:
            q.put((fn(), None))
        except Exception as exc:  # noqa: BLE001
            q.put((None, exc))
    threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            result, err = q.get_nowait()
        except queue.Empty:
            if widget.winfo_exists():
                widget.after(poll_ms, poll)
            return
        if widget.winfo_exists():
            on_done(result, err)
    widget.after(poll_ms, poll)


class FormDialog(ctk.CTkToplevel):
    """fields: (key, label, kind, default) with kind in text|choice|multiline.
    on_save(data) runs in a worker thread and returns (message, refresh_callable)."""

    def __init__(self, app, title, fields, on_save, width=640):
        super().__init__(app)
        self.app, self.on_save = app, on_save
        self.title(title)
        self.geometry(f"{width}x{min(780, max(340, 150 + len(fields) * 46))}")
        self.minsize(width, 340)
        self.transient(app)
        self.request_id = uuid.uuid4().hex
        self.flags = {}
        self.widgets = {}
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.grid_columnconfigure(1, weight=1)
        first = None
        for i, (key, label, kind, default) in enumerate(fields):
            ctk.CTkLabel(frame, text=label, anchor="w").grid(row=i, column=0, sticky="ew", padx=5, pady=5)
            if kind == "choice":
                w = ctk.CTkComboBox(frame, values=[str(x) for x in default])
                w.set(str(default[0]) if default else "")
            elif kind == "multiline":
                w = ctk.CTkTextbox(frame, height=70)
                w.insert("1.0", str(default or ""))
            else:
                # Self-clearing in-field guidance: CTkEntry's placeholder disappears on typing.
                money_keys = {"subtotal", "tax_amount", "other_charges", "invoice_total", "amount",
                              "gross_sales", "discounts", "refunds", "net_sales", "cash_collected",
                              "upi_collected", "card_collected", "other_collected", "credit_sales",
                              "credit_collections", "tips_collected", "service_charges_collected",
                              "taxes_collected", "expected_cash", "actual_cash", "cash_difference",
                              "collected", "distributed", "retained", "retained_or_payable"}
                date_keys = {"business_date", "invoice_date", "due_date", "payment_date", "entry_date",
                             "distribution_date", "adjustment_date", "effective_date"}
                if key in money_keys:
                    hint = "Whole number only (no decimals)"
                    default_text = str(default if default is not None else "")
                    if default_text.endswith(".00"):
                        default_text = default_text[:-3]
                elif key in date_keys or "date" in key.lower():
                    hint, default_text = "Date: DD-MM-YYYY", str(default if default is not None else "")
                elif key in {"payment_terms_days"}:
                    hint, default_text = "Whole number of days", str(default if default is not None else "")
                elif key in {"phone"}:
                    hint, default_text = "Phone number", str(default if default is not None else "")
                elif key in {"email"}:
                    hint, default_text = "Email address", str(default if default is not None else "")
                elif key in {"attachment_path"}:
                    hint, default_text = "File path (PDF, JPG or PNG)", str(default if default is not None else "")
                elif key in {"invoice_number", "reference"}:
                    hint, default_text = "Text / reference number", str(default if default is not None else "")
                elif key in {"name", "vendor_name", "contact_person"}:
                    hint, default_text = "Enter text", str(default if default is not None else "")
                else:
                    hint, default_text = "Enter value", str(default if default is not None else "")
                w = ctk.CTkEntry(frame, placeholder_text=hint)
                w.insert(0, default_text)
                if key in money_keys or key == "payment_terms_days":
                    # The UI accepts whole numbers only; accounting storage remains Decimal-safe.
                    vcmd = (self.register(lambda proposed: proposed == "" or proposed.isdigit()), "%P")
                    w.configure(validate="key", validatecommand=vcmd)
            w.grid(row=i, column=1, sticky="ew", padx=5, pady=5)
            self.widgets[key] = w
            first = first or w
        self.status = ctk.CTkLabel(self, text="", text_color="gray")
        self.status.pack(fill="x", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(bar, text="Cancel", command=self.destroy, fg_color="gray").pack(side="right", padx=5)
        self.save_btn = ctk.CTkButton(bar, text="Save", command=self.save)
        self.save_btn.pack(side="right", padx=5)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(150, lambda: (self.grab_set(), first.focus_set() if first else None))
        for key, w in self.widgets.items():
            if isinstance(w, ctk.CTkEntry):
                w.bind("<Return>", lambda e: self.save())

    def collect(self):
        data = {k: (w.get("1.0", "end-1c") if isinstance(w, ctk.CTkTextbox) else w.get())
                for k, w in self.widgets.items()}
        data["_request_id"] = self.request_id
        data.update(self.flags)
        return data

    def save(self):
        data = self.collect()
        self.save_btn.configure(state="disabled")
        self.status.configure(text="Saving...")
        run_bg(self, lambda: self.on_save(data), self._finished)

    def _finished(self, result, err):
        self.save_btn.configure(state="normal")
        self.status.configure(text="")
        if err is None:
            msg, refresh = result if result else ("Saved", None)
            self.app.done(msg, refresh)
            self.destroy()
        elif isinstance(err, DuplicateWarning):
            if messagebox.askyesno("Possible duplicate", f"{err}\n\nSave anyway?", parent=self):
                self.flags["_allow_duplicate"] = True
                self.save()
        elif isinstance(err, AttachmentMissing):
            if messagebox.askyesno("Attachment not found", f"{err}\n\nSave the link anyway?", parent=self):
                self.flags["_allow_missing"] = True
                self.save()
        else:
            messagebox.showerror("Could not save", str(err), parent=self)


class AllocationDialog(ctk.CTkToplevel):
    """Allocate an amount across a vendor's open bills.  on_done(list[(bill_id, Decimal)])."""

    def __init__(self, app, bills, available, on_done, symbol="\u20b9", indian=True, style="DD-MM-YYYY"):
        super().__init__(app)
        self.title("Allocate Payment")
        self.geometry("1000x600")
        self.transient(app)
        self.bills, self.available, self.on_done = bills, parse_stored(available), on_done
        self.entries = {}
        self.fmt = lambda v: format_money(v, symbol, indian)
        ctk.CTkLabel(self, text=f"Amount available to allocate: {self.fmt(self.available)}. Allocations cannot "
                                "exceed this or any bill's outstanding balance. Unallocated money stays unallocated.",
                     wraplength=950).pack(padx=15, pady=10)
        head = ctk.CTkFrame(self)
        head.pack(fill="x", padx=15)
        heads = ["Invoice", "Invoice date", "Due", "Original", "Paid", "Outstanding", "Days overdue", "Status", "Allocate"]
        body = ctk.CTkScrollableFrame(self)
        body.pack(fill="both", expand=True, padx=15, pady=5)
        for i, t in enumerate(heads):
            ctk.CTkLabel(head, text=t, font=ctk.CTkFont(weight="bold"), width=100, anchor="w").grid(row=0, column=i, padx=4, pady=5)
        for r, b in enumerate(bills):
            vals = [b["invoice_number"], display_date(b["invoice_date"], style), display_date(b["due_date"], style),
                    self.fmt(b["invoice_total"]), self.fmt(b["amount_paid"]), self.fmt(b["outstanding_balance"]),
                    b["days_overdue"], b["calculated_status"]]
            for i, v in enumerate(vals):
                ctk.CTkLabel(body, text=str(v), width=100, anchor="w").grid(row=r, column=i, padx=4, pady=5)
            e = ctk.CTkEntry(body, width=110, placeholder_text="Whole number")
            e.insert(0, "0")
            e.configure(validate="key", validatecommand=(self.register(lambda proposed: proposed == "" or proposed.isdigit()), "%P"))
            e.grid(row=r, column=8, padx=4, pady=5)
            self.entries[b["bill_id"]] = e
        self.msg = ctk.CTkLabel(self, text="", text_color="gray")
        self.msg.pack()
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=8)
        ctk.CTkButton(bar, text="Auto: oldest due first", command=self.auto, fg_color="gray").pack(side="left", padx=5)
        ctk.CTkButton(bar, text="Apply allocation", command=self.finish).pack(side="left", padx=5)
        ctk.CTkButton(bar, text="Cancel", command=self.destroy, fg_color="gray").pack(side="left", padx=5)
        self.after(150, self.grab_set)

    def auto(self):
        left = self.available
        for b in sorted(self.bills, key=lambda x: (x["due_date"], x["invoice_date"])):
            take = min(left, parse_stored(b["outstanding_balance"]))
            e = self.entries[b["bill_id"]]
            e.delete(0, "end")
            e.insert(0, str(int(take)))
            left -= take

    def finish(self):
        out = []
        for bid, e in self.entries.items():
            try:
                raw = (e.get() or "0").replace(",", "")
                if not raw.isdigit():
                    raise ValueError("Enter a whole number without decimals.")
                v = Decimal(raw)
            except Exception:  # noqa: BLE001
                self.msg.configure(text="Allocation must be a number.")
                return
            if v > 0:
                out.append((bid, v))
        if sum((v for _, v in out), Decimal(0)) > self.available:
            self.msg.configure(text="Total exceeds the amount available.")
            return
        self.destroy()
        self.on_done(out)


class DataTable(ctk.CTkFrame):
    """Searchable, sortable table with optional date-range and choice filters."""
    MAX_ROWS = 3000

    def __init__(self, parent, columns, rows, money_cols=(), date_cols=(), date_col=None, choice_filters=None,
                 symbol="\u20b9", indian=True, style="DD-MM-YYYY"):
        # Use the same warm charcoal/gold visual system on every data-heavy page.
        super().__init__(parent, fg_color="#242219", corner_radius=12,
                         border_width=1, border_color="#393529")
        self.columns, self.rows = list(columns), list(rows)
        self.money_cols, self.date_cols = set(money_cols), set(date_cols)
        self.date_col, self.symbol, self.indian, self.style = date_col, symbol, indian, style
        self.choice_filters = choice_filters or {}
        self.sort_col, self.sort_desc = None, False
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 8))
        self.search = ctk.CTkEntry(bar, width=220, placeholder_text="Search records...",
                                   fg_color="#171610", border_color="#494231",
                                   text_color="#F3F0E7", placeholder_text_color="#8D8879")
        self.search.pack(side="left", padx=3)
        self.search.bind("<Return>", lambda e: self.apply())
        self.start = self.end = None
        if date_col:
            self.start = ctk.CTkEntry(bar, width=125, placeholder_text="From DD-MM-YYYY",
                                      fg_color="#171610", border_color="#494231", text_color="#F3F0E7",
                                      placeholder_text_color="#8D8879")
            self.end = ctk.CTkEntry(bar, width=125, placeholder_text="To DD-MM-YYYY",
                                    fg_color="#171610", border_color="#494231", text_color="#F3F0E7",
                                    placeholder_text_color="#8D8879")
            self.start.pack(side="left", padx=3)
            self.end.pack(side="left", padx=3)
        self.combos = {}
        for col, values in self.choice_filters.items():
            c = ctk.CTkComboBox(bar, values=["All"] + list(values), width=155,
                                fg_color="#171610", border_color="#494231",
                                text_color="#F3F0E7", button_color="#393529",
                                button_hover_color="#F4B323", dropdown_fg_color="#242219",
                                dropdown_text_color="#F3F0E7")
            c.set("All")
            c.pack(side="left", padx=3)
            self.combos[col] = c
        ctk.CTkButton(bar, text="Apply filters", width=100, command=self.apply,
                      fg_color="#F4B323", hover_color="#D99A19", text_color="#171610").pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Reset", width=70, command=self.clear,
                      fg_color="#393529", hover_color="#494231", text_color="#E8E1D0").pack(side="left", padx=4)
        self.info = ctk.CTkLabel(bar, text="", text_color="#A7A18F")
        self.info.pack(side="right", padx=8)

        # ttk.Treeview ignores CustomTkinter colors unless explicitly themed; this was
        # the source of the stark white table area on bills, payments, sales, vendors and tips.
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Cafe.Treeview", background="#1D1C16", foreground="#E8E1D0",
                        fieldbackground="#1D1C16", bordercolor="#393529",
                        lightcolor="#393529", darkcolor="#393529",
                        rowheight=32, font=("Segoe UI", 10))
        style.map("Cafe.Treeview", background=[("selected", "#5A4618")],
                  foreground=[("selected", "#FFF8E6")])
        style.configure("Cafe.Treeview.Heading", background="#302C22", foreground="#F4B323",
                        relief="flat", bordercolor="#393529", padding=(10, 10),
                        font=("Segoe UI", 9, "bold"))
        style.map("Cafe.Treeview.Heading", background=[("active", "#393529")],
                  foreground=[("active", "#FFD36A")])
        style.configure("Cafe.Vertical.TScrollbar", background="#393529", troughcolor="#171610",
                        bordercolor="#171610", arrowcolor="#D7D1C2")
        style.configure("Cafe.Horizontal.TScrollbar", background="#393529", troughcolor="#171610",
                        bordercolor="#171610", arrowcolor="#D7D1C2")
        self.tree = ttk.Treeview(self, columns=self.columns, show="headings", selectmode="browse",
                                 style="Cafe.Treeview")
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview, style="Cafe.Vertical.TScrollbar")
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview, style="Cafe.Horizontal.TScrollbar")
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        for c in self.columns:
            self.tree.heading(c, text=c.replace("_", " ").title(), command=lambda col=c: self.sort_by(col))
            self.tree.column(c, width=max(95, min(210, 9 * len(c) + 40)), anchor="e" if c in self.money_cols else "w")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(12, 0), pady=(0, 0))
        vs.grid(row=1, column=1, sticky="ns", padx=(0, 12), pady=(0, 0))
        hs.grid(row=2, column=0, sticky="ew", padx=(12, 0), pady=(0, 12))
        self.visible = []
        self.apply()

    def clear(self):
        self.search.delete(0, "end")
        for e in (self.start, self.end):
            if e:
                e.delete(0, "end")
        for c in self.combos.values():
            c.set("All")
        self.apply()

    def _cell(self, col, v):
        if v is None:
            return ""
        if col in self.money_cols or isinstance(v, Decimal):
            try:
                return format_money(v, "", self.indian).strip()
            except Exception:  # noqa: BLE001
                return str(v)
        if col in self.date_cols:
            return display_date(v, self.style)
        return str(v)

    def apply(self):
        text = self.search.get().strip().casefold()
        try:
            s = parse_date(self.start.get(), required=False) if self.start else ""
            e = parse_date(self.end.get(), required=False) if self.end else ""
        except ValueError as exc:
            messagebox.showwarning("Date filter", str(exc), parent=self)
            return
        out = []
        for r in self.rows:
            if text and not any(text in str(r.get(c, "")).casefold() for c in self.columns):
                continue
            if self.date_col:
                d = str(r.get(self.date_col, ""))
                if (s and d < s) or (e and d > e):
                    continue
            if any(c.get() != "All" and str(r.get(col, "")) != c.get() for col, c in self.combos.items()):
                continue
            out.append(r)
        if self.sort_col:
            out.sort(key=lambda r: self._sort_key(r.get(self.sort_col)), reverse=self.sort_desc)
        self.visible = out[:self.MAX_ROWS]
        self.tree.delete(*self.tree.get_children())
        self.tree.tag_configure("even", background="#1D1C16", foreground="#E8E1D0")
        self.tree.tag_configure("odd", background="#242219", foreground="#E8E1D0")
        for i, r in enumerate(self.visible):
            self.tree.insert("", "end", iid=str(i),
                             values=[self._cell(c, r.get(c, "")) for c in self.columns],
                             tags=("even" if i % 2 == 0 else "odd",))
        more = f" (first {self.MAX_ROWS} shown)" if len(out) > self.MAX_ROWS else ""
        self.info.configure(text=f"{len(out)} of {len(self.rows)} rows{more}")

    @staticmethod
    def _sort_key(v):
        try:
            return (0, float(Decimal(str(v).replace(",", ""))), "")
        except Exception:  # noqa: BLE001
            return (1, 0, str(v).casefold())

    def sort_by(self, col):
        self.sort_desc = (not self.sort_desc) if self.sort_col == col else False
        self.sort_col = col
        self.apply()

    def selected(self):
        sel = self.tree.selection()
        return self.visible[int(sel[0])] if sel else None
