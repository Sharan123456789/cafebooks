// JSON API client.  Each request carries the name typed in the top bar for the audit log.
export class ApiError extends Error {
  constructor(message, status, body = {}) {
    super(message);
    this.status = status;
    this.confirm = body.confirm || null;
    this.setup = !!body.setup;
  }
}

const NAME_KEY = "cafebooks.user";
export const userName = () => localStorage.getItem(NAME_KEY) || "";
export const setUserName = (v) => localStorage.setItem(NAME_KEY, String(v || "").trim().slice(0, 60));

let onMutation = () => {};
export const setMutationListener = (fn) => { onMutation = fn; };

async function request(method, path, body, { raw = false } = {}) {
  // X-Requested-With proves the request came from this app (the server refuses saves without it).
  const headers = { "X-CafeBooks-User": encodeURIComponent(userName()), "X-Requested-With": "CafeBooks" };
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, cache: "no-store" });
  } catch {
    throw new ApiError("Cannot reach the CafeBooks server. Check the internet connection and try again. Nothing new was saved.", 0);
  }
  if (raw && res.ok) return res;
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok || !data || data.ok === false) {
    const fallback = res.status === 413 ? "The file is too large to upload." : `The server answered with an error (${res.status}).`;
    throw new ApiError((data && data.error) || fallback, res.status, data || {});
  }
  if (method !== "GET") onMutation();
  return data.data;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body ?? {});
export const patch = (path, body) => request("PATCH", path, body ?? {});
export const put = (path, body) => request("PUT", path, body ?? {});
export const del = (path) => request("DELETE", path);
export const upload = (path, file, fields = {}) => {
  const fd = new FormData();
  fd.append("file", file, file.name);
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  return request("POST", path, fd);
};
export const qs = (params) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};

export async function download(path) {
  const res = await request("GET", path, undefined, { raw: true });
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : "download";
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return name;
}
