// Shared UI toolkit.  User data is always inserted as text (never as HTML).
import { ApiError } from "./api.js";

export const state = { boot: null, config: null, today: "" };

// ------------------------------------------------------------------ DOM
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value" && "value" in el) el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  add(el, children);
  return el;
}

// Append children, skipping null/undefined/false (native append() would print "null").
export function add(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }

let uid = 0;
export const nextId = (p = "f") => `${p}${++uid}`;

// Lucide-style outline icons (static, trusted markup).
const ICONS = {
  dashboard: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  quick: '<circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/>',
  bills: '<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 17.5v-11"/>',
  payments: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  sales: '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  reports: '<path d="M3 3v18h18"/><path d="M18 17V9M13 17V5M8 17v-3"/>',
  vendors: '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M15 18H9"/><path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.62l-3.48-4.35A1 1 0 0 0 17.52 8H14"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
  tips: '<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18"/><path d="M7 6h1v4"/><path d="m16.71 13.88.7.71-2.82 2.82"/>',
  settings: '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>',
  activity: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
  coffee: '<path d="M10 2v2M14 2v2M6 2v2"/><path d="M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1"/>',
  link: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  ban: '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
  chart: '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
  wallet: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
};

export function icon(name, label) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.innerHTML = ICONS[name] || "";
  if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); }
  else svg.setAttribute("aria-hidden", "true");
  return svg;
}

export function button(label, opts = {}) {
  const b = h("button", { type: opts.type || "button", class: `btn ${opts.kind || ""} ${opts.small ? "small" : ""}`.trim(),
    title: opts.title, "aria-label": opts.ariaLabel, onclick: opts.onClick, disabled: opts.disabled });
  if (opts.icon) b.append(icon(opts.icon));
  if (label) b.append(h("span", {}, label));
  return b;
}

export async function busy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  const spin = h("span", { class: "spinner", "aria-hidden": "true" });
  btn.prepend(spin);
  try { return await fn(); } finally { spin.remove(); btn.disabled = false; }
}

// ------------------------------------------------------------------ money & dates
export function toPaise(value) {
  const s = String(value ?? "").replace(/[,\s₹]/g, "").replace(/^Rs\.?/i, "");
  if (s === "") return null;
  const m = /^(-)?(\d+)(?:\.(\d{1,2}))?$/.exec(s);
  if (!m) return NaN;
  const p = Number(m[2]) * 100 + Number(((m[3] || "") + "00").slice(0, 2));
  return m[1] ? -p : p;
}

export function fromPaise(p) {
  const neg = p < 0; p = Math.abs(p);
  return `${neg ? "-" : ""}${Math.floor(p / 100)}.${String(p % 100).padStart(2, "0")}`;
}

export function money(value, symbol = true) {
  if (value === null || value === undefined || value === "") return "";
  const p = typeof value === "number" ? Math.round(value * 100) : toPaise(value);
  if (p === null || Number.isNaN(p)) return String(value);
  const cfg = state.config || {};
  const neg = p < 0;
  let whole = String(Math.floor(Math.abs(p) / 100));
  const frac = String(Math.abs(p) % 100).padStart(2, "0");
  if (cfg.indian_number_grouping !== false && whole.length > 3) {
    const tail = whole.slice(-3);
    let head = whole.slice(0, -3);
    const groups = [];
    while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) groups.unshift(head);
    whole = [...groups, tail].join(",");
  } else {
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  return `${neg ? "-" : ""}${symbol ? (cfg.currency_symbol || "₹") : ""}${whole}.${frac}`;
}

export function fmtDate(value) {
  if (!value) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(value));
  if (!m) return String(value);
  const style = (state.config && state.config.date_display) || "DD-MM-YYYY";
  const d = style === "YYYY-MM-DD" ? `${m[1]}-${m[2]}-${m[3]}` : style === "DD/MM/YYYY" ? `${m[3]}/${m[2]}/${m[1]}` : `${m[3]}-${m[2]}-${m[1]}`;
  return m[4] ? `${d} ${m[4]}:${m[5]}` : d;
}

