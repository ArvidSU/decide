"use strict";

// ------------------------------------------------------------------ state

// A link like http://server:8787/#token=… logs this browser in (the hash never reaches the server).
if (location.hash.startsWith("#token=")) {
  storage("token", decodeURIComponent(location.hash.slice(7)));
  history.replaceState(null, "", location.pathname + location.search);
}

const S = {
  config: null,
  token: storage("token") || "",
  status: storage("status") || "open",
  space: storage("space") || "",   // "" = every space this token sees
  q: "",
  listTags: (storage("listTags") || "").split(",").filter(Boolean), // the sidebar shows only decisions with any of these
  listInherit: storage("listInherit") !== "0", // ...counting the tags of the goals they're toward
  list: [],
  counts: {},
  d: null,            // current decision
  events: [],
  deliveries: [],
  cursor: 0,
  selection: new Set(),
  silenced: new Set(), // listener ids the next resolve won't notify ("resumer", "poll:<actor>", webhook id)
  focus: -1,
  editing: false,
  notes: {},          // unsent reason per decision id
  stale: false,       // a live update arrived while you were typing; applied when you leave the field
  ui: {},             // per-decision UI state: filters, view, sort, keepUnknown, text
  goals: [],          // every goal this token sees, with what each serves
  goal: null,         // current goal page, with its events and its part of the map
  goalEvents: [],
  goalGraph: null,
  editingGoal: false,
  addingTag: false,
};

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem("decisions." + key);
    if (value === null) localStorage.removeItem("decisions." + key);
    else localStorage.setItem("decisions." + key, value);
  } catch { return null; }
}

function ui() {
  const id = S.d.id;
  if (!S.ui[id]) {
    let saved = {};
    try { saved = JSON.parse(storage("ui." + id) || "{}"); } catch {}
    S.ui[id] = { filters: {}, view: null, sort: null, keepUnknown: true, text: "", ...saved };
  }
  return S.ui[id];
}
function saveUi() { storage("ui." + S.d.id, JSON.stringify(ui())); }

// ------------------------------------------------------------------ icons

// Stroke icons on a 24px grid (Lucide-style). icon(name, size) returns inline SVG.
const ICONS = {
  settings: '<path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.6a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7v.2a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.6a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  pencil: '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.7 5.7 12.6 12.6"/>',
  rotate: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>',
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
  send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  "eye-off": '<path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.2 3.2M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
  table: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  "arrow-right": '<path d="M5 12h14M13 5l7 7-7 7"/>',
  "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
  "arrow-down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  filter: '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  webhook: '<path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2"/><path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06"/><path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  "chevron-down": '<path d="m6 9 6 6 6-6"/>',
  sort: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
  wifi: '<path d="M12 20h.01M8.5 16.4a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 14 0M2 9.3a15 15 0 0 1 20 0"/>',
  play: '<path d="m6 3 14 9-14 9z"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.8-1.3"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  map: '<rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/><path d="M12 7v5M5 17v-2.5A2.5 2.5 0 0 1 7.5 12h9a2.5 2.5 0 0 1 2.5 2.5V17"/>',
};
const icon = (name, size = 16) =>
  `<svg class="i" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

// Linear-style status glyph for a decision: open (ring, tinted by urgency), resolved, cancelled, expired.
function statusIcon(d, size = 14) {
  const u = urgency(d);
  const g = {
    open: `<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="${u ? "none" : "2.6 1.7"}"/>${u ? `<circle cx="8" cy="8" r="2.4" fill="currentColor"/>` : ""}`,
    resolved: `<circle cx="8" cy="8" r="6.5" fill="currentColor"/><path d="m5.2 8.2 1.9 1.9 3.8-4" fill="none" stroke="var(--panel)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`,
    cancelled: `<circle cx="8" cy="8" r="6.5" fill="currentColor"/><path d="m5.6 5.6 4.8 4.8m0-4.8-4.8 4.8" fill="none" stroke="var(--panel)" stroke-width="1.6" stroke-linecap="round"/>`,
    expired: `<circle cx="8" cy="8" r="6.5" fill="currentColor"/><path d="M8 4.8V8l2 1.3" fill="none" stroke="var(--panel)" stroke-width="1.6" stroke-linecap="round"/>`,
  }[d.status] || "";
  return `<svg class="si ${d.status} ${u} ${isWaiting(d) ? "waiting" : ""}" viewBox="0 0 16 16" width="${size}" height="${size}" aria-label="${esc(d.status)}">${g}</svg>`;
}

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
// Focus is in a text field (not a checkbox or radio), so single-key shortcuts and live re-renders hold off.
const isTyping = (el = document.activeElement) =>
  !!el && (el.tagName === "TEXTAREA" || el.tagName === "SELECT" || (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit"].includes(el.type)));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let tokenAsk = null;
async function api(method, path, body) {
  const headers = { "Content-Type": "application/json", "X-Actor": S.config?.owner || "me" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) {
    // Requests fired together share one sign-in dialog.
    tokenAsk ||= ask({ title: "Sign in", body: "<p>This decisions server needs its API token.</p>", field: "Token", type: "password", ok: "Sign in", cancel: "" })
      .finally(() => (tokenAsk = null));
    const t = await tokenAsk;
    if (t) { S.token = t; storage("token", t); return api(method, path, body); }
    throw new Error("unauthorized");
  }
  const text = await res.text();
  const data = text && (res.headers.get("content-type") || "").includes("json") ? JSON.parse(text) : text;
  if (!res.ok) throw new Error(data?.error || res.statusText);
  return data;
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  if (s < 86400 * 30) return Math.round(s / 86400) + "d ago";
  return new Date(iso).toLocaleDateString();
}
const fullTime = (iso) => iso ? new Date(iso).toLocaleString() : "";
const whenTime = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Time left until an ISO timestamp, coarse: "3d 4h", "5h 12m", "12m", "<1m", or "now" once it has passed.
function until(iso) {
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "now";
  if (s < 60) return "<1m";
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 60)}m`;
}
// How pressing an open decision's deadline is: "urgent" under 6h, "soon" under a day.
function urgency(d) {
  if (d.status !== "open" || !d.due_at || isWaiting(d)) return "";
  const s = (new Date(d.due_at).getTime() - Date.now()) / 1000;
  return s < 6 * 3600 ? "urgent" : s < 86400 ? "soon" : "";
}
const byDeadline = (a, b) => (a.due_at || "9999").localeCompare(b.due_at || "9999");
// Waiting on another open decision: it can't be decided, or expire, until that one is. Not your concern yet.
const isWaiting = (d) => d.status === "open" && !!d.waiting_on?.length;
const actionable = (d) => d.status === "open" && !isWaiting(d);

// Errors stay until dismissed; actions are [{label, run}] (`undo` is shorthand for one "Undo" action).
let toastTimer;
function toast(msg, { undo, undoLabel, actions = [], error, info } = {}) {
  const el = $("#toast");
  if (undo) actions = [{ label: undoLabel || "Undo", run: undo }, ...actions];
  el.className = "toast" + (error ? " error" : "");
  el.setAttribute("role", error ? "alert" : "status");
  el.innerHTML = `<span class="msg">${icon(error ? "ban" : info ? "help" : "check", 15)}${esc(msg)}</span>` +
    actions.map((a, i) => `<button data-i="${i}">${esc(a.label)}</button>`).join("") +
    (error ? `<button data-close aria-label="Dismiss">${icon("x", 15)}</button>` : "");
  el.hidden = false;
  el.querySelectorAll("button[data-i]").forEach((b) => (b.onclick = () => { el.hidden = true; actions[b.dataset.i].run(); }));
  const close = el.querySelector("[data-close]");
  if (close) close.onclick = () => (el.hidden = true);
  clearTimeout(toastTimer);
  if (!error) toastTimer = setTimeout(() => (el.hidden = true), actions.length ? 7000 : 2600);
}
const fail = (e) => toast(e.message || String(e), { error: true });

// In-page replacement for prompt()/confirm(). Resolves to the field's text ("" without a field),
// or null when dismissed. `body` is HTML. With `choices` ([{value, label}]) the field is a select.
function ask({ title, body = "", field, value = "", placeholder = "", type = "text", ok = "OK", cancel = "Cancel", danger = false, choices }) {
  return new Promise((done) => {
    const dlg = document.createElement("dialog");
    dlg.className = "dialog";
    dlg.setAttribute("aria-labelledby", "dlg-title");
    dlg.innerHTML = `<form method="dialog">
      <h3 id="dlg-title">${esc(title)}</h3>
      ${body ? `<div class="dialog-body">${body}</div>` : ""}
      ${field !== undefined ? `<label class="dialog-field">${esc(field)}
        ${choices ? `<select class="input" name="v">${choices.map((c) => `<option value="${esc(c.value)}" ${c.value === value ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`
          : `<input class="input" name="v" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off">`}</label>` : ""}
      <div class="dialog-actions">
        ${cancel ? `<button type="button" class="btn" data-close>${esc(cancel)}</button>` : ""}
        <button class="btn ${danger ? "danger-fill" : "primary"}" value="ok">${esc(ok)}</button></div>
    </form>`;
    document.body.append(dlg);
    const input = dlg.querySelector("input, select");
    dlg.querySelector("[data-close]")?.addEventListener("click", () => dlg.close());
    dlg.addEventListener("close", () => { done(dlg.returnValue === "ok" ? (input?.value.trim() ?? "") : null); dlg.remove(); });
    dlg.showModal();
    if (input) { input.focus(); input.select?.(); }
  });
}
const dialogOpen = () => !!document.querySelector("dialog[open]");

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${what}`); }
  catch { await ask({ title: `Copy ${what}`, field: "The browser blocked the clipboard. Copy it from here:", value: text, ok: "Done", cancel: "" }); }
}

function md(src) {
  if (!src) return "";
  const blocks = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => { blocks.push(code); return `\u0000${blocks.length - 1}\u0000`; });
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  return src.split(/\n{2,}/).map((para) => {
    const code = para.match(/^\u0000(\d+)\u0000$/);
    if (code) return `<pre>${esc(blocks[+code[1]])}</pre>`;
    const lines = para.split("\n");
    if (lines.every((l) => /^\s*[-*] /.test(l))) return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*] /, ""))}</li>`).join("") + "</ul>";
    const h = para.match(/^(#{1,4}) (.*)$/);
    if (h) return `<p><strong>${inline(h[2])}</strong></p>`;
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

// ------------------------------------------------------------------ attributes & filtering

function attrKeys(d) {
  const keys = new Set(Object.keys(d.attribute_defs));
  for (const o of d.options) for (const k of Object.keys(o.attributes)) keys.add(k);
  return [...keys];
}

function attrMeta(d, key) {
  const def = d.attribute_defs[key] || {};
  const values = d.options.map((o) => o.attributes[key]).filter((v) => v !== null && v !== undefined);
  let type = def.type;
  if (!type) {
    if (!values.length) type = "text";
    else if (values.every((v) => typeof v === "number")) type = "number";
    else if (values.every((v) => typeof v === "boolean")) type = "boolean";
    else if (values.every((v) => Array.isArray(v))) type = "tags";
    else if (values.every((v) => typeof v === "string")) {
      const distinct = new Set(values);
      if (values.every((v) => /^https?:\/\//.test(v))) type = "url";
      else if (distinct.size <= 12 && (distinct.size < values.length || distinct.size <= 6)) type = "enum";
      else type = "text";
    } else type = "text";
  }
  const distinct = [...new Set(values.flatMap((v) => (Array.isArray(v) ? v : [v])).map(String))].sort();
  const nums = values.filter((v) => typeof v === "number");
  return {
    key, type, def,
    // Short keys are usually acronyms: "sso" reads as "SSO".
    label: def.label || (key.length <= 3 ? key.toUpperCase() : key.replace(/[_-]+/g, " ").replace(/^./, (c) => c.toUpperCase())),
    unit: def.unit || "",
    better: def.better,
    requested: !!def.requested,
    distinct,
    min: nums.length ? Math.min(...nums) : null,
    max: nums.length ? Math.max(...nums) : null,
  };
}

function filterActive(f) {
  if (!f) return false;
  return (f.min !== undefined && f.min !== "") || (f.max !== undefined && f.max !== "") ||
    (f.bool && f.bool !== "any") || (f.set && f.set.length) || (f.text && f.text.trim());
}

function passes(o, metas) {
  const u = ui();
  if (u.text.trim()) {
    const hay = [o.label, o.description, ...Object.values(o.attributes).map((v) => JSON.stringify(v))].join(" ").toLowerCase();
    if (!u.text.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))) return false;
  }
  for (const m of metas) {
    const f = u.filters[m.key];
    if (!filterActive(f)) continue;
    const v = o.attributes[m.key];
    if (v === null || v === undefined) { if (!u.keepUnknown) return false; continue; }
    if (f.min !== undefined && f.min !== "" && !(Number(v) >= Number(f.min))) return false;
    if (f.max !== undefined && f.max !== "" && !(Number(v) <= Number(f.max))) return false;
    if (f.bool && f.bool !== "any" && Boolean(v) !== (f.bool === "yes")) return false;
    if (f.set && f.set.length) {
      const vals = (Array.isArray(v) ? v : [v]).map(String);
      if (!vals.some((x) => f.set.includes(x))) return false;
    }
    if (f.text && f.text.trim() && !JSON.stringify(v).toLowerCase().includes(f.text.trim().toLowerCase())) return false;
  }
  return true;
}

function describeFilters(metas) {
  const u = ui();
  const parts = [];
  if (u.text.trim()) parts.push(`matches "${u.text.trim()}"`);
  for (const m of metas) {
    const f = u.filters[m.key];
    if (!filterActive(f)) continue;
    if (f.min !== undefined && f.min !== "") parts.push(`${m.label} ≥ ${f.min}${m.unit ? " " + m.unit : ""}`);
    if (f.max !== undefined && f.max !== "") parts.push(`${m.label} ≤ ${f.max}${m.unit ? " " + m.unit : ""}`);
    if (f.bool && f.bool !== "any") parts.push(`${m.label} = ${f.bool}`);
    if (f.set && f.set.length) parts.push(`${m.label} ∈ {${f.set.join(", ")}}`);
    if (f.text && f.text.trim()) parts.push(`${m.label} contains "${f.text.trim()}"`);
  }
  return parts.join(", ");
}

function partition() {
  const d = S.d;
  const metas = attrKeys(d).map((k) => attrMeta(d, k));
  const active = d.options.filter((o) => !o.excluded);
  let visible = active.filter((o) => passes(o, metas));
  const filtered = active.filter((o) => !visible.includes(o));
  const excluded = d.options.filter((o) => o.excluded);
  const sort = ui().sort;
  if (sort) {
    const val = (o) => (sort.key === "__label" ? o.label.toLowerCase() : o.attributes[sort.key]);
    visible = [...visible].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
      return sort.dir === "desc" ? -c : c;
    });
  }
  const best = {};
  for (const m of metas) {
    if (m.type !== "number" || !m.better) continue;
    const nums = visible.map((o) => o.attributes[m.key]).filter((v) => typeof v === "number");
    if (nums.length > 1) best[m.key] = m.better === "lower" ? Math.min(...nums) : Math.max(...nums);
  }
  return { metas, visible, filtered, excluded, best };
}

// Wraps a value with its provenance (estimate / source / note) from option.attribute_meta.
function cellHtml(v, m, meta) {
  const html = valueHtml(v, m);
  if (!meta || v == null) return html;
  const tip = [meta.estimate ? "Estimate" : "", meta.source ? "Source: " + meta.source : "", meta.note || ""].filter(Boolean).join(" · ");
  return `<span class="${meta.estimate ? "estimate" : "sourced"}" title="${esc(tip)}">${meta.estimate ? "≈ " : ""}${html}</span>`;
}

// Returns HTML; every branch escapes its own output.
function valueHtml(v, m) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? `<span class="bool yes">${icon("check", 13)}Yes</span>` : `<span class="bool no">${icon("x", 13)}No</span>`;
  if (Array.isArray(v)) return esc(v.map(String).join(", "));
  if (typeof v === "object") return esc(JSON.stringify(v));
  // "1 day", not "1 days", for the common time units.
  const unit = m.unit ? " " + esc(Math.abs(v) === 1 && /^(second|minute|hour|day|week|month|year)s$/i.test(m.unit) ? m.unit.slice(0, -1) : m.unit) : "";
  if (typeof v === "number") {
    const n = v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    return /^[$€£¥]$/.test(m.unit) ? esc(m.unit) + n : n + unit;  // "$41", "41 SEK"
  }
  if (/^https?:\/\//.test(v)) return `<a href="${esc(v)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(v.replace(/^https?:\/\/(www\.)?/, "").slice(0, 40))}</a>`;
  return esc(v) + unit;
}

// ------------------------------------------------------------------ data loading

async function loadList() {
  const [list, goals] = await Promise.all([api("GET", "/api/decisions?status=all&limit=500"), api("GET", "/api/goals?status=all")]);
  S.list = list.decisions;
  S.goals = goals.goals;
  renderSidebar();
}

// Spaces only show up in the GUI once there's more than one to tell apart.
const multiSpace = () => (S.config?.spaces || []).length > 1;
const inSpace = (d) => !S.space || d.space === S.space;
const spaceChip = (d) => multiSpace() ? `<span class="chip space" title="Space">${esc(d.space)}</span>` : "";

let loadSeq = 0;
async function loadDecision(id, { keepSelection = false } = {}) {
  const seq = ++loadSeq;
  const [d, ev, dl, ls] = await Promise.all([
    api("GET", `/api/decisions/${id}`),
    api("GET", `/api/decisions/${id}/events`),
    api("GET", `/api/deliveries?decision=${id}`),
    api("GET", `/api/decisions/${id}/listeners`),
  ]);
  let resumer;
  try { resumer = await api("GET", resumerPath(d)); } catch {}
  // A slower response for a decision you've since left must not replace the one you're looking at.
  if (seq !== loadSeq) return;
  S.listeners = ls.listeners;
  S.resumer = resumer;
  const switched = !S.d || S.d.id !== d.id;
  S.d = d;
  S.events = ev.events;
  S.deliveries = dl.deliveries;
  if (switched || !keepSelection) {
    S.selection = new Set(d.resolution ? d.resolution.selected : []);
    if (switched) { S.focus = -1; S.editing = S.addingTag = false; S.silenced = new Set(); }
  }
  for (const s of [...S.selection]) if (!d.options.some((o) => o.id === s)) S.selection.delete(s);
  renderDecision();
  renderSidebar();
  if (switched) announce(`Loaded ${d.id}: ${d.title}`);
}

// Screen-reader announcements that survive re-renders.
const announce = (text) => { const el = $("#sr-status"); if (el) { el.textContent = ""; setTimeout(() => (el.textContent = text), 50); } };

