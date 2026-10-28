// Reports, activity log, settings and data tools.
import { get, post, put, upload, download, qs } from "./api.js";
import {
  state, h, clear, button, busy, icon, fmtDate, addDays, today, toPaise, dataTable, barChart, toast, modal, buildForm,
  titleCase,
} from "./ui.js";
import { pageHead } from "./pages.js";

const enc = encodeURIComponent;
const SERIES_COLORS = ["#f4b323", "#3b82f6", "#20c997", "#a855f7", "#f97316"];

// ------------------------------------------------------------------ reports
export async function reports(view) {
  const boot = state.boot;
  view.append(pageHead("Reports", boot.profit_notice));
  const vendors = await get("/api/vendors");
  const saved = JSON.parse(sessionStorage.getItem("cafebooks.report") || "{}");
  const groups = {};
  for (const r of boot.reports) {
    const g = r.name.startsWith("Outstanding") || r.name.startsWith("Aging") ? "Supplier balances"
      : r.name.startsWith("Expenses") ? "Expenses" : r.name.startsWith("Cash") ? "Cash flow" : r.name.startsWith("Tips") ? "Tips & service charges" : "Sales";
    (groups[g] = groups[g] || []).push(r);
  }
  const reportSel = h("select", { id: "rep-name" }, Object.entries(groups).map(([g, list]) =>
    h("optgroup", { label: g }, list.map((r) => h("option", { value: r.name }, r.name)))));
  reportSel.value = saved.name || boot.reports[0].name;
  const from = h("input", { type: "date", id: "rep-from", value: saved.start ?? addDays(today(), -state.config.report_default_range_days) });
  const to = h("input", { type: "date", id: "rep-to", value: saved.end || today() });
  const cat = h("select", { id: "rep-cat" }, h("option", { value: "" }, "All categories"),
    state.config.expense_categories.map((c) => h("option", { value: c }, c)));
  const ven = h("select", { id: "rep-ven" }, h("option", { value: "" }, "All vendors"),
    vendors.map((v) => h("option", { value: v.vendor_id }, v.name)));
  cat.value = saved.category || "";
  ven.value = saved.vendor_id || "";
  const toLabel = h("label", { for: "rep-to" }, "To");
  const field = (label, input) => h("div", { class: "field" }, label, input);
  const result = h("div", { "aria-live": "polite" });
  let last = null, chartOn = false;
  const params = () => ({ name: reportSel.value, start: from.value, end: to.value, category: cat.value, vendor_id: ven.value });
  const meta = () => boot.reports.find((r) => r.name === reportSel.value);
  const sync = () => {
    const m = meta();
    const asof = m.kind === "asof";
    from.disabled = asof;
    toLabel.textContent = asof ? "As of" : "To";
    const exp = m.name.startsWith("Expenses");
    cat.disabled = !exp;
    ven.disabled = !exp;
  };
  reportSel.addEventListener("change", () => { sync(); run(); });
  const runBtn = button("Run report", { kind: "primary", icon: "reports", onClick: () => run() });
  const chartBtn = button("Chart", { icon: "chart", onClick: () => { chartOn = !chartOn; chartBtn.setAttribute("aria-pressed", String(chartOn)); draw(); } });
  chartBtn.setAttribute("aria-pressed", "false");
  const exportBtn = button("Export Excel", { icon: "download", onClick: () => busy(exportBtn, () =>
    download(`/api/reports/export${qs(params())}`).then(() => toast("Report downloaded"), (e) => toast(e.message, "err"))) });
  view.append(h("div", { class: "card", style: { marginBottom: "14px" } },
    h("div", { class: "form-grid", style: { gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" } },
      field(h("label", { for: "rep-name" }, "Report"), reportSel), field(h("label", { for: "rep-from" }, "From"), from),
      field(toLabel, to), field(h("label", { for: "rep-cat" }, "Category"), cat), field(h("label", { for: "rep-ven" }, "Vendor"), ven)),
    h("div", { class: "actions", style: { margin: "14px 0 0" } }, runBtn, chartBtn, exportBtn)),
  result);
  sync();

  function draw() {
    clear(result);
    if (!last) return;
    const { data, m } = last;
    const rows = data.rows.map((r) => Object.fromEntries(data.columns.map((c, i) => [c, r[i]])));
    const note = m.kind === "asof" ? `Balances as of ${fmtDate(to.value || today())}, not today.`
      : m.name.startsWith("Cash") ? boot.profit_notice : "";
    if (note) result.append(h("p", { class: "notice", style: { marginTop: 0 } }, note));
    if (chartOn && rows.length) {
      const label = m.chart.label;
      const points = rows.filter((r) => r[label] !== "TOTAL");
      const series = m.chart.values.filter((c) => data.columns.includes(c))
        .map((c, i) => ({ name: titleCase(c), color: SERIES_COLORS[i % SERIES_COLORS.length], values: points.map((r) => (toPaise(r[c]) || 0) / 100) }));
      result.append(h("div", { class: "card chart", style: { marginBottom: "14px" } },
        h("div", { class: "legend", style: { flexDirection: "row", flexWrap: "wrap", gap: "16px", marginBottom: "8px" } },
          series.map((s) => h("span", { style: { display: "inline-flex", gap: "8px", alignItems: "center" } },
            h("span", { class: "swatch", style: { background: s.color } }), s.name))),
        barChart(points.map((r) => (data.date_columns.includes(label) ? fmtDate(r[label]) : r[label])), series, { label: m.name })));
    }
    const hasTotalRow = rows.some((r) => Object.values(r).includes("TOTAL"));
    const columns = data.columns.map((c) => ({ key: c, label: titleCase(c),
      type: data.money_columns.includes(c) ? "money" : data.date_columns.includes(c) ? "date" : c === "days_overdue" ? "number" : undefined }));
    result.append(dataTable({ rows, columns, totals: hasTotalRow ? [] : data.money_columns.filter((c) => c !== "cash_closing"),
      emptyTitle: "No records for this selection", emptyText: "Try a wider date range." }).el);
  }

  async function run() {
    const p = params();
    sessionStorage.setItem("cafebooks.report", JSON.stringify(p));
    await busy(runBtn, async () => {
      try {
        last = { data: await get(`/api/reports/run${qs(p)}`), m: meta() };
        draw();
      } catch (e) {
        clear(result).append(h("div", { class: "banner error", role: "alert" }, icon("alert"), e.message));
      }
    });
  }
  run();
}

// ------------------------------------------------------------------ activity log
export async function activity(view) {
  view.append(pageHead("Activity Log", "Every change, who made it and when. There is no login yet, so names are what people typed in the top bar."));
  const rows = await get("/api/activity?limit=2000");
  const show = (r) => {
    const pretty = (s) => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } };
    modal({ title: `${titleCase(r.action)} · ${r.entity_type} ${r.entity_id}`, size: "wide", body: h("div", {},
      h("dl", { class: "kv" }, h("dt", {}, "When"), h("dd", {}, fmtDate(r.timestamp)), h("dt", {}, "Who"), h("dd", {}, r.actor),
        h("dt", {}, "Details"), h("dd", {}, r.details || "—"), h("dt", {}, "Audit ID"), h("dd", {}, r.audit_id)),
      r.before ? [h("h3", {}, "Before"), h("pre", { class: "json" }, pretty(r.before))] : null,
      r.after ? [h("h3", {}, "After"), h("pre", { class: "json" }, pretty(r.after))] : null) });
  };
  let selected = null;
  const viewBtn = button("View details", { icon: "search", disabled: true, onClick: () => selected && show(selected) });
  const table = dataTable({ rows, rowKey: "audit_id", onOpen: show,
    onSelect: (r) => { selected = r; viewBtn.disabled = !r; },
    columns: [{ key: "timestamp", label: "When", type: "datetime" }, { key: "actor", label: "Who" }, { key: "action" },
      { key: "entity_type", label: "Record type" }, { key: "entity_id", label: "Record" }, { key: "details" }],
    choices: { actor: { label: "People", values: [...new Set(rows.map((r) => r.actor))].sort() },
      entity_type: { label: "Record types", values: [...new Set(rows.map((r) => r.entity_type))].sort() },
      action: { label: "Actions", values: [...new Set(rows.map((r) => r.action))].sort() } },
    emptyTitle: "No activity yet" });
  view.append(h("div", { class: "actions" }, viewBtn), table.el);
}

