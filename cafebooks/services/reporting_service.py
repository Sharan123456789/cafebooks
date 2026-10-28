"""Reports.  All money stays Decimal; every report is a DataFrame that can be shown as a
table, charted, or exported to Excel (text is written as literal text, never as formulas).

Cash flow is NOT profit: inventory, payroll, depreciation and other costs are not maintained.
"""
from collections import defaultdict
from decimal import Decimal

import pandas as pd

from cafebooks.database.excel_io import write_xlsx
from cafebooks.services.reconciliation_service import ReconciliationService
from cafebooks.utils.currency_utils import parse_stored, ZERO
from cafebooks.utils.date_utils import parse_date, to_date, today_iso, month_key, quarter_key, year_key

PROFIT_NOTICE = ("Cash flow is not profit. CafeBooks does not track inventory cost, payroll, depreciation or "
                 "other expense categories, so complete accounting profit is NOT calculated.")
BUCKETS = ["Current", "1-30 days", "31-60 days", "61-90 days", ">90 days"]

# name -> (method, "asof" | "range", (chart label column, chart value columns))
REPORTS = {
    "Outstanding - bill wise": ("vendor_outstanding", "asof", ("vendor_name", ["outstanding_balance"])),
    "Outstanding - vendor wise": ("vendor_outstanding_summary", "asof", ("vendor_name", ["total_outstanding"])),
    "Aging - bill wise": ("vendor_aging", "asof", ("aging_bucket", ["outstanding_balance"])),
    "Aging - vendor summary": ("vendor_aging_summary", "asof", ("vendor_name", BUCKETS)),
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
EXPENSE_FREQ = {"expense_daily": "D", "expense_monthly": "M", "expense_quarterly": "Q", "expense_annual": "Y"}


def bucket_for(days):
    if days <= 0:
        return "Current"
    return "1-30 days" if days <= 30 else "31-60 days" if days <= 60 else "61-90 days" if days <= 90 else ">90 days"


def _frame(rows, columns):
    return pd.DataFrame(rows, columns=columns, dtype=object) if rows else pd.DataFrame(columns=columns, dtype=object)


class ReportingService:
    def __init__(self, accounting):
        self.a = accounting
        self.recon = ReconciliationService(accounting)

    @staticmethod
    def _rng(start, end):
        return (parse_date(start, "Start date", required=False), parse_date(end, "End date", required=False))

    @staticmethod
    def _in(d, s, e):
        return (not s or d >= s) and (not e or d <= e)

    def run(self, name, start=None, end=None, category=None, vendor_id=None):
        """Run a report from REPORTS by name.  'As-of' reports use `end` as the as-of date."""
        if name not in REPORTS:
            raise ValueError(f"Unknown report '{name}'.")
        meth, kind, _ = REPORTS[name]
        start, end = start or None, end or None
        if kind == "asof":
            return getattr(self, meth)(end)
        if meth in EXPENSE_FREQ:
            return self.expense_periodic(EXPENSE_FREQ[meth], start, end, category, vendor_id)
        if meth in ("expense_breakdown", "expense_by_vendor"):
            return getattr(self, meth)(start, end, category, vendor_id)
        if meth in ("cash_daily", "cash_monthly"):
            return self.cash_flow_summary(start, end, "M" if meth == "cash_monthly" else "D")
        return getattr(self, meth)(start, end)

    # ---------------------------------------------------------------- A. outstanding
    def vendor_outstanding(self, as_of=None):
        """Bill-wise outstanding as of a date."""
        rows = []
        for b in self.a.bill_balances(as_of, exclude_future=True):
            if b["calculated_status"] == "Cancelled" or parse_stored(b["outstanding_balance"]) <= 0:
                continue
            rows.append({"vendor_id": b["vendor_id"], "vendor_name": b["vendor_name"], "bill_id": b["bill_id"],
                         "invoice_number": b["invoice_number"], "invoice_date": b["invoice_date"],
                         "due_date": b["due_date"], "invoice_total": parse_stored(b["invoice_total"]),
                         "amount_paid": parse_stored(b["amount_paid"]),
                         "outstanding_balance": parse_stored(b["outstanding_balance"]),
                         "status": b["calculated_status"], "days_overdue": b["days_overdue"]})
        cols = ["vendor_id", "vendor_name", "bill_id", "invoice_number", "invoice_date", "due_date", "invoice_total",
                "amount_paid", "outstanding_balance", "status", "days_overdue"]
        return _frame(rows, cols)

    def vendor_outstanding_summary(self, as_of=None):
        """Vendor-wise totals split by status (amounts are outstanding balances)."""
        acc = defaultdict(lambda: {"Unpaid": ZERO, "Partially Paid": ZERO, "Overdue": ZERO, "bills": 0, "name": ""})
        for b in self.a.bill_balances(as_of, exclude_future=True):
            bal = parse_stored(b["outstanding_balance"])
            if b["calculated_status"] in ("Cancelled", "Paid") or bal <= 0:
                continue
            e = acc[b["vendor_id"]]
            e["name"] = b["vendor_name"]
            e[b["calculated_status"]] += bal
            e["bills"] += 1
        rows = [{"vendor_id": k, "vendor_name": v["name"], "open_bills": v["bills"], "unpaid": v["Unpaid"],
                 "partially_paid": v["Partially Paid"], "overdue": v["Overdue"],
                 "total_outstanding": v["Unpaid"] + v["Partially Paid"] + v["Overdue"]} for k, v in acc.items()]
        rows.sort(key=lambda r: r["total_outstanding"], reverse=True)
        return _frame(rows, ["vendor_id", "vendor_name", "open_bills", "unpaid", "partially_paid", "overdue",
                             "total_outstanding"])

    # ---------------------------------------------------------------- B. aging
    def vendor_aging(self, as_of=None):
        """Bill-wise aging using balances AS OF the report date (not today)."""
        rows = []
        for b in self.a.bill_balances(as_of, exclude_future=True):
            bal = parse_stored(b["outstanding_balance"])
            if b["calculated_status"] == "Cancelled" or bal <= 0:
                continue
            rows.append({"vendor_id": b["vendor_id"], "vendor_name": b["vendor_name"], "bill_id": b["bill_id"],
                         "invoice_number": b["invoice_number"], "invoice_date": b["invoice_date"],
                         "due_date": b["due_date"], "outstanding_balance": bal, "days_overdue": b["days_overdue"],
                         "aging_bucket": bucket_for(b["days_overdue"])})
        return _frame(rows, ["vendor_id", "vendor_name", "bill_id", "invoice_number", "invoice_date", "due_date",
                             "outstanding_balance", "days_overdue", "aging_bucket"])

    def vendor_aging_summary(self, as_of=None):
        detail = self.vendor_aging(as_of)
        acc = defaultdict(lambda: {b: ZERO for b in BUCKETS})
        names = {}
        for r in detail.to_dict("records"):
            acc[r["vendor_id"]][r["aging_bucket"]] += r["outstanding_balance"]
            names[r["vendor_id"]] = r["vendor_name"]
        rows = []
        totals = {b: ZERO for b in BUCKETS}
        for vid, bk in acc.items():
            rows.append({"vendor_id": vid, "vendor_name": names[vid], **bk, "total": sum(bk.values(), ZERO)})
            for b in BUCKETS:
                totals[b] += bk[b]
        rows.sort(key=lambda r: r["total"], reverse=True)
        if rows:
            rows.append({"vendor_id": "", "vendor_name": "TOTAL", **totals, "total": sum(totals.values(), ZERO)})
        return _frame(rows, ["vendor_id", "vendor_name"] + BUCKETS + ["total"])

    # ---------------------------------------------------------------- C. expenses
    def _expense_bills(self, start, end, category=None, vendor_id=None):
        s, e = self._rng(start, end)
        names = {v[0]: v[1] for v in self.a.vendor_options(active_only=False)}
        for b in self.a.db.read("bills").to_dict("records"):
            if b["status"] == "Cancelled" or not self._in(b["invoice_date"], s, e):
                continue
            if category and b["category"] != category:
                continue
            if vendor_id and str(b["vendor_id"]) != str(vendor_id):
                continue
            yield b, names.get(str(b["vendor_id"]), "(missing)")

    def expense_breakdown(self, start=None, end=None, category=None, vendor_id=None):
        acc = defaultdict(lambda: ZERO)
        for b, _ in self._expense_bills(start, end, category, vendor_id):
            acc[b["category"]] += parse_stored(b["invoice_total"])
        rows = sorted(({"category": k, "invoice_total": v} for k, v in acc.items()),
                      key=lambda r: r["invoice_total"], reverse=True)
        return _frame(rows, ["category", "invoice_total"])

    def expense_by_vendor(self, start=None, end=None, category=None, vendor_id=None):
        acc, names = defaultdict(lambda: ZERO), {}
        for b, n in self._expense_bills(start, end, category, vendor_id):
            acc[str(b["vendor_id"])] += parse_stored(b["invoice_total"])
            names[str(b["vendor_id"])] = n
        rows = sorted(({"vendor_id": k, "vendor_name": names[k], "invoice_total": v} for k, v in acc.items()),
                      key=lambda r: r["invoice_total"], reverse=True)
        return _frame(rows, ["vendor_id", "vendor_name", "invoice_total"])

    def expense_periodic(self, freq="M", start=None, end=None, category=None, vendor_id=None):
        keyfn = {"D": lambda d: d, "M": month_key, "Q": quarter_key, "Y": year_key}[freq]
        acc = defaultdict(lambda: ZERO)
        for b, _ in self._expense_bills(start, end, category, vendor_id):
            acc[keyfn(b["invoice_date"])] += parse_stored(b["invoice_total"])
        return _frame([{"period": k, "invoice_total": acc[k]} for k in sorted(acc)], ["period", "invoice_total"])

    def monthly_expenses(self, start=None, end=None):
        out = self.expense_periodic("M", start, end)
        return out.rename(columns={"period": "month"})

    # ---------------------------------------------------------------- D. cash flow
    @staticmethod
    def _method(m):
        return "Card" if m == "Debit/Credit Card" else m

    def cash_flow(self, start=None, end=None):
        """Detail rows: sales receipts (+) and supplier payments (-).  These are cash movements,
        not accounting revenue or expense."""
        s, e = self._rng(start, end)
        rows = []
        for r in self.a.sales_records(s, e):
            for method, col in (("Cash", "cash_collected"), ("UPI", "upi_collected"),
                                ("Card", "card_collected"), ("Other", "other_collected")):
                if r[col] != 0:
                    rows.append({"date": r["business_date"], "type": "Sales receipt", "method": method,
                                 "amount": r[col]})
        for p in self.a.db.read("payments").to_dict("records"):
            if p["status"] == "Active" and self._in(p["payment_date"], s, e):
                rows.append({"date": p["payment_date"], "type": "Supplier payment",
                             "method": self._method(p["method"]), "amount": -parse_stored(p["amount"])})
        rows.sort(key=lambda r: (r["date"], r["type"]))
        return _frame(rows, ["date", "type", "method", "amount"])

    def cash_flow_by_method(self, start=None, end=None):
        acc = defaultdict(lambda: {"receipts": ZERO, "payments": ZERO})
        for r in self.cash_flow(start, end).to_dict("records"):
            if r["amount"] >= 0:
                acc[r["method"]]["receipts"] += r["amount"]
            else:
                acc[r["method"]]["payments"] += -r["amount"]
        rows = [{"method": m, "receipts": v["receipts"], "supplier_payments": v["payments"],
                 "net_movement": v["receipts"] - v["payments"]} for m, v in sorted(acc.items())]
        return _frame(rows, ["method", "receipts", "supplier_payments", "net_movement"])

    def _opening_cash(self):
        raw = self.a.cfg("opening_cash")
        if raw in (None, ""):
            return None, ""
        return parse_stored(raw), (parse_date(self.a.cfg("opening_cash_date"), required=False) or "")

    def cash_flow_summary(self, start=None, end=None, freq="D"):
        """Per day or month: receipts, supplier payments, net movement and (if an opening cash
        balance is configured) closing cash on hand.  Cash on hand covers the 'Cash' method only."""
        s, e = self._rng(start, end)
        keyfn = month_key if freq == "M" else (lambda d: d)
        acc = defaultdict(lambda: {"receipts": ZERO, "payments": ZERO, "cash": ZERO})
        for r in self.cash_flow(s, e).to_dict("records"):
            k = keyfn(r["date"])
            if r["amount"] >= 0:
                acc[k]["receipts"] += r["amount"]
            else:
                acc[k]["payments"] += -r["amount"]
            if r["method"] == "Cash":
                acc[k]["cash"] += r["amount"]
        opening, odate = self._opening_cash()
        running = None
        if opening is not None:
            before = self.cash_flow(odate or None, None)
            pre = ZERO
            for r in before.to_dict("records"):
                if s and r["date"] < s and r["method"] == "Cash":
                    pre += r["amount"]
            running = opening + pre
        rows = []
        for k in sorted(acc):
            v = acc[k]
            row = {"period": k, "sales_receipts": v["receipts"], "supplier_payments": v["payments"],
                   "net_movement": v["receipts"] - v["payments"], "cash_net": v["cash"]}
            if running is not None:
                running += v["cash"]
                row["cash_closing"] = running
            rows.append(row)
        cols = ["period", "sales_receipts", "supplier_payments", "net_movement", "cash_net"]
        if running is not None:
            cols.append("cash_closing")
        return _frame(rows, cols)

    # ---------------------------------------------------------------- E. sales
    def sales_collections(self, start=None, end=None):
        recon = {r["sales_id"]: r for r in self.recon.sales_rows(start, end)}
        rows = []
        for r in self.a.sales_records(start, end):
            rc = recon[r["sales_id"]]
            rows.append({"business_date": r["business_date"], "shift": r["shift"], "gross_sales": r["gross_sales"],
                         "discounts": r["discounts"], "refunds": r["refunds"], "adjustments": r["adjustments"],
                         "net_sales": r["net_after_adjustments"], "taxes": r["taxes_collected"],
                         "tips": r["tips_collected"], "service_charges": r["service_charges_collected"],
                         "cafe_revenue": r["cafe_revenue"], "cash": r["cash_collected"], "upi": r["upi_collected"],
                         "card": r["card_collected"], "other": r["other_collected"],
                         "credit_sales": r["credit_sales"], "credit_collections": r["credit_collections"],
                         "receipts": rc["receipts"], "reconciliation_difference": rc["difference"],
                         "cash_difference": rc["cash_difference"]})
        rows.sort(key=lambda r: (r["business_date"], r["shift"]))
        cols = ["business_date", "shift", "gross_sales", "discounts", "refunds", "adjustments", "net_sales", "taxes",
                "tips", "service_charges", "cafe_revenue", "cash", "upi", "card", "other", "credit_sales",
                "credit_collections", "receipts", "reconciliation_difference", "cash_difference"]
        return _frame(rows, cols)

    def reconciliation(self, start=None, end=None):
        cols = ["sales_id", "business_date", "shift", "net_sales", "taxes", "tips", "service_charges", "credit_sales",
                "credit_collections", "expected_receipts", "receipts", "difference", "tip_ledger_diff",
                "service_ledger_diff", "cash_difference", "status"]
        return _frame([{k: r[k] for k in cols} for r in self.recon.sales_rows(start, end)], cols)

    # ---------------------------------------------------------------- F. tips
    def tips_report(self, start=None, end=None):
        s, e = self._rng(start, end)
        rows = []
        for r in self.a.db.read("tips_ledger").to_dict("records"):
            if r["status"] != "Active" or not self._in(r["entry_date"], s, e):
                continue
            rows.append({"entry_id": r["entry_id"], "sales_id": r["sales_id"], "entry_type": r["entry_type"],
                         "entry_date": r["entry_date"], "collected": parse_stored(r["collected"]),
                         "distributed": parse_stored(r["distributed"]),
                         "retained_or_payable": parse_stored(r["retained_or_payable"]),
                         "distribution_date": r["distribution_date"], "notes": r["notes"]})
        return _frame(rows, ["entry_id", "sales_id", "entry_type", "entry_date", "collected", "distributed",
                             "retained_or_payable", "distribution_date", "notes"])

    def tips_summary(self, start=None, end=None):
        acc = defaultdict(lambda: {"collected": ZERO, "distributed": ZERO, "retained_or_payable": ZERO})
        for r in self.tips_report(start, end).to_dict("records"):
            for k in ("collected", "distributed", "retained_or_payable"):
                acc[r["entry_type"]][k] += r[k]
        rows = [{"entry_type": k, **v} for k, v in sorted(acc.items())]
        return _frame(rows, ["entry_type", "collected", "distributed", "retained_or_payable"])

    # ---------------------------------------------------------------- export
    @staticmethod
    def export(df, path, title="Report"):
        """path: a file path or a binary file object."""
        money_cols = [c for c in df.columns if len(df) and any(isinstance(v, Decimal) for v in df[c])]
        rows = [[(v.item() if hasattr(v, "item") else v) for v in row] for row in df.itertuples(index=False, name=None)]
        write_xlsx(path, title, list(df.columns), rows, money_columns=money_cols)
        return path