let pollAbort = 0;
async function poll() {
  const me = ++pollAbort;
  S.cursor = S.config.cursor;
  while (me === pollAbort) {
    try {
      const r = await api("GET", `/api/events?after=${S.cursor}&wait=${S.offline ? 0 : 25}&limit=500`);
      if (r.events.length) {
        S.cursor = r.cursor;
        await loadList();
        if (S.d && r.events.some((e) => e.decision_id === S.d.id)) {
          if (!isTyping()) await loadDecision(S.d.id, { keepSelection: true });
          else S.stale = true;
        } else if ((S.goal || location.pathname === "/map") && !isTyping() && !dialogOpen()) {
          // The goal page and the map show decisions too, so any change can matter there.
          await (S.goal ? loadGoal(S.goal.id) : renderMap());
        }
      }
      if (S.offline) { S.offline = false; $("#conn").hidden = true; await loadList(); if (S.d && !isTyping()) await loadDecision(S.d.id, { keepSelection: true }); }
    } catch {
      // Live updates stopped: say so instead of quietly showing stale data.
      if (!S.offline) { S.offline = true; $("#conn").hidden = false; }
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
}

// ------------------------------------------------------------------ routing

function navigate(path, replace) {
  if (replace) history.replaceState({}, "", path); else history.pushState({}, "", path);
  route();
}

function renderSkeleton() {
  $("#main").innerHTML = `<div class="page" aria-busy="true" aria-label="Loading"><div class="skel skel-line" style="width:120px"></div>
    <div class="skel skel-title"></div><div class="skel skel-line" style="width:40%"></div><div class="skel skel-line" style="width:70%;margin-top:24px"></div>
    <div class="skel skel-line" style="width:62%"></div><div class="skel skel-block"></div></div>`;
}
function renderError(e, retry = "retry") {
  $("#main").innerHTML = `<div class="page"><div class="empty-state error-state" role="alert"><span class="es-ic">${icon("wifi", 24)}</span>
    <h2>${/fetch|network/i.test(e.message) ? "Can't reach the decisions server" : "Something went wrong"}</h2>
    <p>${esc(e.message)}. Check that the server is running, then try again.</p>
    <button class="btn primary" data-action="${retry}">${icon("rotate", 15)}Retry</button></div></div>`;
}

async function route() {
  const path = location.pathname;
  drawer(false);
  const m = path.match(/^\/d\/([^/]+)/);
  const gm = path.match(/^\/g\/([^/]+)/);
  if ((m && S.d?.id !== decodeURIComponent(m[1])) || (gm && S.goal?.id !== decodeURIComponent(gm[1]))) renderSkeleton();
  try {
    if (m) { S.goal = null; return await loadDecision(decodeURIComponent(m[1])); }
    S.d = null;
    if (gm) return await loadGoal(decodeURIComponent(gm[1]));
    S.goal = null;
    renderSidebar();
    if (path === "/map") return await renderMap();
    if (path === "/settings") return await renderSettings();
    const first = S.list.filter((d) => actionable(d) && inSpace(d)).sort(byDeadline)[0];
    if (first) return navigate(`/d/${first.id}`, true);
    renderEmpty();
  } catch (e) {
    renderError(e);
  }
}

// ------------------------------------------------------------------ sidebar

// Replace an element's HTML only when it changed, so periodic refreshes keep focus, hover and scroll.
function setHtml(el, html) {
  if (!el || el.__html === html) return;
  withFocus(() => { el.innerHTML = html; el.__html = html; });
}

const STATUS_NAMES = { open: "Open", resolved: "Resolved", expired: "Expired", cancelled: "Cancelled", all: "All" };

function renderSidebar() {
  const spaces = (S.config?.spaces || []).map((s) => s.name);
  if (S.space && !spaces.includes(S.space)) S.space = "";
  const spaceTabs = $("#space-tabs");
  spaceTabs.hidden = !multiSpace();
  if (multiSpace()) {
    const openIn = (s) => S.list.filter((d) => actionable(d) && (!s || d.space === s)).length;
    setHtml(spaceTabs, ["", ...spaces].map((s) => { const n = openIn(s);
      return `<button class="tab ${S.space === s ? "active" : ""} ${n ? "has-open" : ""}" data-space="${esc(s)}" aria-pressed="${S.space === s}">${s ? esc(s) : "All spaces"}<span class="n">${n || ""}</span></button>`; }).join(""));
  }
  const list = S.list.filter(inSpace);
  S.counts = { open: 0, resolved: 0, expired: 0, cancelled: 0, all: list.length };
  for (const d of list) S.counts[d.status]++;
  // The badge counts what you can act on; decisions waiting on another one come later.
  const open = list.filter(actionable).sort(byDeadline);
  const later = list.filter(isWaiting).length;
  setHtml($("#status-tabs"), Object.keys(STATUS_NAMES).map((t) =>
    `<button class="tab ${S.status === t ? "active" : ""} ${t === "open" && open.length ? "has-open" : ""}" data-status="${t}" aria-pressed="${S.status === t}" title="${S.counts[t]} ${STATUS_NAMES[t].toLowerCase()}${t === "open" && later ? `, ${later} waiting on another decision` : ""}">${STATUS_NAMES[t]}${t === "open" && open.length ? `<span class="n">${open.length}</span>` : ""}</button>`).join(""));
  $("#settings-btn").classList.toggle("active", location.pathname === "/settings");
  $("#map-btn").classList.toggle("active", location.pathname === "/map" || !!S.goal);
  document.title = (open.length ? `(${open.length}) ` : "") + (S.d ? `${S.d.id} · ${S.d.title}` : S.goal ? `${S.goal.id} · ${S.goal.title}`
    : location.pathname === "/map" ? "Map" : "Decisions");
  setHtml($("#mb-title"), `Decisions${open.length ? `<span class="n" aria-label="${open.length} open">${open.length}</span>` : ""}`);
  setHtml($("#attention"), open.length
    ? `<a href="/d/${open[0].id}" data-nav class="attention ${urgency(open[0])}" title="Undecided decisions expire at their deadline">
        <span class="att-ic">${icon(urgency(open[0]) ? "clock" : "inbox", 15)}</span>
        <div><strong>${open.length} waiting on you</strong>
        <span>${open[0].due_at ? `Next expires in ${until(open[0].due_at)} · ${esc(open[0].id)}` : `Oldest: ${esc(open[0].id)}`}</span></div></a>`
    : `<div class="attention clear"><span class="att-ic">${icon("check", 15)}</span><div><strong>All caught up</strong><span>${later ? `${later} more once others are decided` : "Nothing is waiting on you"}</span></div></div>`);
  const tagsOf = decisionTags();
  const goalTagged = S.goals.some((g) => inSpace(g) && g.tags.length);
  const known = [...new Set([...S.listTags, ...list.flatMap(tagsOf)])].sort();
  const listTags = $("#list-tags");
  listTags.hidden = !known.length && !goalTagged;
  setHtml(listTags, `${known.map((t) => `<button class="tagb ${S.listTags.includes(t) ? "on" : ""}" data-action="list-tag" data-tag="${esc(t)}" aria-pressed="${S.listTags.includes(t)}">#${esc(t)}</button>`).join("")}
    ${S.listTags.length ? `<button class="btn small ghost" data-action="list-tags-clear">Show all</button>` : ""}
    ${goalTagged ? `<label class="inherit" title="A decision also has the tags of the goals it's a step toward, like on the map"><input type="checkbox" data-action="list-inherit" ${S.listInherit ? "checked" : ""}>Goals' tags too</label>` : ""}`);
  const q = S.q.toLowerCase();
  let items = list.filter((d) => (S.status === "all" || d.status === S.status) &&
    (!S.listTags.length || tagsOf(d).some((t) => S.listTags.includes(t))) &&
    (!q || d.title.toLowerCase().includes(q) || d.id.toLowerCase().includes(q) || (d.source || "").toLowerCase().includes(q)));
  // Most urgent first: open decisions by deadline, then those waiting on another one, then everything else.
  items = [...items.filter(actionable).sort(byDeadline), ...items.filter(isWaiting), ...items.filter((d) => d.status !== "open")];
  const sep = `<span class="sep" aria-hidden="true">·</span>`;
  setHtml($("#decision-list"), items.length ? items.map((d) => {
    const pending = Object.keys(d.pending_attribute_requests || {}).length;
    const due = actionable(d) && d.due_at;
    const live = d.option_count - d.excluded_count;
    const meta = [
      `<span class="dl-id" title="${d.excluded_count ? `${live} of ` : ""}${d.option_count} option${d.option_count === 1 ? "" : "s"}${d.excluded_count ? " in play" : ""}">${esc(d.id)}</span>`,
      multiSpace() && !S.space ? `<span class="dl-space">${esc(d.space)}</span>` : "",
      d.source ? `<span class="dl-source">${esc(d.source)}</span>` : "",
    ].filter(Boolean).join(sep);
    const current = S.d && S.d.id === d.id;
    return `<a href="/d/${esc(d.id)}" data-nav class="dl-item ${current ? "active" : ""} ${d.status} ${urgency(d)} ${isWaiting(d) ? "waiting" : ""}" ${current ? `aria-current="page"` : ""}>
      ${statusIcon(d)}<span class="dl-title">${esc(d.title)}</span>
      <div class="dl-meta">${meta}
        ${pending ? `<span class="dl-flag" title="Waiting for requested attributes"><span class="dot"></span>attrs</span>` : ""}
        ${["pending", "failed"].includes(d.resume_state?.status) && d.status === "resolved" ? `<span class="dl-flag bad" title="The requesting agent hasn't been resumed"><span class="dot bad"></span>not resumed</span>` : ""}
        ${d.outdated_by?.length && d.status === "resolved" ? `<span class="dl-flag" title="${esc(d.outdated_by.join(", "))} changed after this was decided"><span class="dot"></span>outdated</span>` : ""}
        ${isWaiting(d) ? `<span class="dl-due" title="Can't be decided until ${esc(d.waiting_on.join(", "))} is">after ${esc(d.waiting_on[0])}</span>`
          : due ? `<span class="dl-due" title="Expires ${esc(fullTime(d.due_at))} unless decided">${until(d.due_at)}</span>`
              : `<span class="dl-due" title="${esc(fullTime(d.created_at))}">${d.status === "expired" ? "expired" : ago(d.updated_at || d.created_at)}</span>`}
      </div></a>`;
  }).join("") : `<div class="list-empty">${q ? `No decisions match “${esc(S.q)}”.` : `No ${S.status === "all" ? "" : esc(STATUS_NAMES[S.status].toLowerCase()) + " "}decisions${S.listTags.length ? ` with ${S.listTags.map((t) => `#${esc(t)}`).join(" or ")}` : ""}.`}</div>`);
}

// A decision's tags for the sidebar filter: its own, plus its goals' while listInherit is on.
function decisionTags() {
  const goalTags = new Map(S.listInherit ? S.goals.map((g) => [g.id, g.tags]) : []);
  return (d) => [...new Set([...d.tags, ...d.goals.flatMap((g) => goalTags.get(g) || [])])];
}

function setListTags(tags) {
  S.listTags = tags;
  storage("listTags", tags.length ? tags.join(",") : null);
  renderSidebar();
}

// The sidebar is a drawer on narrow screens: while closed it's inert; while open, the page behind it is.
const narrow = matchMedia("(max-width: 860px)");
function drawer(open = !document.body.classList.contains("drawer-open")) {
  const was = document.body.classList.contains("drawer-open");
  document.body.classList.toggle("drawer-open", open);
  $("#drawer-btn")?.setAttribute("aria-expanded", open);
  syncInert();
  if (open && !was) ($(".dl-item.active") || $("#search"))?.focus();
  if (!open && was) $("#drawer-btn")?.focus({ preventScroll: true });
}
function syncInert() {
  const open = document.body.classList.contains("drawer-open");
  $("#sidebar").inert = narrow.matches && !open;
  $("#main").inert = $(".mobile-bar").inert = narrow.matches && open;
}
narrow.addEventListener("change", () => { if (!narrow.matches) document.body.classList.remove("drawer-open"); syncInert(); });

// ------------------------------------------------------------------ empty / settings

function renderEmpty() {
  const base = S.config?.base_url || location.origin;
  $("#main").innerHTML = `<div class="page"><div class="empty-state">
    <span class="es-ic">${icon("check", 24)}</span>
    <h2>All caught up</h2>
    <p>Nothing is waiting on you. Agents and apps ask for decisions through the API, the <code>decide</code> CLI or the agent skill, for example:</p>
<pre>curl -X POST ${esc(base)}/api/decisions \\
  -H 'Content-Type: application/json' -H 'X-Actor: my-agent' \\
  -d '{
    "title": "Which database for the new service?",
    "context": "Low write volume, needs JSON queries.",
    "attributes": {"cost": {"type": "number", "unit": "$/mo", "better": "lower"}},
    "options": [
      {"label": "Postgres", "attributes": {"cost": 25, "managed": true}},
      {"label": "SQLite",   "attributes": {"cost": 0,  "managed": false}}
    ],
    "webhooks": [{"url": "http://localhost:8790/"}]
  }'</pre></div></div>`;
}

// Event types a global webhook can subscribe to, grouped and described.
const HOOK_EVENTS = {
  "Outcome": { "decision.resolved": "Decided", "decision.expired": "Expired undecided", "decision.cancelled": "Cancelled", "decision.reopened": "Reopened" },
  "Activity": { "decision.created": "Created", "decision.updated": "Edited or deadline moved", "decision.commented": "Commented", "attributes.requested": "Attributes requested" },
  "Options": { "options.added": "Options added", "options.updated": "Options updated", "options.excluded": "Options ruled out" },
  "Dependencies": { "decision.unblocked": "Unlocked: what it waited on is settled", "dependency.changed": "Something it depends on changed" },
  "Goals": { "goal.created": "Goal created", "goal.updated": "Goal edited", "goal.achieved": "Goal achieved", "goal.abandoned": "Goal abandoned", "goal.reactivated": "Goal active again" },
};

async function renderSettings() {
  const cfg = S.config = await api("GET", "/api/config");
  const [hooks, verify, rsAll, spaces, tokens] = await Promise.all([api("GET", "/api/webhooks"), api("GET", "/api/events/verify"),
    api("GET", "/api/resumer"), cfg.admin ? api("GET", "/api/spaces") : null, cfg.admin ? api("GET", "/api/tokens") : null]);
  const global = hooks.webhooks.filter((w) => !w.decision_id);
  const w = cfg.webhook;
  const resumers = rsAll.resumers || [];
  $("#main").innerHTML = `<div class="page narrow">
    <div class="settings-head"><h1 class="d-title">Settings</h1><p>Spaces, the machines that use them, where events go, and the audit log.</p></div>
    ${cfg.admin ? spacesSetting(spaces.spaces) + machinesSetting(cfg, tokens.tokens, resumers) : sharedTokenSetting(cfg, resumers)}
    <section class="setting">
      <header><h2>Global webhooks</h2><p>Receive events for every decision${S.space ? ` in <strong>${esc(S.space)}</strong>` : ""}, e.g. to notify Slack or sync another app. Agents set per-decision webhooks themselves.</p></header>
      <div>
        <div class="panel">${webhookTable(global)}</div>
        <form class="hook-form panel" id="add-global-hook"><h3 class="form-title">Add a webhook</h3>
          <label class="lf">Endpoint URL<input class="input" name="url" type="url" placeholder="https://example.com/hooks/decisions" required></label>
          <fieldset><legend>Events</legend><div class="event-groups">
            ${Object.entries(HOOK_EVENTS).map(([group, evs]) => `<div class="event-group"><div class="eg-title">${group}</div>
              ${Object.entries(evs).map(([e, label]) => `<label class="event-pick"><input type="checkbox" name="events" value="${e}" ${cfg.default_webhook_events.includes(e) ? "checked" : ""}><span>${label}<code>${e}</code></span></label>`).join("")}</div>`).join("")}
          </div></fieldset>
          <label class="check-label small"><input type="checkbox" name="ack"> Re-fire every ${w.refire_interval}s until acknowledged</label>
          <div><button class="btn primary">${icon("plus", 15)}Add webhook</button></div>
        </form>
      </div>
    </section>
    <section class="setting">
      <header><h2>Delivery policy</h2><p>Set in <code>~/.config/decisions/config.toml</code> or <code>DECISIONS_WEBHOOK_*</code> env vars.</p></header>
      <dl class="kv" style="font-size:13.5px;gap:8px 24px;margin:0">
        <dt>Retries</dt><dd>Exponential backoff, ${w.backoff_base}s × 2ⁿ, at most ${w.backoff_max}s apart</dd>
        <dt>Attempts</dt><dd>${w.max_attempts || "Unlimited"}</dd>
        <dt>Timeout</dt><dd>${w.timeout}s per request</dd>
        <dt>Acknowledgement</dt><dd>${w.require_ack ? "Every delivery" : "Webhooks created with <code>require_ack</code>"} re-fires every ${w.refire_interval}s until acknowledged via <code>ack_url</code></dd>
      </dl>
    </section>
    <section class="setting">
      <header><h2>Audit log</h2><p>Events are append-only: the database rejects updates and deletes. Each space has its own SHA-256 hash chain.</p></header>
      <div>${Object.entries(verify.chains).map(([space, c], _, all) => `<div class="status-row" style="margin-top:0">${c.ok ? `<span class="badge ok">Intact</span>` : `<span class="badge failed">Broken</span>`}
          ${multiSpace() || all.length > 1 ? `<strong>${space === "*" ? "server" : esc(space)}</strong>` : ""}
          ${c.ok ? `<span>${c.events} event${c.events === 1 ? "" : "s"} · head <code>${esc(c.head.slice(0, 16))}…</code></span>`
                 : `<span>Fails verification at event #${c.broken_at}</span>`}</div>`).join("") || `<span class="muted">No events yet.</span>`}</div>
    </section>
  </div>`;
  $("#add-space")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { name: f.get("name").trim() };
    if (f.get("days") !== "") body.deadline_days = Number(f.get("days"));
    try { await api("POST", "/api/spaces", body); toast(`Created space ${body.name}`); await spacesChanged(); } catch (err) { fail(err); }
  });
  $("#add-token")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { showToken(await api("POST", "/api/tokens", { name: f.get("name").trim(), space: f.get("space") }), "created"); renderSettings(); }
    catch (err) { fail(err); }
  });
  $("#add-global-hook").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { url: f.get("url"), events: f.getAll("events") };
    if (!body.events.length) return toast("Pick at least one event", { error: true });
    if (f.get("ack")) body.require_ack = true;
    if (S.space) body.space = S.space;  // otherwise it hears every space this token sees
    try {
      const sub = await api("POST", "/api/webhooks", body);
      const copyIt = await ask({ title: "Webhook created", body: `<p>Its signing secret is shown only this once. Verify <code>X-Decisions-Signature</code> with it.</p>
        <pre class="install-cmd">${esc(sub.secret)}</pre>`, ok: "Copy secret", cancel: "Close" });
      if (copyIt !== null) copy(sub.secret, "signing secret");
      renderSettings();
    } catch (err) { fail(err); }
  };
}

const installCommand = (cfg, token) => `curl -fsSL ${cfg.install_url} | sh` + (token ? ` -s -- --token ${token}` : "");

// Spaces separate contexts (work, private, …). Each machine gets its own token for one space.
function spacesSetting(spaces) {
  return `<section class="setting">
    <header><h2>Spaces</h2><p>Hard boundaries between contexts. A machine's token sees only its space, so work agents never see private decisions. Use tags for topics within a space.</p></header>
    <div>
      <div class="panel"><div class="table-scroll"><table class="plain"><tr><th>Space</th><th>Decisions</th><th>Deadline</th><th>Machines</th><th><span class="sr-only">Actions</span></th></tr>
        ${spaces.map((sp) => `<tr><td><strong>${esc(sp.name)}</strong></td>
          <td class="nowrap">${sp.decisions}${sp.open ? ` <span class="muted">· ${sp.open} open</span>` : ""}</td>
          <td class="nowrap" ${sp.deadline_days === null ? `title="The server's deadline_days"` : ""}>${sp.effective_deadline_days ? `${sp.effective_deadline_days} days` : "None"}${sp.deadline_days === null ? ` <span class="muted">· default</span>` : ""}</td>
          <td>${sp.tokens}</td>
          <td style="text-align:right;white-space:nowrap">
            <button class="btn small ghost" data-action="space-deadline" data-id="${esc(sp.name)}" data-days="${sp.deadline_days ?? ""}">Deadline</button>
            ${sp.name !== "default" ? `<button class="btn small ghost danger" data-action="delete-space" data-id="${esc(sp.name)}" data-decisions="${sp.decisions}" data-tokens="${sp.tokens}">Delete</button>` : ""}</td></tr>`).join("")}</table></div>
        <form class="form-row" id="add-space" style="margin-top:14px">
          <label class="lf grow">New space<input class="input" name="name" required pattern="[a-z0-9][a-z0-9_\-]{0,39}" placeholder="e.g. work" title="Lowercase letters, digits, - and _"></label>
          <label class="lf">Deadline (days)<input class="input" name="days" type="number" min="0" step="any" placeholder="${esc(S.config.deadline_days)}" style="width:130px"></label>
          <button class="btn">${icon("plus", 15)}Create space</button></form></div>
    </div>
  </section>`;
}