// ------------------------------------------------------------------ settings
function listEditor(values, label) {
  let items = [...values];
  const box = h("div", { class: "list-editor" });
  const input = h("input", { type: "text", placeholder: `Add ${label.toLowerCase()}…`, "aria-label": `New ${label.toLowerCase()}`, maxlength: 60 });
  const draw = () => {
    clear(box);
    if (!items.length) box.append(h("span", { class: "hint", style: { color: "var(--muted)" } }, "None"));
    items.forEach((it, i) => box.append(h("span", { class: "chip" }, it,
      h("button", { type: "button", "aria-label": `Remove ${it}`, onclick: () => { items.splice(i, 1); draw(); } }, icon("close")))));
  };
  const addItem = () => {
    const v = input.value.trim();
    if (v && !items.includes(v)) items.push(v);
    input.value = "";
    draw();
    input.focus();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addItem(); } });
  draw();
  return { el: h("div", {}, box, h("div", { class: "add-row" }, input, button("Add", { onClick: addItem }))), get: () => items };
}

export async function settings(view, ctx) {
  const tab = ctx.params.get("tab") === "data" ? "data" : "settings";
  view.append(pageHead("Settings", "Settings are shared: a change here applies to everyone using CafeBooks."),
    h("div", { class: "tabs", role: "tablist" }, [["settings", "Business settings"], ["data", "Backups & data"]].map(([id, label]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(tab === id),
        onclick: () => { location.hash = id === "data" ? "#/settings?tab=data" : "#/settings"; } }, label))));
  if (tab === "data") return dataTools(view, ctx);
  const cfg = await get("/api/settings");
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  if (!zones.includes(cfg.timezone)) zones.unshift(cfg.timezone);
  const desktop = state.boot.mode === "desktop";
  const sections = [
    ["Business", [
      { key: "cafe_name", label: "Cafe / bakery name", required: true, value: cfg.cafe_name, max: 80 },
      { key: "timezone", label: "Timezone", type: zones.length ? "select" : "text", options: zones, value: cfg.timezone,
        hint: "Decides what “today” is, even when the server is in another country." },
      { key: "business_details", label: "Business details (address, GSTIN)", type: "textarea", value: cfg.business_details, max: 500 },
      { key: "currency_code", label: "Currency code", value: cfg.currency_code, required: true, max: 10 },
      { key: "currency_symbol", label: "Currency symbol", value: cfg.currency_symbol, required: true, max: 5 },
      { key: "date_display", label: "Date format", type: "select", options: ["DD-MM-YYYY", "DD/MM/YYYY", "YYYY-MM-DD"], value: cfg.date_display },
      { key: "indian_number_grouping", label: "Indian number grouping (1,00,000)", type: "checkbox", value: cfg.indian_number_grouping },
    ]],
    ["Revenue policy — confirm with your accountant", [
      { key: "tips_excluded_from_revenue", label: "Tips are NOT cafe revenue", type: "checkbox", value: cfg.tips_excluded_from_revenue },
      { key: "service_charges_excluded_from_revenue", label: "Service charges are NOT cafe revenue", type: "checkbox", value: cfg.service_charges_excluded_from_revenue },
      { key: "taxes_excluded_from_revenue", label: "Taxes collected are NOT cafe revenue", type: "checkbox", value: cfg.taxes_excluded_from_revenue },
      { key: "gross_sales_includes_tax", label: "The till's gross sales already include tax", type: "checkbox", value: cfg.gross_sales_includes_tax },
      { key: "track_taxes", label: "Track taxes collected", type: "checkbox", value: cfg.track_taxes },
      { key: "track_customer_credit", label: "Track customer credit (summary only)", type: "checkbox", value: cfg.track_customer_credit },
    ]],
    ["Cash, defaults and backups", [
      { key: "opening_cash", label: "Opening cash on hand", type: "money", value: cfg.opening_cash, hint: "Leave blank if not set. Used for closing cash in cash-flow reports." },
      { key: "opening_cash_date", label: "Opening cash date", type: "date", value: cfg.opening_cash_date },
      { key: "default_payment_terms_days", label: "Default vendor payment terms (days)", type: "number", min: 0, value: cfg.default_payment_terms_days },
      { key: "report_default_range_days", label: "Default report range (days)", type: "number", min: 0, value: cfg.report_default_range_days },
      { key: "backup_retention", label: "Backups to keep", type: "number", min: 1, value: cfg.backup_retention },
      { key: "auto_backup_daily", label: "Automatic daily backup", type: "checkbox", value: cfg.auto_backup_daily },
      desktop ? { key: "copy_attachments", label: "Copy linked invoice files into the CafeBooks data folder", type: "checkbox", value: cfg.copy_attachments } : null,
    ]],
  ];
  const forms = sections.map(([title, fields]) => {
    const f = buildForm(fields);
    view.append(h("section", { class: "card settings-section" }, h("h2", { style: { marginBottom: "12px" } }, title), f.el));
    return f;
  });
  const lists = {
    vendor_categories: listEditor(cfg.vendor_categories, "Vendor category"),
    expense_categories: listEditor(cfg.expense_categories, "Expense category"),
    shift_names: listEditor(cfg.shift_names, "Shift"),
  };
  const multi = buildForm([{ key: "allow_multiple_shifts", label: "Allow more than one sales record per day (one per shift)", type: "checkbox", value: cfg.allow_multiple_shifts }]);
  forms.push(multi);
  view.append(h("section", { class: "card settings-section" }, h("h2", { style: { marginBottom: "12px" } }, "Categories and shifts"),
    h("h3", { style: { fontSize: "14px", margin: "4px 0 8px" } }, "Vendor categories"), lists.vendor_categories.el,
    h("h3", { style: { fontSize: "14px", margin: "18px 0 8px" } }, "Expense categories"), lists.expense_categories.el,
    h("h3", { style: { fontSize: "14px", margin: "18px 0 8px" } }, "Shift names"), lists.shift_names.el,
    h("div", { style: { marginTop: "12px" } }, multi.el)));
  const err = h("div", { class: "form-error", role: "alert", hidden: true });
  const save = button("Save settings", { kind: "primary", icon: "check", onClick: () => busy(save, async () => {
    err.hidden = true;
    if (!forms.every((f) => f.validate())) return;
    const values = Object.assign({}, ...forms.map((f) => f.values()), Object.fromEntries(Object.entries(lists).map(([k, l]) => [k, l.get()])));
    try {
      const fresh = await put("/api/settings", values);
      state.config = fresh;
      ctx.onSettingsChanged();
      toast("Settings saved for everyone");
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      err.scrollIntoView({ block: "center" });
    }
  }) });
  view.append(err, h("div", { class: "save-bar" }, save, h("span", { style: { color: "var(--muted)", fontSize: "13px" } }, "Changes are recorded in the activity log.")));
}

