// Record pages: dashboard, quick entry, vendors, bills, payments, sales, tips.
import { get, post, patch, del, qs } from "./api.js";
import {
  state, h, clear, button, icon, money, fmtDate, addDays, today, toPaise, fromPaise, dataTable, donut, toast,
  confirmDialog, formDialog, reasonDialog, modal, badge,
} from "./ui.js";
import {
  vendorForm, billForm, billEditForm, paymentFlow, allocationDialog, salesForm, adjustmentForm, tipDistributionForm,
  tipEntryForm, tipLabel,
} from "./forms.js";

const enc = encodeURIComponent;
const P = (v) => { const p = toPaise(v); return p === null || Number.isNaN(p) ? 0 : p; };
const newRequestId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}${Math.random()}`).replace(/[-.]/g, "");

export function pageHead(title, subtitle, extra) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", { tabindex: "-1", id: "page-title" }, title), subtitle ? h("p", {}, subtitle) : null), extra || null);
}

// Buttons that act on the selected table row.  def: {label, icon, kind, needsRow, when(row) -> true | reason, run(row)}
function actionBar(defs) {
  const bar = h("div", { class: "actions" });
  let selected = null;
  const items = [];
  for (const d of defs) {
    if (d === "sep") { bar.append(h("span", { class: "sep" })); continue; }
    const b = button(d.label, { kind: d.kind, icon: d.icon, onClick: () => {
      if (d.needsRow && !selected) return;
      const ok = d.when && selected ? d.when(selected) : true;
      if (ok !== true) { toast(ok, "err"); return; }
      d.run(selected);
    } });
    bar.append(b);
    items.push([d, b]);
  }
  const update = (row) => {
    selected = row;
    for (const [d, b] of items) {
      if (!d.needsRow) continue;
      const ok = row ? (d.when ? d.when(row) : true) : "Select a row in the table first.";
      b.disabled = ok !== true;
      b.title = ok === true ? "" : ok;
    }
  };
  update(null);
  return { el: bar, update };
}

const hint = (text) => h("p", { class: "sub", style: { color: "var(--muted)", margin: "-6px 0 12px", fontSize: "13px" } }, text);

// ------------------------------------------------------------------ dashboard
export async function dashboard(view, ctx) {
  const start = ctx.params.get("start") || "", end = ctx.params.get("end") || "";
  const t = today();
  const ranges = [["Today", t, t], ["Yesterday", addDays(t, -1), addDays(t, -1)], ["Last 7 days", addDays(t, -6), t],
    ["Last 30 days", addDays(t, -29), t], ["This month", `${t.slice(0, 8)}01`, t], ["Last 12 months", addDays(t, -364), t],
    ["All time", "", ""]];
  const go = (s, e) => { location.hash = `#/dashboard${qs({ start: s, end: e })}`; };
  const from = h("input", { type: "date", value: start, "aria-label": "Start date" });
  const to = h("input", { type: "date", value: end, "aria-label": "End date" });
  const rangeBar = h("div", { class: "range-bar", role: "group", "aria-label": "Reporting period" },
    ranges.map(([label, s, e]) => h("button", { type: "button", class: "btn small", "aria-pressed": String(s === start && e === end),
      onclick: () => go(s, e) }, label)),
    h("div", { class: "range-custom" }, from, h("span", { style: { color: "var(--muted)" } }, "to"), to,
      button("Apply", { small: true, onClick: () => {
        if (from.value && to.value && from.value > to.value) return toast("The start date is after the end date.", "err");
        go(from.value, to.value);
      } })));
  view.append(pageHead("Dashboard", null), rangeBar);
  const data = await get(`/api/dashboard${qs({ start, end })}`);
  const d = data.summary, by = d.collections_by_method;
  const kpi = (label, value, note, color, small) => h("div", { class: `card kpi ${small ? "small" : ""}`, style: { "--kpi": color, "--kpi-text": color } },
    h("div", { class: "kpi-label" }, label), h("div", { class: "kpi-value" }, money(value)), note ? h("div", { class: "kpi-note" }, note) : null);
  const period = !start && !end ? "All recorded dates" : `${start ? fmtDate(start) : "the beginning"} → ${end ? fmtDate(end) : "today"}`;
  view.append(h("div", { class: "card welcome", style: { marginBottom: "14px" } },
    h("h2", {}, `${state.config.cafe_name} at a glance`), h("p", { class: "sub", style: { margin: 0 } }, `Period: ${period}`)));
  view.append(h("div", { class: "grid k4" },
    kpi("Net sales", d.net_sales, "Sales after discounts, refunds and adjustments", "#f4b323"),
    kpi("Gross sales", d.gross_sales, "Before discounts and refunds", "#e8a33c"),
    kpi("Cash movement", d.net_cash_movement, "Receipts minus supplier payments — not profit", "#d7cdb4"),
    kpi("Supplier outstanding", d.supplier_outstanding, "Still payable to suppliers", "#f2d06b")));

  const segments = [["Cash", by.Cash, "var(--c-cash)", "#20c997"], ["UPI", by.UPI, "var(--c-upi)", "#3b82f6"],
    ["Card", by.Card, "var(--c-card)", "#a855f7"], ["Other", by.Other, "var(--c-other)", "#f97316"]];
  const total = segments.reduce((a, s) => a + P(s[1]), 0);
  const mix = h("div", { class: "card" }, h("h2", {}, "Collection mix"), h("p", { class: "sub" }, "How customers paid in this period"),
    h("div", { class: "donut-wrap" },
      donut(segments.map(([label, v, , color]) => ({ label, value: P(v), color })), "TOTAL", money(fromPaise(total))),
      h("div", { class: "legend" }, segments.map(([label, v, css]) => h("div", { class: "legend-row" },
        h("span", { class: "swatch", style: { background: css } }), h("span", {}, label,
          h("span", { class: "pct" }, total ? `${Math.round((100 * P(v)) / total)}%` : "")),
        h("b", { class: "money" }, money(v)))))));
  const compare = [["Net sales", d.net_sales, "#f4b323"], ["Supplier payments", d.supplier_payments, "#3b82f6"],
    ["Supplier outstanding", d.supplier_outstanding, "#f2d06b"]];
  const maxC = Math.max(1, ...compare.map((c) => P(c[1])));
  const snap = h("div", { class: "card" }, h("h2", {}, "Money snapshot"), h("p", { class: "sub" }, "Sales compared with supplier obligations"),
    h("div", { class: "bars" }, compare.map(([label, v, color]) => h("div", {},
      h("div", { class: "bar-label" }, h("span", {}, label), h("span", { class: "money", style: { color } }, money(v))),
      h("div", { class: "bar-track", role: "img", "aria-label": `${label} ${money(v)}` },
        h("div", { class: "bar-fill", style: { width: `${Math.max(0, (100 * P(v)) / maxC)}%`, background: color } }))))));
  view.append(h("div", { class: "grid k2", style: { marginTop: "14px" } }, mix, snap));

  view.append(h("h2", { class: "section-title" }, "Operational details"));
  view.append(h("div", { class: "grid k4" },
    kpi("Cafe revenue", d.cafe_revenue, "Per the revenue policy in Settings", "#f4b323", true),
    kpi("Supplier bills entered", d.bills_entered, `${d.bill_count} bills`, "#c8bfa8", true),
    kpi("Supplier payments", d.supplier_payments, null, "#8ab4f8", true),
    kpi("Overdue to suppliers", d.overdue_outstanding, null, "#f2777a", true),
    kpi("Unallocated payments", d.unallocated_payments, "Paid but not matched to bills", "#c8bfa8", true),
    kpi("Tips", d.tips, "Tracked separately", "#7fd1a0", true),
    kpi("Service charges", d.service_charges, "Tracked separately", "#7fd1a0", true),
    kpi("Reconciliation differences", d.recon_difference_total, `${d.recon_difference_days} sales record(s) to check`, "#f2777a", true)));
  const integ = data.integrity;
  const bad = integ.errors > 0;
  view.append(h("div", { class: `card status-card ${bad ? "bad" : "ok"}`, style: { marginTop: "14px" } },
    h("h2", {}, badge(bad ? "Needs attention" : integ.warnings ? "Check warnings" : "OK"), " Data integrity"),
    h("p", { class: "sub", style: { margin: "6px 0 0" } }, bad || integ.warnings
      ? `${integ.errors} error(s) and ${integ.warnings} warning(s). Open Settings → Backups & data → Integrity check for details.`
      : `All checks passed · ${d.bill_count} bills · ${d.sales_days} sales records.`)));
  view.append(h("p", { class: "notice" }, state.boot.profit_notice));
}