function machinesSetting(cfg, tokens, resumers) {
  const spaces = cfg.spaces.map((s) => s.name);
  const tokenOf = new Set(tokens.map((t) => t.resumer?.machine).filter(Boolean));
  const others = resumers.filter((r) => !tokenOf.has(r.machine));
  return `<section class="setting">
    <header><h2>Machines</h2><p>Each machine gets its own token for one space. Its install command sets up the <code>decide</code> CLI, the agent skill and the resumer, so agent sessions pick back up when you decide.</p></header>
    <div>
      <div class="panel">${tokens.length ? `<div class="table-scroll"><table class="plain"><tr><th>Machine</th>${multiSpace() ? "<th>Space</th>" : ""}<th class="nowrap">Last used</th><th>Resumer</th><th><span class="sr-only">Actions</span></th></tr>
        ${tokens.map((t) => `<tr><td><strong>${esc(t.name)}</strong><div class="muted small mono">${esc(t.id)}</div></td>
          ${multiSpace() ? `<td>${esc(t.space)}</td>` : ""}
          <td class="nowrap">${t.last_used_at ? `<span title="${esc(fullTime(t.last_used_at))}">${ago(t.last_used_at)}</span>` : `<span class="muted">Never</span>`}${t.rolled_at ? `<div class="muted small">rolled ${ago(t.rolled_at)}</div>` : ""}</td>
          <td class="nowrap">${t.resumer ? `<span class="item"><span class="live ${t.resumer.online ? "on" : "off"}"></span> ${esc(t.resumer.machine)}</span>` : `<span class="muted">Not seen</span>`}</td>
          <td style="text-align:right;white-space:nowrap">
            <button class="btn small ghost" data-action="roll-token" data-id="${t.id}" data-name="${esc(t.name)}">${icon("rotate", 14)}Roll</button>
            <button class="btn small ghost danger" data-action="revoke-token" data-id="${t.id}" data-name="${esc(t.name)}">Revoke</button></td></tr>`).join("")}</table></div>`
        : `<div class="muted small">No machine tokens yet. Connect one below.</div>`}
        <form class="form-row" id="add-token" style="margin-top:14px">
          <label class="lf grow">Connect a machine<input class="input" name="name" required maxlength="60" placeholder="e.g. work laptop"></label>
          <label class="lf">Space<select class="input" name="space">${spaces.map((n) => `<option ${n === (S.space || "default") ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
          <button class="btn primary">${icon("plus", 15)}Create token</button></form>
      </div>
      ${others.length ? `<div class="status-row"><span class="muted">Other resumers</span>
        ${others.map((r) => `<span class="item"><span class="live ${r.online ? "on" : "off"}"></span>${esc(r.machine)} <span class="muted">${r.token ? "" : "admin token · "}${r.online ? "online" : "offline"}</span></span>`).join("")}</div>` : ""}
      ${cfg.auth ? `<div class="form-row" style="margin-top:12px"><button class="btn small ghost" data-action="copy-gui-link">${icon("link", 14)}Copy GUI sign-in link</button></div>
        <p class="hint">The sign-in link holds the admin token, which sees and manages every space. Give machines their own token instead.</p>`
        : `<p class="hint">Auth is off: anyone who can reach the server sees everything, and a token only picks the space new decisions go to. To accept other machines safely, run the server with <code>host = "0.0.0.0"</code> and a <code>token</code>; <code>decide server install --host 0.0.0.0</code> sets both.</p>`}
    </div>
  </section>`;
}

// A browser signed in with a machine token can't manage tokens; it can only pass its own on.
function sharedTokenSetting(cfg, resumers) {
  return `<section class="setting">
    <header><h2>Connect a machine</h2><p>This browser uses a token for the <strong>${esc(cfg.space)}</strong> space. Machines set up with it share that token. For a token of their own, ask whoever has the admin token.</p></header>
    <div>
      <pre class="install-cmd">${esc(installCommand(cfg, cfg.auth && S.token))}</pre>
      <div class="form-row" style="margin-top:10px"><button class="btn small" data-action="copy-install">${icon("copy", 14)}Copy command</button></div>
      <div class="status-row"><span class="muted">Resumers</span>
        ${resumers.map((r) => `<span class="item"><span class="live ${r.online ? "on" : "off"}"></span>${esc(r.machine)} <span class="muted">${r.online ? "online" : "offline"}</span></span>`).join("") || `<span class="muted">None seen since the server started</span>`}</div>
    </div>
  </section>`;
}

async function showToken(t, verb) {
  const cmd = installCommand(S.config, t.token);
  const copyIt = await ask({ title: `Token ${verb} for ${t.name}`, body: `<p>Shown only this once. Run this on <strong>${esc(t.name)}</strong> to install <code>decide</code>, the agent skill and the resumer, connected to the <strong>${esc(t.space)}</strong> space:</p>
    <pre class="install-cmd">${esc(cmd)}</pre>
    <p class="hint">Already installed there? <code>decide connect ${esc(S.config.base_url)} --token ${esc(t.token)}</code></p>`, ok: "Copy command", cancel: "Close" });
  if (copyIt !== null) copy(cmd, "install command");
}

async function spacesChanged() {
  S.config = await api("GET", "/api/config");
  await loadList();
  renderSettings();
}

function webhookTable(hooks) {
  if (!hooks.length) return `<div class="muted small">No global webhooks yet.</div>`;
  const spaces = multiSpace() && hooks.some((h) => !h.decision_id);
  return `<table class="plain"><tr><th>URL</th>${spaces ? "<th>Space</th>" : ""}<th>Events</th><th>Ack</th><th><span class="sr-only">Actions</span></th></tr>${hooks.map((h) => `
    <tr><td class="mono">${esc(h.url)}${h.active ? "" : ` <span class="badge">Paused</span>`}</td>
    ${spaces ? `<td class="small">${h.space ? esc(h.space) : "all"}</td>` : ""}
    <td class="small">${esc(h.events.join(", "))}</td>
    <td>${h.require_ack === null ? "default" : h.require_ack ? "required" : "no"}</td>
    <td style="text-align:right;white-space:nowrap">
      <button class="btn small ghost" data-action="toggle-hook" data-id="${h.id}" data-active="${h.active}">${h.active ? "Pause" : "Resume"}</button>
      <button class="btn small ghost danger" data-action="delete-hook" data-id="${h.id}">Delete</button></td></tr>`).join("")}</table>`;
}

// ------------------------------------------------------------------ decision page

// Re-rendering replaces the DOM; put focus (and the caret) back on the equivalent element.
function focusKey(el) {
  if (!el || el === document.body || !el.tagName) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  const attrs = ["data-filter", "data-part", "data-set", "data-action", "data-id", "data-key", "data-value", "data-pick", "data-sort", "data-status", "data-space", "name", "value"]
    .filter((a) => el.hasAttribute(a));
  if (!attrs.length) return null;
  return el.tagName.toLowerCase() + attrs.map((a) => `[${a}="${CSS.escape(el.getAttribute(a))}"]`).join("");
}
function withFocus(fn) {
  const el = document.activeElement;
  const key = focusKey(el);
  let caret = null;
  try { if (isTyping(el) && el.selectionStart != null) caret = [el.selectionStart, el.selectionEnd]; } catch {}
  fn();
  if (!key || document.activeElement === el && el.isConnected) return;
  const again = document.querySelector(key);
  if (!again) return;
  again.focus({ preventScroll: true });
  if (caret) try { again.setSelectionRange(...caret); } catch {}
}

function crumbsHtml(d) {
  return `<nav class="crumbs" aria-label="Breadcrumb"><a href="/" data-nav class="muted">Decisions</a>${icon("chevron", 14)}<span>${esc(d.id)}</span></nav>`;
}

function renderDecision() {
  const d = S.d;
  if (d.status !== "open") return renderClosed(d);
  const u = ui();
  const parts = partition();
  const { metas, visible, filtered, excluded } = parts;
  // Cards by default for images, and on phones where a wide table would scroll sideways.
  const view = u.view || (d.options.some((o) => o.image) || matchMedia("(max-width: 860px)").matches ? "cards" : "table");
  const single = d.max_select === 1;
  const rule = single ? "Pick one" : `Pick ${d.min_select}${d.max_select === null ? " or more" : d.max_select === d.min_select ? "" : "–" + d.max_select}`;

  const main = $("#main");
  const scroll = main.scrollTop;
  const html = `<div class="page">
    ${crumbsHtml(d)}
    ${headerHtml(d)}
    <div id="deadline-card">${deadlineHtml(d)}</div>
    <div id="resume-card">${resumeHtml(d)}</div>
    ${goalGoneHtml(d)}
    <div id="deps-panel">${depsHtml(d)}</div>
    <section class="block" aria-labelledby="opts-h">
      <div class="block-head">
        <h2 id="opts-h">Options</h2><span class="count">${visible.length} of ${d.options.length} in play</span>
        <span class="spacer"></span>
        <span class="pick-rule">${rule}</span>
        <div class="seg" role="group" aria-label="View"><button data-action="view" data-view="table" class="${view === "table" ? "on" : ""}" aria-pressed="${view === "table"}">${icon("table", 14)}Table</button><button data-action="view" data-view="cards" class="${view === "cards" ? "on" : ""}" aria-pressed="${view === "cards"}">${icon("grid", 14)}Cards</button></div>
      </div>
      ${d.options.length ? filtersHtml(parts) : `<div class="panel muted">No options yet. The agent can add them with <code>POST /api/decisions/${esc(d.id)}/options</code>.</div>`}
      ${view === "table" ? tableHtml(parts, single) : cardsHtml(parts, single)}
      ${filtered.length ? `<details class="subtle-list" ${u.openFiltered ? "open" : ""} data-key="openFiltered"><summary>Hidden by filters (${filtered.length})</summary>
        <div class="mini-opts">${filtered.map((o) => miniHtml(o, "filtered")).join("")}</div></details>` : ""}
      ${excluded.length ? `<details class="subtle-list" ${u.openExcluded ? "open" : ""} data-key="openExcluded"><summary>Ruled out (${excluded.length})</summary>
        <div class="mini-opts">${excluded.map((o) => miniHtml(o, "excluded")).join("")}</div></details>` : ""}
      ${d.options.length ? decideBarHtml(d, single, parts) : ""}
    </section>
    <div class="cols">
      <section class="block">${attrRequestHtml(d, metas)}</section>
      <section class="block" id="listeners-panel">${webhooksHtml(d)}</section>
    </div>
    <section class="block">${activityHtml(d)}</section>
  </div>`;
  withFocus(() => {
    main.innerHTML = html;
    main.scrollTop = scroll;
    bindDecision();
  });
  syncPopModal();
  const shown = `${visible.length} of ${d.options.length} options shown`;
  if (S.lastShown && S.lastShown !== shown && S.lastShownId === d.id) announce(shown);
  S.lastShown = shown; S.lastShownId = d.id;
}

// Closed decisions (resolved, expired, cancelled) lead with the outcome, followed by the full
// record read-only: context, every option with its attributes, and the activity log. Reopen to change it.
function renderClosed(d) {
  const r = d.resolution;
  const others = S.list.filter((x) => actionable(x) && x.id !== d.id && inSpace(x)).sort(byDeadline);
  const main = $("#main");
  const scroll = main.scrollTop;
  const html = `<div class="page narrow">
    ${crumbsHtml(d)}
    <h1 class="d-title">${esc(d.title)}</h1>
    <div class="d-sub">${subHtml(d)}</div>
    ${towardHtml(d)}
    ${outcomeHtml(d)}
    ${outdatedHtml(d)}
    <div id="resume-card">${resumeHtml(d)}</div>
    <div class="closed-actions">
      ${r ? `<button class="btn primary" data-action="copy-provenance" title="Copy the choice, reason and link, e.g. for a commit message or PR">${icon("copy", 15)}Copy decision record</button>` : ""}
      <button class="btn" data-action="copy-md" title="Copy a markdown summary with the options table">Copy summary</button>
      <button class="btn ${r ? "ghost" : "primary"}" data-action="reopen">${icon("rotate", 15)}Reopen</button>
      ${d.goals.length ? "" : `<button class="btn ghost" data-action="add-goal" title="Link it to the goal it was a step toward">${icon("target", 15)}Goal…</button>`}
      <span class="spacer"></span>
      ${others.length ? `<a class="btn ghost" href="/d/${esc(others[0].id)}" data-nav title="${esc(others[0].title)}">Next open: ${esc(others[0].id)}${icon("arrow-right", 15)}</a>` : ""}
    </div>
    ${d.context || linksHtml(d) ? `<section class="block"><div class="block-head"><h2>Context</h2></div>
      ${d.context ? `<div class="context" style="margin-top:0">${md(d.context)}</div>` : ""}${linksHtml(d)}</section>` : ""}
    ${depsHtml(d)}
    ${d.options.length ? `<section class="block">${closedOptionsHtml(d)}</section>` : ""}
    <section class="block">${activityHtml(d)}</section>
  </div>`;
  withFocus(() => {
    main.innerHTML = html;
    main.scrollTop = scroll;
    bindComments(d);
  });
  bindTagInput("decision");
}

function outcomeHtml(d) {
  if (d.status === "resolved" && d.resolution) {
    const r = d.resolution;
    const picked = d.selected_options.map((o) => esc(o.label)).join(", ") || "<em>Nothing</em>";
    return `<div class="outcome">${icon("check-circle", 20)}<div style="flex:1;min-width:0">
      <div class="o-label">Decided</div>
      <div class="o-pick">${picked}</div>
      <div class="note" title="${esc(fullTime(r.resolved_at))}">by ${esc(r.resolved_by)} · ${ago(r.resolved_at)}</div>
      <div class="reason"><h3>Reason</h3>${r.note ? md(r.note) : `<span class="muted">No reason given.</span>`}</div></div></div>`;
  }
  if (d.status === "cancelled") {
    const ev = [...S.events].reverse().find((e) => e.type === "decision.cancelled");
    return `<div class="outcome cancelled">${icon("ban", 20)}<div>
      <div class="o-label">Cancelled</div>
      <div class="o-pick">${ev?.actor === "dependency" ? esc(ev.data.note) : `Withdrawn without a choice${ev?.data.note ? `: “${esc(ev.data.note)}”` : ""}`}</div>
      ${ev ? `<div class="note" title="${esc(fullTime(ev.ts))}">by ${esc(ev.actor)} · ${ago(ev.ts)}</div>` : ""}</div></div>`;
  }
  const days = S.config?.deadline_days;
  return `<div class="outcome expired">${icon("clock", 20)}<div>
    <div class="o-label">Expired</div>
    <div class="o-pick">Nobody decided before the deadline${d.due_at ? ` (<span title="${esc(fullTime(d.due_at))}">${ago(d.due_at)}</span>)` : ""}, so the requester carried on without it.</div>
    <div class="note">Reopen to decide after all${days ? `; that starts a new ${days}-day deadline` : ""}.</div></div></div>`;
}

function subHtml(d) {
  const sep = `<span class="sep" aria-hidden="true">·</span>`;
  return `<button class="chip id" data-action="copy-ref" title="Copy a markdown link for Slack or Linear">${esc(d.id)}${icon("copy", 12)}</button>
    <span class="chip status ${d.status} ${urgency(d)}">${statusIcon(d, 12)}${d.status}</span>
    ${spaceChip(d)}
    ${d.source ? `<span>from <strong>${esc(d.source)}</strong></span>${sep}` : ""}
    <span title="${esc(fullTime(d.created_at))}">created ${ago(d.created_at)}</span>
    ${tagsHtml("decision", d.tags, "Topics like business, product or tech: the map filters on them, along with the tags of its goals")}`;
}

// Tags on a goal or decision: chips that remove themselves, then "+ tag", an input while adding.
function tagsHtml(kind, tags, hint) {
  return `${tags.map((t) => `<span class="chip tag">#${esc(t)}<button class="chip-x" data-action="remove-tag" data-kind="${kind}" data-tag="${esc(t)}" aria-label="Remove #${esc(t)}" title="Remove">${icon("x", 12)}</button></span>`).join("")}
    ${S.addingTag ? `<input class="tag-input" id="tag-input" data-kind="${kind}" placeholder="business, tech…" aria-label="New tags, comma-separated" autocomplete="off">`
      : `<button class="chip add-tag" data-action="add-tag" data-kind="${kind}" title="${esc(hint)}">${icon("plus", 12)}tag</button>`}`;
}

const tagged = (kind) => kind === "goal"
  ? { tags: S.goal.tags, render: renderGoal, save: (tags, okMsg) => goalAct(() => api("PATCH", `/api/goals/${S.goal.id}`, { tags }), okMsg) }
  : { tags: S.d.tags, render: renderDecision, save: (tags, okMsg) => act(() => api("PATCH", `/api/decisions/${S.d.id}`, { tags }), okMsg) };

// Enter adds and stays open for the next tag; Escape, or leaving it empty, closes it.
function bindTagInput(kind) {
  const input = $("#tag-input");
  if (!input) return;
  input.focus();
  let done = false;
  const finish = async (more) => {
    if (done) return;
    done = true;
    const t = tagged(kind);
    const add = [...new Set(input.value.split(",").map((x) => x.trim().replace(/^#/, "")).filter((x) => x && !t.tags.includes(x)))];
    S.addingTag = more && !!input.value.trim();
    if (!add.length || !await t.save([...t.tags, ...add])) { S.addingTag = false; t.render(); }
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.stopPropagation(); input.value = ""; finish(false); }
  };
  input.onblur = () => finish(false);
}

function linksHtml(d) {
  const links = d.links.map((l) => `<a class="chip" href="${esc(l.url)}" target="_blank" rel="noopener">${icon("external", 12)}${esc(l.title || l.url.replace(/^https?:\/\//, "").slice(0, 50))}</a>`).join("");
  const meta = Object.entries(d.metadata).filter(([, v]) => typeof v !== "object").slice(0, 8)
    .map(([k, v]) => `<span class="chip" title="metadata">${esc(k)}: ${esc(String(v).slice(0, 40))}</span>`).join("");
  return links || meta ? `<div class="links">${links}${meta}</div>` : "";
}

// Read-only comparison: picked options first, then the rest, then ruled-out ones with their reason.
function closedOptionsHtml(d) {
  const metas = attrKeys(d).map((k) => attrMeta(d, k)).filter((m) => m.type !== "image");
  const chosen = new Set(d.resolution?.selected || []);
  const rank = (o) => (chosen.has(o.id) ? 0 : o.excluded ? 2 : 1);
  const opts = [...d.options].sort((a, b) => rank(a) - rank(b));
  const excludedCount = d.options.filter((o) => o.excluded).length;
  const head = `<div class="block-head"><h2>Options</h2><span class="count">${d.options.length} considered${excludedCount ? `, ${excludedCount} ruled out` : ""}</span></div>`;
  if (narrow.matches) return head + `<div class="cards readonly">${opts.map((o) => `
    <div class="card ${chosen.has(o.id) ? "selected" : ""} ${o.excluded ? "excluded" : ""}">
      ${o.image ? `<img class="hero" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
      <div class="body">${chosen.has(o.id) ? `<span class="sel-badge">${icon("check", 13)}Selected</span>` : ""}<div class="top"><div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.label)}</a>` : esc(o.label)}</div></div>
        ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}
        ${o.excluded ? `<div class="opt-desc">Ruled out${o.exclude_reason ? `: ${esc(o.exclude_reason)}` : ""}</div>` : ""}
        ${metas.length ? `<dl>${metas.map((m) => `<dt>${esc(m.label)}</dt><dd class="${o.attributes[m.key] == null ? "missing" : ""}">${cellHtml(o.attributes[m.key], m, o.attribute_meta[m.key])}</dd>`).join("")}</dl>` : ""}</div>
    </div>`).join("")}</div>`;
  return `${head}
    <div class="table-wrap"><table class="opts readonly">
      <thead><tr><th class="check"><span class="sr-only">Picked</span></th><th>Option</th>${metas.map((m) => `<th class="${m.type === "number" ? "num" : ""}" title="${esc(m.def.description || "")}">${esc(m.label)}</th>`).join("")}</tr></thead>
      <tbody>${opts.map((o) => `
        <tr class="${chosen.has(o.id) ? "selected" : ""} ${o.excluded ? "excluded" : ""}">
          <td class="check">${chosen.has(o.id) ? `<span class="picked" title="Picked">${icon("check-circle", 18)}</span>` : ""}</td>
          <td>${o.image ? `<img class="thumb" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
            <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.label)}</a>` : esc(o.label)}${chosen.has(o.id) ? `<span class="sr-only"> (picked)</span>` : ""}</div>
            ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}
            ${o.excluded ? `<div class="opt-desc">Ruled out${o.exclude_reason ? `: ${esc(o.exclude_reason)}` : ""}</div>` : ""}</td>
          ${metas.map((m) => {
            const v = o.attributes[m.key];
            return `<td class="${v == null ? "missing" : ""} ${typeof v === "number" ? "num" : ""}">${cellHtml(v, m, o.attribute_meta[m.key])}</td>`;
          }).join("")}
        </tr>`).join("")}</tbody></table></div>`;
}

function headerHtml(d) {
  if (S.editing) {
    return `<form class="edit-form" id="edit-form">
      <label class="lf">Title<input class="input" name="title" value="${esc(d.title)}" style="font-size:16px;font-weight:600;height:40px"></label>
      <label class="lf">Context <span style="font-weight:400">(markdown)</span><textarea class="input" name="context">${esc(d.context)}</textarea></label>
      <div class="form-row"><button class="btn primary">Save</button><button type="button" class="btn ghost" data-action="cancel-edit">Cancel</button></div>
    </form>`;
  }
  return `<div class="d-head" style="display:flex;gap:16px;align-items:flex-start;justify-content:space-between">
      <div style="min-width:0">
        <h1 class="d-title">${esc(d.title)}</h1>
        <div class="d-sub">${subHtml(d)}</div>
        ${towardHtml(d)}
      </div>
      <div class="d-actions">
        <button class="btn small" data-action="copy-md" title="Copy a markdown summary with the options table">${icon("copy", 14)}Copy summary</button>
        <button class="btn small ghost" data-action="edit">${icon("pencil", 14)}Edit</button>
        ${d.goals.length ? "" : `<button class="btn small ghost" data-action="add-goal" title="Link it to the goal it's a step toward">${icon("target", 14)}Goal…</button>`}
        <button class="btn small ghost" data-action="add-dep" title="Lock this until another decision is made">${icon("link", 14)}Depends on…</button>
        <button class="btn small ghost danger" data-action="cancel-decision" title="Withdraw it without picking anything">${icon("ban", 14)}Cancel</button>
      </div>
    </div>
    ${d.context ? `<div class="context">${md(d.context)}</div>` : ""}
    ${linksHtml(d)}`;
}

// Open decisions expire at their deadline. Keep the countdown and its consequence in plain view.
function deadlineHtml(d) {
  if (isWaiting(d)) return waitingHtml(d);
  if (d.status !== "open" || !d.due_at) return "";
  const who = esc(d.source || "the requester");
  const days = S.config?.deadline_days || 4;
  const u = urgency(d);
  return `<div class="callout deadline ${u || "slim"}">${icon("clock", u ? 18 : 16)}
    <div class="callout-main"><div><span class="deadline-left">${until(d.due_at)} left</span> <span class="muted">· until</span> <span title="${esc(fullTime(d.due_at))}">${esc(whenTime(d.due_at))}</span></div>
      <div class="small">${u ? `If it's still undecided then, it expires and ${who} is notified.` : `· then it expires and ${who} is notified`}</div></div>
    <div class="callout-actions">
      <button class="btn small" data-action="extend" data-days="1" title="Move the deadline a day later">${icon("plus", 14)}1 day</button>
      <button class="btn small" data-action="extend" data-days="${days}" title="Move the deadline ${days} days later">${icon("plus", 14)}${days} days</button></div>
  </div>`;
}

// A decision waiting on another can't be decided yet. Say so first, and point at the one to decide instead.
function waitingHtml(d) {
  const ups = d.depends_on.filter((x) => x.state === "waiting");
  const names = ups.map((x) => `<a href="/d/${esc(x.decision)}" data-nav>${esc(x.decision)}</a> “${esc(x.title)}”`).join(" and ");
  const only = ups.filter((x) => x.when).map((x) => ` If ${esc(x.decision)} doesn't pick ${esc(x.when_labels.join(" or "))}, this is cancelled for you.`).join("");
  return `<div class="callout waiting">${icon("lock", 18)}
    <div class="callout-main"><div><strong class="c-title">Don't worry about this yet</strong></div>
      <div class="small">It depends on ${names}, which ${ups.length > 1 ? "are" : "is"} still open. Decide that first.${only} Until then it can't be decided and won't expire.</div></div>
    <div class="callout-actions">
      <a class="btn small primary" href="/d/${esc(ups[0].decision)}" data-nav>Go to ${esc(ups[0].decision)}${icon("arrow-right", 14)}</a>
      <button class="btn small ghost" data-action="decide-anyway" title="Remove the dependency so you can decide this now">Decide anyway</button></div>
  </div>`;
}

const depLink = (x) => `<a href="/d/${esc(x.decision)}" data-nav class="dep-title">${statusIcon(x)}<span class="dep-id">${esc(x.decision)}</span><span class="dep-name">${esc(x.title)}</span></a>`;

// What happens to a decision that depends on this one, given what's selected (or was decided).
function effectOf(d, x, picked) {
  const others = (x.waiting_on || []).filter((id) => id !== d.id);
  const applies = !x.when || x.when.some((w) => picked.includes(w));
  if (x.status === "resolved") return d.status === "open" && picked.length ? ["warn", "Decided already: flagged for a second look"] : ["", "Decided"];
  if (x.status !== "open") return ["", x.status[0].toUpperCase() + x.status.slice(1)];
  if (d.status !== "open") return ["", "Open"];
  if (!picked.length) return ["", "Waits for this"];
  if (!applies) return ["bad", "Will be cancelled"];
  return others.length ? ["", `Still waits on ${others.join(", ")}`] : ["ok", "Unlocks"];
}

// Dependencies both ways: what this waits on, and what deciding it does to others.
function depsHtml(d) {
  const ups = d.depends_on || [], downs = d.affects || [];
  if (!ups.length && !downs.length) return "";
  const open = d.status === "open";
  const picked = open ? [...S.selection] : d.resolution?.selected || [];
  const label = (id) => d.options.find((o) => o.id === id)?.label || id;
  const upState = (x) => d.outdated_by?.includes(x.decision) ? ["warn", x.status === "open" ? "Reopened since" : "Changed since"]
    : x.state === "waiting" ? ["warn", "Decide this first"] : x.state === "unmet" ? ["bad", "Didn't pick it: this doesn't apply"]
    : ["ok", x.status === "resolved" ? "Decided" : `${x.status[0].toUpperCase() + x.status.slice(1)}, doesn't hold this up`];
  const row = (x, [tone, text], cond, remove) => `<div class="dep">${depLink(x)}
      ${cond ? `<span class="chip dep-cond">${cond}</span>` : ""}
      <span class="dep-effect ${tone}">${esc(text)}</span>
      ${remove ? `<button class="icon-btn" data-action="remove-dep" data-id="${esc(x.decision)}" title="Remove this dependency" aria-label="Remove the dependency on ${esc(x.decision)}">${icon("x", 14)}</button>` : ""}
      ${x.note ? `<div class="dep-note">${esc(x.note)}</div>` : ""}</div>`;
  return `<section class="block deps" aria-labelledby="deps-h"><div class="block-head"><h2 id="deps-h">Dependencies</h2></div>
    ${ups.length ? `<h3 class="dep-head">Depends on</h3>${ups.map((x) => row(x, upState(x), x.when && `only if it picks ${esc(x.when_labels.join(" or "))}`, open)).join("")}` : ""}
    ${downs.length ? `<h3 class="dep-head">Affects</h3>${downs.map((x) => row(x, effectOf(d, x, picked), x.when && `only if this picks ${esc(x.when.map(label).join(" or "))}`, false)).join("")}` : ""}
  </section>`;
}

// One line under the decide button: what picking the current selection does to decisions that depend on this one.
function effectsNote(d) {
  if (!S.selection.size || !d.affects?.length) return "";
  const picked = [...S.selection];
  const by = (tone) => d.affects.filter((x) => effectOf(d, x, picked)[0] === tone).map((x) => esc(x.decision));
  const parts = [[by("bad"), "cancels"], [by("ok"), "unlocks"], [by("warn"), "flags for a second look:"]]
    .filter(([ids]) => ids.length).map(([ids, verb]) => `${verb} <strong>${ids.join(", ")}</strong>`);
  return parts.length ? `<div class="bar-note info">${icon("link", 14)}<span>Deciding this ${parts.join(" · ")}</span></div>` : "";
}

// A decision whose upstream changed after it was made. It's left as decided, but you're asked to look again.
function outdatedHtml(d) {
  if (d.status !== "resolved" || !d.outdated_by?.length) return "";
  const ups = d.depends_on.filter((x) => d.outdated_by.includes(x.decision));
  const reopened = ups.filter((x) => x.status === "open");
  const names = ups.map((x) => `<a href="/d/${esc(x.decision)}" data-nav>${esc(x.decision)}</a> “${esc(x.title)}”`).join(" and ");
  return `<div class="callout warn">${icon("link", 18)}
    <div class="callout-main"><div><strong class="c-title">Worth a second look</strong></div>
      <div class="small">${names} changed after this was decided${reopened.length ? `, and ${reopened.length > 1 ? "they're" : "it's"} open again. Decide ${reopened.length > 1 ? "those" : "that"} first, then come back.` : ". If this still holds, confirm it. Otherwise reopen it."}</div></div>
    <div class="callout-actions">${reopened.length
      ? `<a class="btn small primary" href="/d/${esc(reopened[0].decision)}" data-nav>Go to ${esc(reopened[0].decision)}${icon("arrow-right", 14)}</a>`
      : `<button class="btn small primary" data-action="still-holds" title="Confirm the same choice without notifying its listeners again">${icon("check", 14)}Still holds</button>`}</div>
  </div>`;
}

// Sessions are resumed by the resumer on the machine they ran on.
const resumerPath = (d) => "/api/resumer" + (d?.resume?.machine ? `?machine=${encodeURIComponent(d.resume.machine)}` : "");

// Is the requesting agent going to be picked back up? One status line, one recovery action, details folded away.
function resumeHtml(d) {
  const r = d.resume, st = d.resume_state, rs = S.resumer || {};
  if (!r) return "";
  const agent = esc(r.agent || "agent");
  const where = r.machine ? ` on ${esc(r.machine)}` : "";
  const stale = st && st.status === "pending" && (Date.now() - new Date(st.since).getTime() > 60000 || !rs.online);
  const expired = st?.trigger === "decision.expired";
  const copyBtn = (primary) => `<button class="btn small ${primary ? "primary" : ""}" data-action="copy-resume">${icon("terminal", 14)}${d.resume_command ? "Copy resume command" : "Copy session ID"}</button>`;
  let cls = "", ic = "play", title, text = "", actions = "";
  switch (st?.status) {
    case "armed":
      if (rs.online) { title = "Resumes automatically"; text = `The ${agent} session${where} picks back up when you decide or it expires.`; }
      else { cls = "warn"; ic = "help"; title = "Won't resume automatically"; text = `The resumer${where} is offline. Start it with <code>decide resumer run</code>, or resume the session yourself after deciding.`; actions = copyBtn(false); }
      break;
    case "pending":
      if (stale) { cls = "bad"; ic = "help"; title = "Not resumed"; text = `The ${agent} session${where} hasn't been picked up${expired ? " to hear that this expired" : ""}${rs.online ? "" : " because the resumer is offline"}.`; actions = copyBtn(true) + `<button class="btn small ghost" data-action="mark-resumed">Mark resumed</button>`; }
      else { title = expired ? "Telling the agent it expired…" : "Resuming…"; text = `Starting the ${agent} session${where}.`; }
      break;
    case "started": title = "Agent working"; text = `Resumed the ${agent} session ${ago(st.since)}${expired ? " to tell it this expired" : ""}.`; break;
    case "finished": cls = "ok"; ic = "check-circle"; title = "Agent resumed"; text = `The ${agent} session ran ${ago(st.since)}${expired ? " and heard this expired" : ""} and finished.`; break;
    case "failed":
      cls = "bad"; ic = "ban"; title = "Resume failed";
      text = `${ago(st.since)}${st.detail?.exit_code != null ? `, exit ${st.detail.exit_code}` : ""}${st.detail?.error ? `: ${esc(st.detail.error)}` : ""}. Resume the session yourself.`;
      actions = copyBtn(true) + `<button class="btn small ghost" data-action="mark-resumed">Mark resumed</button>`;
      break;
    case "manual": cls = "ok"; ic = "check-circle"; title = "Resumed manually"; text = `Marked by ${esc(st.actor)} ${ago(st.since)}.`; break;
    default: title = "Agent session attached"; text = `${agent}${where}`;
  }
  const online = rs.online ? `<span class="live on" style="display:inline-block"></span> online` : `<span class="live off" style="display:inline-block"></span> offline${rs.last_seen ? `, last seen ${ago(rs.last_seen)}` : ""}`;
  return `<div class="callout resume ${cls}">${icon(ic, 18)}
    <div class="callout-main"><div><strong class="c-title">${title}</strong> <span class="muted">· ${text}</span></div>
      <details><summary>Session details</summary><dl class="kv">
        <dt>Agent</dt><dd>${agent}</dd>
        <dt>Session</dt><dd><code>${esc(r.session_id)}</code></dd>
        ${r.cwd ? `<dt>Directory</dt><dd><code>${esc(r.cwd.replace(/^\/(Users|home)\/[^/]+/, "~"))}</code></dd>` : ""}
        <dt>Resumer</dt><dd>${r.machine ? esc(r.machine) + " · " : ""}${online}</dd></dl>
        ${st?.detail?.output_tail ? `<pre class="small">${esc(st.detail.output_tail)}</pre>` : ""}</details></div>
    ${actions ? `<div class="callout-actions">${actions}</div>` : ""}
  </div>`;
}

function filterSummary(m, f) {
  const unit = m.unit ? " " + m.unit : "";
  const hasMin = f.min !== undefined && f.min !== "", hasMax = f.max !== undefined && f.max !== "";
  if (hasMin && hasMax) return `${f.min}–${f.max}${unit}`;
  if (hasMin) return `≥ ${f.min}${unit}`;
  if (hasMax) return `≤ ${f.max}${unit}`;
  if (f.bool && f.bool !== "any") return f.bool === "yes" ? "Yes" : "No";
  if (f.set?.length) return f.set.length === 1 ? f.set[0] : `${f.set.length} selected`;
  if (f.text?.trim()) return `“${f.text.trim()}”`;
  return "";
}

function filtersHtml({ metas, filtered }) {
  const u = ui();
  const d = S.d;
  const usable = metas.filter((m) => m.type !== "url" && m.type !== "image" && (m.distinct.length || m.requested));
  const active = usable.filter((m) => filterActive(u.filters[m.key]));
  // A short list without attributes is quicker to scan than to filter.
  if (!usable.length && d.options.length <= 7 && !u.text.trim()) return "";
  const live = d.options.filter((o) => !o.excluded);
  const fs = usable.map((m) => {
    const f = u.filters[m.key] || {};
    const on = filterActive(f);
    const k = esc(m.key);
    let body;
    if (m.type === "number") {
      body = `<div class="range">
          <label class="lf">Min<input class="field" type="number" step="any" inputmode="decimal" data-filter="${k}" data-part="min" value="${esc(f.min ?? "")}"></label>
          <label class="lf">Max<input class="field" type="number" step="any" inputmode="decimal" data-filter="${k}" data-part="max" value="${esc(f.max ?? "")}"></label></div>
        ${m.min !== null ? `<p class="hint">Options range from ${m.min.toLocaleString()} to ${m.max.toLocaleString()}${m.unit ? " " + esc(m.unit) : ""}.</p>` : ""}`;
    } else if (m.type === "boolean") {
      body = `<div class="choices" role="radiogroup" aria-label="${esc(m.label)}">${["any", "yes", "no"].map((v) => `<label class="choice"><input type="radio" name="bool-${k}" data-filter="${k}" data-part="bool" value="${v}" ${(f.bool || "any") === v ? "checked" : ""}>${v === "any" ? "Any" : v === "yes" ? "Yes" : "No"}</label>`).join("")}</div>`;
    } else if ((m.type === "enum" || m.type === "tags") && m.distinct.length) {
      const count = (v) => live.filter((o) => [].concat(o.attributes[m.key] ?? []).map(String).includes(v)).length;
      body = `<div class="choices">${m.distinct.map((v) => `<label class="choice"><input type="checkbox" data-set="${k}" value="${esc(v)}" ${(f.set || []).includes(v) ? "checked" : ""}>${esc(v)}<span class="ct">${count(v)}</span></label>`).join("")}</div>`;
    } else {
      body = `<label class="lf">Contains<input class="field" type="text" data-filter="${k}" data-part="text" value="${esc(f.text ?? "")}"></label>`;
    }
    return `<details class="fpop ${on ? "on" : ""}" data-pop="${k}" ${S.pop === m.key ? "open" : ""}>
      <summary class="fbtn" title="${esc(m.def.description || `Filter by ${m.label}`)}" aria-haspopup="dialog">${esc(m.label)}${on ? `: <span class="val">${esc(filterSummary(m, f))}</span>` : ""}${m.requested ? ` <span class="dot" title="Requested from the agent"></span>` : ""}${icon("chevron-down", 14)}</summary>
      <div class="pop-scrim" data-action="close-pop"></div>
      <div class="pop" role="dialog" aria-label="Filter by ${esc(m.label)}"><div class="pop-title"><span>${esc(m.label)}${m.unit ? ` (${esc(m.unit)})` : ""}</span>${on ? `<button class="btn small ghost" data-action="clear-filter" data-key="${k}">Clear</button>` : ""}<button class="icon-btn" data-action="close-pop" aria-label="Close">${icon("x", 16)}</button></div>${body}
        <button class="btn primary pop-done" data-action="close-pop">Done</button></div>
    </details>`;
  }).join("");
  const chips = [
    u.text.trim() ? `<span class="afchip">Matches “${esc(u.text.trim())}”<button data-action="clear-text" aria-label="Remove search">${icon("x", 13)}</button></span>` : "",
    ...active.map((m) => `<span class="afchip">${esc(m.label)}: ${esc(filterSummary(m, u.filters[m.key]))}<button data-action="clear-filter" data-key="${esc(m.key)}" aria-label="Remove ${esc(m.label)} filter">${icon("x", 13)}</button></span>`),
  ].filter(Boolean);
  return `<div class="toolbar">
      <div class="tsearch">${icon("search", 15)}<input type="search" id="opt-text" placeholder="Search options" aria-label="Search options" value="${esc(u.text)}"><span class="kbd" aria-hidden="true">/</span></div>
      ${fs ? `<span class="tlabel">${icon("filter", 14)}${active.length ? `${active.length} filter${active.length > 1 ? "s" : ""}` : "Filter"}</span>${fs}` : ""}
      <span class="spacer"></span>
    </div>
    ${chips.length ? `<div class="active-filters" role="status">
      ${chips.join("")}
      <button class="btn small ghost" data-action="clear-all-filters">Reset</button>
      ${active.length ? `<label class="check-label"><input type="checkbox" id="keep-unknown" ${u.keepUnknown ? "checked" : ""}> Keep options with unknown values</label>` : ""}
      <span class="spacer"></span>
      ${filtered.length ? `<button class="btn small" data-action="exclude-filtered" title="Record the ${filtered.length} hidden options as ruled out, with the filter as the reason">${icon("x", 14)}Rule out ${filtered.length} hidden</button>` : ""}
    </div>` : ""}`;
}

function sortState(key) {
  const s = ui().sort;
  return s && s.key === key ? (s.dir === "asc" ? "ascending" : "descending") : "none";
}
function sortHead(key, label, extra = "") {
  const st = sortState(key);
  return `<button class="sortb" data-sort="${esc(key)}" title="Sort by ${esc(label.replace(/<[^>]+>/g, ""))}">${label}${extra}${icon(st === "none" ? "sort" : st === "ascending" ? "arrow-up" : "arrow-down", 13)}</button>`;
}

function bestTitle(m) { return `${m.better === "lower" ? "Lowest" : "Highest"} ${m.label.toLowerCase()} among shown options`; }
const bestTag = (m) => `<span class="best-tag">${m.better === "lower" ? "lowest" : "highest"}</span>`;

function tableHtml({ metas, visible, best }, single) {
  if (!visible.length) return S.d.options.length ? `<div class="panel muted" style="margin-top:12px">No options match the filters. <button class="btn small ghost" data-action="clear-all-filters">Reset filters</button></div>` : "";
  const cols = metas.filter((m) => m.type !== "image");
  return `<div class="table-wrap"><table class="opts">
    <thead><tr><th class="check"><span class="sr-only">Select</span></th><th aria-sort="${sortState("__label")}">${sortHead("__label", "Option")}</th>
      ${cols.map((m) => `<th class="${m.type === "number" ? "num" : ""}" aria-sort="${sortState(m.key)}" title="${esc([m.def.description, m.requested && "Requested from the agent"].filter(Boolean).join(" · "))}">${sortHead(m.key, esc(m.label),
        (m.requested ? ` <span class="req" aria-label="requested"></span>` : "") + (m.better ? ` <span class="better">${m.better} better</span>` : ""))}</th>`).join("")}
      <th><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${visible.map((o, i) => `
      <tr class="opt ${S.selection.has(o.id) ? "selected" : ""} ${S.focus === i ? "focus" : ""}" data-option="${esc(o.id)}">
        <td class="check"><span class="pick-hit"><input class="pick" type="${single ? "radio" : "checkbox"}" name="pick" data-pick="${esc(o.id)}" ${S.selection.has(o.id) ? "checked" : ""} aria-label="Select ${esc(o.label)}"></span></td>
        <td>${o.image ? `<img class="thumb" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
          <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.label)}</a>` : esc(o.label)}</div>
          ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}</td>
        ${cols.map((m) => {
          const v = o.attributes[m.key];
          const isBest = best[m.key] !== undefined && v === best[m.key];
          const cls = [v == null ? "missing" : "", typeof v === "number" ? "num" : "", isBest ? "best" : ""].join(" ");
          return `<td class="${cls}" ${isBest ? `title="${esc(bestTitle(m))}"` : ""}>${isBest ? bestTag(m) : ""}${cellHtml(v, m, o.attribute_meta[m.key])}</td>`;
        }).join("")}
        <td class="act">${optMenuHtml(o, false)}</td>
      </tr>`).join("")}</tbody></table></div>`;
}

function cardsHtml({ metas, visible, best }, single) {
  if (!visible.length) return S.d.options.length ? `<div class="panel muted" style="margin-top:12px">No options match the filters. <button class="btn small ghost" data-action="clear-all-filters">Reset filters</button></div>` : "";
  const cols = metas.filter((m) => m.type !== "image");
  return `<div class="cards">${visible.map((o, i) => `
    <div class="card ${S.selection.has(o.id) ? "selected" : ""} ${S.focus === i ? "focus" : ""}" data-option="${esc(o.id)}">
      ${o.image ? `<img class="hero" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
      <div class="body">
        <div class="top"><span class="pick-hit"><input class="pick" type="${single ? "radio" : "checkbox"}" name="pick" data-pick="${esc(o.id)}" ${S.selection.has(o.id) ? "checked" : ""} aria-label="Select ${esc(o.label)}"></span>
          <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.label)}</a>` : esc(o.label)}</div>
          ${optMenuHtml(o, true)}</div>
        ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}
        ${cols.length ? `<dl>${cols.map((m) => {
          const v = o.attributes[m.key];
          const isBest = best[m.key] !== undefined && v === best[m.key];
          return `<dt>${esc(m.label)}</dt><dd class="${v == null ? "missing" : ""} ${isBest ? "best" : ""}" ${isBest ? `title="${esc(bestTitle(m))}"` : ""}>${isBest ? bestTag(m) : ""}${cellHtml(v, m, o.attribute_meta[m.key])}</dd>`;
        }).join("")}</dl>` : ""}
      </div>
    </div>`).join("")}</div>`;
}