// ------------------------------------------------------------------ backups & data tools
const fmtSize = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

function filePicker(accept, onFile) {
  const input = h("input", { type: "file", accept, hidden: true });
  input.addEventListener("change", () => { if (input.files[0]) onFile(input.files[0]); input.value = ""; });
  return input;
}

function issuesList(errors = [], warnings = []) {
  if (!errors.length && !warnings.length) return h("p", { class: "banner ok" }, icon("check"), "No problems found.");
  return h("ul", { class: "issues" }, errors.map((e) => h("li", { class: "e" }, `Error: ${e}`)), warnings.map((w) => h("li", { class: "w" }, `Warning: ${w}`)));
}

function countsTable(tables, a, b, la, lb) {
  return h("div", { class: "table-scroll", style: { maxHeight: "240px", minHeight: 0 } }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["Table", la, lb].map((t) => h("th", {}, h("button", { type: "button", tabindex: "-1" }, t))))),
    h("tbody", {}, Object.entries(tables).map(([t, v]) => h("tr", {}, h("td", {}, titleCase(t)), h("td", { class: "number" }, v[a]), h("td", { class: "number" }, v[b]))))));
}

async function confirmRestore(preview, apply, reload) {
  const blocked = preview.errors.length > 0;
  const go = button("Restore this backup", { kind: "danger solid", disabled: blocked });
  const m = modal({ title: "Restore backup", size: "wide", body: h("div", {},
    h("p", {}, `Backup created: ${preview.created_at ? fmtDate(preview.created_at) : "unknown (older backup without a checksum manifest)"}`),
    countsTable(preview.tables, "backup", "current", "Rows in backup", "Rows now"),
    h("h3", {}, "Checks"), issuesList(preview.errors, preview.warnings),
    h("p", { class: "notice" }, blocked ? "This backup has accounting errors, so it cannot be restored."
      : "A safety backup of the current data is taken first. Everyone using CafeBooks will see the restored data.")),
  footer: [button("Cancel", { onClick: () => m.close() }), go] });
  go.addEventListener("click", () => busy(go, async () => {
    try {
      const res = await apply();
      m.close();
      toast(`Backup restored. Safety copy: ${res.safety_backup}`);
      reload();
    } catch (e) { toast(e.message, "err"); }
  }));
}

