// Entry forms shared by Quick Entry and the record pages.
import { get, post, patch, qs } from "./api.js";
import {
  state, h, add, clear, button, busy, modal, formDialog, confirmDialog, toast, money, fmtDate, addDays, today,
  toPaise, fromPaise, badge,
} from "./ui.js";

const enc = encodeURIComponent;
const P = (v) => { const p = toPaise(v); return p === null || Number.isNaN(p) ? 0 : p; };
const zeroBlank = (v) => (v === undefined || v === null || P(v) === 0 ? "" : v);
const calcItem = (label, value, cls) => h("div", {}, h("span", {}, label), h("b", { class: cls || "" }, value));

function attachmentField(value) {
  const desktop = state.boot.mode === "desktop";
  return {
    key: "attachment_path", label: desktop ? "Invoice file or link" : "Invoice link", full: true, value, max: 500,
    hint: desktop ? "A file on this computer (e.g. C:\\Invoices\\INV-123.pdf) or a link to the scanned invoice."
      : "Paste a link to the scanned invoice (Google Drive, OneDrive, Dropbox…). Everyone using CafeBooks can open it.",
  };
}

// ------------------------------------------------------------------ vendors
export function vendorForm(row, after) {
  const cfg = state.config;
  const cats = [...cfg.vendor_categories];
  if (row && row.category && !cats.includes(row.category)) cats.unshift(row.category);
  formDialog({
    title: row ? `Edit vendor · ${row.name}` : "New vendor",
    fields: [
      { key: "name", label: "Vendor name", required: true, value: row?.name, max: 200 },
      { key: "category", label: "Category", type: "select", options: cats, value: row?.category || cats[0] },
      { key: "contact_person", label: "Contact person", value: row?.contact_person },
      { key: "phone", label: "Phone", type: "tel", value: row?.phone, placeholder: "98765 43210" },
      { key: "email", label: "Email", type: "email", value: row?.email },
      { key: "gstin", label: "GSTIN", value: row?.gstin, max: 20, hint: "15 characters, e.g. 29ABCDE1234F1Z5" },
      { key: "payment_terms_days", label: "Payment terms (days)", type: "number", min: 0,
        value: row ? row.payment_terms_days : cfg.default_payment_terms_days, hint: "Used to fill in bill due dates." },
      { key: "default_payment_method", label: "Default payment method", type: "select",
        options: ["", ...state.boot.payment_methods], value: row?.default_payment_method || "" },
      { key: "address", label: "Address", full: true, value: row?.address },
      { key: "notes", label: "Notes", type: "textarea", value: row?.notes },
    ],
    onSubmit: async (v) => {
      if (row) await patch(`/api/vendors/${enc(row.vendor_id)}`, v);
      else await post("/api/vendors", v);
      after && after();
    },
    successMessage: row ? "Vendor updated" : "Vendor saved",
  });
}