// "Rule out" offers both the quick action and one with a reason, so neither depends on a hidden modifier.
function optMenuHtml(o, compact) {
  return `<details class="omenu ruleout" data-menu="${esc(o.id)}">
    <summary class="omenu-btn ${compact ? "icon-only" : ""}" aria-label="${compact ? `Actions for ${esc(o.label)}` : `Rule out ${esc(o.label)}`}" title="${compact ? "Actions" : "Rule out"}">${compact ? icon("more", 16) : `${icon("x", 14)}Rule out${icon("chevron-down", 14)}`}</summary>
    <div class="menu">
      <button data-action="exclude" data-id="${esc(o.id)}">${icon("x", 15)}Rule out<span class="kbd">X</span></button>
      <button data-action="add-reason" data-id="${esc(o.id)}">${icon("pencil", 15)}Rule out with reason…</button>
      ${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${icon("external", 15)}Open link</a>` : ""}
    </div></details>`;
}

function miniHtml(o, kind) {
  return `<div class="mini-opt ${kind}"><span class="opt-label">${esc(o.label)}</span>
    <span class="why">${kind === "excluded" ? esc(o.exclude_reason || "") : ""}</span>
    ${kind === "excluded" && !o.exclude_reason ? `<button class="btn small ghost" data-action="add-reason" data-id="${esc(o.id)}">${icon("pencil", 14)}Add reason</button>` : ""}
    ${kind === "excluded" ? `<button class="btn small ghost" data-action="include" data-id="${esc(o.id)}">${icon("undo", 14)}Restore</button>`
                          : `<button class="btn small ghost" data-action="exclude" data-id="${esc(o.id)}">${icon("x", 14)}Rule out</button>`}</div>`;
}

function decideBarHtml(d, single, { visible }) {
  if (isWaiting(d)) return `<div class="decide-bar locked" role="region" aria-label="Decide">
    <div class="sel">${icon("lock", 15)}<span>Locked until ${d.waiting_on.map((id) => `<a href="/d/${esc(id)}" data-nav>${esc(id)}</a>`).join(" and ")} ${d.waiting_on.length > 1 ? "are" : "is"} decided</span></div></div>`;
  const n = S.selection.size;
  const min = d.min_select, max = d.max_select;
  const valid = n >= min && (max === null || n <= max);
  const chosen = d.options.filter((o) => S.selection.has(o.id));
  const labels = chosen.map((o) => o.label);
  const hidden = chosen.filter((o) => !visible.includes(o));
  const need = single ? "Pick an option" : max === null ? `Pick at least ${min}` : min === max ? `Pick ${min}` : `Pick ${min}–${max}`;
  // Say which limit applies before the button refuses.
  const limit = single ? "" : n < min ? `${min - n} more needed` : max !== null ? (n >= max ? "limit reached" : `up to ${max}`) : "";
  const why = valid ? "" : n < min ? (min === 1 ? "Select an option first" : `Select at least ${min} options`) : `Select at most ${max} options`;
  const sel = !n ? `<span class="need">${single ? "Select an option to decide" : need}</span>`
    : !valid && n > (max ?? Infinity) ? `<span class="need warn">${why}</span>`
    : single ? `<span class="names" title="${esc(labels[0])}">${esc(labels[0])}</span>`
    : `<span class="count">${n} selected</span>${limit ? `<span class="limit">· ${limit}</span>` : ""}<span class="names" title="${esc(labels.join(", "))}">${esc(labels.join(", "))}</span>`;
  const showReason = S.notes[d.id] || S.showReason === d.id;
  return `<div class="decide-bar ${showReason ? "with-reason" : ""}" role="region" aria-label="Decide">
    <div class="sel" aria-live="polite">${sel}</div>
    ${hidden.length ? `<div class="hid" role="status" title="${esc(hidden.map((o) => o.label).join(", "))}">${icon("help", 14)}<span>${hidden.length} selected outside results</span>
      <button class="btn small ghost" data-action="clear-all-filters">Show</button><button class="btn small ghost" data-action="deselect-hidden">Deselect</button></div>` : ""}
    <div class="reason-field"><label for="note">Reason</label><input id="note" placeholder="Optional, shown to ${esc(d.source || "the requester")}" value="${esc(S.notes[d.id] ?? "")}" autocomplete="off"></div>
    <button class="btn ghost small add-reason-btn" data-action="show-reason">${icon("pencil", 14)}Reason</button>
    <button class="btn primary" data-action="resolve" ${valid && !S.resolving ? "" : "disabled"} title="${esc(why || "Decide (⌘↵)")}" aria-keyshortcuts="Meta+Enter Control+Enter">${S.resolving ? "Deciding…" : `${icon("check", 15)}Decide`}<span class="kbd" aria-hidden="true">⌘↵</span></button>
    ${effectsNote(d)}
    ${S.resolveError?.id === d.id ? `<div class="bar-note bad" role="alert">${icon("ban", 15)}<span>Couldn't decide: ${esc(S.resolveError.message)}</span>
      <button class="btn small" data-action="resolve" ${valid ? "" : "disabled"}>Retry</button></div>` : ""}
  </div>
  <div class="kbd-hint" aria-hidden="true"><span><span class="kbd">J</span><span class="kbd">K</span> move</span><span><span class="kbd">Space</span> select</span><span><span class="kbd">X</span> rule out</span>${attrKeys(d).length || d.options.length > 7 ? `<span><span class="kbd">/</span> search</span>` : ""}<span><span class="kbd">⌘</span><span class="kbd">↵</span> decide</span></div>`;
}

function attrRequestHtml(d, metas) {
  const requested = metas.filter((m) => m.requested);
  const active = d.options.filter((o) => !o.excluded);
  return `<div class="block-head"><h2>Ask for attributes</h2></div>
    <p class="section-note">Missing something to compare on? ${esc(d.source || "The requester")} is notified and fills values in, best effort.</p>
    <form class="form-row" id="attr-form">
      <label class="lf key">Key<input class="input" name="key" placeholder="delivery_days" required></label>
      <label class="lf grow">What you want to know<input class="input" name="description" placeholder="Days until it arrives"></label>
      <button class="btn">Ask</button>
    </form>
    ${requested.length ? `<div class="req-list">${requested.map((m) => {
      const have = active.filter((o) => o.attributes[m.key] != null).length;
      const pct = active.length ? Math.round((have / active.length) * 100) : 0;
      return `<div class="req-item"><span class="k">${esc(m.label)}</span><span class="d">${esc(m.def.description || "")}</span>
        <span class="muted small" title="${have} of ${active.length} options have a value">${have}/${active.length}</span><span class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(m.label)} filled in"><i style="width:${pct}%"></i></span>
        <button class="icon-btn" data-action="cancel-attr" data-key="${esc(m.key)}" title="Withdraw request" aria-label="Withdraw request for ${esc(m.label)}">${icon("x", 14)}</button></div>`;
    }).join("")}</div>` : ""}`;
}

function webhooksHtml(d) {
  const rows = S.deliveries.slice(0, 12).map((x) => `<tr>
      <td>${esc(x.event_type)}<div class="muted small">${ago(x.created_at)}</div></td>
      <td><span class="badge ${x.status}">${x.status}</span>${x.require_ack && !x.acked_at && x.status === "delivered" ? `<div class="muted small">awaiting ack</div>` : ""}</td>
      <td class="small">${x.total_attempts}× ${x.last_status ? `· ${x.last_status}` : ""}${x.last_error && !x.last_status ? `<div class="muted" title="${esc(x.last_error)}">${esc(x.last_error.slice(0, 40))}</div>` : ""}
        ${x.next_attempt_at ? `<div class="muted">next ${new Date(x.next_attempt_at).toLocaleTimeString()}</div>` : ""}</td>
      <td style="text-align:right"><button class="btn small ghost" data-action="redeliver" data-id="${x.id}" title="Fire again now">Re-fire</button></td></tr>`).join("");
  // The eye lets you resolve without telling this listener (say, to fix a typo). Global webhooks always hear.
  const eye = (id, name) => {
    const off = S.silenced.has(id);
    return `<button class="icon-btn eye ${off ? "off" : ""}" data-action="toggle-silence" data-id="${esc(id)}" aria-pressed="${off}"
      aria-label="${off ? `Notify ${esc(name)} when you decide` : `Don't notify ${esc(name)} when you decide`}"
      title="${off ? "Won't be notified when you decide. Click to notify." : "Will be notified when you decide. Click to skip it."}">${off ? EYE_OFF : EYE}</button>`;
  };
  const row = (id, dot, name, sub) => `<div class="listener ${id && S.silenced.has(id) ? "silenced" : ""}">${dot}<div class="l-body"><strong>${name}</strong><div class="l-sub" title="${esc(sub.replace(/<[^>]+>/g, ""))}">${sub}</div></div>${id ? eye(id, name) : ""}</div>`;
  const rs = S.resumer || {};
  const resumerRow = d.resume ? row("resumer", `<span class="live ${rs.online ? "on" : "off"}"></span>`, "Resumer",
    rs.online ? `${rs.machine ? `${esc(rs.machine)} · ` : ""}resumes the ${esc(d.resume.agent || "agent")} session` : `${rs.machine ? `${esc(rs.machine)} · ` : ""}offline, so the session won't resume automatically`) : "";
  const listeners = resumerRow + (S.listeners || []).map((l) => l.kind === "long-poll"
    ? row(l.id, `<span class="live ${l.connected ? "on" : ""}"></span>`, esc(l.actor), `waiting since ${ago(l.since)} · ${esc(l.events.join(", "))}`)
    : row(l.scope === "global" ? null : l.id, `<span class="live on hook"></span>`, l.scope === "global" ? "Global webhook" : "Webhook",
          `<span class="mono">${esc(l.url)}</span> · ${esc(l.events.join(", "))}${l.require_ack ? " · re-fires until acked" : ""}`)).join("");
  const count = (S.listeners || []).length + (d.resume ? 1 : 0);
  return `<div class="block-head"><h2>Notified when you decide</h2><span class="count">${count || ""}</span></div>
    ${listeners ? `<p class="section-note">Use the eye to skip one, e.g. when fixing a typo.</p>${listeners}`
                : `<p class="section-note">Nobody is listening right now. The decision is still recorded, and the requester can check it later.</p>`}
    ${rows ? `<div class="deliveries"><h3>Webhook deliveries</h3><table class="plain"><tr><th>Event</th><th>Status</th><th>Attempts</th><th><span class="sr-only">Actions</span></th></tr>${rows}</table></div>` : ""}`;
}