export function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export const today = () => state.today || new Date().toISOString().slice(0, 10);
export const titleCase = (s) => String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const STATUS = {
  Active: "ok", Paid: "ok", OK: "ok", Unpaid: "warn", "Partially Paid": "info", Overdue: "bad", Cancelled: "mute",
  Voided: "mute", Reversed: "mute", Reversal: "mute", Inactive: "mute",
};
export function badge(text) {
  return h("span", { class: `badge ${STATUS[text] || (String(text).startsWith("OK") ? "ok" : "warn")}` }, text);
}

// ------------------------------------------------------------------ toasts
export function toast(message, kind = "ok") {
  const box = document.getElementById("toasts");
  const t = h("div", { class: `toast ${kind}`, role: kind === "err" ? "alert" : "status" },
    icon(kind === "err" ? "alert" : "check"), h("div", {}, message));
  box.append(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 4000);
}

// ------------------------------------------------------------------ modal
let openCount = 0;
export const modalOpen = () => openCount > 0;

export function modal({ title, body, footer, size = "", onClose, dismissable = true }) {
  const prev = document.activeElement;
  const titleId = nextId("m");
  const close = () => {
    if (!backdrop.isConnected) return;
    backdrop.remove();
    openCount--;
    document.removeEventListener("keydown", onKey, true);
    if (prev && prev.focus) prev.focus();
    if (onClose) onClose();
  };
  const closeBtn = button("", { kind: "ghost small", icon: "close", ariaLabel: "Close", onClick: close });
  const box = h("div", { class: `modal ${size}`, role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("div", { class: "modal-head" }, h("h2", { id: titleId }, title), dismissable ? closeBtn : null),
    h("div", { class: "modal-body" }, body),
    footer ? h("div", { class: "modal-foot" }, footer) : null);
  const backdrop = h("div", { class: "modal-backdrop" }, box);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop && dismissable) close(); });
  function onKey(e) {
    if (!backdrop.isConnected) return;
    if (e.key === "Escape" && dismissable) { e.preventDefault(); close(); return; }
    if (e.key === "Tab") {
      const f = [...box.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
        .filter((x) => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  }
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  openCount++;
  setTimeout(() => {
    if (box.contains(document.activeElement)) return;      // never steal focus from someone already typing
    const first = box.querySelector(".modal-body input:not([type=hidden]), .modal-body select, .modal-body textarea, .modal-foot .primary");
    (first || closeBtn).focus();
  }, 30);
  return { close, box };
}

export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } m.close(); };
    const yes = button(confirmLabel, { kind: danger ? "danger solid" : "primary", onClick: () => finish(true) });
    const m = modal({
      title, size: "narrow", body: h("p", { style: { whiteSpace: "pre-wrap" } }, message),
      footer: [button("Cancel", { onClick: () => finish(false) }), yes],
      onClose: () => { if (!done) { done = true; resolve(false); } },
    });
    setTimeout(() => yes.focus(), 40);
  });
}

