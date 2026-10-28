"""All accounting rules live here (never in GUI handlers).

Ledger design
  * payment_allocations is APPEND-ONLY.  ALLOCATE rows add, REVERSE rows subtract.
    Nothing is ever flipped or deleted, so any historical date can be reconstructed.
  * bill outstanding = invoice total - net allocations (effective_date <= as-of date).
  * every material change writes an audit row (actor, request id, before/after JSON).
  * every create operation accepts an optional request_id; replaying the same request id
    returns the original record instead of creating a duplicate.
"""
import getpass
import json
import os
import re
import shutil
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pandas as pd

from cafebooks.database.ids import next_ids, append_rows
from cafebooks.database.schemas import (PAYMENT_METHODS, ADJUSTMENT_TYPES, TIP_TYPES, ID_PREFIX)
from cafebooks.database.validation import (required_text, optional_text, valid_email, valid_phone, valid_gstin,
                                 norm_name, norm_invoice, is_active, timeline_violation)
from cafebooks.utils.actor import current_actor
from cafebooks.utils.config import DEFAULTS, SETTING_KEYS, validate_config, encode_value, config_from_settings
from cafebooks.utils.currency_utils import money, parse_stored, ZERO
from cafebooks.utils.date_utils import parse_date, to_date, today_iso, now_local, set_timezone

_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


class DuplicateError(ValueError):
    """A record that must be unique already exists."""


class DuplicateWarning(ValueError):
    """Possible duplicate: the caller may confirm and retry with allow_duplicate=True."""


class AttachmentMissing(ValueError):
    """Linked file not found: caller may confirm and retry with allow_missing_attachment=True."""


SALES_AMOUNT_FIELDS = ["gross_sales", "discounts", "refunds", "cash_collected", "upi_collected", "card_collected",
                       "other_collected", "credit_sales", "credit_collections", "tips_collected",
                       "service_charges_collected", "taxes_collected"]
SALES_EDITABLE = set(SALES_AMOUNT_FIELDS) | {"business_date", "shift", "expected_cash", "actual_cash", "notes"}
BILL_EDITABLE = {"invoice_number", "invoice_date", "due_date", "category", "description", "attachment_path",
                 "notes", "subtotal", "tax_amount", "other_charges"}
VENDOR_EDITABLE = {"name", "contact_person", "phone", "email", "address", "category", "gstin",
                   "payment_terms_days", "default_payment_method", "notes"}


def _j(obj):
    return json.dumps(obj, default=str, sort_keys=True, ensure_ascii=False)