const EYE = icon("eye", 15);
const EYE_OFF = icon("eye-off", 15);

const EVENT_ICONS = {
  "decision.created": "sparkle", "decision.updated": "pencil", "decision.resolved": "check", "decision.reopened": "rotate",
  "decision.cancelled": "ban", "decision.expired": "clock", "decision.commented": "message", "options.added": "plus", "options.updated": "pencil",
  "option.removed": "minus", "options.excluded": "x", "options.included": "undo", "attributes.requested": "help",
  "attributes.request_cancelled": "ban", "webhook.acked": "check", "webhook.redelivered": "send",
  "decision.unblocked": "unlock", "dependency.changed": "link",
};
const EVENT_TONE = { "decision.resolved": "resolved", "decision.expired": "bad", "resume.failed": "bad" };

function eventText(e) {
  const d = S.d;
  const label = (id) => d.options.find((o) => o.id === id)?.label || id;
  const x = e.data;
  const quote = (t) => `<div class="sub">“${esc(t)}”</div>`;
  switch (e.type) {
    case "decision.created": return `created this decision with ${x.decision.options.length} option${x.decision.options.length === 1 ? "" : "s"}`;
    case "decision.updated": {
      const due = x.changes.due_at?.[1];
      const rest = Object.keys(x.changes).filter((k) => k !== "due_at");
      const deps = x.changes.depends_on && x.changes.depends_on[1].map((y) => y.decision);
      const [was, now] = x.changes.goals || [[], []];
      const linked = now.filter((g) => !was.includes(g)), unlinked = was.filter((g) => !now.includes(g));
      const other = rest.filter((k) => k !== "depends_on" && k !== "goals");
      return [due ? `moved the deadline to <strong title="${esc(fullTime(due))}">${esc(whenTime(due))}</strong>` : "",
              deps ? (deps.length ? `made it depend on <strong>${esc(deps.join(", "))}</strong>` : "removed its dependencies") : "",
              linked.length ? `linked it to <strong>${esc(linked.map(goalLabel).join(", "))}</strong>` : "",
              unlinked.length ? `unlinked it from <strong>${esc(unlinked.join(", "))}</strong>` : "",
              other.length ? `edited the ${esc(other.join(", "))}` : ""].filter(Boolean).join(" and ");
    }
    case "decision.resolved": return `${x.previous_status !== "resolved" ? "decided" : String(x.previous_resolution?.selected) === String(x.resolution.selected) ? "confirmed" : "changed the decision to"} <strong>${esc(x.resolution.selected.map(label).join(", "))}</strong>${x.resolution.note ? quote(x.resolution.note) : ""}${x.resolution.filters ? `<div class="sub">Filters: ${esc(x.resolution.filters)}</div>` : ""}${x.silenced ? `<div class="sub">Didn't notify ${esc(x.silenced.join(", "))}</div>` : ""}`;
    case "decision.reopened": return `reopened it${x.due_at ? ` with a new deadline, <span title="${esc(fullTime(x.due_at))}">${esc(whenTime(x.due_at))}</span>` : ""}${x.note ? quote(x.note) : ""}`;
    case "decision.expired": return `expired it: nobody decided before the deadline`;
    case "decision.unblocked": return `unlocked it${x.after ? `: <strong>${esc(x.after)}</strong> is settled` : ""}${x.due_at ? `, so its deadline starts: <span title="${esc(fullTime(x.due_at))}">${esc(whenTime(x.due_at))}</span>` : ""}`;
    case "dependency.changed": return `noted that <strong>${esc(x.decision)}</strong> ${x.status === "open" ? "was reopened" : x.status === "resolved" ? "was decided" : `was ${esc(x.status)}`}`;
    case "decision.cancelled": return `cancelled it${x.note ? quote(x.note) : ""}`;
    case "decision.commented": return `commented<div class="comment">${esc(x.text)}</div>`;
    case "options.added": return `added ${x.options.map((o) => `<strong>${esc(o.label)}</strong>`).join(", ")}`;
    case "options.updated": {
      const ids = Object.keys(x.options);
      const attrs = [...new Set(ids.flatMap((id) => Object.keys(x.options[id].attributes || {})))];
      return `updated ${ids.length} option${ids.length > 1 ? "s" : ""}${attrs.length ? ` (${esc(attrs.join(", "))})` : ""}`;
    }
    case "option.removed": return `removed <strong>${esc(x.option.label)}</strong>`;
    case "options.excluded": return `ruled out <strong>${esc(x.option_ids.map(label).join(", "))}</strong>${x.reason ? quote(x.reason) : ""}`;
    case "options.included": return `restored <strong>${esc(x.option_ids.map(label).join(", "))}</strong>`;
    case "attributes.requested": return `asked for ${Object.entries(x.attributes).map(([k, v]) => `<strong>${esc(k)}</strong>${v.description ? ` (${esc(v.description)})` : ""}`).join(", ")}`;
    case "attributes.request_cancelled": return `withdrew the request for <strong>${esc(x.key)}</strong>`;
    case "webhook.acked": return `acknowledged a webhook delivery`;
    case "webhook.redelivered": return `re-fired a webhook delivery`;
    default: return esc(e.type);
  }
}

function activityHtml(d) {
  const evs = [...S.events].reverse();
  return `<div class="block-head"><h2>Activity</h2><span class="count" title="Append-only, SHA-256 hash-chained">${S.events.length} event${S.events.length === 1 ? "" : "s"} · tamper-evident</span></div>
    <form class="composer" id="comment-form"><label class="sr-only" for="comment">Comment</label><input id="comment" class="input" name="text" placeholder="Leave a comment…" autocomplete="off"><button class="btn">Comment</button></form>
    <ul class="timeline">${evs.map((e) => `<li>
      <span class="ic ${EVENT_TONE[e.type] || ""}">${icon(EVENT_ICONS[e.type] || "sparkle", 14)}</span>
      <div class="ev"><span class="who">${esc(e.actor)}</span> <span class="what">${eventText(e)}</span><span class="when" title="${esc(fullTime(e.ts))}">${ago(e.ts)}</span>
        <details><summary>Technical details</summary><div class="tech">#${e.seq} · ${esc(e.type)} · hash ${esc(e.hash.slice(0, 16))}…</div><pre>${esc(JSON.stringify(e.data, null, 2))}</pre></details></div>
    </li>`).join("")}</ul>`;
}

// ------------------------------------------------------------------ goals and the map

const GOAL_STATUS = { active: "Active", achieved: "Achieved", abandoned: "Abandoned" };
const goalIcon = (g, size = 14) => `<span class="gi ${esc(g.status)}" aria-label="${esc(GOAL_STATUS[g.status] || g.status)} goal">${icon("target", size)}</span>`;
const goalsIn = (space) => S.goals.filter((g) => g.space === space);
const goalLabel = (id) => { const g = S.goals.find((x) => x.id === id); return g ? `${g.id} · ${g.title}` : id; };

// Goals a goal leads up to through what it serves, so nothing can be made to serve its own subgoal.
function goalAncestors(id, seen = new Set()) {
  for (const up of S.goals.find((g) => g.id === id)?.serves || []) if (!seen.has(up)) { seen.add(up); goalAncestors(up, seen); }
  return seen;
}
function goalDescendants(id) {
  const out = new Set();
  const walk = (x) => S.goals.filter((g) => g.serves.includes(x)).forEach((g) => { if (!out.has(g.id)) { out.add(g.id); walk(g.id); } });
  walk(id);
  return out;
}

// What a goal-picking dialog offers: active goals in the space, minus those that would make no sense.
async function pickGoal({ title, body, space, exclude = new Set(), ok }) {
  const choices = goalsIn(space).filter((g) => g.status === "active" && !exclude.has(g.id))
    .map((g) => ({ value: g.id, label: `${g.id} · ${g.title}` }));
  if (!choices.length) { toast("No goal to pick: create one on the Map first", { info: true }); return null; }
  return ask({ title, body, field: "Goal", choices, ok });
}

// Layered layout: what something is for sits above it. A goal sits below the goals it serves; a decision
// below its goals and the decisions it depends on. Decisions without either start below every goal.
const MAP = { w: 200, h: 84, gx: 20, gy: 56, pad: 20 };