// ------------------------------------------------------------------ forms
// field: {key, label, type, value, options, required, hint, placeholder, full, min, max, readonly}
// types: text | money | date | select | textarea | number | email | tel | url | checkbox | group
function buildField(f, onInput) {
  const id = nextId("in");
  const hintId = `${id}-h`, errId = `${id}-e`;
  let input;
  const common = { id, name: f.key, "aria-describedby": `${hintId} ${errId}`, required: f.required, readonly: f.readonly };
  if (f.type === "select") {
    input = h("select", common, (f.options || []).map((o) => {
      const opt = typeof o === "object" ? o : { value: o, label: o === "" ? "— none —" : o };
      return h("option", { value: opt.value }, opt.label);
    }));
    // Keep the first option selected unless the given value is one of the options.
    if (f.value !== undefined && f.value !== null && [...input.options].some((o) => o.value === String(f.value))) {
      input.value = String(f.value);
    }
  } else if (f.type === "textarea") {
    input = h("textarea", { ...common, rows: 3, placeholder: f.placeholder, maxlength: f.max || 2000 });
    input.value = f.value ?? "";
  } else if (f.type === "checkbox") {
    input = h("input", { ...common, type: "checkbox" });
    input.checked = !!f.value;
    const wrap = h("div", { class: `field ${f.full ? "full" : ""}` },
      h("label", { class: "check", for: id }, input, h("span", {}, f.label)),
      f.hint ? h("span", { class: "hint", id: hintId }, f.hint) : null);
    input.addEventListener("change", onInput);
    return { wrap, input, get: () => input.checked, setError: () => {} };
  } else {
    const type = { money: "text", date: "date", number: "number", email: "email", tel: "tel", url: "url" }[f.type] || "text";
    input = h("input", { ...common, type, placeholder: f.placeholder, min: f.min, max: f.type === "date" ? f.maxDate : undefined,
      inputmode: f.type === "money" ? "decimal" : undefined, autocomplete: "off", maxlength: f.max });
    let v = f.value ?? "";
    if (f.type === "money" && v !== "" && toPaise(v) === 0 && f.blankZero) v = "";
    input.value = v;
  }
  const err = h("span", { class: "error", id: errId, "aria-live": "polite" });
  const setError = (msg) => { err.textContent = msg || ""; input.setAttribute("aria-invalid", msg ? "true" : "false"); };
  const validate = () => {
    const v = input.value.trim();
    if (f.required && !v) return setError(`${f.label.replace(/\s*\*$/, "")} is required.`), false;
    if (f.type === "money" && v) {
      const p = toPaise(v);
      if (Number.isNaN(p)) return setError("Enter an amount like 1250 or 1250.50 (up to 2 decimals)."), false;
      if (p < 0) return setError("Amount cannot be negative."), false;
    }
    setError("");
    return true;
  };
  input.addEventListener("blur", validate);
  input.addEventListener("input", () => { if (input.getAttribute("aria-invalid") === "true") validate(); onInput(); });
  if (f.type === "select") input.addEventListener("change", onInput);
  const wrap = h("div", { class: `field ${f.full || f.type === "textarea" ? "full" : ""}` },
    h("label", { for: id }, f.label, f.required ? h("span", { class: "req", "aria-hidden": "true" }, "*") : null),
    input, f.hint ? h("span", { class: "hint", id: hintId }, f.hint) : null, err);
  const get = () => (f.type === "money" ? input.value.replace(/[,\s₹]/g, "") : f.type === "textarea" ? input.value : input.value.trim());
  return { wrap, input, get, validate, setError };
}

export function buildForm(fields, onChange = () => {}) {
  const controls = {};
  const grid = h("div", { class: "form-grid" });
  const render = (list, into) => {
    for (const f of list) {
      if (!f) continue;
      if (f.type === "group") {
        const inner = h("div", { class: "form-grid" });
        into.append(h("fieldset", { class: "group" }, h("legend", {}, f.label), inner));
        render(f.fields, inner);
        continue;
      }
      const c = buildField(f, () => onChange(values()));
      controls[f.key] = c;
      into.append(c.wrap);
    }
  };
  const values = () => Object.fromEntries(Object.entries(controls).map(([k, c]) => [k, c.get()]));
  render(fields, grid);
  const validate = () => {
    let first = null;
    for (const c of Object.values(controls)) if (c.validate && !c.validate() && !first) first = c.input;
    if (first) first.focus();
    return !first;
  };
  return { el: grid, controls, values, validate };
}

// Modal form with server validation, "possible duplicate" confirmation and idempotent retries.
export function formDialog({ title, fields, submitLabel = "Save", onSubmit, size = "", intro, extra, onChange, successMessage }) {
  const requestId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/-/g, "");
  const flags = {};
  const errorBox = h("div", { class: "form-error", role: "alert", hidden: true });
  const form = buildForm(fields, (v) => onChange && onChange(v, form));
  const submit = button(submitLabel, { kind: "primary", type: "submit" });
  const formEl = h("form", { novalidate: true }, intro ? h("p", { class: "sub", style: { color: "var(--muted)", marginTop: 0 } }, intro) : null,
    errorBox, form.el, extra || null);
  const m = modal({ title, size, body: formEl, footer: [button("Cancel", { onClick: () => m.close() }), submit] });
  submit.setAttribute("form", (formEl.id = nextId("form")));
  formEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.hidden = true;
    if (!form.validate()) return;
    await busy(submit, async () => {
      for (;;) {
        try {
          const result = await onSubmit({ ...form.values(), request_id: requestId, ...flags }, form);
          if (result === false) return;
          m.close();
          if (successMessage) toast(successMessage);
          return;
        } catch (err) {
          if (err instanceof ApiError && err.confirm) {
            const ok = await confirmDialog({ title: "Please confirm", message: `${err.message}\n\nSave anyway?`, confirmLabel: "Save anyway" });
            if (!ok) return;
            flags[err.confirm] = true;
            continue;
          }
          errorBox.textContent = err.message || String(err);
          errorBox.hidden = false;
          errorBox.scrollIntoView({ block: "nearest" });
          return;
        }
      }
    });
  });
  if (onChange) setTimeout(() => onChange(form.values(), form), 0);
  return { modal: m, form };
}