// ------------------------------------------------------------------ quick entry
export async function quick(view, ctx) {
  view.append(pageHead("Quick Entry", "Enter a day's work from one place. Saved records flow into the lists, dashboard and reports automatically."));
  const refresh = () => ctx.refresh();
  const cards = [
    ["Daily sales", "Record the day's till totals, how customers paid, tips and the cash count.", "sales", () => salesForm(null, refresh)],
    ["Purchase bill", "Enter a supplier invoice. The bill is the expense; its balance updates as you pay it.", "bills", () => billForm(refresh)],
    ["Supplier payment", "Record money paid to a supplier and match it to their open bills.", "payments", () => paymentFlow(refresh)],
    ["New vendor", "Add a supplier once so you can pick them for bills and payments.", "vendors", () => vendorForm(null, refresh)],
    ["Tip / service charge", "Record a tip or service charge that was not entered with daily sales.", "tips", () => tipEntryForm(refresh)],
  ];
  view.append(h("div", { class: "grid k3" }, cards.map(([title, text, ic, fn]) => h("div", { class: "card quick-card" },
    h("h2", { style: { display: "flex", gap: "10px", alignItems: "center" } }, icon(ic), title), h("p", {}, text),
    button(`Enter ${title.toLowerCase()}`, { kind: "primary", icon: "plus", onClick: fn })))));
  const c = state.boot.counts;
  view.append(h("h2", { class: "section-title" }, "Current records"),
    h("div", { class: "grid k4" }, [["Sales entries", c.sales], ["Purchase bills", c.bills], ["Supplier payments", c.payments], ["Vendors", c.vendors]]
      .map(([label, n]) => h("div", { class: "card stat" }, h("b", {}, n), h("span", {}, label)))),
    h("p", { class: "notice" }, "Supplier payments are cash movements; the purchase bill is the expense. Use the individual pages to edit, allocate, reverse or void records."));
}

