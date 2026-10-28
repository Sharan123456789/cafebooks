// App shell: navigation, routing, shared-data change notices, welcome tour.
import { ApiError, get, setMutationListener, userName, setUserName } from "./api.js";
import { state, h, clear, icon, button, modal, toast, modalOpen, formDialog } from "./ui.js";
import * as pages from "./pages.js";
import * as admin from "./admin.js";

const ROUTES = [
  ["dashboard", "Dashboard", "dashboard", pages.dashboard],
  ["quick", "Quick Entry", "quick", pages.quick],
  ["bills", "Purchase Bills", "bills", pages.bills],
  ["payments", "Supplier Payments", "payments", pages.payments],
  ["sales", "Daily Sales", "sales", pages.sales],
  ["reports", "Reports", "reports", admin.reports],
  ["vendors", "Vendors", "vendors", pages.vendors],
  ["tips", "Tips & Charges", "tips", pages.tips],
  ["settings", "Settings", "settings", admin.settings],
];
const EXTRA = { activity: ["Activity Log", admin.activity] };
const root = document.getElementById("app");
let lastVersion = null, quietUntil = 0;

function route() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query = ""] = raw.split("?");
  return { name: name || "dashboard", params: new URLSearchParams(query) };
}

// ------------------------------------------------------------------ shell
let shell;
function buildShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    ROUTES.map(([id, label, ic], i) => h("a", { href: `#/${id}`, "data-route": id, title: i < 9 ? `${label} (Alt+${i + 1})` : label }, icon(ic), label)),
    h("div", { class: "nav-sep", role: "separator" }),
    h("a", { href: "#/activity", "data-route": "activity" }, icon("activity"), "Activity Log"),
    h("a", { href: "#", onclick: (e) => { e.preventDefault(); welcome(); } }, icon("help"), "Welcome & tour"));
  const sub = h("div", { class: "brand-sub" });
  const foot = h("div", { class: "side-foot" });
  const sidebar = h("aside", { class: "sidebar", id: "sidebar" },
    h("div", { class: "brand" }, icon("coffee"), "CafeBooks"), sub, nav, foot);
  const userBtn = h("button", { type: "button", class: "user-chip", onclick: askName });
  const menuBtn = button("", { kind: "ghost menu-btn", icon: "menu", ariaLabel: "Open menu", onClick: () => app.classList.toggle("nav-open") });
  menuBtn.setAttribute("aria-controls", "sidebar");
  const dbChip = h("span", { class: "db-chip" });
  const banner = h("div", { id: "changes" });
  const view = h("div", { class: "content", id: "main", tabindex: "-1" });
  const app = h("div", { class: "app" }, sidebar,
    h("div", { class: "main" }, h("header", { class: "topbar" }, menuBtn, h("span", { class: "spacer" }), dbChip, userBtn),
      h("main", {}, h("div", { class: "content", style: { paddingBottom: 0 } }, banner), view)));
  app.addEventListener("click", (e) => { if (app.classList.contains("nav-open") && e.target === app) app.classList.remove("nav-open"); });
  clear(root).append(h("a", { class: "skip-link", href: "#main", onclick: (e) => { e.preventDefault(); view.focus(); } }, "Skip to content"), app);
  shell = { app, nav, sub, foot, userBtn, dbChip, banner, view };
  refreshShell();
}

function refreshShell() {
  const b = state.boot;
  shell.sub.textContent = state.config.cafe_name;
  shell.foot.textContent = `v${b.app_version} · ${b.mode === "desktop" ? "Desktop app" : "Web app"}`;
  clear(shell.dbChip).append(icon("database"), `${b.database.engine}${b.mode === "desktop" ? " · this computer" : " · shared"}`);
  const name = userName();
  clear(shell.userBtn).append(icon("user"), h("span", {}, name || "Set your name"));
  shell.userBtn.setAttribute("aria-label", name ? `You are ${name}. Change name` : "Set your name for the activity log");
  document.title = `CafeBooks · ${state.config.cafe_name}`;
}