export const reasonDialog = (title, onSubmit, opts = {}) => formDialog({
  title, size: "narrow", submitLabel: opts.submitLabel || "Confirm", intro: opts.intro,
  fields: [...(opts.fields || []), { key: "reason", label: "Reason", type: "textarea", required: true, hint: "Saved in the activity log." }],
  onSubmit, successMessage: opts.successMessage,
});

// ------------------------------------------------------------------ data table
// columns: [{key, label, type: money|date|datetime|status|number|text|link, render?}]
export function dataTable({ columns, rows, search = true, dateKey, dateLabel, choices = {}, totals = [], rowKey, onSelect,
  onOpen, emptyTitle = "Nothing here yet", emptyText = "", initialSort }) {
  const PAGE = 300;
  const st = { q: "", from: "", to: "", choice: {}, sort: initialSort || null, limit: PAGE, selected: null };
  const info = h("span", { class: "info", "aria-live": "polite" });
  const toolbar = h("div", { class: "toolbar" });
  if (search) {
    const s = h("input", { type: "search", placeholder: "Search…", "aria-label": "Search records" });
    s.addEventListener("input", () => { st.q = s.value.trim().toLowerCase(); st.limit = PAGE; draw(); });
    toolbar.append(h("div", { class: "search" }, icon("search"), s));
  }
  if (dateKey) {
    const f = h("input", { type: "date", "aria-label": `${dateLabel || "Date"} from` });
    const t = h("input", { type: "date", "aria-label": `${dateLabel || "Date"} to` });
    f.addEventListener("change", () => { st.from = f.value; draw(); });
    t.addEventListener("change", () => { st.to = t.value; draw(); });
    toolbar.append(h("label", { class: "sr-only" }, "From"), f, h("span", { "aria-hidden": "true", style: { color: "var(--muted)" } }, "to"), t);
  }
  for (const [key, cfg] of Object.entries(choices)) {
    const label = cfg.label || titleCase(key);
    const values = cfg.values || cfg;
    const sel = h("select", { "aria-label": label },
      h("option", { value: "" }, `All ${label.toLowerCase()}`), values.map((v) => h("option", { value: v }, v)));
    if (cfg.initial) { sel.value = cfg.initial; st.choice[key] = cfg.initial; }
    sel.addEventListener("change", () => { st.choice[key] = sel.value; draw(); });
    toolbar.append(sel);
  }
  toolbar.append(info);

  const thead = h("thead");
  const tbody = h("tbody");
  const tfoot = h("tfoot");
  const table = h("table", { class: "data" }, thead, tbody, tfoot);
  const scroll = h("div", { class: "table-scroll" }, table);
  const more = h("div", { class: "more-row", hidden: true });
  const card = h("div", { class: "card table-card" }, toolbar, scroll, more);

  const cellText = (c, r) => {
    const v = r[c.key];
    if (c.type === "money") return money(v, false);
    if (c.type === "date" || c.type === "datetime") return fmtDate(v);
    if (typeof v === "boolean") return v ? "Yes" : "No";
    return v ?? "";
  };
  const sortVal = (c, r) => {
    const v = r[c.key];
    if (c.type === "money" || c.type === "number") { const p = c.type === "money" ? toPaise(v) : Number(v); return Number.isNaN(p) || p === null ? -Infinity : p; }
    return String(cellText(c, r)).toLowerCase();
  };

  function headerRow() {
    clear(thead).append(h("tr", {}, columns.map((c) => {
      const dir = st.sort && st.sort.key === c.key ? st.sort.dir : null;
      return h("th", { class: c.type || "", scope: "col", "aria-sort": dir ? (dir === 1 ? "ascending" : "descending") : "none" },
        h("button", { type: "button", onclick: () => { st.sort = { key: c.key, dir: dir === 1 ? -1 : 1 }; draw(); } },
          c.label || titleCase(c.key), dir ? h("span", { "aria-hidden": "true" }, dir === 1 ? "▲" : "▼") : null));
    })));
  }

  function filtered() {
    return rows.filter((r) => {
      if (st.q && !columns.some((c) => String(cellText(c, r)).toLowerCase().includes(st.q)) &&
        !Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(st.q))) return false;
      if (dateKey) {
        const d = String(r[dateKey] || "");
        if ((st.from && d < st.from) || (st.to && d > st.to)) return false;
      }
      for (const [k, v] of Object.entries(st.choice)) if (v && String(r[k] ?? "") !== v) return false;
      return true;
    });
  }

  function select(row, tr) {
    st.selected = row;
    for (const x of tbody.querySelectorAll("tr[aria-selected='true']")) x.setAttribute("aria-selected", "false");
    if (tr) tr.setAttribute("aria-selected", "true");
    if (onSelect) onSelect(row);
  }

  function draw() {
    headerRow();
    let list = filtered();
    if (st.sort) {
      const c = columns.find((x) => x.key === st.sort.key);
      if (c) list = [...list].sort((a, b) => { const x = sortVal(c, a), y = sortVal(c, b); return x < y ? -st.sort.dir : x > y ? st.sort.dir : 0; });
    }
    clear(tbody);
    if (!list.length) {
      tbody.append(h("tr", {}, h("td", { colspan: columns.length, class: "empty" },
        h("strong", {}, rows.length ? "No matching records" : emptyTitle), rows.length ? "Try clearing the filters." : emptyText)));
    }
    const keyOf = (r) => (rowKey ? r[rowKey] : r);
    for (const r of list.slice(0, st.limit)) {
      const selected = st.selected && keyOf(st.selected) === keyOf(r);
      const tr = h("tr", { tabindex: "0", "aria-selected": selected ? "true" : "false" }, columns.map((c) => {
        let content;
        if (c.render) content = c.render(r);
        else if (c.type === "status") content = badge(cellText(c, r));
        else content = cellText(c, r);
        const neg = c.type === "money" && String(r[c.key] || "").startsWith("-");
        const title = typeof content === "string" && content.length > 30 ? content : undefined;
        return h("td", { class: `${c.type || ""} ${neg ? "neg" : ""}`, title }, content);
      }));
      tr.addEventListener("click", () => select(r, tr));
      tr.addEventListener("dblclick", () => onOpen && onOpen(r));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(r, tr); if (e.key === "Enter" && onOpen) onOpen(r); }
        if (e.key === "ArrowDown" && tr.nextElementSibling) { e.preventDefault(); tr.nextElementSibling.focus(); }
        if (e.key === "ArrowUp" && tr.previousElementSibling) { e.preventDefault(); tr.previousElementSibling.focus(); }
      });
      tbody.append(tr);
    }
    if (st.selected && !list.some((r) => keyOf(r) === keyOf(st.selected))) select(null, null);
    clear(tfoot);
    if (totals.length && list.length) {
      tfoot.append(h("tr", {}, columns.map((c, i) => {
        if (totals.includes(c.key)) {
          const sum = list.reduce((acc, r) => { const p = toPaise(r[c.key]); return acc + (Number.isNaN(p) || p === null ? 0 : p); }, 0);
          return h("td", { class: `money ${sum < 0 ? "neg" : ""}` }, money(fromPaise(sum), false));
        }
        return h("td", {}, i === 0 ? "Total" : "");
      })));
    }
    info.textContent = `${list.length} of ${rows.length} record${rows.length === 1 ? "" : "s"}`;
    more.hidden = list.length <= st.limit;
    clear(more).append(button(`Show more (${list.length - st.limit} hidden)`, { small: true, onClick: () => { st.limit += PAGE; draw(); } }));
  }
  draw();
  return { el: card, selected: () => st.selected, setRows: (r) => { rows = r; draw(); }, visible: filtered };
}