// ------------------------------------------------------------------ bills
export async function billForm(after) {
  const vendors = (await get("/api/vendors")).filter((v) => v.active);
  if (!vendors.length) { toast("Add an active vendor first (Vendors page).", "err"); return; }
  const cfg = state.config;
  const byId = Object.fromEntries(vendors.map((v) => [v.vendor_id, v]));
  const calc = h("div", { class: "calc", "aria-live": "polite" });
  let dueTouched = false, categoryTouched = false, wired = false;
  formDialog({
    title: "New purchase bill", size: "wide", submitLabel: "Save bill",
    fields: [
      { key: "vendor_id", label: "Vendor", type: "select", required: true,
        options: vendors.map((v) => ({ value: v.vendor_id, label: `${v.name} (${v.vendor_id})` })) },
      { key: "invoice_number", label: "Supplier invoice number", required: true, max: 60 },
      { key: "invoice_date", label: "Invoice date", type: "date", required: true, value: today() },
      { key: "due_date", label: "Due date", type: "date", required: true, value: today(), hint: "Filled from the vendor's payment terms." },
      { key: "category", label: "Expense category", type: "select", options: cfg.expense_categories },
      { key: "subtotal", label: "Subtotal", type: "money", required: true, placeholder: "0.00" },
      { key: "tax_amount", label: "Tax (GST)", type: "money", placeholder: "0.00" },
      { key: "other_charges", label: "Other charges", type: "money", placeholder: "0.00" },
      { key: "invoice_total", label: "Invoice total (optional check)", type: "money",
        hint: "If entered, it must equal subtotal + tax + other charges." },
      { key: "description", label: "Description", full: true, max: 2000 },
      attachmentField(""),
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    extra: calc,
    onChange: (v, form) => {
      if (!wired) {
        wired = true;
        form.controls.due_date.input.addEventListener("input", () => { dueTouched = true; });
        form.controls.category.input.addEventListener("change", () => { categoryTouched = true; });
      }
      const vendor = byId[v.vendor_id];
      if (!dueTouched && vendor && v.invoice_date) {
        form.controls.due_date.input.value = addDays(v.invoice_date, Number(vendor.payment_terms_days || 0));
      }
      if (!categoryTouched && vendor && cfg.expense_categories.includes(vendor.category)) {
        form.controls.category.input.value = vendor.category;
      }
      const total = P(v.subtotal) + P(v.tax_amount) + P(v.other_charges);
      add(clear(calc), calcItem("Invoice total", money(fromPaise(total))),
        v.invoice_total && P(v.invoice_total) !== total ? calcItem("Check", "Does not match the entered total", "neg") : null);
    },
    onSubmit: async (v) => { await post("/api/bills", v); after && after(); },
    successMessage: "Bill saved",
  });
}

export function billEditForm(row, after) {
  const cfg = state.config;
  const cats = [...cfg.expense_categories];
  if (!cats.includes(row.category)) cats.unshift(row.category);
  formDialog({
    title: `Edit bill · ${row.invoice_number}`, size: "wide",
    intro: Number(row.amount_paid) > 0 ? "This bill has payments allocated, so its amounts cannot change until those allocations are reversed." : "",
    fields: [
      { key: "invoice_number", label: "Supplier invoice number", required: true, value: row.invoice_number, max: 60 },
      { key: "invoice_date", label: "Invoice date", type: "date", required: true, value: row.invoice_date },
      { key: "due_date", label: "Due date", type: "date", required: true, value: row.due_date },
      { key: "category", label: "Expense category", type: "select", options: cats, value: row.category },
      { key: "subtotal", label: "Subtotal", type: "money", required: true, value: row.subtotal },
      { key: "tax_amount", label: "Tax (GST)", type: "money", value: row.tax_amount },
      { key: "other_charges", label: "Other charges", type: "money", value: row.other_charges },
      { key: "description", label: "Description", full: true, value: row.description },
      attachmentField(row.attachment_path),
      { key: "notes", label: "Notes", type: "textarea", value: row.notes },
      { key: "reason", label: "Reason for change", full: true, hint: "Saved in the activity log." },
    ],
    onSubmit: async (v) => { await patch(`/api/bills/${enc(row.bill_id)}`, v); after && after(); },
    successMessage: "Bill updated",
  });
}

// ------------------------------------------------------------------ payments
export function allocationDialog({ title, intro, bills, available, withDate, minDate, submitLabel, allowEmpty, onSubmit, successMessage }) {
  const inputs = {};
  const summary = h("div", { class: "alloc-summary", "aria-live": "polite" });
  const errorBox = h("div", { class: "form-error", role: "alert", hidden: true });
  const dateInput = withDate ? h("input", { type: "date", value: today(), min: minDate, id: "alloc-date" }) : null;
  const rows = bills.map((b) => {
    const inp = h("input", { type: "text", inputmode: "decimal", placeholder: "0.00", "aria-label": `Allocate to invoice ${b.invoice_number}` });
    inp.addEventListener("input", update);
    inputs[b.bill_id] = { inp, out: P(b.outstanding_balance), bill: b };
    return h("tr", {},
      h("td", {}, b.invoice_number), h("td", {}, fmtDate(b.invoice_date)), h("td", {}, fmtDate(b.due_date)),
      h("td", { class: "money" }, money(b.invoice_total, false)), h("td", { class: "money" }, money(b.amount_paid, false)),
      h("td", { class: "money" }, money(b.outstanding_balance, false)), h("td", { class: "number" }, b.days_overdue || ""),
      h("td", {}, badge(b.calculated_status)), h("td", {}, inp));
  });
  function totals() {
    let sum = 0, bad = "";
    for (const { inp, out, bill } of Object.values(inputs)) {
      const p = toPaise(inp.value);
      if (p === null) continue;
      if (Number.isNaN(p) || p < 0) { bad = `Invoice ${bill.invoice_number}: enter a valid amount.`; continue; }
      if (p > out) bad = `Invoice ${bill.invoice_number}: more than its outstanding ${money(fromPaise(out))}.`;
      sum += p;
    }
    if (!bad && sum > available) bad = "The total is more than the amount available.";
    return { sum, bad };
  }
  function update() {
    const { sum, bad } = totals();
    add(clear(summary), 
      h("span", {}, "Available ", h("b", {}, money(fromPaise(available)))),
      h("span", {}, "Allocating ", h("b", {}, money(fromPaise(sum)))),
      h("span", {}, "Left unallocated ", h("b", {}, money(fromPaise(available - sum)))),
      bad ? h("span", { class: "neg" }, bad) : null);
  }
  const auto = () => {
    let left = available;
    for (const { inp, out } of Object.values(inputs)) {
      const take = Math.min(left, out);
      inp.value = take > 0 ? fromPaise(take) : "";
      left -= take;
    }
    update();
  };
  const save = button(submitLabel || "Save", { kind: "primary" });
  const body = h("div", {},
    h("p", { style: { color: "var(--muted)", marginTop: 0 } }, intro ||
      "Allocations cannot exceed the amount available or any bill's outstanding balance. Money you do not allocate stays unallocated and can be allocated later."),
    dateInput ? h("div", { class: "field", style: { maxWidth: "220px", marginBottom: "10px" } },
      h("label", { for: "alloc-date" }, "Allocation date"), dateInput) : null,
    errorBox, summary,
    h("div", { class: "table-scroll alloc-table", style: { maxHeight: "50dvh" } },
      h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["Invoice", "Invoice date", "Due", "Total", "Paid", "Outstanding", "Days overdue", "Status", "Allocate"]
          .map((t, i) => h("th", { class: i >= 3 && i <= 6 ? "money" : "" }, h("button", { type: "button", tabindex: "-1" }, t))))),
        h("tbody", {}, rows))));
  const m = modal({
    title, size: "wide", body,
    footer: [button("Auto: oldest due first", { onClick: auto, kind: "left" }),
      button("Clear", { onClick: () => { Object.values(inputs).forEach(({ inp }) => { inp.value = ""; }); update(); } }),
      button("Cancel", { onClick: () => m.close() }), save],
  });
  update();
  save.addEventListener("click", () => busy(save, async () => {
    errorBox.hidden = true;
    const { sum, bad } = totals();
    if (bad) { errorBox.textContent = bad; errorBox.hidden = false; return; }
    if (!sum && !allowEmpty) { errorBox.textContent = "Enter at least one amount to allocate."; errorBox.hidden = false; return; }
    if (!sum && !(await confirmDialog({ title: "Nothing allocated", message: "Save this payment as fully unallocated? You can allocate it to bills later.", confirmLabel: "Save unallocated" }))) return;
    const allocations = Object.entries(inputs).map(([bill_id, { inp }]) => ({ bill_id, amount: inp.value.replace(/[,\s₹]/g, "") }))
      .filter((a) => P(a.amount) > 0);
    try {
      await onSubmit(allocations, dateInput ? dateInput.value : undefined);
      m.close();
      if (successMessage) toast(successMessage);
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    }
  }));
}