async function render() {
  const { name, params } = route();
  const known = ROUTES.find((r) => r[0] === name) || (EXTRA[name] && [name, ...EXTRA[name]]);
  if (!known) { location.hash = "#/dashboard"; return; }
  const fn = known.length === 4 ? known[3] : known[2];
  for (const a of shell.nav.querySelectorAll("a[data-route]")) {
    if (a.dataset.route === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  shell.app.classList.remove("nav-open");
  clear(shell.banner);
  const view = clear(shell.view);
  view.append(h("p", { style: { color: "var(--muted)" }, "aria-live": "polite" }, "Loading…"));
  const ctx = {
    params,
    refresh: render,
    reloadApp: async () => { await loadBoot(); refreshShell(); render(); },
    onSettingsChanged: () => refreshShell(),
  };
  try {
    const frag = h("div");
    await fn(frag, ctx);
    clear(view).append(...frag.childNodes);
    const title = view.querySelector("#page-title");
    if (title && document.activeElement !== document.body) title.focus({ preventScroll: true });
  } catch (e) {
    clear(view).append(pages.pageHead(known[1]), h("div", { class: "banner error", role: "alert" }, icon("alert"),
      h("span", {}, e.message || String(e)), button("Try again", { small: true, onClick: render })));
  }
}

// ------------------------------------------------------------------ shared data change notices
async function checkVersion(silent) {
  try {
    const v = await get("/api/version");
    state.today = v.today;
    if (lastVersion !== null && v.version !== lastVersion && !silent && Date.now() > quietUntil) {
      clear(shell.banner).append(h("div", { class: "banner", role: "status" }, icon("refresh"),
        h("span", {}, "Someone else saved changes."), button("Refresh", { small: true, kind: "primary", onClick: () => render() })));
    }
    lastVersion = v.version;
  } catch { /* offline: try again later */ }
}
setMutationListener(() => { quietUntil = Date.now() + 1500; setTimeout(() => checkVersion(true), 300); });
setInterval(() => { if (document.visibilityState === "visible" && !modalOpen()) checkVersion(false); }, 30000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkVersion(false); });

// ------------------------------------------------------------------ name, welcome, tour
function askName() {
  formDialog({
    title: "Who is using CafeBooks?", size: "narrow", submitLabel: "Save name",
    intro: "Your name is saved on this device only and written next to every change in the activity log. It is not a login.",
    fields: [{ key: "name", label: "Your name", value: userName(), max: 60, placeholder: "e.g. Priya (counter)" }],
    onSubmit: async (v) => { setUserName(v.name); localStorage.setItem("cafebooks.namePrompted", "1"); refreshShell(); },
  });
}

const TOUR = [
  ["Start here: Quick Entry", "Quick Entry is your hub for the day's work: daily sales, purchase bills, supplier payments, new vendors and tips. Every form checks the entry before saving.", "quick"],
  ["Set up your vendors", "Add each supplier once before recording their bills or payments. Keep invoice numbers exactly as printed so duplicate bills are caught. Vendors with history are deactivated, never deleted.", "vendors"],
  ["Record the day's sales", "At closing, enter the till totals, how customers paid, tips and the cash count. The form shows straight away whether collections match sales, so mistakes are caught before saving.", "sales"],
  ["Bills first, payments second", "A purchase bill is the expense. A supplier payment is money leaving the business and is matched (allocated) to bills. Money not allocated stays visible as unallocated until you match it.", "bills"],
  ["Read the dashboard", "Pick a period to see sales, what you owe suppliers and how money moved. Cash movement is not profit: stock, wages and rent are not tracked here.", "dashboard"],
  ["Shared books, backups and reports", "Everyone using this CafeBooks link sees the same books. Put your name at the top so the Activity Log shows who did what. Download backups regularly from Settings → Backups & data.", "reports"],
];

function tour(i = 0) {
  const [title, text, target] = TOUR[i];
  const last = i === TOUR.length - 1;
  const m = modal({
    title: `${i + 1}. ${title}`,
    body: h("div", {}, h("p", { style: { color: "var(--muted)", margin: 0, fontSize: "12px", fontWeight: 700, letterSpacing: ".06em" } }, `STEP ${i + 1} OF ${TOUR.length}`),
      h("div", { class: "progress", role: "progressbar", "aria-valuemin": 1, "aria-valuemax": TOUR.length, "aria-valuenow": i + 1 },
        h("div", { style: { width: `${(100 * (i + 1)) / TOUR.length}%` } })),
      h("p", { style: { fontSize: "16px" } }, text)),
    footer: [
      button("Back", { kind: "left", disabled: i === 0, onClick: () => { m.close(); tour(i - 1); } }),
      button("Go to this section", { onClick: () => { m.close(); location.hash = `#/${target}`; } }),
      button(last ? "Finish" : "Next", { kind: "primary", onClick: () => { m.close(); if (!last) tour(i + 1); } }),
    ],
  });
}

function welcome() {
  const keep = h("input", { type: "checkbox", id: "welcome-off" });
  keep.checked = localStorage.getItem("cafebooks.welcomeSeen") === "1";
  const close = () => { localStorage.setItem("cafebooks.welcomeSeen", keep.checked ? "1" : "0"); };
  const m = modal({
    title: "Welcome to CafeBooks", onClose: close,
    body: h("div", {},
      h("p", { style: { fontSize: "17px", color: "var(--accent-text)" } }, `Everyday records for ${state.config.cafe_name}, in one place.`),
      h("p", {}, "Record daily sales, manage vendors and purchase bills, track supplier payments and review reports. ",
        state.boot.mode === "desktop" ? "Your records are stored on this computer." : "Everyone with this link works on the same shared books."),
      h("p", { class: "banner" }, icon("alert"), "Cash movement is not profit: stock cost, wages and other costs are not tracked, so CafeBooks does not calculate profit."),
      h("label", { class: "check", for: "welcome-off" }, keep, "Don't show this automatically again")),
    footer: [button("Maybe later", { onClick: () => m.close() }),
      button("Start the guided tour", { kind: "primary", onClick: () => { m.close(); tour(0); } })],
  });
}

// ------------------------------------------------------------------ start
async function loadBoot() {
  const boot = await get("/api/bootstrap");
  state.boot = boot;
  state.config = boot.config;
  state.today = boot.today;
  lastVersion = boot.version;
}

function setupScreen(err) {
  clear(root).append(h("div", { class: "setup" },
    h("div", { class: "brand", style: { paddingLeft: 0 } }, icon("coffee"), "CafeBooks"),
    h("h1", {}, err.setup ? "CafeBooks needs to be set up" : "CafeBooks cannot start"),
    h("div", { class: "banner error", role: "alert" }, icon("alert"), err.message),
    err.setup ? h("ol", {},
      h("li", {}, "In your Vercel project, open Storage and add a Postgres database (for example Neon). This sets DATABASE_URL."),
      h("li", {}, "Redeploy the project."),
      h("li", {}, "Reload this page.")) : null,
    button("Try again", { kind: "primary", icon: "refresh", onClick: start })));
}

async function start() {
  try {
    await loadBoot();
  } catch (e) {
    setupScreen(e instanceof ApiError ? e : new ApiError(String(e), 0));
    return;
  }
  buildShell();
  window.addEventListener("hashchange", render);
  document.addEventListener("keydown", (e) => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-9]$/.test(e.key) && !modalOpen()) {
      const r = ROUTES[Number(e.key) - 1];
      if (r) { e.preventDefault(); location.hash = `#/${r[0]}`; }
    }
  });
  await render();
  if (!localStorage.getItem("cafebooks.namePrompted") && !userName()) {
    localStorage.setItem("cafebooks.namePrompted", "1");
    askName();
  } else if (localStorage.getItem("cafebooks.welcomeSeen") !== "1") {
    welcome();
  }
}

start();