async function dataTools(view, ctx) {
  const reload = () => ctx.reloadApp();
  const [backups, boot] = [await get("/api/backups"), state.boot];
  const db = boot.database;
  const vercelNote = boot.on_vercel ? " Uploads and downloads are limited to about 4.5 MB on Vercel." : "";

  view.append(h("section", { class: "card settings-section" }, h("h2", {}, "System"),
    h("dl", { class: "kv", style: { marginTop: "10px" } },
      h("dt", {}, "App version"), h("dd", {}, boot.app_version),
      h("dt", {}, "Running as"), h("dd", {}, boot.mode === "desktop" ? "Desktop app (this computer only)" : "Shared web app"),
      h("dt", {}, "Database"), h("dd", {}, `${db.engine} · ${db.location}`),
      h("dt", {}, "Backups are kept"), h("dd", {}, backups.store === "database" ? "inside the database" : "in the backups folder on this computer"),
      h("dt", {}, "Backups kept"), h("dd", {}, `newest ${backups.retention}`))));

  // backups
  const backupBtn = button("Back up now", { kind: "primary", icon: "database" });
  backupBtn.addEventListener("click", () => busy(backupBtn, async () => {
    try { const r = await post("/api/backups"); toast(`Backup created: ${r.name}`); ctx.refresh(); } catch (e) { toast(e.message, "err"); }
  }));
  const restorePicker = filePicker(".zip", async (file) => {
    try {
      const preview = await upload("/api/restore-upload/preview", file);
      confirmRestore(preview, () => upload("/api/restore-upload/apply", file), reload);
    } catch (e) { toast(e.message, "err"); }
  });
  const list = h("div", { class: "table-scroll", style: { maxHeight: "340px", minHeight: 0 } });
  const rows = backups.items;
  list.append(h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["Backup", "Created", "Size", ""].map((t) => h("th", {}, h("button", { type: "button", tabindex: "-1" }, t))))),
    h("tbody", {}, rows.length ? rows.map((b) => h("tr", {},
      h("td", {}, b.name), h("td", {}, fmtDate(b.created_at)), h("td", { class: "number" }, fmtSize(b.size)),
      h("td", {}, h("div", { style: { display: "flex", gap: "6px" } },
        button("Download", { small: true, icon: "download", onClick: () => download(`/api/backups/${enc(b.name)}/download`).catch((e) => toast(e.message, "err")) }),
        button("Restore", { small: true, kind: "danger", icon: "undo", onClick: async () => {
          try { confirmRestore(await post(`/api/backups/${enc(b.name)}/preview`), () => post(`/api/backups/${enc(b.name)}/restore`), reload); }
          catch (e) { toast(e.message, "err"); }
        } })))))
      : h("tr", {}, h("td", { colspan: 4, class: "empty" }, "No backups yet.")))));
  view.append(h("section", { class: "card settings-section" }, h("h2", {}, "Backups"),
    h("p", { class: "sub" }, `Each backup is a verified zip of every table. Download copies regularly and keep them somewhere safe (another computer, cloud drive).${vercelNote}`),
    h("div", { class: "actions" }, backupBtn, button("Restore from a file…", { icon: "upload", onClick: () => restorePicker.click() }), restorePicker),
    list));

  // integrity
  const integOut = h("div", { "aria-live": "polite" });
  const integBtn = button("Run integrity check", { icon: "shield", onClick: () => busy(integBtn, async () => {
    try {
      const issues = await get("/api/integrity");
      clear(integOut).append(issuesList(issues.filter((i) => i.startsWith("ERROR")).map((i) => i.replace(/^ERROR:\s*/, "")),
        issues.filter((i) => !i.startsWith("ERROR")).map((i) => i.replace(/^WARNING:\s*/, ""))));
    } catch (e) { clear(integOut).append(h("div", { class: "banner error" }, e.message)); }
  }) });
  view.append(h("section", { class: "card settings-section" }, h("h2", {}, "Integrity check"),
    h("p", { class: "sub" }, "Checks every record and relationship: totals, allocations, references and daily reconciliation. Nothing is changed."),
    h("div", { class: "actions" }, integBtn), integOut));

  // Excel tools
  const snapBtn = button("Download Excel snapshot", { icon: "download", onClick: () => busy(snapBtn, () => download("/api/data/snapshot").catch((e) => toast(e.message, "err"))) });
  const packBtn = button("Download edit pack", { icon: "download", onClick: () => busy(packBtn, () => download("/api/data/edit-pack").then(() => toast("Edit pack downloaded"), (e) => toast(e.message, "err"))) });
  const packPicker = filePicker(".zip", (file) => editPackPreview(file, reload));
  const filePickerEl = filePicker(".xlsx,.csv,.txt", (file) => dataFilePreview(file, reload));
  view.append(h("section", { class: "card settings-section" }, h("h2", {}, "Excel tools"),
    h("p", { class: "sub" }, "Snapshot: every table in one workbook, for your accountant. Edit pack: download, correct records in Excel, then upload it — changes are checked, conflicts with newer edits are shown, and a backup is taken before anything is applied." + vercelNote),
    h("div", { class: "actions" }, snapBtn, packBtn, button("Upload edited pack…", { icon: "upload", onClick: () => packPicker.click() }), packPicker),
    h("h3", { style: { fontSize: "15px", margin: "14px 0 4px" } }, "Import a data file"),
    h("p", { class: "sub" }, "Replace whole tables from a workbook whose sheet names match CafeBooks tables (for example the demo workbook or an Excel snapshot), or a CSV with a ‘table’ column. The result is validated first and a backup is taken."),
    h("div", { class: "actions" }, button("Import data file…", { icon: "upload", onClick: () => filePickerEl.click() }), filePickerEl,
      button("Download demo workbook", { kind: "ghost", icon: "download",
        onClick: () => download("/api/data/demo-workbook").catch((e) => toast(e.message, "err")) }))));

  // danger zone
  const resetBtn = button("Reset all data…", { kind: "danger", icon: "trash", onClick: () => {
    const input = h("input", { type: "text", id: "reset-confirm", autocomplete: "off" });
    const go = button("Reset everything", { kind: "danger solid", disabled: true });
    input.addEventListener("input", () => { go.disabled = input.value.trim() !== "RESET"; });
    const m = modal({ title: "Reset all data", size: "narrow", body: h("div", {},
      h("p", {}, "This removes every vendor, bill, payment, sales record and tip entry for everyone. Settings are kept, and a backup is taken first so it can be restored."),
      h("div", { class: "field" }, h("label", { for: "reset-confirm" }, "Type RESET to confirm"), input)),
    footer: [button("Cancel", { onClick: () => m.close() }), go] });
    go.addEventListener("click", () => busy(go, async () => {
      try { const r = await post("/api/data/reset", { confirm: "RESET" }); m.close(); toast(`All records cleared. Backup: ${r.backup}`); reload(); }
      catch (e) { toast(e.message, "err"); }
    }));
  } });
  view.append(h("section", { class: "card settings-section", style: { borderColor: "#7a3134" } }, h("h2", { style: { color: "var(--danger)" } }, "Danger zone"),
    h("p", { class: "sub" }, "Use only to start over, for example after trying the demo data."), resetBtn));
}