function layoutMap(g) {
  const nodes = new Map(g.nodes.map((n) => [n.id, { ...n, ups: [], downs: [] }]));
  const edges = g.edges.filter((e) => nodes.has(e.from) && nodes.has(e.to));
  for (const e of edges) { nodes.get(e.from).ups.push(e.to); nodes.get(e.to).downs.push(e.from); }
  const rank = new Map();
  let floor = 0;
  const rankOf = (id) => {
    if (!rank.has(id)) {
      const n = nodes.get(id);
      rank.set(id, 0);  // guards against a cycle, which the server refuses anyway
      rank.set(id, n.ups.length ? 1 + Math.max(...n.ups.map(rankOf)) : n.kind === "goal" ? 0 : floor);
    }
    return rank.get(id);
  };
  const all = [...nodes.values()].sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "goal" ? -1 : 1) || num(a.id) - num(b.id));
  all.filter((n) => n.kind === "goal").forEach((n) => rankOf(n.id));
  floor = Math.max(-1, ...all.filter((n) => n.kind === "goal").map((n) => rank.get(n.id))) + 1;
  all.forEach((n) => rankOf(n.id));
  const layers = [];
  for (const n of all) (layers[rank.get(n.id)] ||= []).push(n);
  const step = MAP.w + MAP.gx;
  const x = new Map();
  layers.forEach((l) => l?.forEach((n, i) => x.set(n.id, i * step)));
  const mean = (ids) => ids.reduce((s, id) => s + x.get(id), 0) / ids.length;
  // Order each layer by where its neighbours are (fewer crossings), then pull nodes toward them without overlapping.
  for (let pass = 0; pass < 6; pass++) {
    const down = pass % 2 === 0;
    for (const layer of down ? layers : [...layers].reverse()) {
      if (!layer) continue;
      const want = new Map(layer.map((n) => { const near = down ? n.ups : n.downs; return [n.id, near.length ? mean(near) : x.get(n.id)]; }));
      layer.sort((a, b) => want.get(a.id) - want.get(b.id));
      let at = -Infinity;
      for (const n of layer) { at = Math.max(want.get(n.id), at + step); x.set(n.id, at); }
      for (let i = layer.length - 2; i >= 0; i--) x.set(layer[i].id, Math.min(x.get(layer[i].id), x.get(layer[i + 1].id) - step));
    }
  }
  const minX = Math.min(...x.values());
  const at = new Map(all.map((n) => [n.id, { x: x.get(n.id) - minX + MAP.pad, y: MAP.pad + rank.get(n.id) * (MAP.h + MAP.gy) }]));
  return {
    nodes: all, edges, at,
    width: Math.max(...[...at.values()].map((p) => p.x)) + MAP.w + MAP.pad,
    height: (layers.length - 1) * (MAP.h + MAP.gy) + MAP.h + 2 * MAP.pad,
  };
}
const num = (id) => Number(String(id).replace(/\D/g, ""));

function mapNodeHtml(n, p, focus) {
  const goal = n.kind === "goal";
  const sub = goal ? (n.status === "active" ? "" : GOAL_STATUS[n.status])
    : n.status === "resolved" ? `→ ${n.outcome.join(", ")}`
    : isWaiting(n) ? `after ${n.waiting_on.join(", ")}` : n.status === "open" ? "Undecided" : n.status[0].toUpperCase() + n.status.slice(1);
  return `<a href="/${goal ? "g" : "d"}/${esc(n.id)}" data-nav class="mnode ${n.kind} ${esc(n.status)} ${n.id === focus ? "focus" : ""} ${n.context ? "context" : ""}" data-node="${esc(n.id)}"
      style="left:${p.x}px;top:${p.y}px" title="${esc(`${n.id}: ${n.title}${n.context ? " (doesn't have the tag: shown because what does leads up to it)" : ""}`)}">
    <span class="mn-top">${goal ? icon("target", 12) : statusIcon(n, 12)}<span>${goal ? "Goal " : ""}${esc(n.id)}</span>
      ${n.changed ? `<span class="mn-changed" title="Changed its mind ${n.changed} time${n.changed > 1 ? "s" : ""}">${icon("rotate", 11)}${n.changed}</span>` : ""}</span>
    <span class="mn-title">${esc(n.title)}</span>
    ${sub ? `<span class="mn-sub">${esc(sub)}</span>` : ""}</a>`;
}

function mapHtml(g, { focus } = {}) {
  if (!g.nodes.length) return "";
  const L = layoutMap(g);
  const { w, h } = MAP;
  const paths = L.edges.map((e) => {
    const a = L.at.get(e.from), b = L.at.get(e.to);
    const x1 = a.x + w / 2, y1 = a.y, x2 = b.x + w / 2, y2 = b.y + h, m = (y1 + y2) / 2;
    return `<path class="medge ${e.kind}" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="M${x1} ${y1}C${x1} ${m} ${x2} ${m} ${x2} ${y2}"/>`;
  }).join("");
  return `<div class="map-scroll"><div class="map" style="width:${L.width}px;height:${L.height}px">
    <svg class="medges" width="${L.width}" height="${L.height}" aria-hidden="true">${paths}</svg>
    ${L.nodes.map((n) => mapNodeHtml(n, L.at.get(n.id), focus)).join("")}</div></div>`;
}

const mapLegend = () => `<div class="map-legend" aria-hidden="true">
  <span><i class="lg goal"></i>Goal</span><span><i class="lg decision"></i>Decision</span>
  <span><svg width="26" height="8"><path class="medge toward" d="M0 4H26"/></svg>step toward</span>
  <span><svg width="26" height="8"><path class="medge depends_on" d="M0 4H26"/></svg>depends on</span>
  <span>${icon("rotate", 12)}changed its mind</span></div>`;

// Hovering or focusing a node lights up everything that led to it and everything it led to.
function bindMaps(root = $("#main")) {
  root.querySelectorAll(".map").forEach((map) => {
    const ups = {}, downs = {};
    map.querySelectorAll(".medge").forEach((p) => { (ups[p.dataset.from] ||= []).push(p.dataset.to); (downs[p.dataset.to] ||= []).push(p.dataset.from); });
    const reach = (id, adj, seen) => { for (const n of adj[id] || []) if (!seen.has(n)) { seen.add(n); reach(n, adj, seen); } return seen; };
    let current = null;
    const trace = (id) => {
      if (id === current) return;
      current = id;
      map.classList.toggle("tracing", !!id);
      if (!id) return;
      const keep = new Set([id, ...reach(id, ups, new Set()), ...reach(id, downs, new Set())]);
      map.querySelectorAll(".mnode").forEach((n) => n.classList.toggle("lit", keep.has(n.dataset.node)));
      map.querySelectorAll(".medge").forEach((p) => p.classList.toggle("lit", keep.has(p.dataset.from) && keep.has(p.dataset.to)));
    };
    map.addEventListener("pointerover", (e) => trace(e.target.closest(".mnode")?.dataset.node || null));
    map.addEventListener("pointerleave", () => trace(null));
    map.addEventListener("focusin", (e) => trace(e.target.closest(".mnode")?.dataset.node || null));
    map.addEventListener("focusout", () => trace(null));
    // Start with the focused node in view.
    const f = map.querySelector(".mnode.focus");
    if (f) { const sc = map.parentElement; sc.scrollLeft = f.offsetLeft - sc.clientWidth / 2 + MAP.w / 2; }
  });
}

// Goals as an outline: each under the goals it serves (one serving several shows under each).
function goalTreeHtml(goals) {
  const ids = new Set(goals.map((g) => g.id));
  const under = (parent) => goals.filter((g) => parent ? g.serves.includes(parent) : !g.serves.some((s) => ids.has(s)));
  const row = (g) => {
    const n = g.decisions;
    const kids = under(g.id);
    return `<li><a href="/g/${esc(g.id)}" data-nav class="gt-row ${esc(g.status)}">${goalIcon(g)}<span class="gt-id">${esc(g.id)}</span><span class="gt-title">${esc(g.title)}</span>
      <span class="gt-meta">${g.status !== "active" ? `${GOAL_STATUS[g.status]} · ` : ""}${n.total ? `${n.total} decision${n.total === 1 ? "" : "s"}${n.open ? `, ${n.open} open` : ""}` : "no decisions yet"}</span></a>
      ${kids.length ? `<ul>${kids.map(row).join("")}</ul>` : ""}</li>`;
  };
  return `<ul class="goal-tree">${under(null).map(row).join("")}</ul>`;
}

const refChip = (id, title) => `<a class="chip ref" href="/${id.startsWith("G") ? "g" : "d"}/${esc(id)}" data-nav title="${esc(title || "")}">${esc(id)}</a>`;

function insightsHtml(ins) {
  const titleOf = (id) => S.list.find((d) => d.id === id)?.title || S.goals.find((g) => g.id === id)?.title || "";
  const chips = (ids) => ids.map((id) => refChip(id, titleOf(id))).join("");
  const dead = ins.dead_ends.length ? `<div class="table-scroll"><table class="plain insight-table">
      <tr><th>Option</th><th class="num" title="Decisions that picked it">Picked</th><th class="num" title="Picked, then changed or withdrawn">Reversed</th>
        <th class="num" title="Picked for a goal that was later abandoned">Goal abandoned</th><th class="num" title="Ruled out, of the times it was offered">Ruled out</th><th>Where</th></tr>
      ${ins.dead_ends.map((x) => `<tr><td><strong>${esc(x.label)}</strong>${x.dead_end_rate != null ? `<div class="muted small">${Math.round(x.dead_end_rate * 100)}% of picks were dead ends</div>` : ""}</td>
        <td class="num">${x.picked}</td><td class="num ${x.reversed ? "bad" : ""}">${x.reversed || "—"}</td><td class="num ${x.abandoned ? "bad" : ""}">${x.abandoned || "—"}</td>
        <td class="num">${x.ruled_out ? `${x.ruled_out} of ${x.offered}` : "—"}</td>
        <td><div class="chips">${x.decisions.map((d) => `<a class="chip ref ${esc(d.outcome)}" href="/d/${esc(d.decision)}" data-nav title="${esc(`${d.title}: ${d.outcome.replace("_", " ")}${d.reason ? ` (${d.reason})` : ""}`)}">${esc(d.decision)}</a>`).join("")}</div></td></tr>`).join("")}
    </table></div>` : `<p class="muted small">None yet. Options show up here once picking them didn't hold, or they keep being ruled out.</p>`;
  const turns = ins.reversals.length ? ins.reversals.map((x) => `<div class="reversal">
      <div class="rv-head"><a href="/d/${esc(x.decision)}" data-nav class="dep-title"><span class="dep-id">${esc(x.decision)}</span><span class="dep-name">${esc(x.title)}</span></a>
        <span class="muted small nowrap" title="${esc(fullTime(x.at))}">${esc(x.by)} · ${ago(x.at)}</span></div>
      <div class="rv-turn"><s>${esc(x.from.join(", "))}</s>${icon("arrow-right", 14)}<strong>${x.withdrawn ? "withdrawn" : esc(x.to.join(", "))}</strong></div>
      ${x.note ? `<div class="rv-note">“${esc(x.note)}”</div>` : ""}
      <div class="rv-links">
        ${x.upstream.length ? `<span>Led there by ${chips(x.upstream)}</span>` : ""}
        ${x.invalidated.length ? `<span class="warn">Already decided on top of it ${chips(x.invalidated)}</span>` : ""}
        ${x.goals.length ? `<span>Toward ${chips(x.goals)}</span>` : ""}
        <a href="/map?focus=${esc(x.decision)}" data-nav>Trace on the map</a></div></div>`).join("")
    : `<p class="muted small">No decision has changed its mind yet.</p>`;
  const sunk = ins.abandoned_goals.length ? ins.abandoned_goals.map((x) => `<div class="dep"><a href="/g/${esc(x.goal)}" data-nav class="dep-title">${goalIcon({ status: "abandoned" })}<span class="dep-id">${esc(x.goal)}</span><span class="dep-name">${esc(x.title)}</span></a>
      <span class="dep-effect">${x.decisions} decision${x.decisions === 1 ? "" : "s"} went into it</span>${x.note ? `<div class="dep-note">${esc(x.note)}</div>` : ""}</div>`).join("")
    : `<p class="muted small">No goal has been abandoned.</p>`;
  return `<section class="block insights" aria-labelledby="ins-h"><div class="block-head"><h2 id="ins-h">What the history says</h2></div>
    <h3 class="dep-head">Dead ends</h3>
    <p class="section-note">Options that were picked and didn't hold, were picked for a goal that was then abandoned, or keep being ruled out. Matched by label across decisions. Think twice before spending time on them again.</p>
    ${dead}
    <h3 class="dep-head">Changes of mind</h3>
    <p class="section-note">Each time a decision was changed or withdrawn after it was made: what led there, and what had already been decided on top of it.</p>
    ${turns}
    <h3 class="dep-head">Abandoned goals</h3>
    ${sunk}
  </section>`;
}

