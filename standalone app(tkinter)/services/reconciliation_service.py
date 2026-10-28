"""Reconciles sales totals, payment-method collections and the tips/service-charge ledger.

Identity checked for every active sales record (all amounts as entered on the record):

    cash + UPI + card + other collections
        = net sales [+ taxes unless gross already includes tax]
          + tips + service charges + credit collections - credit sales

Any non-zero difference, a tips/service-charge ledger total that differs from the
record, or a cash-count difference is flagged.  Nothing is corrected automatically.
"""
from collections import defaultdict
from decimal import Decimal

from utils.currency_utils import parse_stored, ZERO
from utils.date_utils import parse_date


class ReconciliationService:
    def __init__(self, accounting):
        self.a = accounting

    def _ledger_totals(self):
        totals = defaultdict(lambda: ZERO)
        for r in self.a.db.read("tips_ledger").to_dict("records"):
            if r["status"] == "Active" and r["sales_id"]:
                totals[(str(r["sales_id"]), r["entry_type"])] += parse_stored(r["collected"])
        return totals

    def sales_rows(self, start=None, end=None):
        start = parse_date(start, "Start date", required=False)
        end = parse_date(end, "End date", required=False)
        includes_tax = self.a.cfg("gross_sales_includes_tax")
        ledger = self._ledger_totals()
        rows = []
        for r in self.a.db.read("customer_sales").to_dict("records"):
            if r["status"] != "Active":
                continue
            if (start and r["business_date"] < start) or (end and r["business_date"] > end):
                continue
            g = {k: parse_stored(r[k]) for k in (
                "net_sales", "cash_collected", "upi_collected", "card_collected", "other_collected",
                "credit_sales", "credit_collections", "tips_collected", "service_charges_collected",
                "taxes_collected")}
            receipts = g["cash_collected"] + g["upi_collected"] + g["card_collected"] + g["other_collected"]
            expected = (g["net_sales"] + (ZERO if includes_tax else g["taxes_collected"]) + g["tips_collected"]
                        + g["service_charges_collected"] + g["credit_collections"] - g["credit_sales"])
            sid = str(r["sales_id"])
            tip_l = ledger.get((sid, "tip"), ZERO)
            sc_l = ledger.get((sid, "service_charge"), ZERO)
            cash_diff = parse_stored(r["cash_difference"]) if r["cash_difference"] != "" else None
            problems = []
            diff = receipts - expected
            if diff != 0:
                problems.append(f"Collections differ from sales by {diff}")
            if tip_l != g["tips_collected"]:
                problems.append(f"Tip ledger differs by {tip_l - g['tips_collected']}")
            if sc_l != g["service_charges_collected"]:
                problems.append(f"Service-charge ledger differs by {sc_l - g['service_charges_collected']}")
            if cash_diff:
                problems.append(f"Cash count differs by {cash_diff}")
            rows.append({"sales_id": sid, "business_date": r["business_date"], "shift": r["shift"],
                         "net_sales": g["net_sales"], "taxes": g["taxes_collected"], "tips": g["tips_collected"],
                         "service_charges": g["service_charges_collected"], "credit_sales": g["credit_sales"],
                         "credit_collections": g["credit_collections"], "expected_receipts": expected,
                         "receipts": receipts, "difference": diff, "tip_ledger": tip_l,
                         "tip_ledger_diff": tip_l - g["tips_collected"], "service_ledger": sc_l,
                         "service_ledger_diff": sc_l - g["service_charges_collected"],
                         "cash_difference": cash_diff if cash_diff is not None else ZERO,
                         "status": "OK" if not problems else "; ".join(problems)})
        return rows

    def orphan_ledger_entries(self):
        """Active ledger entries whose related sales record is missing or voided."""
        active = {str(r["sales_id"]) for r in self.a.db.read("customer_sales").to_dict("records")
                  if r["status"] == "Active"}
        return [r for r in self.a.db.read("tips_ledger").to_dict("records")
                if r["status"] == "Active" and r["sales_id"] and str(r["sales_id"]) not in active]

    def unlinked_ledger_total(self):
        return sum((parse_stored(r["collected"]) for r in self.a.db.read("tips_ledger").to_dict("records")
                    if r["status"] == "Active" and not r["sales_id"]), ZERO)
