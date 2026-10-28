"""Field validators and whole-dataset validation (used by the integrity checker,
by restore, and by the external import workflow)."""
import re
from collections import namedtuple, defaultdict
from decimal import Decimal

from cafebooks.utils.currency_utils import money, parse_stored, ZERO  # noqa: F401  (re-exported)
from cafebooks.utils.date_utils import parse_date, to_date
from .excel_io import clean_text
from .schemas import (SCHEMAS, PRIMARY_KEYS, PAYMENT_METHODS, ADJUSTMENT_TYPES, TIP_TYPES,
                      MONEY_COLUMNS, DATE_COLUMNS)

iso_date = parse_date
Issue = namedtuple("Issue", "severity table ref message")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^[0-9+\-() ]{5,20}$")
_GSTIN = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def required_text(value, field, max_len=200):
    text = " ".join(clean_text(value if value is not None else "").split())
    if not text:
        raise ValueError(f"{field} is required.")
    if len(text) > max_len:
        raise ValueError(f"{field} is too long (max {max_len}).")
    return text


def optional_text(value, max_len=2000):
    text = clean_text(value if value is not None else "").strip()
    if len(text) > max_len:
        raise ValueError(f"Text is too long (max {max_len} characters).")
    return text


def valid_email(value):
    v = optional_text(value, 200)
    if v and not _EMAIL.match(v):
        raise ValueError("Email address is not valid.")
    return v


def valid_phone(value):
    v = optional_text(value, 40)
    if v and not _PHONE.match(v):
        raise ValueError("Phone number may contain only digits, spaces, + - ( ).")
    return v


def valid_gstin(value):
    v = optional_text(value, 20).upper().replace(" ", "")
    if v and not _GSTIN.match(v):
        raise ValueError("GSTIN is not in the valid 15-character format.")
    return v


def norm_name(value):
    return " ".join(str(value or "").casefold().split())