// ------------------------------------------------------------------ charts
export function donut(segments, centerTop, centerBottom) {
  const total = segments.reduce((a, s) => a + Math.max(0, s.value), 0);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 42 42");
  svg.setAttribute("class", "donut");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", total
    ? segments.map((s) => `${s.label} ${Math.round((100 * s.value) / total)}%`).join(", ")
    : "No collections recorded");
  const ring = (color, dash, offset) => {
    const c = document.createElementNS(NS, "circle");
    Object.entries({ cx: 21, cy: 21, r: 15.9155, fill: "none", stroke: color, "stroke-width": 5.2,
      "stroke-dasharray": dash, "stroke-dashoffset": offset }).forEach(([k, v]) => c.setAttribute(k, v));
    return c;
  };
  svg.append(ring("#3a3527", "100 0", 0));
  let acc = 0;
  for (const s of segments) {
    if (s.value <= 0 || !total) continue;
    const pct = (100 * s.value) / total;
    const seg = ring(s.color, `${Math.max(pct - 0.6, 0.1)} ${100 - Math.max(pct - 0.6, 0.1)}`, 25 - acc);
    const t = document.createElementNS(NS, "title");
    t.textContent = `${s.label}: ${Math.round(pct)}%`;
    seg.append(t);
    svg.append(seg);
    acc += pct;
  }
  const text = (y, size, fill, content, weight) => {
    const t = document.createElementNS(NS, "text");
    Object.entries({ x: 21, y, "text-anchor": "middle", fill, "font-size": size, "font-weight": weight || 400 }).forEach(([k, v]) => t.setAttribute(k, v));
    t.textContent = content;
    return t;
  };
  svg.append(text(19.2, 2.6, "#b3ac98", centerTop), text(24, String(centerBottom).length > 12 ? 2.8 : 3.4, "#f3f0e7", centerBottom, 700));
  return svg;
}