async function dataFilePreview(file, reload) {
  let preview;
  try { preview = await upload("/api/data/import-file/preview", file); } catch (e) { toast(e.message, "err"); return; }
  const blocked = preview.errors.length > 0;
  const go = button("Replace these tables", { kind: "danger solid", disabled: blocked });
  const m = modal({ title: `Import ${file.name}`, size: "wide", body: h("div", {},
    h("p", {}, "These tables will be replaced with the rows in the file. Other tables are not touched."),
    countsTable(preview.tables, "incoming", "current", "Rows in file", "Rows now"),
    h("h3", {}, "Checks"), issuesList(preview.errors, preview.warnings),
    h("p", { class: "notice" }, blocked ? "Fix these problems in the file and try again; nothing was changed." : "A backup is taken first. The file itself is not modified.")),
  footer: [button("Cancel", { onClick: () => m.close() }), go] });
  go.addEventListener("click", () => busy(go, async () => {
    try { const r = await upload("/api/data/import-file/apply", file); m.close(); toast(`Imported. Safety backup: ${r.backup}`); reload(); }
    catch (e) { toast(e.message, "err"); }
  }));
}

async function editPackPreview(file, reload) {
  let plan;
  try { plan = await upload("/api/data/edit-pack/preview", file); } catch (e) { toast(e.message, "err"); return; }
  const choices = {};
  const blocked = plan.errors.length > 0;
  const go = button("Apply changes", { kind: "primary", disabled: blocked || !plan.has_changes });
  const conflictRows = plan.conflicts.map((c) => {
    const key = `${c.table}|${c.pk}`;
    const name = `c-${key}`;
    const radio = (value, label) => {
      const r = h("input", { type: "radio", name, value });
      r.addEventListener("change", () => { choices[key] = value; });
      return h("label", { class: "check", style: { marginRight: "12px" } }, r, label);
    };
    return h("tr", {}, h("td", {}, titleCase(c.table)), h("td", {}, c.pk),
      h("td", {}, c.cols.map((col) => h("div", {}, `${col}: now “${c.live[col]}” · file “${c.imported[col]}”`))),
      h("td", {}, radio("live", "Keep current"), radio("import", "Use file")));
  });
  const setAll = (v) => {
    for (const c of plan.conflicts) choices[`${c.table}|${c.pk}`] = v;
    for (const r of document.querySelectorAll(".conflicts input[type=radio]")) r.checked = r.value === v;
  };
  const counts = Object.entries(plan.counts).map(([t, c]) => `${titleCase(t)}: ${c.added} new, ${c.modified} changed`);
  const m = modal({ title: "Review edited pack", size: "wide", body: h("div", {},
    h("p", {}, `Exported ${fmtDate(plan.exported_at)}.`),
    plan.stale_tables.length ? h("div", { class: "banner" }, icon("alert"), `Records changed in CafeBooks since this pack was downloaded: ${plan.stale_tables.map(titleCase).join(", ")}.`) : null,
    counts.length ? h("ul", {}, counts.map((c) => h("li", {}, c))) : h("p", {}, "No differences found: nothing to import."),
    plan.conflicts.length ? [h("h3", {}, `Conflicts (${plan.conflicts.length}) — changed both in CafeBooks and in the file`),
      h("div", { class: "actions" }, button("Keep all current", { small: true, onClick: () => setAll("live") }),
        button("Use file for all", { small: true, onClick: () => setAll("import") })),
      h("div", { class: "table-scroll conflicts", style: { maxHeight: "260px", minHeight: 0 } }, h("table", { class: "data" }, h("tbody", {}, conflictRows)))] : null,
    h("h3", {}, "Checks"), issuesList(plan.errors, plan.warnings)),
  footer: [button("Cancel", { onClick: () => m.close() }), go] });
  go.addEventListener("click", () => busy(go, async () => {
    const missing = plan.conflicts.filter((c) => !choices[`${c.table}|${c.pk}`]);
    if (missing.length) { toast(`Choose “Keep current” or “Use file” for all ${missing.length} conflict(s).`, "err"); return; }
    const resolutions = plan.conflicts.map((c) => ({ table: c.table, pk: c.pk, choice: choices[`${c.table}|${c.pk}`] }));
    try {
      const r = await upload("/api/data/edit-pack/apply", file, { resolutions: JSON.stringify(resolutions) });
      m.close();
      toast(r.message);
      reload();
    } catch (e) { toast(e.message, "err"); }
  }));
}