def norm_invoice(value):
    """Duplicate-detection key: case-insensitive, alphanumerics only."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").casefold())


def is_active(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"false", "0", "no", "nan", "none", "", "inactive"}


def timeline_violation(rows, total):
    """rows: dicts with entry_type/amount/effective_date/allocated_at for ONE bill.
    Returns an error string if net allocated ever exceeds the bill total or goes negative."""
    ordered = sorted(rows, key=lambda r: (str(r["effective_date"]), 0 if r["entry_type"] == "ALLOCATE" else 1,
                                          str(r.get("allocated_at", ""))))
    running = ZERO
    for r in ordered:
        amt = parse_stored(r["amount"])
        running += amt if r["entry_type"] == "ALLOCATE" else -amt
        if running > total:
            return f"net allocations reach {running} which exceeds the bill total {total} on {r['effective_date']}"
        if running < 0:
            return f"net allocations become negative on {r['effective_date']}"
    return ""


def validate_dataset(frames):
    """Full structural + accounting validation.  Returns a list of Issue."""
    issues = []

    def err(table, ref, msg):
        issues.append(Issue("error", table, str(ref), msg))

    def warn(table, ref, msg):
        issues.append(Issue("warning", table, str(ref), msg))

    recs = {t: frames[t].to_dict("records") for t in SCHEMAS if t in frames}

    for table, rows in recs.items():
        pk = PRIMARY_KEYS[table]
        seen = set()
        for r in rows:
            key = str(r.get(pk, "")).strip()
            if not key:
                err(table, "?", f"Row with empty {pk}.")
            elif key in seen:
                err(table, key, f"Duplicate {pk}.")
            seen.add(key)
        for col in DATE_COLUMNS.get(table, []):
            for r in rows:
                v = r.get(col, "")
                if v != "":
                    try:
                        parse_date(v)
                    except ValueError:
                        err(table, r.get(pk), f"{col} '{v}' is not a valid date.")
        for col in MONEY_COLUMNS.get(table, []):
            for r in rows:
                v = r.get(col, "")
                if v == "":
                    continue
                d = parse_stored(v)
                if d < 0 and col != "cash_difference":
                    err(table, r.get(pk), f"{col} is negative.")

    vendors = {str(r["vendor_id"]): r for r in recs.get("vendors", [])}
    bills = {str(r["bill_id"]): r for r in recs.get("bills", [])}
    pays = {str(r["payment_id"]): r for r in recs.get("payments", [])}
    sales = {str(r["sales_id"]): r for r in recs.get("customer_sales", [])}

    for r in vendors.values():
        if not str(r["name"]).strip():
            err("vendors", r["vendor_id"], "Vendor name is empty.")
        try:
            if int(float(r["payment_terms_days"] or 0)) < 0:
                err("vendors", r["vendor_id"], "Payment terms are negative.")
        except (TypeError, ValueError):
            err("vendors", r["vendor_id"], "Payment terms are not a number.")

    inv_seen = {}
    for r in bills.values():
        bid = r["bill_id"]
        if str(r["vendor_id"]) not in vendors:
            err("bills", bid, f"References missing vendor {r['vendor_id']}.")
        total = parse_stored(r["subtotal"]) + parse_stored(r["tax_amount"]) + parse_stored(r["other_charges"])
        if total != parse_stored(r["invoice_total"]):
            err("bills", bid, f"Invoice total {parse_stored(r['invoice_total'])} does not equal subtotal+tax+charges {total}.")
        if r["invoice_date"] and r["due_date"] and str(r["due_date"]) < str(r["invoice_date"]):
            err("bills", bid, "Due date is before invoice date.")
        if str(r["status"]) not in ("Active", "Unpaid", "Cancelled", "Paid", "Partially Paid", "Overdue"):
            err("bills", bid, f"Unknown record status '{r['status']}'.")
        if str(r["status"]) != "Cancelled":
            key = (str(r["vendor_id"]), norm_invoice(r["invoice_number"]))
            if key in inv_seen:
                err("bills", bid, f"Duplicate supplier invoice (same as {inv_seen[key]}).")
            inv_seen[key] = bid

    for r in pays.values():
        pid = r["payment_id"]
        if str(r["vendor_id"]) not in vendors:
            err("payments", pid, f"References missing vendor {r['vendor_id']}.")
        if parse_stored(r["amount"]) <= 0:
            err("payments", pid, "Payment amount must be positive.")
        if str(r["method"]) not in PAYMENT_METHODS:
            err("payments", pid, f"Invalid payment method '{r['method']}'.")
        if str(r["status"]) not in ("Active", "Reversed"):
            err("payments", pid, f"Unknown payment status '{r['status']}'.")

    allocs = recs.get("payment_allocations", [])
    by_bill = defaultdict(list)
    by_pay_net = defaultdict(lambda: ZERO)
    alloc_by_id = {str(r["allocation_id"]): r for r in allocs}
    reversed_total = defaultdict(lambda: ZERO)
    for r in allocs:
        aid = r["allocation_id"]
        if str(r["payment_id"]) not in pays:
            err("payment_allocations", aid, f"References missing payment {r['payment_id']}.")
        if str(r["bill_id"]) not in bills:
            err("payment_allocations", aid, f"References missing bill {r['bill_id']}.")
        if r["entry_type"] not in ("ALLOCATE", "REVERSE"):
            err("payment_allocations", aid, f"Invalid entry_type '{r['entry_type']}'.")
            continue
        if parse_stored(r["amount"]) <= 0:
            err("payment_allocations", aid, "Allocation amount must be positive.")
        if not r["effective_date"]:
            err("payment_allocations", aid, "Missing effective date.")
        p, b = pays.get(str(r["payment_id"])), bills.get(str(r["bill_id"]))
        if p and b and str(p["vendor_id"]) != str(b["vendor_id"]):
            err("payment_allocations", aid, "Payment and bill belong to different vendors.")
        sign = 1 if r["entry_type"] == "ALLOCATE" else -1
        by_pay_net[str(r["payment_id"])] += sign * parse_stored(r["amount"])
        by_bill[str(r["bill_id"])].append(r)
        if r["entry_type"] == "REVERSE":
            orig = alloc_by_id.get(str(r["reversal_of"]))
            if not r["reversal_of"] or orig is None:
                err("payment_allocations", aid, "Reversal does not reference an existing allocation.")
            reversed_total[str(r["reversal_of"])] += parse_stored(r["amount"])
    for oid, tot in reversed_total.items():
        orig = alloc_by_id.get(oid)
        if orig and tot > parse_stored(orig["amount"]):
            err("payment_allocations", oid, "Reversals exceed the original allocation.")

    for pid, net in by_pay_net.items():
        p = pays.get(pid)
        if p and net > parse_stored(p["amount"]):
            err("payments", pid, f"Net allocations {net} exceed payment amount {parse_stored(p['amount'])}.")
        if net < 0:
            err("payments", pid, "Net allocations are negative.")
    for bid, rows in by_bill.items():
        b = bills.get(bid)
        if not b:
            continue
        msg = timeline_violation([x for x in rows if x["entry_type"] in ("ALLOCATE", "REVERSE")],
                                 parse_stored(b["invoice_total"]))
        if msg:
            err("bills", bid, f"Allocation problem: {msg}.")
        net = sum((parse_stored(x["amount"]) * (1 if x["entry_type"] == "ALLOCATE" else -1) for x in rows), ZERO)
        if str(b["status"]) == "Cancelled" and net != 0:
            err("bills", bid, "Cancelled bill still has net allocated payments.")

    active_keys = {}
    for r in sales.values():
        sid = r["sales_id"]
        if str(r["status"]) not in ("Active", "Voided"):
            err("customer_sales", sid, f"Unknown status '{r['status']}'.")
        net = parse_stored(r["gross_sales"]) - parse_stored(r["discounts"]) - parse_stored(r["refunds"])
        if net != parse_stored(r["net_sales"]):
            err("customer_sales", sid, f"Net sales {parse_stored(r['net_sales'])} != gross-discounts-refunds {net}.")
        if r["expected_cash"] != "" and r["actual_cash"] != "":
            diff = parse_stored(r["actual_cash"]) - parse_stored(r["expected_cash"])
            if diff != parse_stored(r["cash_difference"]):
                err("customer_sales", sid, "Cash difference does not equal actual-expected.")
        if str(r["status"]) == "Active":
            key = (str(r["business_date"]), str(r["shift"]))
            if key in active_keys:
                warn("customer_sales", sid, f"Same business date/shift as {active_keys[key]}.")
            active_keys[key] = sid

    for r in recs.get("sales_adjustments", []):
        aid = r["adjustment_id"]
        if str(r["sales_id"]) not in sales:
            err("sales_adjustments", aid, f"References missing sales record {r['sales_id']}.")
        if r["adjustment_type"] not in ADJUSTMENT_TYPES:
            err("sales_adjustments", aid, f"Invalid adjustment type '{r['adjustment_type']}'.")
        if parse_stored(r["amount"]) <= 0:
            err("sales_adjustments", aid, "Adjustment amount must be positive.")

    for r in recs.get("tips_ledger", []):
        eid = r["entry_id"]
        if r["sales_id"] and str(r["sales_id"]) not in sales:
            err("tips_ledger", eid, f"References missing sales record {r['sales_id']}.")
        if r["entry_type"] not in TIP_TYPES:
            err("tips_ledger", eid, f"Invalid entry type '{r['entry_type']}'.")
        if parse_stored(r["distributed"]) + parse_stored(r["retained_or_payable"]) != parse_stored(r["collected"]):
            err("tips_ledger", eid, "Distributed + retained/payable does not equal collected.")
    return issues