export async function paymentFlow(after) {
  const vendors = await get("/api/vendors");
  if (!vendors.length) { toast("Add a vendor first (Vendors page).", "err"); return; }
  vendors.sort((a, b) => Number(b.active) - Number(a.active) || a.name.localeCompare(b.name));
  const byId = Object.fromEntries(vendors.map((v) => [v.vendor_id, v]));
  let methodTouched = false, wired = false, lastVendor = null;
  formDialog({
    title: "Record supplier payment", submitLabel: "Next: allocate to bills",
    intro: "A supplier payment is cash leaving the business, not an expense. The purchase bill is the expense.",
    fields: [
      { key: "vendor_id", label: "Vendor", type: "select", required: true,
        options: vendors.map((v) => ({ value: v.vendor_id, label: `${v.name}${v.active ? "" : " (inactive)"} · owed ${money(v.outstanding)}` })) },
      { key: "payment_date", label: "Payment date", type: "date", required: true, value: today() },
      { key: "amount", label: "Amount paid", type: "money", required: true, placeholder: "0.00" },
      { key: "method", label: "Payment method", type: "select", options: state.boot.payment_methods },
      { key: "reference", label: "Reference / transaction ID", max: 200 },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    onChange: (v, form) => {
      if (!wired) { wired = true; form.controls.method.input.addEventListener("change", () => { methodTouched = true; }); }
      const vendor = byId[v.vendor_id];
      if (vendor && vendor.vendor_id !== lastVendor) {
        lastVendor = vendor.vendor_id;
        if (!methodTouched && vendor.default_payment_method) form.controls.method.input.value = vendor.default_payment_method;
      }
    },
    onSubmit: async (v) => {
      if (!(P(v.amount) > 0)) throw new Error("Amount paid must be greater than zero.");
      const bills = await get(`/api/bills/open${qs({ vendor_id: v.vendor_id, as_of: v.payment_date })}`);
      if (!bills.length) {
        const ok = await confirmDialog({ title: "No open bills", confirmLabel: "Save unallocated",
          message: "This vendor has no unpaid bills on that date. Save it as an unallocated payment? You can allocate it to bills later." });
        if (!ok) return false;
        await post("/api/payments", { ...v, allocations: [] });
        toast("Payment saved (unallocated)");
        after && after();
        return;
      }
      setTimeout(() => allocationDialog({
        title: `Allocate ${money(v.amount)} · ${byId[v.vendor_id].name}`, bills, available: P(v.amount),
        submitLabel: "Save payment", allowEmpty: true, successMessage: "Payment saved",
        onSubmit: async (allocations) => { await post("/api/payments", { ...v, allocations }); after && after(); },
      }), 0);
    },
  });
}

// ------------------------------------------------------------------ sales
export function salesForm(row, after) {
  const cfg = state.config;
  const g = (k) => (row ? zeroBlank(row[k]) : "");
  const money_ = (key, label, hint) => ({ key, label, type: "money", value: g(key), placeholder: "0.00", hint });
  const shifts = [...cfg.shift_names];
  if (row && row.shift && !shifts.includes(row.shift)) shifts.unshift(row.shift);
  const calc = h("div", { class: "calc", "aria-live": "polite" });
  const fields = [
    { key: "business_date", label: "Business date", type: "date", required: true, value: row ? row.business_date : today() },
    cfg.allow_multiple_shifts && shifts.length
      ? { key: "shift", label: "Shift", type: "select", options: shifts, value: row?.shift || shifts[0] }
      : { key: "shift", label: "Shift (optional)", value: row?.shift || "", max: 40 },
    { type: "group", label: "Sales (from the till / POS)", fields: [
      { ...money_("gross_sales", "Gross sales"), required: true },
      money_("discounts", "Discounts"), money_("refunds", "Refunds")] },
    { type: "group", label: "How customers paid", fields: [
      money_("cash_collected", "Cash"), money_("upi_collected", "UPI"), money_("card_collected", "Card"),
      money_("other_collected", "Other")] },
    cfg.track_customer_credit ? { type: "group", label: "Customer credit (summary only)", fields: [
      money_("credit_sales", "Sold on credit", "Not yet paid by the customer."),
      money_("credit_collections", "Credit collected", "Old dues paid today. Not new revenue.")] } : null,
    { type: "group", label: "Tips, service charges and taxes", fields: [
      money_("tips_collected", "Tips"), money_("service_charges_collected", "Service charges"),
      cfg.track_taxes ? money_("taxes_collected", "Taxes collected") : null] },
    { type: "group", label: "Cash count", fields: [
      { key: "expected_cash", label: "Expected cash in drawer", type: "money",
        value: row && row.expected_cash !== row.cash_collected ? row.expected_cash : "", hint: "Leave blank = cash collected." },
      { key: "actual_cash", label: "Actual cash counted", type: "money", value: row ? row.actual_cash : "", hint: "Leave blank if not counted yet." }] },
    { key: "notes", label: "Notes", type: "textarea", value: row?.notes },
    row ? { key: "reason", label: "Reason for change", full: true, hint: "Saved in the activity log." } : null,
  ];
  formDialog({
    title: row ? `Edit sales · ${fmtDate(row.business_date)}${row.shift ? ` · ${row.shift}` : ""}` : "Daily sales entry",
    size: "wide", fields, extra: calc, submitLabel: row ? "Save changes" : "Save sales",
    onChange: (v) => {
      const net = P(v.gross_sales) - P(v.discounts) - P(v.refunds);
      const tax = cfg.track_taxes && !cfg.gross_sales_includes_tax ? P(v.taxes_collected) : 0;
      const expected = net + tax + P(v.tips_collected) + P(v.service_charges_collected) + P(v.credit_collections) - P(v.credit_sales);
      const receipts = P(v.cash_collected) + P(v.upi_collected) + P(v.card_collected) + P(v.other_collected);
      const diff = receipts - expected;
      const expCash = v.expected_cash ? P(v.expected_cash) : P(v.cash_collected);
      add(clear(calc), 
        calcItem("Net sales", money(fromPaise(net)), net < 0 ? "neg" : ""),
        calcItem("Should have collected", money(fromPaise(expected))),
        calcItem("Collected", money(fromPaise(receipts))),
        calcItem("Difference", diff === 0 ? "Balanced ✓" : money(fromPaise(diff)), diff === 0 ? "" : "neg"),
        v.actual_cash ? calcItem("Cash over / short", money(fromPaise(P(v.actual_cash) - expCash)), P(v.actual_cash) - expCash ? "neg" : "") : null);
    },
    onSubmit: async (v) => {
      if (row) await patch(`/api/sales/${enc(row.sales_id)}`, v);
      else await post("/api/sales", v);
      after && after();
    },
    successMessage: row ? "Sales updated" : "Sales saved",
  });
}

export function adjustmentForm(row, after) {
  formDialog({
    title: `Later refund / discount · ${fmtDate(row.business_date)}`, size: "narrow",
    intro: "Records a refund or discount given after the day was closed. The original sales record is not changed.",
    fields: [
      { key: "adjustment_type", label: "Type", type: "select", options: state.boot.adjustment_types.map((t) => ({ value: t, label: t === "REFUND" ? "Refund" : "Discount" })) },
      { key: "amount", label: "Amount", type: "money", required: true },
      { key: "adjustment_date", label: "Date", type: "date", value: today() },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    onSubmit: async (v) => { await post(`/api/sales/${enc(row.sales_id)}/adjustments`, v); after && after(); },
    successMessage: "Adjustment saved",
  });
}

// ------------------------------------------------------------------ tips
export const tipLabel = (t) => (t === "service_charge" ? "Service charge" : "Tip");

export function tipDistributionForm(row, after) {
  const collected = P(row.collected);
  let retainedTouched = false, wired = false;
  formDialog({
    title: `${tipLabel(row.entry_type)} distribution · ${row.entry_id}`, size: "narrow",
    intro: `Collected: ${money(row.collected)}. Distributed + retained/payable must add up to this amount.`,
    fields: [
      { key: "distributed", label: "Distributed to staff", type: "money", value: row.distributed },
      { key: "retained", label: "Retained / still payable", type: "money", value: row.retained_or_payable },
      { key: "distribution_date", label: "Distribution date", type: "date", value: row.distribution_date || today() },
      { key: "notes", label: "Notes", type: "textarea", value: row.notes },
    ],
    onChange: (v, form) => {
      if (!wired) { wired = true; form.controls.retained.input.addEventListener("input", () => { retainedTouched = true; }); }
      if (!retainedTouched && toPaise(v.distributed) !== null && !Number.isNaN(toPaise(v.distributed))) {
        form.controls.retained.input.value = fromPaise(Math.max(0, collected - P(v.distributed)));
      }
    },
    onSubmit: async (v) => { await patch(`/api/tips/${enc(row.entry_id)}`, v); after && after(); },
    successMessage: "Distribution updated",
  });
}

export async function tipEntryForm(after) {
  const sales = (await get("/api/sales")).filter((s) => s.status === "Active").reverse().slice(0, 200);
  formDialog({
    title: "Manual tip / service charge entry", size: "narrow",
    intro: "Use this for amounts not entered through Daily Sales (for example from a POS summary).",
    fields: [
      { key: "entry_type", label: "Type", type: "select", options: state.boot.tip_types.map((t) => ({ value: t, label: tipLabel(t) })) },
      { key: "collected", label: "Amount collected", type: "money", required: true },
      { key: "entry_date", label: "Date", type: "date", required: true, value: today() },
      { key: "sales_id", label: "Related sales record (optional)", type: "select",
        options: [{ value: "", label: "— not linked —" }, ...sales.map((s) => ({ value: s.sales_id, label: `${s.sales_id} · ${fmtDate(s.business_date)}${s.shift ? ` · ${s.shift}` : ""}` }))] },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    onSubmit: async (v) => { await post("/api/tips", v); after && after(); },
    successMessage: "Entry saved",
  });
}