async function renderMap() {
  const params = new URLSearchParams(location.search);
  const focus = params.get("focus") || "";
  const all = params.get("all") === "1";
  const tags = mapTags();
  const q = new URLSearchParams(), iq = new URLSearchParams();
  if (S.space) { q.set("space", S.space); iq.set("space", S.space); }
  if (tags.length) { q.set("tags", tags.join(",")); iq.set("tags", tags.join(",")); }
  if (focus) q.set("focus", focus);
  if (all) q.set("all", "1");
  const [g, ins] = await Promise.all([api("GET", `/api/graph?${q}`), api("GET", `/api/insights?${iq}`)]);
  if (location.pathname !== "/map") return;
  const shown = new Set(g.nodes.map((n) => n.id));
  const goals = S.goals.filter((x) => inSpace(x) && (!tags.length || shown.has(x.id)));
  // Every tag in this space; decisions count too, since agents tag them.
  const known = [...new Set([...tags, ...[...S.goals, ...S.list].filter(inSpace).flatMap((x) => x.tags || [])])].sort();
  const main = $("#main");
  const scroll = main.scrollTop, mapScroll = $(".map-scroll")?.scrollLeft;
  main.innerHTML = `<div class="page wide">
    <div class="map-head"><div><h1 class="d-title">Map</h1>
      <p>Goals are why decisions are made. Each goal serves larger ones, from the vision down to technical goals, and each decision is a step toward one. Hover anything to trace what led to it and what it led to.</p></div>
      <button class="btn primary" data-action="new-goal">${icon("plus", 15)}New goal</button></div>
    ${known.length ? `<div class="tag-filter" role="group" aria-label="Only show goals and decisions with these tags">
        <span class="tlabel">${icon("filter", 14)}Topics</span>
        ${known.map((t) => `<button class="tagb ${tags.includes(t) ? "on" : ""}" data-action="map-tag" data-tag="${esc(t)}" aria-pressed="${tags.includes(t)}">#${esc(t)}</button>`).join("")}
        ${tags.length ? `<button class="btn small ghost" data-action="map-tags-clear">Show all</button>
          <span class="muted small">Decisions have their goals' tags. Faded: what matches leads up to it.</span>` : ""}</div>` : ""}
    ${g.nodes.length || focus || tags.length ? `<div class="toolbar map-tools">
        ${focus ? `<span class="afchip">Connected to ${esc(focus)}<a href="/map" data-nav aria-label="Show the whole map">${icon("x", 13)}</a></span>` : ""}
        ${tags.length && !g.nodes.length ? `<span class="muted small">Nothing has ${tags.map((t) => `#${esc(t)}`).join(" or ")}${S.space ? ` in ${esc(S.space)}` : ""}.</span>` : ""}
        <label class="check-label small"><input type="checkbox" id="map-all" ${all ? "checked" : ""}> Show decisions without a goal</label>
        <span class="spacer"></span>${mapLegend()}</div>
      ${mapHtml(g, { focus })}`
    : `<div class="empty-state"><span class="es-ic">${icon("target", 24)}</span><h2>No goals yet</h2>
        <p>Start with the vision, then add the goals that serve it. Link decisions to the goal they're a step toward, and the map shows how every choice connects to why it was made.</p>
        <button class="btn primary" data-action="new-goal">${icon("plus", 15)}New goal</button></div>`}
    ${goals.length ? `<section class="block"><div class="block-head"><h2>Goals</h2><span class="count">${goals.filter((x) => x.status === "active").length} active</span></div>${goalTreeHtml(goals)}</section>` : ""}
    ${insightsHtml(ins)}
  </div>`;
  main.scrollTop = scroll;
  if (mapScroll != null && $(".map-scroll")) $(".map-scroll").scrollLeft = mapScroll;
  $("#map-all")?.addEventListener("change", (e) => {
    const p = new URLSearchParams(location.search);
    if (e.target.checked) p.set("all", "1"); else p.delete("all");
    navigate(`/map${p.toString() ? `?${p}` : ""}`, true);
  });
  bindMaps();
  document.title = "Map · Decisions";
}

// Topics the map is filtered to: the link's, else the last ones picked in this browser.
function mapTags() {
  const raw = new URLSearchParams(location.search).get("tags") ?? storage("mapTags") ?? "";
  return raw.split(",").map((t) => t.trim()).filter(Boolean);
}
function setMapTags(tags) {
  storage("mapTags", tags.length ? tags.join(",") : null);
  const p = new URLSearchParams(location.search);
  if (tags.length) p.set("tags", tags.join(",")); else p.delete("tags");
  navigate(`/map${p.toString() ? `?${p}` : ""}`, true);
}

async function loadGoal(id) {
  const seq = ++loadSeq;
  const [g, ev, graph] = await Promise.all([
    api("GET", `/api/goals/${id}`),
    api("GET", `/api/goals/${id}/events`),
    api("GET", `/api/graph?focus=${encodeURIComponent(id)}`),
  ]);
  if (seq !== loadSeq) return;
  if (S.goal?.id !== g.id) S.editingGoal = S.addingTag = false;
  S.goal = g;
  S.goalEvents = ev.events;
  S.goalGraph = graph;
  renderGoal();
  renderSidebar();
}

function goalEventText(e) {
  const x = e.data;
  const quote = (t) => (t ? `<div class="sub">“${esc(t)}”</div>` : "");
  switch (e.type) {
    case "goal.created": return "created this goal";
    case "goal.updated": {
      const c = x.changes;
      const serves = c.serves && (c.serves[1].length ? `made it serve <strong>${esc(c.serves[1].join(", "))}</strong>` : "made it a top-level goal");
      const rest = Object.keys(c).filter((k) => k !== "serves");
      return [serves, rest.length ? `edited the ${esc(rest.join(", "))}` : ""].filter(Boolean).join(" and ");
    }
    case "goal.achieved": return `marked it achieved${quote(x.note)}`;
    case "goal.abandoned": return `abandoned it${quote(x.note)}`;
    case "goal.reactivated": return `made it active again${quote(x.note)}`;
    default: return esc(e.type);
  }
}

function goalStatusHtml(g) {
  if (g.status === "active") return "";
  const achieved = g.status === "achieved";
  return `<div class="outcome ${achieved ? "" : "cancelled"}">${icon(achieved ? "check-circle" : "ban", 20)}<div style="flex:1;min-width:0">
    <div class="o-label">${GOAL_STATUS[g.status]}</div>
    <div class="o-pick">${g.status_note ? esc(g.status_note) : achieved ? "Done" : "No longer pursued"}</div>
    <div class="note" title="${esc(fullTime(g.closed_at))}">${ago(g.closed_at)}</div></div>
    <button class="btn small" data-action="goal-status" data-status="active">${icon("rotate", 14)}Reactivate</button></div>`;
}

function renderGoal() {
  const g = S.goal;
  const main = $("#main");
  const scroll = main.scrollTop;
  const sep = `<span class="sep" aria-hidden="true">·</span>`;
  const active = g.status === "active";
  const head = S.editingGoal ? `<form class="edit-form" id="goal-form">
      <label class="lf">Title<input class="input" name="title" value="${esc(g.title)}" style="font-size:16px;font-weight:600;height:40px"></label>
      <label class="lf">Description <span style="font-weight:400">(markdown: what success looks like, and why it matters)</span><textarea class="input" name="description">${esc(g.description)}</textarea></label>
      <div class="form-row"><button class="btn primary">Save</button><button type="button" class="btn ghost" data-action="cancel-goal-edit">Cancel</button></div></form>`
    : `<div class="d-head" style="display:flex;gap:16px;align-items:flex-start;justify-content:space-between">
      <div style="min-width:0"><h1 class="d-title">${esc(g.title)}</h1>
        <div class="d-sub"><button class="chip id" data-action="copy-goal-ref" title="Copy a markdown link">${esc(g.id)}${icon("copy", 12)}</button>
          <span class="chip status ${esc(g.status)}">${goalIcon(g, 12)}${esc(g.status)}</span>${spaceChip(g)}
          <span title="${esc(fullTime(g.created_at))}">created ${ago(g.created_at)}</span>
          ${tagsHtml("goal", g.tags, "Topics like business, product or tech: the map filters on them, and decisions toward it share them")}</div></div>
      <div class="d-actions">
        <button class="btn small ghost" data-action="edit-goal">${icon("pencil", 14)}Edit</button>
        ${active ? `<button class="btn small ghost" data-action="goal-status" data-status="achieved" title="It's done">${icon("check", 14)}Achieved</button>
          <button class="btn small ghost danger" data-action="goal-status" data-status="abandoned" title="Stop pursuing it">${icon("ban", 14)}Abandon</button>` : ""}
      </div></div>
    ${g.description ? `<div class="context">${md(g.description)}</div>` : ""}`;
  const row = (id, title, status, effect, remove, glyph) => `<div class="dep"><a href="/${id.startsWith("G") ? "g" : "d"}/${esc(id)}" data-nav class="dep-title">${glyph}<span class="dep-id">${esc(id)}</span><span class="dep-name">${esc(title)}</span></a>
      <span class="dep-effect ${effect[0]}">${esc(effect[1])}</span>${remove || ""}</div>`;
  const x = (action, id, what) => `<button class="icon-btn" data-action="${action}" data-id="${esc(id)}" title="${esc(what)}" aria-label="${esc(what)}">${icon("x", 14)}</button>`;
  const goalEffect = (s) => [s.status === "achieved" ? "ok" : "", GOAL_STATUS[s.status]];
  const decisionEffect = (d) => d.status === "resolved" ? ["ok", `→ ${d.outcome.join(", ")}`] : d.status === "open"
    ? (d.waiting_on.length ? ["", `After ${d.waiting_on.join(", ")}`] : ["warn", "Undecided"]) : ["", d.status[0].toUpperCase() + d.status.slice(1)];
  main.innerHTML = `<div class="page">
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/map" data-nav class="muted">Map</a>${icon("chevron", 14)}<span>${esc(g.id)}</span></nav>
    ${head}
    ${goalStatusHtml(g)}
    <div class="cols goal-cols">
      <section class="block"><div class="block-head"><h2>Serves</h2><span class="spacer"></span>
          <button class="btn small ghost" data-action="add-serves">${icon("plus", 14)}Larger goal…</button></div>
        ${g.serves.length ? g.serves.map((s) => row(s.goal, s.title, s.status, goalEffect(s), x("remove-serves", s.goal, `Stop serving ${s.goal}`), goalIcon(s))).join("")
          : `<p class="section-note">A top-level goal: the vision, or something pursued for its own sake.</p>`}
      </section>
      <section class="block"><div class="block-head"><h2>Subgoals</h2><span class="spacer"></span>
          <button class="btn small ghost" data-action="new-goal" data-serves="${esc(g.id)}">${icon("plus", 14)}New subgoal</button></div>
        ${g.subgoals.length ? g.subgoals.map((s) => row(s.goal, s.title, s.status, goalEffect(s), "", goalIcon(s))).join("")
          : `<p class="section-note">Break it down into the strategic, product or technical goals it takes.</p>`}
      </section>
    </div>
    <section class="block"><div class="block-head"><h2>Decisions toward it</h2><span class="count">${g.decisions.length || ""}</span><span class="spacer"></span>
        <button class="btn small ghost" data-action="link-decision">${icon("link", 14)}Link a decision…</button></div>
      ${g.decisions.length ? g.decisions.map((d) => row(d.decision, d.title, d.status, decisionEffect(d), x("unlink-decision", d.decision, `Unlink ${d.decision}`), statusIcon(d))).join("")
        : `<p class="section-note">None yet. Link an existing one, or have agents pass <code>"goals": ["${esc(g.id)}"]</code> (<code>decide create --goal ${esc(g.id)}</code>) when they ask.</p>`}
    </section>
    ${S.goalGraph.nodes.length > 1 ? `<section class="block"><div class="block-head"><h2>On the map</h2><span class="count">what it serves, and everything working toward it</span><span class="spacer"></span>
        <a class="btn small ghost" href="/map?focus=${esc(g.id)}" data-nav>${icon("map", 14)}Open in the map</a></div>${mapHtml(S.goalGraph, { focus: g.id })}</section>` : ""}
    <section class="block"><div class="block-head"><h2>Activity</h2><span class="count">${S.goalEvents.length} event${S.goalEvents.length === 1 ? "" : "s"} · tamper-evident</span></div>
      <ul class="timeline">${[...S.goalEvents].reverse().map((e) => `<li>
        <span class="ic ${e.type === "goal.achieved" ? "resolved" : e.type === "goal.abandoned" ? "bad" : ""}">${icon({ "goal.created": "sparkle", "goal.updated": "pencil", "goal.achieved": "check", "goal.abandoned": "ban", "goal.reactivated": "rotate" }[e.type] || "sparkle", 14)}</span>
        <div class="ev"><span class="who">${esc(e.actor)}</span> <span class="what">${goalEventText(e)}</span><span class="when" title="${esc(fullTime(e.ts))}">${ago(e.ts)}</span>
          <details><summary>Technical details</summary><div class="tech">#${e.seq} · ${esc(e.type)} · hash ${esc(e.hash.slice(0, 16))}…</div><pre>${esc(JSON.stringify(e.data, null, 2))}</pre></details></div></li>`).join("")}</ul></section>
  </div>`;
  main.scrollTop = scroll;
  bindMaps();
  document.title = `${g.id} · ${g.title}`;
  const form = $("#goal-form");
  if (form) {
    form.querySelector("input").focus();
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      if (await goalAct(() => api("PATCH", `/api/goals/${g.id}`, { title: f.get("title"), description: f.get("description") }), "Saved")) {
        S.editingGoal = false; renderGoal();
      }
    };
  }
  bindTagInput("goal");
}

// Like act(), for goals: true once the server confirmed it.
async function goalAct(fn, okMsg) {
  try {
    await fn();
    await loadList();
    if (S.goal) await loadGoal(S.goal.id);
    if (okMsg) toast(okMsg);
    return true;
  } catch (e) { fail(e); return false; }
}

// Toward: the goals a decision is a step toward, shown under its title.
function towardHtml(d) {
  if (!d.goals?.length) return "";
  return `<div class="toward">${icon("target", 14)}<span>Toward</span>
    ${d.goals.map((g) => `<span class="chip goal ${esc(g.status)}"><a href="/g/${esc(g.goal)}" data-nav title="${esc(GOAL_STATUS[g.status])} goal">${esc(g.goal)} · ${esc(g.title)}</a>
      <button class="chip-x" data-action="remove-goal" data-id="${esc(g.goal)}" aria-label="Unlink from ${esc(g.goal)}" title="Unlink">${icon("x", 12)}</button></span>`).join("")}
    <button class="btn small ghost" data-action="add-goal">${icon("plus", 14)}Goal</button>
    <a class="btn small ghost" href="/map?focus=${esc(d.id)}" data-nav>${icon("map", 14)}Trace on the map</a></div>`;
}

// An open decision whose every goal was abandoned may not be worth deciding.
function goalGoneHtml(d) {
  if (d.status !== "open" || !d.goals?.length || !d.goals.every((g) => g.status === "abandoned")) return "";
  return `<div class="callout warn">${icon("target", 18)}
    <div class="callout-main"><div><strong class="c-title">Its goal was abandoned</strong></div>
      <div class="small">${d.goals.map((g) => `<a href="/g/${esc(g.goal)}" data-nav>${esc(g.goal)}</a> “${esc(g.title)}”`).join(" and ")} ${d.goals.length > 1 ? "are" : "is"} no longer pursued, so this may not need deciding.</div></div>
    <div class="callout-actions"><button class="btn small" data-action="cancel-decision">${icon("ban", 14)}Cancel it</button></div></div>`;
}

async function newGoal(serves) {
  const title = await ask({ title: serves ? `New subgoal of ${serves}` : "New goal", body: `<p>${serves ? "A step on the way to it" : "Start with the vision, then the goals that serve it"}. Describe what success looks like on its page.</p>`,
    field: "Goal", placeholder: serves ? "e.g. Onboard a customer in a day" : "e.g. Be the default tool for EU utilities", ok: "Create goal" });
  if (!title) return;
  try {
    // A subgoal starts with its parent's topics (business, tech, …); edit them on its page.
    const tags = serves ? S.goals.find((x) => x.id === serves)?.tags || [] : [];
    const g = await api("POST", "/api/goals", { title, serves: serves ? [serves] : [], tags, space: S.goal?.space || S.space || S.config.space });
    await loadList();
    navigate(`/g/${g.id}`);
  } catch (err) { fail(err); }
}

const setGoals = (d, goals, okMsg) => act(() => api("PATCH", `/api/decisions/${d.id}`, { goals }), okMsg);

// ------------------------------------------------------------------ interactions

function visibleIds() { return partition().visible.map((o) => o.id); }

function toggleSelect(id) {
  const d = S.d;
  if (S.selection.has(id)) S.selection.delete(id);
  else {
    if (d.max_select === 1) S.selection.clear();
    else if (d.max_select !== null && S.selection.size >= d.max_select) {
      toast(`You can pick at most ${d.max_select}. Deselect one first.`, { info: true });
      return renderDecision();
    }
    S.selection.add(id);
  }
  renderDecision();
}

// Runs a mutation; true once the server confirmed it. Failures show a persistent error.
async function act(fn, okMsg) {
  try {
    const d = await fn();
    if (d && d.id && d.options) { S.d = d; await loadDecision(d.id, { keepSelection: true }); }
    if (okMsg) toast(okMsg);
    loadList();
    return true;
  } catch (e) { fail(e); return false; }
}

async function exclude(ids, reason, filters) {
  const d = S.d;
  const labels = ids.map((id) => d.options.find((o) => o.id === id)?.label || id);
  if (!await act(() => api("POST", `/api/decisions/${d.id}/exclude`, { option_ids: ids, reason, filters }))) return;
  ids.forEach((id) => S.selection.delete(id));
  renderDecision();
  const actions = !reason && ids.length === 1 ? [{ label: "Add reason", run: () => excludeWithReason(ids[0]) }] : [];
  toast(`Ruled out ${labels.length > 2 ? labels.length + " options" : labels.join(", ")}`, {
    undo: () => act(() => api("POST", `/api/decisions/${d.id}/include`, { option_ids: ids }), "Restored"),
    actions,
  });
}

async function excludeWithReason(id) {
  const d = S.d;
  const label = d.options.find((o) => o.id === id)?.label || id;
  const already = d.options.find((o) => o.id === id)?.excluded;
  const reason = await ask({ title: already ? `Why was ${label} ruled out?` : `Rule out ${label}?`, field: "Why isn't it viable? (recorded in the log)", ok: already ? "Save reason" : "Rule out" });
  if (reason === null) return;
  if (!already) return exclude([id], reason || undefined);
  if (reason) act(() => api("POST", `/api/decisions/${d.id}/exclude`, { option_ids: [id], reason }), "Reason recorded");
}

function bindDecision() {
  const d = S.d;
  const u = ui();
  const main = $("#main");

  main.querySelectorAll("[data-filter]").forEach((el) => {
    el.addEventListener(el.type === "radio" || el.tagName === "SELECT" ? "change" : "input", () => {
      const f = (u.filters[el.dataset.filter] ||= {});
      f[el.dataset.part] = el.value;
      saveUi();
      renderDecision();
    });
  });
  main.querySelectorAll("[data-set]").forEach((el) => el.addEventListener("change", () => {
    const f = (u.filters[el.dataset.set] ||= {});
    const set = new Set(f.set || []);
    el.checked ? set.add(el.value) : set.delete(el.value);
    f.set = [...set]; saveUi(); renderDecision();
  }));
  main.querySelectorAll("details[data-pop]").forEach((el) => el.addEventListener("toggle", () => {
    if (el.open) {
      S.pop = el.dataset.pop;
      main.querySelectorAll("details[data-pop][open]").forEach((o) => o !== el && o.removeAttribute("open"));
      if (!el.contains(document.activeElement) || el.querySelector("summary") === document.activeElement) el.querySelector(".pop input")?.focus();
    } else if (S.pop === el.dataset.pop) S.pop = null;
    syncPopModal();
  }));
  const text = $("#opt-text");
  if (text) text.addEventListener("input", () => { u.text = text.value; saveUi(); renderDecision(); });
  const keep = $("#keep-unknown");
  if (keep) keep.onchange = () => { u.keepUnknown = keep.checked; saveUi(); renderDecision(); };
  main.querySelectorAll("details.omenu").forEach((el) => el.addEventListener("toggle", () => {
    if (!el.open) return;
    document.querySelectorAll("details.omenu[open]").forEach((o) => o !== el && o.removeAttribute("open"));
    placeMenu(el);
    el.querySelector(".menu button")?.focus();
  }));
  main.querySelectorAll("details[data-key]").forEach((el) => el.addEventListener("toggle", () => { u[el.dataset.key] = el.open; saveUi(); }));
  main.querySelectorAll("[data-sort]").forEach((b) => b.onclick = () => {
    const k = b.dataset.sort;
    u.sort = !u.sort || u.sort.key !== k ? { key: k, dir: "asc" } : u.sort.dir === "asc" ? { key: k, dir: "desc" } : null;
    saveUi(); renderDecision();
  });
  const note = $("#note");
  if (note) note.addEventListener("input", () => {
    S.notes[d.id] = note.value;
    note.closest(".decide-bar").classList.toggle("with-reason", !!note.value);
  });

  const attrForm = $("#attr-form");
  if (attrForm) attrForm.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(attrForm);
    const key = f.get("key").trim();
    act(() => api("POST", `/api/decisions/${d.id}/attribute-requests`, { key, description: f.get("description").trim() }), `Asked for “${key}”`);
  };
  bindComments(d);
  const editForm = $("#edit-form");
  if (editForm) {
    editForm.querySelector("input").focus();
    editForm.onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(editForm);
      if (await act(() => api("PATCH", `/api/decisions/${d.id}`, { title: f.get("title"), context: f.get("context") }), "Saved")) {
        S.editing = false; renderDecision();
      }
    };
  }
  bindTagInput("decision");
}

// Menus are fixed to the viewport so table scrolling and card overflow can't clip them.
function placeMenu(el) {
  const menu = el.querySelector(".menu"), at = el.querySelector("summary").getBoundingClientRect();
  menu.style.visibility = "hidden";
  menu.style.left = menu.style.top = "0px";
  const h = menu.offsetHeight, w = menu.offsetWidth;
  const below = at.bottom + 4 + h <= innerHeight - 8;
  menu.style.top = `${below ? at.bottom + 4 : Math.max(8, at.top - 4 - h)}px`;
  menu.style.left = `${Math.max(8, Math.min(at.right - w, innerWidth - w - 8))}px`;
  menu.style.visibility = "";
}
addEventListener("scroll", () => document.querySelectorAll("details.omenu[open]").forEach((m) => m.removeAttribute("open")), true);

function bindComments(d) {
  const form = $("#comment-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const t = new FormData(form).get("text").trim();
    if (t && await act(() => api("POST", `/api/decisions/${d.id}/comments`, { text: t }), "Comment added")) $("#comment")?.blur();
  };
}

// On phones the open filter is a bottom sheet: everything else is inert until it closes.
let modalInert = [];
function syncPopModal() {
  modalInert.forEach((el) => (el.inert = false));
  modalInert = [];
  syncInert();
  const open = narrow.matches && S.pop && document.querySelector(`details[data-pop="${CSS.escape(S.pop)}"][open]`);
  document.querySelectorAll(".pop[aria-modal]").forEach((p) => p.removeAttribute("aria-modal"));
  if (!open) return;
  open.querySelector(".pop").setAttribute("aria-modal", "true");
  for (let node = open.querySelector(".pop"); node.parentElement && node !== document.body; node = node.parentElement)
    for (const sib of node.parentElement.children)
      if (sib !== node && !sib.inert && !sib.matches(".pop-scrim, #toast, #conn, script")) { sib.inert = true; modalInert.push(sib); }
}

// Closes the open filter popover; `refocus` puts focus back on its button.
function closePops(refocus) {
  if (!S.pop) return;
  const summary = document.querySelector(`details[data-pop="${CSS.escape(S.pop)}"] summary`);
  S.pop = null;
  document.querySelectorAll("details[data-pop][open]").forEach((o) => o.removeAttribute("open"));
  syncPopModal();
  if (refocus) summary?.focus();
}

document.addEventListener("click", async (e) => {
  if (!e.target.closest(".fpop")) closePops();
  document.querySelectorAll("details.omenu[open]").forEach((m) => !m.contains(e.target) && m.removeAttribute("open"));
  if (e.target.closest(".omenu") && !e.target.closest("[data-action]")) return;
  const nav = e.target.closest("a[data-nav]");
  if (nav && !e.metaKey && !e.ctrlKey) { e.preventDefault(); return navigate(nav.getAttribute("href")); }
  const tab = e.target.closest("[data-status]");
  if (tab) { S.status = tab.dataset.status; storage("status", S.status); return renderSidebar(); }
  const spaceTab = e.target.closest("[data-space]");
  if (spaceTab) {
    S.space = spaceTab.dataset.space; storage("space", S.space); renderSidebar();
    return location.pathname === "/map" && renderMap().catch(fail);
  }

  const a = e.target.closest("[data-action]");
  const d = S.d;
  if (!a) {
    // Clicking anywhere on an option (but not its links) toggles it.
    const opt = e.target.closest("[data-option]");
    if (opt && d?.status === "open" && !e.target.closest("a")) {
      if (e.target.matches(".pick")) S.focus = visibleIds().indexOf(opt.dataset.option);
      toggleSelect(opt.dataset.option);
    }
    return;
  }
  e.stopPropagation();
  const id = a.dataset.id;
  switch (a.dataset.action) {
    case "drawer": return drawer();
    case "close-pop": return closePops(true);
    case "palette": return palette();
    case "show-reason": S.showReason = d.id; renderDecision(); return $("#note")?.focus();
    case "retry": return route();
    case "reload": return location.reload();
    case "view": ui().view = a.dataset.view; saveUi(); return renderDecision();
    case "exclude": a.closest("details")?.removeAttribute("open"); return e.altKey ? excludeWithReason(id) : exclude([id]);
    case "include": return act(() => api("POST", `/api/decisions/${d.id}/include`, { option_ids: [id] }), "Restored");
    case "exclude-filtered": {
      const { metas, filtered } = partition();
      const desc = describeFilters(metas);
      const reason = await ask({ title: `Rule out ${filtered.length} hidden option${filtered.length === 1 ? "" : "s"}?`,
        body: `<p>${filtered.map((o) => esc(o.label)).join(", ")} will be recorded as ruled out in the log. You can restore them later.</p>`,
        field: "Reason", value: `filtered out: ${desc}`, ok: "Rule out" });
      if (reason === null) return;
      return exclude(filtered.map((o) => o.id), reason || undefined, desc);
    }
    case "add-reason": a.closest("details")?.removeAttribute("open"); return excludeWithReason(id);
    case "deselect-hidden": {
      const vis = new Set(visibleIds());
      for (const s of [...S.selection]) if (!vis.has(s)) S.selection.delete(s);
      return renderDecision();
    }
    case "clear-filter": delete ui().filters[a.dataset.key]; saveUi(); return renderDecision();
    case "clear-text": ui().text = ""; saveUi(); return renderDecision();
    case "clear-all-filters": ui().filters = {}; ui().text = ""; S.pop = null; saveUi(); return renderDecision();
    case "resolve": return resolve();
    case "toggle-silence": {
      S.silenced.has(id) ? S.silenced.delete(id) : S.silenced.add(id);
      return setHtml($("#listeners-panel"), webhooksHtml(d));
    }
    case "reopen": {
      // The server restarts a deadline that has already passed.
      const days = S.config?.deadline_days;
      const restarts = days && (!d.due_at || new Date(d.due_at) <= Date.now());
      const note = await ask({ title: `Reopen ${d.id}?`, body: `<p>It goes back to open${restarts ? ` with a new ${days}-day deadline` : ""}, and its listeners hear <code>decision.reopened</code>.</p>`,
        field: "Note (optional)", ok: "Reopen" });
      if (note === null) return;
      return act(() => api("POST", `/api/decisions/${d.id}/reopen`, { note }), "Reopened");
    }
    case "cancel-decision": {
      const note = await ask({ title: `Cancel ${d.id}?`, body: `<p>${esc(d.source || "The requester")} is told it was withdrawn without a choice. You can reopen it later.</p>`,
        field: "Note (optional)", ok: "Cancel decision", cancel: "Keep open", danger: true });
      if (note === null) return;
      return act(() => api("POST", `/api/decisions/${d.id}/cancel`, { note }), "Cancelled");
    }
    case "extend": {
      const from = Math.max(Date.now(), new Date(d.due_at || 0).getTime());
      const due = new Date(from + Number(a.dataset.days) * 86400000).toISOString();
      return act(() => api("PATCH", `/api/decisions/${d.id}`, { due_at: due }), `Deadline moved to ${whenTime(due)}`);
    }
    case "edit": S.editing = true; return renderDecision();
    case "cancel-edit": S.editing = false; return renderDecision();
    case "copy-install": copy(installCommand(S.config, S.config.auth && S.token), "install command"); return;
    case "roll-token": {
      const ok = await ask({ title: `Roll the token for ${a.dataset.name}?`, body: `<p>It gets a new secret and the old one stops working at once. Run the new install command (or <code>decide connect</code>) on that machine afterwards.</p>`, ok: "Roll token", danger: true });
      if (ok === null) return;
      try { showToken(await api("POST", `/api/tokens/${id}/roll`), "rolled"); renderSettings(); } catch (err) { fail(err); }
      return;
    }
    case "revoke-token": {
      const ok = await ask({ title: `Revoke the token for ${a.dataset.name}?`, body: `<p>That machine's <code>decide</code>, agents and resumer lose access at once. Its decisions stay.</p>`, ok: "Revoke", danger: true });
      if (ok === null) return;
      try { await api("DELETE", `/api/tokens/${id}`); toast(`Revoked ${a.dataset.name}`); renderSettings(); } catch (err) { fail(err); }
      return;
    }
    case "space-deadline": {
      const v = await ask({ title: `Default deadline in ${id}`, body: `<p>New decisions in this space expire this many days after they're created unless they set their own. 0 means no default deadline. Leave it empty to use the server's ${esc(S.config.deadline_days)} days.</p>`,
        field: "Days", type: "number", value: a.dataset.days, ok: "Save" });
      if (v === null) return;
      try { await api("PATCH", `/api/spaces/${id}`, { deadline_days: v === "" ? null : Number(v) }); await spacesChanged(); } catch (err) { fail(err); }
      return;
    }
    case "delete-space": {
      if (Number(a.dataset.decisions)) return toast(`${id} still has decisions, so it can't be deleted`, { error: true });
      const n = Number(a.dataset.tokens);
      const ok = await ask({ title: `Delete the ${id} space?`, body: `<p>${n ? `Its ${n} machine token${n === 1 ? " is" : "s are"} revoked. ` : ""}Its audit chain stays on record.</p>`, ok: "Delete space", danger: true });
      if (ok === null) return;
      try { await api("DELETE", `/api/spaces/${id}`); toast(`Deleted space ${id}`); await spacesChanged(); } catch (err) { fail(err); }
      return;
    }
    case "copy-gui-link": copy(`${location.origin}/#token=${encodeURIComponent(S.token)}`, "GUI link"); return;
    case "copy-resume": {
      await (d.resume_command ? copy(d.resume_command, "resume command") : copy(d.resume.session_id, "session ID"));
      if (d.resume_state && ["pending", "failed"].includes(d.resume_state.status))
        toast("Copied. Paste it in a terminal to resume.", { undo: () => markResumed(), undoLabel: "Mark resumed" });
      return;
    }
    case "mark-resumed": return markResumed();
    case "add-dep": return addDependency();
    case "add-goal": {
      const v = await pickGoal({ title: `What is ${d.id} a step toward?`, body: `<p>Linking decisions to goals puts them on the map, next to why they were made.</p>`,
        space: d.space, exclude: new Set(d.goals.map((g) => g.goal)), ok: "Link" });
      return v && setGoals(d, [...d.goals.map((g) => g.goal), v], `Linked to ${v}`);
    }
    case "remove-goal": return setGoals(d, d.goals.map((g) => g.goal).filter((g) => g !== id), `Unlinked from ${id}`);
    case "new-goal": return newGoal(a.dataset.serves);
    case "map-tag": {
      const tags = mapTags(), t = a.dataset.tag;
      return setMapTags(tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t]);
    }
    case "map-tags-clear": return setMapTags([]);
    case "list-tag": {
      const t = a.dataset.tag;
      return setListTags(S.listTags.includes(t) ? S.listTags.filter((x) => x !== t) : [...S.listTags, t]);
    }
    case "list-tags-clear": return setListTags([]);
    case "list-inherit": S.listInherit = !S.listInherit; storage("listInherit", S.listInherit ? null : "0"); return renderSidebar();
    case "edit-goal": S.editingGoal = true; return renderGoal();
    case "cancel-goal-edit": S.editingGoal = false; return renderGoal();
    case "add-tag": S.addingTag = true; return tagged(a.dataset.kind).render();
    case "remove-tag": {
      const t = tagged(a.dataset.kind);
      return t.save(t.tags.filter((x) => x !== a.dataset.tag), `Removed #${a.dataset.tag}`);
    }
    case "copy-goal-ref": return copy(`[${S.goal.id}: ${S.goal.title}](${S.goal.url})`, "link");
    case "goal-status": {
      const g = S.goal, to = a.dataset.status;
      const note = await ask({ title: { achieved: `Mark ${g.id} achieved?`, abandoned: `Abandon ${g.id}?`, active: `Make ${g.id} active again?` }[to],
        body: to === "abandoned" ? `<p>Open decisions toward it are flagged, and the decisions made for it count as dead ends on the map. Say why, so the next person learns from it.</p>` : "",
        field: to === "abandoned" ? "Why? (recorded in the log)" : "Note (optional)", ok: { achieved: "Mark achieved", abandoned: "Abandon goal", active: "Reactivate" }[to], danger: to === "abandoned" });
      if (note === null) return;
      return goalAct(() => api("PATCH", `/api/goals/${g.id}`, { status: to, note }), `${g.id} ${{ achieved: "achieved", abandoned: "abandoned", active: "is active again" }[to]}`);
    }
    case "add-serves": {
      const g = S.goal;
      const v = await pickGoal({ title: `What does ${g.id} serve?`, body: `<p>The larger goal this one is a step toward.</p>`, space: g.space,
        exclude: new Set([g.id, ...g.serves.map((s) => s.goal), ...goalDescendants(g.id)]), ok: "Link" });
      return v && goalAct(() => api("PATCH", `/api/goals/${g.id}`, { serves: [...g.serves.map((s) => s.goal), v] }), `Serves ${v}`);
    }
    case "remove-serves": return goalAct(() => api("PATCH", `/api/goals/${S.goal.id}`, { serves: S.goal.serves.map((s) => s.goal).filter((x) => x !== id) }), "Unlinked");
    case "link-decision": {
      const g = S.goal;
      const linked = new Set(g.decisions.map((x) => x.decision));
      const choices = S.list.filter((x) => x.space === g.space && !linked.has(x.id))
        .map((x) => ({ value: x.id, label: `${x.id} · ${x.title}${x.status === "open" ? "" : ` (${x.status})`}` }));
      if (!choices.length) return toast("Every decision in this space is already linked to it", { info: true });
      const v = await ask({ title: `Link a decision to ${g.id}`, body: `<p>Which decision is a step toward “${esc(g.title)}”? Past decisions count too: that's how the map shows what led where.</p>`, field: "Decision", choices, ok: "Link" });
      if (!v) return;
      const goals = S.list.find((x) => x.id === v)?.goals || [];
      return goalAct(() => api("PATCH", `/api/decisions/${v}`, { goals: [...goals, g.id] }), `Linked ${v}`);
    }
    case "unlink-decision": {
      const goals = (S.list.find((x) => x.id === id)?.goals || []).filter((x) => x !== S.goal.id);
      return goalAct(() => api("PATCH", `/api/decisions/${id}`, { goals }), `Unlinked ${id}`);
    }
    case "remove-dep": return setDependencies(d.depends_on.filter((x) => x.decision !== id), "Dependency removed");
    case "decide-anyway": {
      const ok = await ask({ title: `Decide ${d.id} now?`, body: `<p>This removes its dependency on ${esc(d.waiting_on.join(" and "))}, so ${esc(d.source || "the requester")} can't count on that being settled first. Its deadline starts now.</p>`, ok: "Remove dependency" });
      if (ok === null) return;
      return setDependencies(d.depends_on.filter((x) => x.state !== "waiting"), "Unlocked");
    }
    case "still-holds": {
      // Same choice and reason, confirmed against what changed. Nobody needs to act on it again.
      const silence = [...(d.resume ? ["resumer"] : []), ...(S.listeners || []).filter((l) => l.scope !== "global").map((l) => l.id)];
      return act(() => api("POST", `/api/decisions/${d.id}/resolve`, { selected: d.resolution.selected, note: d.resolution.note, silence }), "Confirmed");
    }
    case "copy-ref": return copy(`[${d.id}: ${d.title}](${d.url})`, "link");
    case "copy-md": try { return copy(await api("GET", `/api/decisions/${d.id}/markdown`), "summary"); } catch (err) { return fail(err); }
    case "copy-provenance": {
      const r = d.resolution;
      const picked = d.selected_options.map((o) => o.label).join(", ") || "nothing";
      return copy([`Decision: ${d.title}`, `Choice: ${picked}`, ...(r.note ? [`Reason: ${r.note.trim()}`] : []), `Link: ${d.url}`].join("\n"), "decision record");
    }
    case "cancel-attr": return act(() => api("DELETE", `/api/decisions/${d.id}/attribute-requests/${encodeURIComponent(a.dataset.key)}`), "Request withdrawn");
    case "redeliver": return act(async () => { await api("POST", `/api/deliveries/${id}/redeliver`); return null; }, "Re-firing").then(() => d && loadDecision(d.id, { keepSelection: true }));
    case "toggle-hook": try { await api("PATCH", `/api/webhooks/${id}`, { active: a.dataset.active !== "true" }); renderSettings(); } catch (err) { fail(err); } return;
    case "delete-hook": if (await ask({ title: "Delete this webhook?", body: `<p>It stops receiving events and its pending retries are cancelled. Past deliveries stay on record.</p>`, ok: "Delete", danger: true }) !== null) { try { await api("DELETE", `/api/webhooks/${id}`); renderSettings(); } catch (err) { fail(err); } } return;
  }
});