class AccountingService:
    def __init__(self, db, config=None, actor=None, local_files=False, attachment_dir=None):
        self.db = db
        self.config = config if config is not None else dict(DEFAULTS)
        self.actor = actor or self._os_user()
        # local_files: the server runs on the user's own PC (desktop app), so typed file paths
        # can be checked and opened.  On a shared web server they are just references.
        self.local_files = local_files
        self.attachment_dir = attachment_dir
        self._bal_cache = {}

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def _os_user():
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001
            return "operator"

    def cfg(self, key):
        return self.config.get(key, DEFAULTS[key])

    @staticmethod
    def _now():
        return now_local().isoformat(timespec="microseconds")

    @staticmethod
    def _stamp():
        return now_local().isoformat(timespec="seconds")

    def current_actor(self):
        return current_actor() or self.actor

    def _audit(self, f, entity_type, entity_id, action, details="", before=None, after=None, request_id=""):
        row = {"audit_id": next_ids(f["audit_log"]["audit_id"], ID_PREFIX["audit_log"])[0],
               "timestamp": self._now(), "actor": self.current_actor(), "request_id": request_id or "",
               "entity_type": entity_type, "entity_id": str(entity_id), "action": action,
               "details": str(details)[:2000], "before": _j(before) if before is not None else "",
               "after": _j(after) if after is not None else ""}
        f["audit_log"] = append_rows(f["audit_log"], [row])

    @staticmethod
    def _seen(f, request_id, entity_type):
        if not request_id:
            return None
        a = f["audit_log"]
        hit = a[(a["request_id"] == request_id) & (a["entity_type"] == entity_type) & (a["action"] == "create")]
        return str(hit["entity_id"].iloc[0]) if not hit.empty else None

    @staticmethod
    def _index(df, col, value, what):
        hit = df.index[df[col].astype(str) == str(value)]
        if len(hit) == 0:
            raise ValueError(f"{what} not found.")
        return hit[0]

    def _check_category(self, value, key, label, legacy=None):
        value = required_text(value, label)
        if value not in self.cfg(key) and value != legacy:
            raise ValueError(f"{label} '{value}' is not in the configured list (Settings).")
        return value

    # ================================================================== VENDORS
    def _vendor_fields(self, kw, existing=None):
        out = {}
        if "name" in kw:
            out["name"] = required_text(kw["name"], "Vendor name")
        if "category" in kw:
            out["category"] = self._check_category(kw["category"], "vendor_categories", "Vendor category",
                                                   existing["category"] if existing is not None else None)
        for key in ("contact_person", "address", "notes"):
            if key in kw:
                out[key] = optional_text(kw[key])
        if "phone" in kw:
            out["phone"] = valid_phone(kw["phone"])
        if "email" in kw:
            out["email"] = valid_email(kw["email"])
        if "gstin" in kw:
            out["gstin"] = valid_gstin(kw["gstin"])
        if "payment_terms_days" in kw:
            raw = kw["payment_terms_days"]
            try:
                terms = int(str(raw).strip() or 0)
            except ValueError:
                raise ValueError("Payment terms must be a whole number of days.") from None
            if terms < 0 or terms > 365:
                raise ValueError("Payment terms must be between 0 and 365 days.")
            out["payment_terms_days"] = terms
        if "default_payment_method" in kw:
            m = str(kw["default_payment_method"] or "").strip()
            if m and m not in PAYMENT_METHODS:
                raise ValueError("Invalid default payment method.")
            out["default_payment_method"] = m
        return out

    def add_vendor(self, name, category, request_id="", allow_duplicate=False, **kw):
        unknown = set(kw) - VENDOR_EDITABLE
        if unknown:
            raise ValueError(f"Unknown vendor fields: {sorted(unknown)}")
        kw.setdefault("payment_terms_days", self.cfg("default_payment_terms_days"))
        fields = self._vendor_fields({**kw, "name": name, "category": category})
        with self.db.transaction(["vendors", "audit_log"], "add_vendor") as f:
            seen = self._seen(f, request_id, "vendor")
            if seen:
                return seen
            v = f["vendors"]
            for _, r in v.iterrows():
                if norm_name(r["name"]) == norm_name(fields["name"]) and not allow_duplicate:
                    state = "active" if is_active(r["active"]) else "inactive (reactivate it instead)"
                    raise DuplicateWarning(f"A {state} vendor named '{r['name']}' already exists. "
                                           "Confirm to create a separate vendor with the same name.")
            vid = next_ids(v["vendor_id"], ID_PREFIX["vendors"])[0]
            now = self._stamp()
            row = {"vendor_id": vid, "contact_person": "", "phone": "", "email": "", "address": "", "gstin": "",
                   "default_payment_method": "", "notes": "", **fields, "active": True,
                   "created_at": now, "modified_at": now}
            f["vendors"] = append_rows(v, [row])
            self._audit(f, "vendor", vid, "create", fields["name"], after=row, request_id=request_id)
        return vid

    def update_vendor(self, vendor_id, **kw):
        unknown = set(kw) - VENDOR_EDITABLE - {"active"}
        if unknown:
            raise ValueError(f"Fields cannot be edited: {sorted(unknown)}")
        active = kw.pop("active", None)
        with self.db.transaction(["vendors", "audit_log"], "update_vendor") as f:
            v = f["vendors"]
            i = self._index(v, "vendor_id", vendor_id, "Vendor")
            current = v.loc[i]
            fields = self._vendor_fields(kw, current)
            if active is not None:
                fields["active"] = bool(active)
            if "name" in fields and norm_name(fields["name"]) != norm_name(current["name"]):
                for j, r in v.iterrows():
                    if j != i and norm_name(r["name"]) == norm_name(fields["name"]):
                        raise DuplicateWarning(f"Another vendor named '{r['name']}' already exists.")
            before = {k: current[k] for k in fields if str(current[k]) != str(fields[k])}
            after = {k: fields[k] for k in before}
            if not before:
                return
            for k, val in after.items():
                v.at[i, k] = val
            v.at[i, "modified_at"] = self._stamp()
            self._audit(f, "vendor", vendor_id, "update", ", ".join(sorted(after)), before=before, after=after)

    def deactivate_vendor(self, vendor_id):
        self.update_vendor(vendor_id, active=False)

    def reactivate_vendor(self, vendor_id):
        self.update_vendor(vendor_id, active=True)

    def delete_vendor(self, vendor_id):
        """Permanent delete only when no bill or payment references the vendor."""
        with self.db.transaction(["vendors", "audit_log"], "delete_vendor") as f:
            v = f["vendors"]
            i = self._index(v, "vendor_id", vendor_id, "Vendor")
            bills, pays = self.db.read("bills"), self.db.read("payments")
            if (bills["vendor_id"].astype(str) == str(vendor_id)).any() or \
                    (pays["vendor_id"].astype(str) == str(vendor_id)).any():
                raise ValueError("This vendor has bills or payments in the historical database and cannot be "
                                 "permanently deleted. Use Deactivate instead so the accounting history is preserved.")
            before = v.loc[i].to_dict()
            f["vendors"] = v.drop(index=i).reset_index(drop=True)
            self._audit(f, "vendor", vendor_id, "delete", before["name"], before=before)

    def vendor_options(self, active_only=True):
        v = self.db.read("vendors")
        return [(r["vendor_id"], r["name"]) for r in v.to_dict("records") if not active_only or is_active(r["active"])]

    # ================================================================== BILLS
    def _attachment(self, path, allow_missing):
        """A link (https://...) or, in the desktop app, a file path on this PC."""
        path = optional_text(path, 500)
        if not path:
            return ""
        if _URL.match(path) or not self.local_files:
            return path
        if not os.path.exists(path):
            if allow_missing:
                return path
            raise AttachmentMissing(f"Invoice attachment not found: {path}")
        if self.cfg("copy_attachments"):
            dest_dir = self._attachment_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{now_local().strftime('%Y%m%d%H%M%S%f')}_{os.path.basename(path)}"
            shutil.copy2(path, dest)
            return str(dest)
        return path

    def _attachment_dir(self):
        return Path(self.attachment_dir) if self.attachment_dir else Path("attachments").resolve()

    @staticmethod
    def _find_invoice_duplicate(bills, vendor_id, invoice_number, exclude_id=None):
        key = norm_invoice(invoice_number)
        for r in bills.to_dict("records"):
            if str(r["vendor_id"]) == str(vendor_id) and str(r["status"]) != "Cancelled" \
                    and str(r["bill_id"]) != str(exclude_id) and norm_invoice(r["invoice_number"]) == key:
                return r
        return None

    def check_duplicate_invoice(self, vendor_id, invoice_number, exclude_bill_id=None):
        """Pre-save warning helper for the UI.  Returns the clashing bill id or None."""
        hit = self._find_invoice_duplicate(self.db.read("bills"), vendor_id, invoice_number, exclude_bill_id)
        return str(hit["bill_id"]) if hit else None

    def add_bill(self, vendor_id, invoice_number, invoice_date, due_date, category, subtotal, tax_amount=0,
                 other_charges=0, description="", attachment_path="", notes="", request_id="",
                 invoice_total=None, allow_missing_attachment=False):
        invoice_number = required_text(invoice_number, "Supplier invoice number", 60)
        if not norm_invoice(invoice_number):
            raise ValueError("Supplier invoice number must contain letters or digits.")
        inv, due = parse_date(invoice_date, "Invoice date"), parse_date(due_date, "Due date")
        if due < inv:
            raise ValueError("Due date cannot be before invoice date.")
        sub = money(subtotal, "Subtotal")
        tax, other = money(tax_amount, "Tax amount"), money(other_charges, "Other charges")
        total = sub + tax + other
        if total <= 0:
            raise ValueError("Invoice total must be greater than zero.")
        if invoice_total not in (None, ""):
            if money(invoice_total, "Invoice total") != total:
                raise ValueError(f"Invoice total {money(invoice_total, 'Invoice total')} does not match "
                                 f"subtotal + tax + other charges = {total}.")
        category = self._check_category(category, "expense_categories", "Expense category")
        attachment = self._attachment(attachment_path, allow_missing_attachment)
        with self.db.transaction(["bills", "audit_log"], "add_bill") as f:
            seen = self._seen(f, request_id, "bill")
            if seen:
                return seen
            vendors = self.db.read("vendors")
            vrow = vendors[vendors["vendor_id"].astype(str) == str(vendor_id)]
            if vrow.empty:
                raise ValueError("Vendor not found.")
            if not is_active(vrow["active"].iloc[0]):
                raise ValueError("Vendor is inactive. Reactivate it before adding new bills.")
            clash = self._find_invoice_duplicate(f["bills"], vendor_id, invoice_number)
            if clash:
                raise DuplicateError(f"Duplicate supplier invoice: this vendor already has invoice "
                                     f"'{clash['invoice_number']}' (bill {clash['bill_id']}).")
            bid = next_ids(f["bills"]["bill_id"], ID_PREFIX["bills"])[0]
            now = self._stamp()
            row = {"bill_id": bid, "vendor_id": str(vendor_id), "invoice_number": invoice_number,
                   "invoice_date": inv, "due_date": due, "category": category,
                   "description": optional_text(description), "subtotal": sub, "tax_amount": tax,
                   "other_charges": other, "invoice_total": total, "attachment_path": attachment,
                   "notes": optional_text(notes), "status": "Active", "created_at": now, "modified_at": now}
            f["bills"] = append_rows(f["bills"], [row])
            self._audit(f, "bill", bid, "create", f"Invoice {invoice_number}; total {total}", after=row,
                        request_id=request_id)
        return bid

    def _net_for_bill(self, bill_id):
        alloc = self.db.read("payment_allocations")
        net = ZERO
        for r in alloc[alloc["bill_id"].astype(str) == str(bill_id)].to_dict("records"):
            amt = parse_stored(r["amount"])
            net += amt if r["entry_type"] == "ALLOCATE" else -amt
        return net

    def update_bill(self, bill_id, reason="", allow_missing_attachment=False, **kw):
        if "invoice_total" in kw:
            raise ValueError("Invoice total is calculated from subtotal, tax and other charges.")
        unknown = set(kw) - BILL_EDITABLE
        if unknown:
            raise ValueError(f"Fields cannot be edited: {sorted(unknown)}")
        with self.db.transaction(["bills", "audit_log"], "update_bill") as f:
            b = f["bills"]
            i = self._index(b, "bill_id", bill_id, "Bill")
            cur = b.loc[i].to_dict()
            if cur["status"] == "Cancelled":
                raise ValueError("A cancelled bill cannot be edited.")
            new = dict(cur)
            for k in ("subtotal", "tax_amount", "other_charges"):
                if k in kw:
                    new[k] = money(kw[k], k.replace("_", " ").title())
            if "invoice_number" in kw:
                new["invoice_number"] = required_text(kw["invoice_number"], "Supplier invoice number", 60)
                if not norm_invoice(new["invoice_number"]):
                    raise ValueError("Supplier invoice number must contain letters or digits.")
            if "invoice_date" in kw:
                new["invoice_date"] = parse_date(kw["invoice_date"], "Invoice date")
            if "due_date" in kw:
                new["due_date"] = parse_date(kw["due_date"], "Due date")
            if "category" in kw:
                new["category"] = self._check_category(kw["category"], "expense_categories", "Expense category",
                                                       cur["category"])
            for k in ("description", "notes"):
                if k in kw:
                    new[k] = optional_text(kw[k])
            if "attachment_path" in kw and kw["attachment_path"] != cur["attachment_path"]:
                new["attachment_path"] = self._attachment(kw["attachment_path"], allow_missing_attachment)
            if new["due_date"] < new["invoice_date"]:
                raise ValueError("Due date cannot be before invoice date.")
            new_total = parse_stored(new["subtotal"]) + parse_stored(new["tax_amount"]) + parse_stored(new["other_charges"])
            if new_total != parse_stored(cur["invoice_total"]):
                net = self._net_for_bill(bill_id)
                if net != 0:
                    raise ValueError("This bill has payments allocated, so its amounts cannot be edited. "
                                     "Reverse the allocations first (Supplier Payments).")
                if new_total <= 0:
                    raise ValueError("Invoice total must be greater than zero.")
            new["invoice_total"] = new_total
            if norm_invoice(new["invoice_number"]) != norm_invoice(cur["invoice_number"]):
                clash = self._find_invoice_duplicate(b, cur["vendor_id"], new["invoice_number"], bill_id)
                if clash:
                    raise DuplicateError(f"Duplicate supplier invoice: bill {clash['bill_id']} already uses it.")
            before = {k: cur[k] for k in new if str(cur[k]) != str(new[k])}
            after = {k: new[k] for k in before}
            if not before:
                return
            for k, v in after.items():
                b.at[i, k] = v
            b.at[i, "modified_at"] = self._stamp()
            self._audit(f, "bill", bill_id, "update", reason or ", ".join(sorted(after)), before=before, after=after)

    def cancel_bill(self, bill_id, reason):
        reason = required_text(reason, "Cancellation reason", 500)
        with self.db.transaction(["bills", "audit_log"], "cancel_bill") as f:
            b = f["bills"]
            i = self._index(b, "bill_id", bill_id, "Bill")
            if b.at[i, "status"] == "Cancelled":
                raise ValueError("Bill is already cancelled.")
            net = self._net_for_bill(bill_id)
            if net != 0:
                raise ValueError(f"This bill has {net} of payments allocated. Reverse those allocations "
                                 "(Supplier Payments) before cancelling it; cancelling would hide paid money.")
            b.at[i, "status"] = "Cancelled"
            b.at[i, "modified_at"] = self._stamp()
            self._audit(f, "bill", bill_id, "cancel", reason, before={"status": "Active"}, after={"status": "Cancelled"})

    # ------------------------------------------------------------------ balances
    def _net_by(self, key_col, as_of_iso=None):
        alloc = self.db.read("payment_allocations")
        net = defaultdict(lambda: ZERO)
        for k, et, amt, eff in zip(alloc[key_col], alloc["entry_type"], alloc["amount"], alloc["effective_date"]):
            if as_of_iso is not None and eff and eff > as_of_iso:
                continue
            a = parse_stored(amt)
            net[str(k)] += a if et == "ALLOCATE" else -a
        return net

    def bill_balances(self, as_of=None, exclude_future=False):
        """Per-bill amounts as of a date (default today).  Paid = net allocations whose
        effective date is on or before the as-of date; never uses today's date implicitly
        for historical questions."""
        as_of_iso = parse_date(as_of or today_iso(), "As-of date")
        key = (self.db.version, as_of_iso, exclude_future)
        if key in self._bal_cache:
            return [dict(r) for r in self._bal_cache[key]]
        if len(self._bal_cache) > 8:
            self._bal_cache.clear()
        bills = self.db.read("bills").to_dict("records")
        vendors = {str(r["vendor_id"]): r["name"] for r in self.db.read("vendors").to_dict("records")}
        net = self._net_by("bill_id", as_of_iso)
        as_of_d = to_date(as_of_iso)
        out = []
        for b in bills:
            if exclude_future and b["invoice_date"] and b["invoice_date"] > as_of_iso:
                continue
            total = parse_stored(b["invoice_total"])
            paid = net.get(str(b["bill_id"]), ZERO)
            cancelled = b["status"] == "Cancelled"
            balance = ZERO if cancelled else max(ZERO, total - paid)
            due = b["due_date"]
            overdue = bool(due) and due < as_of_iso and balance > 0
            if cancelled:
                status = "Cancelled"
            elif balance == 0:
                status = "Paid"
            elif overdue:
                status = "Overdue"
            elif paid > 0:
                status = "Partially Paid"
            else:
                status = "Unpaid"
            out.append({**b, "vendor_name": vendors.get(str(b["vendor_id"]), "(missing)"),
                        "amount_paid": str(paid), "outstanding_balance": str(balance),
                        "calculated_status": status,
                        "days_overdue": (as_of_d - to_date(due)).days if overdue else 0})
        self._bal_cache[key] = out
        return [dict(r) for r in out]

    def vendor_outstanding_total(self, vendor_id, as_of=None):
        return sum((parse_stored(b["outstanding_balance"]) for b in self.bill_balances(as_of)
                    if str(b["vendor_id"]) == str(vendor_id)), ZERO)

    # ================================================================== PAYMENTS
    @staticmethod
    def _norm_allocations(allocations, payment_amount):
        norm = []
        for bid, v in (allocations or []):
            norm.append((str(bid), money(v, "Allocation amount", allow_zero=False)))
        if len({b for b, _ in norm}) != len(norm):
            raise ValueError("A bill cannot be listed twice in the same payment.")
        if sum((v for _, v in norm), ZERO) > payment_amount:
            raise ValueError("Allocations exceed the payment amount.")
        return norm

    def _apply_allocations(self, f, payment_id, vendor_id, norm, effective_date, now):
        if not norm:
            return ZERO
        alloc = f["payment_allocations"]
        ids = [b for b, _ in norm]
        bills_df = self.db.read("bills")
        bills = {str(r["bill_id"]): r for r in bills_df[bills_df["bill_id"].astype(str).isin(ids)].to_dict("records")}
        per_bill = defaultdict(list)
        for r in alloc[alloc["bill_id"].astype(str).isin(ids)].to_dict("records"):
            per_bill[str(r["bill_id"])].append(r)
        new_ids = next_ids(alloc["allocation_id"], ID_PREFIX["payment_allocations"], len(norm))
        rows = []
        for (bid, amt), aid in zip(norm, new_ids):
            b = bills.get(bid)
            if b is None:
                raise ValueError("Bill not found.")
            if str(b["vendor_id"]) != str(vendor_id):
                raise ValueError("All allocations must belong to the payment's vendor.")
            if b["status"] == "Cancelled":
                raise ValueError(f"Cannot allocate to cancelled bill {b['invoice_number']}.")
            total = parse_stored(b["invoice_total"])
            net = sum((parse_stored(r["amount"]) * (1 if r["entry_type"] == "ALLOCATE" else -1)
                       for r in per_bill[bid]), ZERO)
            if amt > total - net:
                raise ValueError(f"Allocation {amt} exceeds the outstanding balance {total - net} "
                                 f"of invoice {b['invoice_number']}.")
            row = {"allocation_id": aid, "payment_id": payment_id, "bill_id": bid, "entry_type": "ALLOCATE",
                   "amount": amt, "effective_date": effective_date, "allocated_at": now, "reversal_of": "",
                   "status": "Active"}
            problem = timeline_violation(per_bill[bid] + [row], total)
            if problem:
                raise ValueError(f"Allocation to {b['invoice_number']} rejected: {problem}.")
            rows.append(row)
        f["payment_allocations"] = append_rows(alloc, rows)
        return sum((r["amount"] for r in rows), ZERO)

    def record_payment(self, vendor_id, payment_date, amount, method, allocations=None, reference="", notes="",
                       request_id=""):
        pdate = parse_date(payment_date, "Payment date")
        amt = money(amount, "Payment amount", allow_zero=False)
        if method not in PAYMENT_METHODS:
            raise ValueError("Invalid payment method.")
        norm = self._norm_allocations(allocations, amt)
        with self.db.transaction(["payments", "payment_allocations", "audit_log"], "record_payment") as f:
            seen = self._seen(f, request_id, "payment")
            if seen:
                return seen
            vendors = self.db.read("vendors")
            if not (vendors["vendor_id"].astype(str) == str(vendor_id)).any():
                raise ValueError("Vendor not found.")
            pid = next_ids(f["payments"]["payment_id"], ID_PREFIX["payments"])[0]
            now = self._now()
            allocated = self._apply_allocations(f, pid, vendor_id, norm, pdate, now)
            row = {"payment_id": pid, "vendor_id": str(vendor_id), "payment_date": pdate, "amount": amt,
                   "method": method, "reference": optional_text(reference, 200), "notes": optional_text(notes),
                   "status": "Active", "reversal_reason": "", "reversed_at": "",
                   "created_at": self._stamp(), "modified_at": self._stamp()}
            f["payments"] = append_rows(f["payments"], [row])
            self._audit(f, "payment", pid, "create", f"Amount {amt}; allocated {allocated}; "
                        f"unallocated {amt - allocated}", after=row, request_id=request_id)
        return pid

    def allocate_payment(self, payment_id, allocations, allocation_date=None, request_id=""):
        """Allocate (part of) the unallocated balance of an existing payment."""
        with self.db.transaction(["payments", "payment_allocations", "audit_log"], "allocate_payment") as f:
            seen = self._seen(f, request_id, "allocation")
            if seen:
                return seen
            p = f["payments"]
            i = self._index(p, "payment_id", payment_id, "Payment")
            row = p.loc[i]
            if row["status"] != "Active":
                raise ValueError("A reversed payment cannot be allocated.")
            adate = parse_date(allocation_date or today_iso(), "Allocation date")
            if adate < row["payment_date"]:
                raise ValueError("Allocation date cannot be before the payment date.")
            net = self._net_for_payment(f["payment_allocations"], payment_id)
            free = parse_stored(row["amount"]) - net
            norm = self._norm_allocations(allocations, free)
            if not norm:
                raise ValueError("Nothing to allocate.")
            done = self._apply_allocations(f, payment_id, row["vendor_id"], norm, adate, self._now())
            self._audit(f, "allocation", payment_id, "create", f"Allocated {done} of unallocated {free}",
                        after={"payment_id": payment_id, "allocations": [(b, str(a)) for b, a in norm]},
                        request_id=request_id)
        return payment_id

    @staticmethod
    def _net_for_payment(alloc, payment_id):
        net = ZERO
        for r in alloc[alloc["payment_id"].astype(str) == str(payment_id)].to_dict("records"):
            amt = parse_stored(r["amount"])
            net += amt if r["entry_type"] == "ALLOCATE" else -amt
        return net

    def reverse_allocation(self, allocation_id, reason, amount=None, reversal_date=None):
        """Undo (part of) one allocation by appending a REVERSE row (history is kept)."""
        reason = required_text(reason, "Reason", 500)
        with self.db.transaction(["payment_allocations", "audit_log"], "reverse_allocation") as f:
            alloc = f["payment_allocations"]
            i = self._index(alloc, "allocation_id", allocation_id, "Allocation")
            orig = alloc.loc[i]
            if orig["entry_type"] != "ALLOCATE":
                raise ValueError("Only an original allocation can be reversed.")
            already = sum((parse_stored(r["amount"]) for r in alloc.to_dict("records")
                           if r["entry_type"] == "REVERSE" and str(r["reversal_of"]) == str(allocation_id)), ZERO)
            remaining = parse_stored(orig["amount"]) - already
            amt = remaining if amount in (None, "") else money(amount, "Reversal amount", allow_zero=False)
            if amt > remaining or remaining <= 0:
                raise ValueError(f"Only {remaining} of this allocation can still be reversed.")
            rdate = parse_date(reversal_date or today_iso(), "Reversal date")
            if rdate < orig["effective_date"]:
                raise ValueError("Reversal date cannot be before the allocation date.")
            rid = next_ids(alloc["allocation_id"], ID_PREFIX["payment_allocations"])[0]
            row = {"allocation_id": rid, "payment_id": orig["payment_id"], "bill_id": orig["bill_id"],
                   "entry_type": "REVERSE", "amount": amt, "effective_date": rdate, "allocated_at": self._now(),
                   "reversal_of": str(allocation_id), "status": "Reversal"}
            f["payment_allocations"] = append_rows(alloc, [row])
            self._audit(f, "allocation", rid, "reverse", reason, after=row)
        return rid

    def reverse_payment(self, payment_id, reason, reversal_date=None):
        """Void a payment: append REVERSE rows for everything still allocated; mark it Reversed."""
        reason = required_text(reason, "Reversal reason", 500)
        with self.db.transaction(["payments", "payment_allocations", "audit_log"], "reverse_payment") as f:
            p = f["payments"]
            i = self._index(p, "payment_id", payment_id, "Payment")
            if p.at[i, "status"] == "Reversed":
                raise ValueError("Payment is already reversed.")
            rdate = parse_date(reversal_date or today_iso(), "Reversal date")
            if rdate < p.at[i, "payment_date"]:
                raise ValueError("Reversal date cannot be before the payment date.")
            alloc = f["payment_allocations"]
            mine = [r for r in alloc.to_dict("records") if str(r["payment_id"]) == str(payment_id)]
            reversed_by = defaultdict(lambda: ZERO)
            for r in mine:
                if r["entry_type"] == "REVERSE":
                    reversed_by[str(r["reversal_of"])] += parse_stored(r["amount"])
            ids = next_ids(alloc["allocation_id"], ID_PREFIX["payment_allocations"], len(mine) + 1)
            rows = []
            for r in mine:
                if r["entry_type"] != "ALLOCATE":
                    continue
                remaining = parse_stored(r["amount"]) - reversed_by[str(r["allocation_id"])]
                if remaining > 0:
                    rows.append({"allocation_id": ids[len(rows)], "payment_id": str(payment_id),
                                 "bill_id": r["bill_id"], "entry_type": "REVERSE", "amount": remaining,
                                 "effective_date": max(rdate, r["effective_date"]), "allocated_at": self._now(),
                                 "reversal_of": r["allocation_id"], "status": "Reversal"})
            before = {"status": "Active"}
            p.at[i, "status"] = "Reversed"
            p.at[i, "reversal_reason"] = reason
            p.at[i, "reversed_at"] = self._stamp()
            p.at[i, "modified_at"] = self._stamp()
            f["payment_allocations"] = append_rows(alloc, rows)
            self._audit(f, "payment", payment_id, "reverse", reason, before=before,
                        after={"status": "Reversed", "allocations_reversed": len(rows)})

    def update_payment_details(self, payment_id, reference=None, notes=None):
        """Non-financial correction (reference/notes) with audit; amounts are never editable."""
        with self.db.transaction(["payments", "audit_log"], "update_payment_details") as f:
            p = f["payments"]
            i = self._index(p, "payment_id", payment_id, "Payment")
            before, after = {}, {}
            for key, val, mx in (("reference", reference, 200), ("notes", notes, 2000)):
                if val is not None and optional_text(val, mx) != p.at[i, key]:
                    before[key], after[key] = p.at[i, key], optional_text(val, mx)
                    p.at[i, key] = after[key]
            if after:
                p.at[i, "modified_at"] = self._stamp()
                self._audit(f, "payment", payment_id, "update", "details", before=before, after=after)

    def payment_rows(self):
        pays = self.db.read("payments").to_dict("records")
        net = self._net_by("payment_id")
        names = {v[0]: v[1] for v in self.vendor_options(active_only=False)}
        out = []
        for p in pays:
            allocated = net.get(str(p["payment_id"]), ZERO)
            amount = parse_stored(p["amount"])
            out.append({**p, "vendor_name": names.get(str(p["vendor_id"]), "(missing)"),
                        "allocated": str(allocated),
                        "unallocated": str(ZERO if p["status"] != "Active" else amount - allocated)})
        return out

    def unallocated_total(self, as_of=None):
        as_of_iso = parse_date(as_of or today_iso())
        net = self._net_by("payment_id", as_of_iso)
        total = ZERO
        for p in self.db.read("payments").to_dict("records"):
            if p["status"] == "Active" and p["payment_date"] <= as_of_iso:
                total += parse_stored(p["amount"]) - net.get(str(p["payment_id"]), ZERO)
        return total

    # ================================================================== SALES
    def _sales_duplicate(self, sales, date, shift, exclude=None):
        shift_n = norm_name(shift)
        multi = self.cfg("allow_multiple_shifts")
        for r in sales.to_dict("records"):
            if r["status"] == "Active" and r["business_date"] == date and str(r["sales_id"]) != str(exclude):
                if not multi or norm_name(r["shift"]) == shift_n:
                    return r
        return None

    def _sales_values(self, d):
        """d: raw dict of the amount fields -> validated Decimal dict + derived values."""
        vals = {k: money(d.get(k, 0), k.replace("_", " ").capitalize()) for k in SALES_AMOUNT_FIELDS}
        if not self.cfg("track_taxes"):
            vals["taxes_collected"] = ZERO
        if not self.cfg("track_customer_credit"):
            vals["credit_sales"] = vals["credit_collections"] = ZERO
        net = vals["gross_sales"] - vals["discounts"] - vals["refunds"]
        if net < 0:
            raise ValueError("Discounts and refunds cannot exceed gross sales.")
        vals["net_sales"] = net
        exp, act = d.get("expected_cash"), d.get("actual_cash")
        exp_v = money(exp, "Expected cash") if exp not in (None, "") else vals["cash_collected"]
        vals["expected_cash"] = exp_v
        if act in (None, ""):
            vals["actual_cash"] = ""
            vals["cash_difference"] = ""
        else:
            vals["actual_cash"] = money(act, "Actual cash")
            vals["cash_difference"] = vals["actual_cash"] - exp_v
        return vals

    def add_sales(self, business_date, gross_sales, discounts=0, refunds=0, cash=0, upi=0, card=0, other=0,
                  credit_sales=0, credit_collections=0, tips=0, service_charges=0, taxes=0, shift="",
                  expected_cash=None, actual_cash=None, notes="", request_id="", allow_duplicate=False):
        date = parse_date(business_date, "Business date")
        shift = optional_text(shift, 40)
        vals = self._sales_values({
            "gross_sales": gross_sales, "discounts": discounts, "refunds": refunds, "cash_collected": cash,
            "upi_collected": upi, "card_collected": card, "other_collected": other, "credit_sales": credit_sales,
            "credit_collections": credit_collections, "tips_collected": tips,
            "service_charges_collected": service_charges, "taxes_collected": taxes,
            "expected_cash": expected_cash, "actual_cash": actual_cash})
        with self.db.transaction(["customer_sales", "tips_ledger", "audit_log"], "add_sales") as f:
            seen = self._seen(f, request_id, "sales")
            if seen:
                return seen
            dup = self._sales_duplicate(f["customer_sales"], date, shift)
            if dup and not allow_duplicate:
                raise DuplicateWarning(f"A sales record ({dup['sales_id']}) already exists for {date}"
                                       f"{' / shift ' + shift if shift else ''}. Confirm to add another.")
            sid = next_ids(f["customer_sales"]["sales_id"], ID_PREFIX["customer_sales"])[0]
            now = self._stamp()
            row = {"sales_id": sid, "business_date": date, "shift": shift, **vals,
                   "notes": optional_text(notes), "status": "Active", "created_at": now, "modified_at": now}
            f["customer_sales"] = append_rows(f["customer_sales"], [row])
            for kind, col in (("tip", "tips_collected"), ("service_charge", "service_charges_collected")):
                if vals[col] > 0:
                    self._new_tip_entry(f, kind, vals[col], date, sid, "Auto-created from daily sales")
            self._audit(f, "sales", sid, "create", f"Net sales {vals['net_sales']}", after=row, request_id=request_id)
        return sid

    def _new_tip_entry(self, f, kind, collected, date, sales_id, notes):
        eid = next_ids(f["tips_ledger"]["entry_id"], ID_PREFIX["tips_ledger"])[0]
        row = {"entry_id": eid, "sales_id": sales_id, "entry_type": kind, "collected": collected,
               "distributed": ZERO, "retained_or_payable": collected, "entry_date": date,
               "distribution_date": "", "notes": notes, "created_at": self._stamp(), "status": "Active"}
        f["tips_ledger"] = append_rows(f["tips_ledger"], [row])
        return eid

    def _sync_tip_entries(self, f, sales_id, date, kind, new_amount):
        t = f["tips_ledger"]
        idx = t.index[(t["sales_id"].astype(str) == str(sales_id)) & (t["entry_type"] == kind)
                      & (t["status"] == "Active")].tolist()
        if not idx:
            if new_amount > 0:
                self._new_tip_entry(f, kind, new_amount, date, sales_id, "Auto-created from daily sales")
            return
        others = sum((parse_stored(t.at[j, "collected"]) for j in idx[1:]), ZERO)
        target = new_amount - others
        first = idx[0]
        dist = parse_stored(t.at[first, "distributed"])
        if target < dist:
            raise ValueError(f"The {kind.replace('_', ' ')} amount cannot be lowered below the "
                             f"{dist} already distributed. Update the distribution first.")
        if target < 0:
            raise ValueError("Manual ledger entries already exceed the new amount.")
        if target == 0 and dist == 0:
            t.at[first, "status"] = "Voided"
        else:
            t.at[first, "collected"] = target
            t.at[first, "retained_or_payable"] = target - dist

    def update_sales(self, sales_id, reason="", allow_duplicate=False, **kw):
        unknown = set(kw) - SALES_EDITABLE
        if unknown:
            raise ValueError(f"Fields cannot be edited: {sorted(unknown)}")
        with self.db.transaction(["customer_sales", "tips_ledger", "audit_log"], "update_sales") as f:
            s = f["customer_sales"]
            i = self._index(s, "sales_id", sales_id, "Sales record")
            cur = s.loc[i].to_dict()
            if cur["status"] != "Active":
                raise ValueError("A voided sales record cannot be edited.")
            merged = {k: cur[k] for k in SALES_AMOUNT_FIELDS + ["expected_cash", "actual_cash"]}
            if "expected_cash" not in kw and cur["expected_cash"] == cur["cash_collected"] and "cash_collected" in kw:
                merged["expected_cash"] = None   # keep "expected = cash collected" default in sync
            merged.update({k: v for k, v in kw.items() if k in merged})
            vals = self._sales_values(merged)
            new = {**cur, **vals}
            if "business_date" in kw:
                new["business_date"] = parse_date(kw["business_date"], "Business date")
            if "shift" in kw:
                new["shift"] = optional_text(kw["shift"], 40)
            if "notes" in kw:
                new["notes"] = optional_text(kw["notes"])
            if (new["business_date"], new["shift"]) != (cur["business_date"], cur["shift"]):
                dup = self._sales_duplicate(s, new["business_date"], new["shift"], exclude=sales_id)
                if dup and not allow_duplicate:
                    raise DuplicateWarning(f"Another sales record ({dup['sales_id']}) exists for that date/shift.")
            adj = self._active_adjustment_total(sales_id)
            if vals["discounts"] + vals["refunds"] + adj > vals["gross_sales"]:
                raise ValueError("Discounts, refunds and later adjustments cannot exceed gross sales.")
            changed = [k for k in new if str(cur[k]) != str(new[k])]
            if not changed:
                return
            before = {k: cur[k] for k in changed}
            after = {k: new[k] for k in changed}
            for k in changed:
                s.at[i, k] = new[k]
            s.at[i, "modified_at"] = self._stamp()
            self._sync_tip_entries(f, sales_id, new["business_date"], "tip", vals["tips_collected"])
            self._sync_tip_entries(f, sales_id, new["business_date"], "service_charge",
                                   vals["service_charges_collected"])
            self._audit(f, "sales", sales_id, "update", reason or ", ".join(sorted(after)), before=before, after=after)

    def _active_adjustment_total(self, sales_id):
        adj = self.db.read("sales_adjustments")
        return sum((parse_stored(r["amount"]) for r in adj.to_dict("records")
                    if str(r["sales_id"]) == str(sales_id) and r["status"] == "Active"), ZERO)

    def void_sales(self, sales_id, reason):
        reason = required_text(reason, "Void reason", 500)
        with self.db.transaction(["customer_sales", "tips_ledger", "sales_adjustments", "audit_log"], "void_sales") as f:
            s = f["customer_sales"]
            i = self._index(s, "sales_id", sales_id, "Sales record")
            if s.at[i, "status"] == "Voided":
                raise ValueError("Sales record is already voided.")
            t = f["tips_ledger"]
            mine = (t["sales_id"].astype(str) == str(sales_id)) & (t["status"] == "Active")
            if any(parse_stored(x) > 0 for x in t.loc[mine, "distributed"]):
                raise ValueError("Tips/service charges from this record were already distributed. "
                                 "Set the distribution back to zero in Tips & Charges first.")
            s.at[i, "status"] = "Voided"
            s.at[i, "modified_at"] = self._stamp()
            t.loc[mine, "status"] = "Voided"
            a = f["sales_adjustments"]
            a.loc[(a["sales_id"].astype(str) == str(sales_id)) & (a["status"] == "Active"), "status"] = "Voided"
            self._audit(f, "sales", sales_id, "void", reason, before={"status": "Active"}, after={"status": "Voided"})

    def add_sales_adjustment(self, sales_id, adjustment_type, amount, adjustment_date=None, notes="", request_id=""):
        """Record a later refund/discount against a closed sales record without overwriting it."""
        if adjustment_type not in ADJUSTMENT_TYPES:
            raise ValueError(f"Adjustment type must be one of {ADJUSTMENT_TYPES}.")
        amt = money(amount, "Adjustment amount", allow_zero=False)
        adate = parse_date(adjustment_date or today_iso(), "Adjustment date")
        with self.db.transaction(["sales_adjustments", "audit_log"], "add_sales_adjustment") as f:
            seen = self._seen(f, request_id, "adjustment")
            if seen:
                return seen
            sales = self.db.read("customer_sales")
            hit = sales[sales["sales_id"].astype(str) == str(sales_id)]
            if hit.empty or hit["status"].iloc[0] != "Active":
                raise ValueError("Active sales record not found.")
            s = hit.iloc[0]
            used = parse_stored(s["discounts"]) + parse_stored(s["refunds"]) + self._active_adjustment_total(sales_id)
            if used + amt > parse_stored(s["gross_sales"]):
                raise ValueError("Discounts, refunds and adjustments would exceed gross sales.")
            aid = next_ids(f["sales_adjustments"]["adjustment_id"], ID_PREFIX["sales_adjustments"])[0]
            row = {"adjustment_id": aid, "sales_id": str(sales_id), "adjustment_type": adjustment_type,
                   "amount": amt, "adjustment_date": adate, "notes": optional_text(notes),
                   "created_at": self._stamp(), "reversal_of": "", "status": "Active"}
            f["sales_adjustments"] = append_rows(f["sales_adjustments"], [row])
            self._audit(f, "adjustment", aid, "create", f"{adjustment_type} {amt}", after=row, request_id=request_id)
        return aid

    def void_sales_adjustment(self, adjustment_id, reason):
        reason = required_text(reason, "Reason", 500)
        with self.db.transaction(["sales_adjustments", "audit_log"], "void_sales_adjustment") as f:
            a = f["sales_adjustments"]
            i = self._index(a, "adjustment_id", adjustment_id, "Adjustment")
            if a.at[i, "status"] != "Active":
                raise ValueError("Adjustment is already voided.")
            a.at[i, "status"] = "Voided"
            self._audit(f, "adjustment", adjustment_id, "void", reason)

    # ------------------------------------------------------------------ tips & service charges
    def add_tip_entry(self, entry_type, collected, entry_date, sales_id="", notes="", request_id=""):
        """Manual ledger entry (e.g. from a POS summary).  Linked entries are reconciled to the sales record."""
        if entry_type not in TIP_TYPES:
            raise ValueError(f"Entry type must be one of {TIP_TYPES}.")
        amt = money(collected, "Collected amount", allow_zero=False)
        d = parse_date(entry_date, "Entry date")
        with self.db.transaction(["tips_ledger", "audit_log"], "add_tip_entry") as f:
            seen = self._seen(f, request_id, "tip")
            if seen:
                return seen
            if sales_id:
                sales = self.db.read("customer_sales")
                if not (sales["sales_id"].astype(str) == str(sales_id)).any():
                    raise ValueError("Related sales record not found.")
            eid = self._new_tip_entry(f, entry_type, amt, d, str(sales_id or ""), optional_text(notes) or "Manual entry")
            self._audit(f, "tip", eid, "create", f"{entry_type} {amt}", request_id=request_id)
        return eid

    def update_tip(self, entry_id, distributed, retained, notes="", distribution_date=""):
        distributed = money(distributed, "Distributed")
        retained = money(retained, "Retained/payable")
        ddate = parse_date(distribution_date, "Distribution date", required=False)
        with self.db.transaction(["tips_ledger", "audit_log"], "update_tip") as f:
            t = f["tips_ledger"]
            i = self._index(t, "entry_id", entry_id, "Ledger entry")
            if t.at[i, "status"] != "Active":
                raise ValueError("Ledger entry is voided.")
            collected = parse_stored(t.at[i, "collected"])
            if distributed + retained != collected:
                raise ValueError(f"Distributed ({distributed}) + retained/payable ({retained}) must equal "
                                 f"the collected amount ({collected}).")
            if distributed > 0 and not ddate:
                ddate = today_iso()
            before = {"distributed": t.at[i, "distributed"], "retained_or_payable": t.at[i, "retained_or_payable"],
                      "distribution_date": t.at[i, "distribution_date"]}
            t.at[i, "distributed"] = distributed
            t.at[i, "retained_or_payable"] = retained
            t.at[i, "distribution_date"] = ddate if distributed > 0 else ""
            if notes:
                t.at[i, "notes"] = optional_text(notes)
            self._audit(f, "tip", entry_id, "update", f"Distributed {distributed}; retained {retained}", before=before,
                        after={"distributed": distributed, "retained_or_payable": retained,
                               "distribution_date": t.at[i, "distribution_date"]})

    def void_tip_entry(self, entry_id, reason):
        reason = required_text(reason, "Reason", 500)
        with self.db.transaction(["tips_ledger", "audit_log"], "void_tip_entry") as f:
            t = f["tips_ledger"]
            i = self._index(t, "entry_id", entry_id, "Ledger entry")
            if parse_stored(t.at[i, "distributed"]) > 0:
                raise ValueError("Reduce the distributed amount to zero before voiding this entry.")
            t.at[i, "status"] = "Voided"
            self._audit(f, "tip", entry_id, "void", reason)

    # ------------------------------------------------------------------ revenue definitions
    def sales_records(self, start=None, end=None):
        """Active sales records with later adjustments applied, as Decimal dicts."""
        start = parse_date(start, "Start date", required=False)
        end = parse_date(end, "End date", required=False)
        adj_by = defaultdict(lambda: {"REFUND": ZERO, "DISCOUNT": ZERO})
        for a in self.db.read("sales_adjustments").to_dict("records"):
            if a["status"] == "Active":
                adj_by[str(a["sales_id"])][a["adjustment_type"]] += parse_stored(a["amount"])
        out = []
        for r in self.db.read("customer_sales").to_dict("records"):
            if r["status"] != "Active":
                continue
            if (start and r["business_date"] < start) or (end and r["business_date"] > end):
                continue
            rec = {k: parse_stored(r[k]) for k in SALES_AMOUNT_FIELDS + ["net_sales"]}
            rec.update({k: r[k] for k in ("sales_id", "business_date", "shift")})
            rec["cash_difference"] = parse_stored(r["cash_difference"]) if r["cash_difference"] != "" else None
            a = adj_by.get(rec["sales_id"], {"REFUND": ZERO, "DISCOUNT": ZERO})
            rec["adjustments"] = a["REFUND"] + a["DISCOUNT"]
            rec["net_after_adjustments"] = rec["net_sales"] - rec["adjustments"]
            rec["cafe_revenue"] = self.cafe_revenue(rec)
            out.append(rec)
        return out

    def cafe_revenue(self, rec):
        """Revenue per configured policy.  Tips, service charges and taxes are excluded unless the
        accountant configures them as revenue.  Credit collections are NEVER revenue."""
        base = rec["net_after_adjustments"]
        if self.cfg("gross_sales_includes_tax"):
            base -= rec["taxes_collected"]
        if not self.cfg("taxes_excluded_from_revenue"):
            base += rec["taxes_collected"]
        if not self.cfg("tips_excluded_from_revenue"):
            base += rec["tips_collected"]
        if not self.cfg("service_charges_excluded_from_revenue"):
            base += rec["service_charges_collected"]
        return base

    # ================================================================== DASHBOARD / INTEGRITY
    def dashboard_summary(self, as_of=None, start=None, end=None):
        s_iso = parse_date(start, "Start date", required=False)
        e_iso = parse_date(end, "End date", required=False)
        as_of_iso = parse_date(as_of or e_iso or today_iso())
        balances = [b for b in self.bill_balances(as_of_iso, exclude_future=True) if b["calculated_status"] != "Cancelled"]
        outstanding = sum((parse_stored(b["outstanding_balance"]) for b in balances), ZERO)
        overdue = sum((parse_stored(b["outstanding_balance"]) for b in balances if b["calculated_status"] == "Overdue"), ZERO)

        def in_range(d):
            return (not s_iso or d >= s_iso) and (not e_iso or d <= e_iso)

        sales = self.sales_records(s_iso, e_iso)
        bills_entered = sum((parse_stored(b["invoice_total"]) for b in balances if in_range(b["invoice_date"])), ZERO)
        payments = [p for p in self.db.read("payments").to_dict("records")
                    if p["status"] == "Active" and in_range(p["payment_date"])]
        by_method = {"Cash": ZERO, "UPI": ZERO, "Card": ZERO, "Other": ZERO}
        for r in sales:
            by_method["Cash"] += r["cash_collected"]
            by_method["UPI"] += r["upi_collected"]
            by_method["Card"] += r["card_collected"]
            by_method["Other"] += r["other_collected"]
        from cafebooks.services.reconciliation_service import ReconciliationService
        recon = ReconciliationService(self).sales_rows(s_iso, e_iso)
        sup_paid = sum((parse_stored(p["amount"]) for p in payments), ZERO)
        receipts = sum(by_method.values(), ZERO)
        return {
            "gross_sales": sum((r["gross_sales"] for r in sales), ZERO),
            "net_sales": sum((r["net_after_adjustments"] for r in sales), ZERO),
            "cafe_revenue": sum((r["cafe_revenue"] for r in sales), ZERO),
            "tips": sum((r["tips_collected"] for r in sales), ZERO),
            "service_charges": sum((r["service_charges_collected"] for r in sales), ZERO),
            "taxes": sum((r["taxes_collected"] for r in sales), ZERO),
            "bills_entered": bills_entered,
            "supplier_payments": sup_paid,
            "supplier_outstanding": outstanding,
            "overdue_outstanding": overdue,
            "unallocated_payments": self.unallocated_total(as_of_iso),
            "collections_by_method": by_method,
            "sales_receipts": receipts,
            "net_cash_movement": receipts - sup_paid,
            "recon_difference_total": sum((abs(r["difference"]) for r in recon), ZERO),
            "recon_difference_days": sum(1 for r in recon if r["status"] != "OK"),
            "bill_count": len(balances), "sales_days": len(sales),
        }

    def integrity_check(self):
        """Human-readable list of problems ('ERROR: ...' / 'WARNING: ...')."""
        issues = [f"{i.severity.upper()}: [{i.table}] {i.ref}: {i.message}" for i in self.db.validate_all()]
        from cafebooks.services.reconciliation_service import ReconciliationService
        for r in ReconciliationService(self).sales_rows():
            if r["status"] != "OK":
                issues.append(f"WARNING: [reconciliation] {r['sales_id']} ({r['business_date']}): {r['status']}")
        return issues

    # ================================================================== SHARED SETTINGS / ACTIVITY
    def reload_settings(self):
        """Refresh the business settings from the database (another user or server may have
        changed them).  The dict is updated in place because callers share it."""
        fresh = config_from_settings(self.db.read("settings"))
        for key, value in fresh.items():
            if self.config.get(key) != value:
                self.config[key] = value
        set_timezone(self.config["timezone"])
        self.db.backups.retention = self.config["backup_retention"]
        return self.config

    def update_settings(self, changes):
        """Validate and store business settings for every user; the change is audited."""
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        with self.db.transaction(["settings", "audit_log"], "update_settings") as f:
            df = f["settings"]
            current = config_from_settings(df)
            new = validate_config({**current, **changes})
            diff = {k: new[k] for k in SETTING_KEYS if new[k] != current[k]}
            if diff:
                now = self._stamp()
                for key, value in diff.items():
                    if (df["key"] == key).any():
                        df.loc[df["key"] == key, ["value", "modified_at"]] = [encode_value(value), now]
                    else:
                        df.loc[len(df)] = [key, encode_value(value), now]
                self._audit(f, "settings", "settings", "update", ", ".join(sorted(diff)),
                            before={k: current[k] for k in diff}, after=diff)
        return self.reload_settings()

    def log_event(self, entity_type, entity_id, action, details="", before=None, after=None):
        """Audit an operation that is not a single-record change (imports, restores, resets)."""
        with self.db.transaction(["audit_log"], action) as f:
            self._audit(f, entity_type, entity_id, action, details, before, after)

    def activity(self, limit=300):
        """Newest audit-log entries first."""
        rows = self.db.read("audit_log").to_dict("records")
        return rows[::-1][:max(1, int(limit))]