// ------------------------------------------------------------------ vendors
export async function vendors(view, ctx) {
  view.append(pageHead("Vendors", "Suppliers you buy from. A vendor with bills or payments can be deactivated but never deleted, so its history stays intact."));
  const rows = await get("/api/vendors");
  rows.forEach((r) => { r.status = r.active ? "Active" : "Inactive"; });
  const refresh = () => ctx.refresh();
  const actions = actionBar([
    { label: "Add vendor", icon: "plus", kind: "primary", run: () => vendorForm(null, refresh) },
    { label: "Edit", icon: "edit", needsRow: true, run: (r) => vendorForm(r, refresh) },
    { label: "Deactivate", icon: "ban", needsRow: true, when: (r) => (r.active ? true : "This vendor is already inactive."),
      run: async (r) => {
        if (!(await confirmDialog({ title: "Deactivate vendor", message: `Deactivate ${r.name}? Their history stays; you just won't be able to add new bills for them.`, confirmLabel: "Deactivate" }))) return;
        await post(`/api/vendors/${enc(r.vendor_id)}/deactivate`).then(() => { toast("Vendor deactivated"); refresh(); }, (e) => toast(e.message, "err"));
      } },
    { label: "Reactivate", icon: "undo", needsRow: true, when: (r) => (!r.active ? true : "This vendor is already active."),
      run: (r) => post(`/api/vendors/${enc(r.vendor_id)}/reactivate`).then(() => { toast("Vendor reactivated"); refresh(); }, (e) => toast(e.message, "err")) },
    { label: "Delete", icon: "trash", kind: "danger", needsRow: true, run: async (r) => {
      if (!(await confirmDialog({ title: "Delete vendor", danger: true, confirmLabel: "Delete permanently",
        message: `Permanently delete ${r.name}? This only works for vendors with no bills or payments.` }))) return;
      await del(`/api/vendors/${enc(r.vendor_id)}`).then(() => { toast("Vendor deleted"); refresh(); }, (e) => toast(e.message, "err"));
    } },
    "sep",
    { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh },
  ]);
  const table = dataTable({
    rows, rowKey: "vendor_id", onSelect: actions.update, onOpen: (r) => vendorForm(r, refresh),
    columns: [{ key: "vendor_id", label: "ID" }, { key: "name" }, { key: "category" }, { key: "contact_person", label: "Contact" },
      { key: "phone" }, { key: "gstin", label: "GSTIN" }, { key: "payment_terms_days", label: "Terms (days)", type: "number" },
      { key: "default_payment_method", label: "Usual method" }, { key: "outstanding", label: "Outstanding", type: "money" },
      { key: "status", type: "status" }],
    choices: { category: { label: "Categories", values: [...new Set(rows.map((r) => r.category))].sort() },
      status: { label: "Statuses", values: ["Active", "Inactive"] } },
    totals: ["outstanding"], emptyTitle: "No vendors yet", emptyText: "Add your suppliers to start recording bills.",
  });
  view.append(actions.el, hint("Tip: select a row to edit it; double-click (or press Enter) to open it."), table.el);
}