const setDependencies = (deps, okMsg) =>
  act(() => api("PATCH", `/api/decisions/${S.d.id}`, { depends_on: deps.map(({ decision, when, note }) => ({ decision, when, note })) }), okMsg);

async function addDependency() {
  const d = S.d;
  const v = await ask({ title: `What does ${d.id} depend on?`, body: `<p>Until that decision is made, this one is locked and out of your way, and it won't expire. Its deadline starts once it unlocks.</p>`,
    field: "Decision", placeholder: "D-12", ok: "Add dependency" });
  if (!v) return;
  return setDependencies([...d.depends_on, { decision: v }], "Dependency added");
}

function markResumed() {
  const d = S.d;
  return act(() => api("POST", `/api/decisions/${d.id}/resume-events`, { status: "manual", trigger_seq: d.resume_state?.trigger_seq }), "Marked as resumed");
}

// The note and listener choices survive a failed attempt; the button is disabled while it's in flight.
async function resolve() {
  const d = S.d;
  if (S.resolving) return;
  const note = S.notes[d.id] || "";
  const { metas } = partition();
  const filters = describeFilters(metas) || undefined;
  const silence = [...S.silenced];
  const told = (S.listeners || []).length + (d.resume ? 1 : 0) - silence.length;
  S.resolving = true;
  renderDecision();
  S.resolveError = null;
  let ok = false;
  try {
    await api("POST", `/api/decisions/${d.id}/resolve`, { selected: [...S.selection], note, filters, silence });
    ok = true;
    S.resolving = false;
    await loadDecision(d.id, { keepSelection: true });
    toast(`${d.id} decided${told > 0 ? ` · notifying ${told}` : ""}${silence.length ? ` · ${silence.length} not notified` : ""}`);
    loadList();
  } catch (e) { S.resolveError = { id: d.id, message: e.message || String(e) }; fail(e); }
  S.resolving = false;
  if (ok) { delete S.notes[d.id]; S.silenced = new Set(); }
  if (S.d?.id === d.id) renderDecision();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("drawer-open")) return drawer(false);
  if (e.key === "Escape" && S.pop) return closePops(true);
  const menu = document.querySelector("details.omenu[open]");
  if (e.key === "Escape" && menu) { menu.removeAttribute("open"); return menu.querySelector("summary").focus(); }
  if (S.pop || menu) return;
  if (!S.d || S.editing || S.d.status !== "open" || dialogOpen()) return;
  const el = document.activeElement;
  // Enter and space belong to a focused button, link or disclosure.
  if (["BUTTON", "A", "SUMMARY"].includes(el?.tagName) && (e.key === "Enter" || e.key === " ")) return;
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    const btn = $('[data-action="resolve"]');
    if (btn && !btn.disabled) { e.preventDefault(); resolve(); }
    return;
  }
  if (isTyping()) { if (e.key === "Escape") el.blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  // Option shortcuts apply when nothing in particular has focus, or an option does; other controls keep their keys.
  const inOptions = !el || el === document.body || el.id === "main" || !!el.closest?.("[data-option]");
  if (!inOptions && e.key !== "/") return;
  const ids = visibleIds();
  // A focused option control is the cursor.
  const at = el?.dataset?.pick ? ids.indexOf(el.dataset.pick) : S.focus;
  const moveTo = (i) => { S.focus = i; renderDecision(); const p = $(`[data-pick="${CSS.escape(ids[i])}"]`); p?.focus(); p?.closest("[data-option]")?.scrollIntoView({ block: "nearest" }); };
  if (e.key === "/") { e.preventDefault(); $("#opt-text")?.focus(); }
  else if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); if (ids.length) moveTo(Math.min(ids.length - 1, at + 1)); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); if (ids.length) moveTo(Math.max(0, at - 1)); }
  else if (e.key === " " && el?.dataset?.pick) return; // the checkbox toggles itself
  else if ((e.key === " " || e.key === "Enter") && ids[at]) { e.preventDefault(); S.focus = at; toggleSelect(ids[at]); $(`[data-pick="${CSS.escape(ids[at])}"]`)?.focus(); }
  else if (e.key === "x" && ids[at]) { e.preventDefault(); S.focus = Math.min(at, ids.length - 2); exclude([ids[at]]); }
});

setInterval(async () => {
  if (!S.d || document.hidden) return;
  try {
    [S.listeners, S.resumer] = await Promise.all([
      api("GET", `/api/decisions/${S.d.id}/listeners`).then((r) => r.listeners),
      api("GET", resumerPath(S.d)),
    ]);
    if (S.d.status === "open") setHtml($("#listeners-panel"), webhooksHtml(S.d));
    setHtml($("#resume-card"), resumeHtml(S.d));
  } catch {}
}, 5000);

// Countdowns tick without waiting for an event.
setInterval(() => {
  if (document.hidden) return;
  renderSidebar();
  if (S.d) setHtml($("#deadline-card"), deadlineHtml(S.d));
}, 30000);

$("#search").addEventListener("input", (e) => { S.q = e.target.value; renderSidebar(); });
window.addEventListener("popstate", route);

// ------------------------------------------------------------------ command palette (⌘K)

function palette() {
  if (dialogOpen()) return;
  const dlg = document.createElement("dialog");
  dlg.className = "dialog palette";
  dlg.setAttribute("aria-label", "Jump to");
  dlg.innerHTML = `<div class="pal-search">${icon("search", 16)}<input type="text" placeholder="Jump to a decision or command…" aria-label="Search decisions and commands" role="combobox" aria-expanded="true" aria-controls="pal-list" autocomplete="off"><span class="kbd">esc</span></div>
    <ul class="pal-list" id="pal-list" role="listbox"></ul>
    <div class="pal-foot"><span><span class="kbd">↑</span><span class="kbd">↓</span> move</span><span><span class="kbd">↵</span> open</span><span><span class="kbd">esc</span> close</span><span class="pal-more"></span></div>`;
  document.body.append(dlg);
  const input = dlg.querySelector("input"), list = dlg.querySelector("ul");
  const back = document.activeElement;
  let navigated = false;
  const commands = [
    ...Object.entries(STATUS_NAMES).map(([k, v]) => ({ kind: "cmd", icon: "filter", label: `Show ${v.toLowerCase()} decisions`, run: () => { S.status = k; storage("status", k); renderSidebar(); drawer(true); } })),
    { kind: "cmd", icon: "map", label: "Map of goals and decisions", run: () => navigate("/map") },
    { kind: "cmd", icon: "target", label: "New goal…", run: () => newGoal() },
    { kind: "cmd", icon: "settings", label: "Settings", run: () => navigate("/settings") },
    ...(S.d ? [{ kind: "cmd", icon: "copy", label: `Copy link to ${S.d.id}`, run: () => copy(`[${S.d.id}: ${S.d.title}](${S.d.url})`, "link") }] : []),
    ...(S.d?.status === "open" ? [{ kind: "cmd", icon: "link", label: `Make ${S.d.id} depend on another decision…`, run: addDependency }] : []),
  ];
  const decisions = [...S.list.filter((d) => d.status === "open").sort(byDeadline), ...S.list.filter((d) => d.status !== "open")]
    .map((d) => ({ kind: "decision", d, label: d.title, run: () => navigate(`/d/${d.id}`) }));
  const goals = [...S.goals.filter((g) => g.status === "active"), ...S.goals.filter((g) => g.status !== "active")]
    .map((g) => ({ kind: "goal", g, label: g.title, run: () => navigate(`/g/${g.id}`) }));
  const GROUPS = { decision: "Decisions", goal: "Goals", cmd: "Commands" };
  let items = [], at = 0;
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    const hit = (t) => q.split(/\s+/).every((w) => t.toLowerCase().includes(w));
    const ds = decisions.filter((x) => !q || hit(`${x.d.id} ${x.d.title} ${x.d.source || ""} ${x.d.status}`));
    const gs = goals.filter((x) => !q || hit(`${x.g.id} ${x.g.title} ${x.g.status}`));
    const cs = commands.filter((c) => !q || hit(c.label));
    items = [...ds.slice(0, 12), ...gs.slice(0, 6), ...cs];
    at = Math.min(at, Math.max(0, items.length - 1));
    dlg.querySelector(".pal-more").textContent = ds.length > 12 ? `Showing 12 of ${ds.length} decisions` : "";
    const group = (i) => i === 0 || items[i - 1].kind !== items[i].kind ? `<li class="pal-group" role="presentation">${GROUPS[items[i].kind]}</li>` : "";
    list.innerHTML = items.length ? items.map((x, i) => `${group(i)}<li role="option" id="pal-${i}" aria-selected="${i === at}" class="${i === at ? "on" : ""}" data-i="${i}">
      ${x.kind === "decision" ? `${statusIcon(x.d)}<span class="pal-label">${esc(x.d.title)}</span><span class="pal-meta">${esc(x.d.id)}${isWaiting(x.d) ? ` · after ${esc(x.d.waiting_on[0])}` : x.d.status === "open" && x.d.due_at ? ` · ${until(x.d.due_at)}` : ` · ${esc(x.d.status)}`}</span>`
        : x.kind === "goal" ? `${goalIcon(x.g, 15)}<span class="pal-label">${esc(x.g.title)}</span><span class="pal-meta">${esc(x.g.id)}${x.g.status === "active" ? "" : ` · ${esc(x.g.status)}`}</span>`
        : `${icon(x.icon, 15)}<span class="pal-label">${esc(x.label)}</span>`}</li>`).join("")
      : `<li class="pal-empty">No matches</li>`;
    input.setAttribute("aria-activedescendant", items.length ? `pal-${at}` : "");
    list.querySelector(".on")?.scrollIntoView({ block: "nearest" });
  };
  const go = (i) => { const x = items[i]; if (!x) return; navigated = true; dlg.close(); x.run(); };
  input.addEventListener("input", () => { at = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); at = Math.min(items.length - 1, at + 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); at = Math.max(0, at - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); go(at); }
  });
  list.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) go(+li.dataset.i); });
  list.addEventListener("mousemove", (e) => { const li = e.target.closest("[data-i]"); if (li && +li.dataset.i !== at) { at = +li.dataset.i; draw(); } });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => { dlg.remove(); if (!navigated) back?.focus?.(); });
  draw();
  dlg.showModal();
  input.focus();
}
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); palette(); }
}, true);

// ------------------------------------------------------------------ boot

(async function boot() {
  $("#drawer-btn").innerHTML = icon("menu", 20);
  $("#conn").innerHTML = `${icon("wifi", 15)}Reconnecting… live updates paused<button class="btn small ghost" data-action="reload">Reload</button>`;
  $("#decision-list").innerHTML = Array.from({ length: 5 }, () => `<div class="skel skel-item"></div>`).join("");
  renderSkeleton();
  $("#mb-settings").innerHTML = icon("settings", 18);
  $("#settings-btn").innerHTML = icon("settings", 16);
  $("#map-btn").innerHTML = icon("map", 16);
  $("#mb-map").innerHTML = icon("map", 18);
  $("#logo-mark").innerHTML = icon("check", 14);
  $("#search-wrap").insertAdjacentHTML("afterbegin", icon("search", 15));
  $("#search-wrap").insertAdjacentHTML("beforeend", `<button class="kbd pal-hint" data-action="palette" title="Jump to anything (⌘K)" aria-label="Open command palette">⌘K</button>`);
  syncInert();
  // Live updates that arrived while typing are applied once you leave the field.
  document.addEventListener("focusout", () => setTimeout(() => {
    if (S.stale && S.d && !isTyping()) { S.stale = false; loadDecision(S.d.id, { keepSelection: true }); }
  }, 0));
  try {
    S.config = await api("GET", "/api/config");
    await loadList();
    await route();
    poll();
  } catch (e) {
    renderError(e, "reload");
  }
})();