export function barChart(labels, series, opts = {}) {
  // series: [{name, color, values:[number]}]  (vertical grouped bars, SVG)
  const NS = "http://www.w3.org/2000/svg";
  const W = 900, H = 360, L = 90, R = 16, T = 18, B = 96;
  const all = series.flatMap((s) => s.values);
  const max = Math.max(1, ...all.map((v) => Math.max(0, v)));
  const min = Math.min(0, ...all);
  const span = max - min || 1;
  const y = (v) => T + ((max - v) / span) * (H - T - B);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", opts.label || "Chart");
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    svg.append(e);
    return e;
  };
  for (let i = 0; i <= 4; i++) {
    const v = min + (span * i) / 4;
    el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "#3a3527", "stroke-width": 1 });
    el("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", fill: "#b3ac98", "font-size": 12 }, money(v.toFixed(2)));
  }
  const n = Math.max(labels.length, 1);
  const slot = (W - L - R) / n;
  const bw = Math.max(2, Math.min(42, (slot * 0.78) / series.length));
  labels.forEach((lab, i) => {
    series.forEach((s, j) => {
      const v = s.values[i] || 0;
      const x = L + i * slot + (slot - bw * series.length) / 2 + j * bw;
      const r = el("rect", { x, y: Math.min(y(v), y(0)), width: bw - 1, height: Math.max(1, Math.abs(y(v) - y(0))), fill: s.color, rx: 2 });
      const t = document.createElementNS(NS, "title");
      t.textContent = `${lab} · ${s.name}: ${money(v.toFixed(2))}`;
      r.append(t);
    });
    if (n <= 40 || i % Math.ceil(n / 40) === 0) {
      const tx = L + i * slot + slot / 2;
      el("text", { x: tx, y: H - B + 16, fill: "#d7d1c2", "font-size": 11, "text-anchor": "end", transform: `rotate(-40 ${tx} ${H - B + 16})` },
        String(lab).length > 18 ? `${String(lab).slice(0, 17)}…` : lab);
    }
  });
  return svg;
}