// ------------------------------------------------------------------ bills
export async function bills(view, ctx) {
  view.append(pageHead("Purchase Bills", "Supplier invoices. Paid, outstanding and status are calculated from the payment ledger — they are never typed in."));
  const rows = await get("/api/bills");
  const refresh = () => ctx.refresh();
  const isUrl = (s) => /^https?:\/\//i.test(s || "");
  const openInvoice = async (r) => {
    if (isUrl(r.attachment_path)) { window.open(r.attachment_path, "_blank", "noopener"); return; }
    if (state.boot.mode === "desktop") { await post(`/api/bills/${enc(r.bill_id)}/open-attachment`).catch((e) => toast(e.message, "err")); return; }
    toast(`This bill's invoice reference is not a link: ${r.attachment_path}`, "err");
  };
  const actions = actionBar([
    { label: "Add bill", icon: "plus", kind: "primary", run: () => billForm(refresh) },
    { label: "Edit", icon: "edit", needsRow: true, when: (r) => (r.status === "Cancelled" ? "A cancelled bill cannot be edited." : true),
      run: (r) => billEditForm(r, refresh) },
    { label: "Open invoice", icon: "link", needsRow: true, when: (r) => (r.attachment_path ? true : "This bill has no invoice link."), run: openInvoice },
    { label: "Cancel bill", icon: "ban", kind: "danger", needsRow: true,
      when: (r) => (r.status === "Cancelled" ? "This bill is already cancelled." : P(r.amount_paid) > 0 ? "Reverse the payments allocated to this bill first (Supplier Payments)." : true),
      run: (r) => reasonDialog(`Cancel bill ${r.invoice_number}`, async (v) => { await post(`/api/bills/${enc(r.bill_id)}/cancel`, v); refresh(); },
        { submitLabel: "Cancel bill", successMessage: "Bill cancelled", intro: "Cancelled bills stay in the records but no longer count as expenses or payables." }) },
    "sep",
    { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh },
  ]);
  const table = dataTable({
    rows, rowKey: "bill_id", onSelect: actions.update,
    onOpen: (r) => (r.status === "Cancelled" ? null : billEditForm(r, refresh)),
    columns: [{ key: "bill_id", label: "ID" }, { key: "vendor_name", label: "Vendor" }, { key: "invoice_number", label: "Invoice #" },
      { key: "invoice_date", label: "Invoice date", type: "date" }, { key: "due_date", label: "Due", type: "date" }, { key: "category" },
      { key: "invoice_total", label: "Total", type: "money" }, { key: "amount_paid", label: "Paid", type: "money" },
      { key: "outstanding_balance", label: "Outstanding", type: "money" }, { key: "calculated_status", label: "Status", type: "status" },
      { key: "days_overdue", label: "Days overdue", type: "number" },
      { key: "attachment_path", label: "Invoice", render: (r) => (r.attachment_path ? (isUrl(r.attachment_path)
        ? h("a", { href: r.attachment_path, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, "Open")
        : "File") : "") }],
    dateKey: "invoice_date", dateLabel: "Invoice date",
    choices: { vendor_name: { label: "Vendors", values: [...new Set(rows.map((r) => r.vendor_name))].sort() },
      category: { label: "Categories", values: state.config.expense_categories },
      calculated_status: { label: "Statuses", values: ["Unpaid", "Partially Paid", "Overdue", "Paid", "Cancelled"] } },
    totals: ["invoice_total", "amount_paid", "outstanding_balance"], initialSort: { key: "invoice_date", dir: -1 },
    emptyTitle: "No bills yet", emptyText: "Add a purchase bill when a supplier invoice arrives.",
  });
  view.append(actions.el, table.el);
}

// ------------------------------------------------------------------ payments
export async function payments(view, ctx) {
  view.append(pageHead("Supplier Payments", "Payments are never edited or deleted. To correct one, reverse it (or one allocation) — the history is kept."));
  const rows = await get("/api/payments");
  const refresh = () => ctx.refresh();
  const active = (r) => (r.status === "Active" ? true : "This payment has been reversed.");
  const actions = actionBar([
    { label: "Record payment", icon: "plus", kind: "primary", run: () => paymentFlow(refresh) },
    { label: "Allocate unallocated", icon: "wallet", needsRow: true,
      when: (r) => (active(r) !== true ? active(r) : P(r.unallocated) > 0 ? true : "This payment is fully allocated."),
      run: async (r) => {
        const open = await get(`/api/bills/open${qs({ vendor_id: r.vendor_id })}`);
        if (!open.length) return toast("This vendor has no open bills to allocate to.", "err");
        const requestId = newRequestId();
        allocationDialog({
          title: `Allocate ${money(r.unallocated)} from ${r.payment_id} · ${r.vendor_name}`, bills: open, available: P(r.unallocated),
          withDate: true, minDate: r.payment_date, submitLabel: "Save allocation", successMessage: "Allocation saved",
          onSubmit: async (allocations, date) => {
            await post(`/api/payments/${enc(r.payment_id)}/allocate`, { allocations, allocation_date: date, request_id: requestId });
            refresh();
          },
        });
      } },
    { label: "Reverse allocation", icon: "undo", needsRow: true, when: active, run: async (r) => {
      const allocs = (await get(`/api/payments/${enc(r.payment_id)}/allocations`)).filter((a) => a.entry_type === "ALLOCATE" && P(a.remaining) > 0);
      if (!allocs.length) return toast("This payment has no allocations left to reverse.", "err");
      formDialog({
        title: `Reverse an allocation · ${r.payment_id}`, size: "narrow",
        intro: "Use this when a payment was matched to the wrong bill. A reversing entry is added; nothing is deleted.",
        fields: [
          { key: "allocation", label: "Allocation", type: "select", options: allocs.map((a) => ({ value: a.allocation_id,
            label: `${a.allocation_id} · invoice ${a.invoice_number || a.bill_id} · ${money(a.remaining)} left` })) },
          { key: "amount", label: "Amount to reverse", type: "money", hint: "Leave blank to reverse everything left." },
          { key: "reversal_date", label: "Reversal date", type: "date", value: today() },
          { key: "reason", label: "Reason", type: "textarea", required: true },
        ],
        onSubmit: async (v) => { await post(`/api/allocations/${enc(v.allocation)}/reverse`, v); refresh(); },
        successMessage: "Allocation reversed",
      });
    } },
    { label: "Edit reference / notes", icon: "edit", needsRow: true, run: (r) => formDialog({
      title: `Payment details · ${r.payment_id}`, size: "narrow", intro: "Amounts can never be edited; reverse the payment instead.",
      fields: [{ key: "reference", label: "Reference / transaction ID", value: r.reference, max: 200 },
        { key: "notes", label: "Notes", type: "textarea", value: r.notes }],
      onSubmit: async (v) => { await patch(`/api/payments/${enc(r.payment_id)}`, v); refresh(); }, successMessage: "Payment updated",
    }) },
    { label: "Reverse payment", icon: "ban", kind: "danger", needsRow: true, when: active,
      run: (r) => reasonDialog(`Reverse payment ${r.payment_id} (${money(r.amount)})`, async (v) => {
        await post(`/api/payments/${enc(r.payment_id)}/reverse`, v); refresh();
      }, { submitLabel: "Reverse payment", successMessage: "Payment reversed",
        intro: "All of this payment's allocations are reversed and it stops counting as paid. The records are kept for history.",
        fields: [{ key: "reversal_date", label: "Reversal date", type: "date", value: today() }] }) },
    "sep",
    { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh },
  ]);
  const table = dataTable({
    rows, rowKey: "payment_id", onSelect: actions.update,
    columns: [{ key: "payment_id", label: "ID" }, { key: "vendor_name", label: "Vendor" }, { key: "payment_date", label: "Date", type: "date" },
      { key: "amount", type: "money" }, { key: "allocated", type: "money" }, { key: "unallocated", type: "money" }, { key: "method" },
      { key: "reference" }, { key: "status", type: "status" }, { key: "reversal_reason", label: "Reversal reason" }],
    dateKey: "payment_date", dateLabel: "Payment date",
    choices: { vendor_name: { label: "Vendors", values: [...new Set(rows.map((r) => r.vendor_name))].sort() },
      method: { label: "Methods", values: state.boot.payment_methods }, status: { label: "Statuses", values: ["Active", "Reversed"] } },
    totals: ["amount", "allocated", "unallocated"], initialSort: { key: "payment_date", dir: -1 },
    emptyTitle: "No supplier payments yet", emptyText: "Record a payment when you pay a supplier.",
  });
  view.append(actions.el, table.el);
}

// ------------------------------------------------------------------ sales
export async function sales(view, ctx) {
  view.append(pageHead("Daily Sales", "Till totals per day (or shift). Credit collections are not new revenue. Corrections are audited; later refunds and discounts are separate adjustments."));
  const tab = ctx.params.get("tab") === "adjustments" ? "adjustments" : "sales";
  view.append(h("div", { class: "tabs", role: "tablist" },
    [["sales", "Sales records"], ["adjustments", "Later refunds & discounts"]].map(([id, label]) => h("button", {
      type: "button", role: "tab", "aria-selected": String(tab === id), onclick: () => { location.hash = id === "sales" ? "#/sales" : "#/sales?tab=adjustments"; },
    }, label))));
  const refresh = () => ctx.refresh();
  if (tab === "adjustments") {
    const rows = await get("/api/adjustments");
    const actions = actionBar([
      { label: "Void adjustment", icon: "ban", kind: "danger", needsRow: true, when: (r) => (r.status === "Active" ? true : "Already voided."),
        run: (r) => reasonDialog(`Void adjustment ${r.adjustment_id}`, async (v) => { await post(`/api/adjustments/${enc(r.adjustment_id)}/void`, v); refresh(); },
          { submitLabel: "Void", successMessage: "Adjustment voided" }) },
      "sep", { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh }]);
    const table = dataTable({ rows, rowKey: "adjustment_id", onSelect: actions.update,
      columns: [{ key: "adjustment_id", label: "ID" }, { key: "sales_id", label: "Sales record" }, { key: "adjustment_type", label: "Type" },
        { key: "amount", type: "money" }, { key: "adjustment_date", label: "Date", type: "date" }, { key: "notes" }, { key: "status", type: "status" }],
      dateKey: "adjustment_date", choices: { status: { label: "Statuses", values: ["Active", "Voided"] } }, totals: ["amount"],
      emptyTitle: "No later refunds or discounts", emptyText: "Select a sales record and use “Refund / discount” to add one." });
    view.append(actions.el, table.el);
    return;
  }
  const rows = await get("/api/sales");
  const active = (r) => (r.status === "Active" ? true : "This sales record is voided.");
  const actions = actionBar([
    { label: "Sales entry", icon: "plus", kind: "primary", run: () => salesForm(null, refresh) },
    { label: "Edit", icon: "edit", needsRow: true, when: active, run: (r) => salesForm(r, refresh) },
    { label: "Refund / discount", icon: "undo", needsRow: true, when: active, run: (r) => adjustmentForm(r, refresh) },
    { label: "Void", icon: "ban", kind: "danger", needsRow: true, when: active,
      run: (r) => reasonDialog(`Void sales record ${r.sales_id}`, async (v) => { await post(`/api/sales/${enc(r.sales_id)}/void`, v); refresh(); },
        { submitLabel: "Void record", successMessage: "Sales record voided", intro: "Its tips/service-charge entries and adjustments are voided too. If tips were already distributed, set the distribution back to zero first." }) },
    "sep", { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh }]);
  const m = (key, label) => ({ key, label, type: "money" });
  const cols = [{ key: "sales_id", label: "ID" }, { key: "business_date", label: "Date", type: "date" }, { key: "shift" },
    m("gross_sales", "Gross"), m("discounts", "Discounts"), m("refunds", "Refunds"), m("net_sales", "Net"),
    m("cash_collected", "Cash"), m("upi_collected", "UPI"), m("card_collected", "Card"), m("other_collected", "Other"),
    ...(state.config.track_customer_credit ? [m("credit_sales", "Credit sales"), m("credit_collections", "Credit collected")] : []),
    m("tips_collected", "Tips"), m("service_charges_collected", "Service"),
    ...(state.config.track_taxes ? [m("taxes_collected", "Taxes")] : []),
    m("cash_difference", "Cash over/short"), { key: "status", type: "status" }];
  const table = dataTable({ rows, rowKey: "sales_id", onSelect: actions.update,
    onOpen: (r) => (r.status === "Active" ? salesForm(r, refresh) : null), columns: cols,
    dateKey: "business_date", dateLabel: "Business date", choices: { status: { label: "Statuses", values: ["Active", "Voided"], initial: "Active" } },
    totals: cols.filter((c) => c.type === "money").map((c) => c.key), initialSort: { key: "business_date", dir: -1 },
    emptyTitle: "No sales recorded yet", emptyText: "Use “Sales entry” at the end of each day." });
  view.append(actions.el, table.el);
}

// ------------------------------------------------------------------ tips
export async function tips(view, ctx) {
  view.append(pageHead("Tips & Service Charges", "Not cafe revenue unless Settings say so. Collected amounts stay intact; record how much was distributed to staff."));
  const rows = await get("/api/tips");
  rows.forEach((r) => { r.type_label = tipLabel(r.entry_type); });
  const refresh = () => ctx.refresh();
  const active = (r) => (r.status === "Active" ? true : "This entry is voided.");
  const actions = actionBar([
    { label: "Manual entry", icon: "plus", kind: "primary", run: () => tipEntryForm(refresh) },
    { label: "Edit distribution", icon: "edit", needsRow: true, when: active, run: (r) => tipDistributionForm(r, refresh) },
    { label: "Void entry", icon: "ban", kind: "danger", needsRow: true,
      when: (r) => (active(r) !== true ? active(r) : P(r.distributed) > 0 ? "Set the distributed amount back to zero first." : true),
      run: (r) => reasonDialog(`Void ${tipLabel(r.entry_type).toLowerCase()} entry ${r.entry_id}`, async (v) => {
        await post(`/api/tips/${enc(r.entry_id)}/void`, v); refresh();
      }, { submitLabel: "Void entry", successMessage: "Entry voided" }) },
    "sep", { label: "Refresh", icon: "refresh", kind: "ghost", run: refresh }]);
  const table = dataTable({ rows, rowKey: "entry_id", onSelect: actions.update,
    onOpen: (r) => (r.status === "Active" ? tipDistributionForm(r, refresh) : null),
    columns: [{ key: "entry_id", label: "ID" }, { key: "sales_id", label: "Sales record" }, { key: "type_label", label: "Type" },
      { key: "entry_date", label: "Date", type: "date" }, { key: "collected", type: "money" }, { key: "distributed", type: "money" },
      { key: "retained_or_payable", label: "Retained / payable", type: "money" }, { key: "distribution_date", label: "Distributed on", type: "date" },
      { key: "notes" }, { key: "status", type: "status" }],
    dateKey: "entry_date", choices: { type_label: { label: "Types", values: ["Tip", "Service charge"] },
      status: { label: "Statuses", values: ["Active", "Voided"], initial: "Active" } },
    totals: ["collected", "distributed", "retained_or_payable"], initialSort: { key: "entry_date", dir: -1 },
    emptyTitle: "No tips or service charges yet", emptyText: "They are added automatically when daily sales include tips." });
  view.append(actions.el, table.el);
}
