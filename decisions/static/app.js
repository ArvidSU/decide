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
  ui: {},             // per-decision UI state: filters, view, sort, keepUnknown, text
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

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json", "X-Actor": S.config?.owner || "me" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401) {
    const t = prompt("API token for this decisions server:");
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

// Time left until an ISO timestamp, coarse: "3d 4h", "5h 12m", "12m" (negative = passed).
function until(iso) {
  const s = Math.max(0, (new Date(iso).getTime() - Date.now()) / 1000);
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.max(1, Math.floor(s / 60))}m`;
}
// How pressing an open decision's deadline is: "urgent" under 6h, "soon" under a day.
function urgency(d) {
  if (d.status !== "open" || !d.due_at) return "";
  const s = (new Date(d.due_at).getTime() - Date.now()) / 1000;
  return s < 6 * 3600 ? "urgent" : s < 86400 ? "soon" : "";
}
const closed = (d) => d.status === "cancelled" || d.status === "expired";
const byDeadline = (a, b) => (a.due_at || "9999").localeCompare(b.due_at || "9999");

let toastTimer;
function toast(msg, { undo, error, undoLabel } = {}) {
  const el = $("#toast");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span>${esc(msg)}</span>` + (undo ? `<button>${esc(undoLabel || "Undo")}</button>` : "");
  el.hidden = false;
  if (undo) el.querySelector("button").onclick = () => { el.hidden = true; undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), undo ? 6000 : 2600);
}
const fail = (e) => toast(e.message || String(e), { error: true });

async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${what}`); }
  catch { prompt("Copy:", text); }
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
    label: def.label || key,
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
  return `<span class="${meta.estimate ? "estimate" : ""}" title="${esc(tip)}">${meta.estimate ? "≈ " : ""}${html}${tip && !meta.estimate ? " <sup>ⓘ</sup>" : ""}</span>`;
}

// Returns HTML; every branch escapes its own output.
function valueHtml(v, m) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "✓ yes" : "✗ no";
  if (Array.isArray(v)) return esc(v.map(String).join(", "));
  if (typeof v === "object") return esc(JSON.stringify(v));
  const unit = m.unit ? " " + esc(m.unit) : "";
  if (typeof v === "number") return v.toLocaleString(undefined, { maximumFractionDigits: 4 }) + unit;
  if (/^https?:\/\//.test(v)) return `<a href="${esc(v)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(v.replace(/^https?:\/\/(www\.)?/, "").slice(0, 40))}</a>`;
  return esc(v) + unit;
}

// ------------------------------------------------------------------ data loading

async function loadList() {
  S.list = (await api("GET", "/api/decisions?status=all&limit=500")).decisions;
  renderSidebar();
}

// Spaces only show up in the GUI once there's more than one to tell apart.
const multiSpace = () => (S.config?.spaces || []).length > 1;
const inSpace = (d) => !S.space || d.space === S.space;
const spaceChip = (d) => multiSpace() ? `<span class="chip space" title="Space">${esc(d.space)}</span>` : "";

async function loadDecision(id, { keepSelection = false } = {}) {
  const [d, ev, dl, ls] = await Promise.all([
    api("GET", `/api/decisions/${id}`),
    api("GET", `/api/decisions/${id}/events`),
    api("GET", `/api/deliveries?decision=${id}`),
    api("GET", `/api/decisions/${id}/listeners`),
  ]);
  S.listeners = ls.listeners;
  try { S.resumer = await api("GET", resumerPath(d)); } catch {}
  const switched = !S.d || S.d.id !== d.id;
  S.d = d;
  S.events = ev.events;
  S.deliveries = dl.deliveries;
  if (switched || !keepSelection) {
    S.selection = new Set(d.resolution ? d.resolution.selected : []);
    if (switched) { S.focus = -1; S.editing = false; S.silenced = new Set(); }
  }
  for (const s of [...S.selection]) if (!d.options.some((o) => o.id === s)) S.selection.delete(s);
  renderDecision();
  renderSidebar();
}

let pollAbort = 0;
async function poll() {
  const me = ++pollAbort;
  S.cursor = S.config.cursor;
  while (me === pollAbort) {
    try {
      const r = await api("GET", `/api/events?after=${S.cursor}&wait=25&limit=500`);
      if (r.events.length) {
        S.cursor = r.cursor;
        await loadList();
        if (S.d && r.events.some((e) => e.decision_id === S.d.id)) {
          const busy = document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
          if (!busy) await loadDecision(S.d.id, { keepSelection: true });
        }
      }
    } catch { await new Promise((r) => setTimeout(r, 3000)); }
  }
}

// ------------------------------------------------------------------ routing

function navigate(path, replace) {
  if (replace) history.replaceState({}, "", path); else history.pushState({}, "", path);
  route();
}

async function route() {
  const path = location.pathname;
  const m = path.match(/^\/d\/([^/]+)/);
  try {
    if (m) return await loadDecision(decodeURIComponent(m[1]));
    S.d = null;
    renderSidebar();
    if (path === "/settings") return await renderSettings();
    const first = S.list.filter((d) => d.status === "open" && inSpace(d)).sort(byDeadline)[0];
    if (first) return navigate(`/d/${first.id}`, true);
    renderEmpty();
  } catch (e) {
    $("#main").innerHTML = `<div class="page"><div class="empty">${esc(e.message)}</div></div>`;
  }
}

// ------------------------------------------------------------------ sidebar

function renderSidebar() {
  const spaces = (S.config?.spaces || []).map((s) => s.name);
  if (S.space && !spaces.includes(S.space)) S.space = "";
  const spaceTabs = $("#space-tabs");
  spaceTabs.hidden = !multiSpace();
  if (multiSpace()) {
    const openIn = (s) => S.list.filter((d) => d.status === "open" && (!s || d.space === s)).length;
    spaceTabs.innerHTML = ["", ...spaces].map((s) => { const n = openIn(s);
      return `<button class="tab ${S.space === s ? "active" : ""} ${n ? "has-open" : ""}" data-space="${esc(s)}">${s ? esc(s) : "All"}<span class="n">${n || ""}</span></button>`; }).join("");
  }
  const list = S.list.filter(inSpace);
  S.counts = { open: 0, resolved: 0, expired: 0, cancelled: 0, all: list.length };
  for (const d of list) S.counts[d.status]++;
  const tabs = ["open", "resolved", "expired", "cancelled", "all"];
  const names = { open: "Open", resolved: "Resolved", expired: "Expired", cancelled: "Cancelled", all: "All" };
  $("#status-tabs").innerHTML = tabs.map((t) =>
    `<button class="tab ${S.status === t ? "active" : ""} ${t === "open" && S.counts.open ? "has-open" : ""}" data-status="${t}">${names[t]}<span class="n">${S.counts[t] ?? ""}</span></button>`).join("");
  const open = list.filter((d) => d.status === "open").sort(byDeadline);
  document.title = (open.length ? `(${open.length}) ` : "") + (S.d ? `${S.d.id} · ${S.d.title}` : "Decisions");
  $("#attention").innerHTML = open.length
    ? `<a href="/d/${open[0].id}" data-nav class="attention ${urgency(open[0])}" title="Undecided decisions expire at their deadline, and the requester is told it wasn't important">
        <strong>${open.length} decision${open.length > 1 ? "s" : ""} waiting on you</strong>
        <span>${open[0].due_at ? `next expires in ${until(open[0].due_at)}: ${esc(open[0].id)}` : `oldest: ${esc(open[0].id)}`}</span></a>`
    : `<div class="attention clear"><strong>Nothing to decide</strong></div>`;
  const q = S.q.toLowerCase();
  let items = list.filter((d) => (S.status === "all" || d.status === S.status) &&
    (!q || d.title.toLowerCase().includes(q) || d.id.toLowerCase().includes(q) || (d.source || "").toLowerCase().includes(q)));
  // Most urgent first: open decisions by deadline, ahead of everything else.
  items = [...items.filter((d) => d.status === "open").sort(byDeadline), ...items.filter((d) => d.status !== "open")];
  $("#decision-list").innerHTML = items.length ? items.map((d) => {
    const pending = Object.keys(d.pending_attribute_requests || {}).length;
    const due = d.status === "open" && d.due_at;
    const left = due ? Math.max(0, Math.min(1, (new Date(d.due_at) - Date.now()) / (new Date(d.due_at) - new Date(d.created_at)))) : 0;
    return `<a href="/d/${d.id}" data-nav class="dl-item ${S.d && S.d.id === d.id ? "active" : ""} ${d.status} ${urgency(d)}">
      <div class="dl-top"><span class="dl-id">${d.id}</span><span class="dl-title">${esc(d.title)}</span>
        ${due ? `<span class="dl-due" title="Expires ${esc(fullTime(d.due_at))} unless decided">${until(d.due_at)}</span>` : ""}
        ${d.status === "expired" ? `<span class="dl-due" title="Expired ${esc(fullTime(d.due_at))}: nobody decided in time">expired</span>` : ""}</div>
      <div class="dl-meta">
        ${multiSpace() && !S.space ? `<span class="dl-space">${esc(d.space)}</span>` : ""}
        ${d.source ? `<span>${esc(d.source)}</span>` : ""}
        <span>${d.option_count - d.excluded_count}/${d.option_count} options</span>
        ${pending ? `<span title="Waiting for requested attributes"><span class="dot"></span>attrs</span>` : ""}
        ${["pending", "failed"].includes(d.resume_state?.status) && d.status === "resolved" ? `<span title="The requesting agent hasn't been resumed" style="color:var(--bad)"><span class="dot" style="background:var(--bad)"></span>not resumed</span>` : ""}
        <span>${ago(d.created_at)}</span>
      </div>
      ${due ? `<div class="dl-time" title="Time left before it expires"><i style="width:${(left * 100).toFixed(1)}%"></i></div>` : ""}</a>`;
  }).join("") : `<div class="muted small" style="padding:12px">No ${S.status === "all" ? "" : esc(names[S.status].toLowerCase())} decisions.</div>`;
}

// ------------------------------------------------------------------ empty / settings

function renderEmpty() {
  const base = S.config?.base_url || location.origin;
  $("#main").innerHTML = `<div class="page"><div class="empty">
    <h2 style="color:var(--text);margin-bottom:8px">Nothing to decide</h2>
    <p>Agents and apps create decisions through the API. Try:</p>
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

async function renderSettings() {
  const [hooks, verify, cfg, rsAll] = await Promise.all([api("GET", "/api/webhooks"), api("GET", "/api/events/verify"),
    api("GET", "/api/config"), api("GET", "/api/resumer")]);
  const global = hooks.webhooks.filter((w) => !w.decision_id);
  const w = cfg.webhook;
  $("#main").innerHTML = `<div class="page">
    <h1 class="d-title">Settings</h1>
    <section class="block">
      <div class="block-head"><h2>Connect a machine</h2></div>
      <div class="panel small">
        <p class="hint" style="margin-top:0">Run this on any machine whose agents should use this server. It installs the <code>decide</code> CLI and the agent skill (for Claude Code, Codex and any agent that reads <code>~/.agents/skills</code>), connects them here, and starts the resumer there so sessions on that machine pick back up when you decide.</p>
        <pre class="install-cmd">${esc(installCommand(cfg))}</pre>
        <div class="form-row"><button class="btn small" data-action="copy-install">Copy command</button>
          ${cfg.auth ? `<button class="btn small ghost" data-action="copy-gui-link">Copy GUI link with token</button>` : ""}
          <span class="spacer"></span>
          <span class="muted">${(rsAll.resumers || []).map((r) => `<span class="live ${r.online ? "on" : "off"}"></span> ${esc(r.machine)}`).join(" &nbsp; ") || "no resumers seen since the server started"}</span></div>
        ${cfg.auth ? `<p class="hint">The command contains the API token: share it only with machines you trust.</p>` : `<p class="hint">Auth is off. To accept other machines, run the server with <code>host = "0.0.0.0"</code> and a <code>token</code> (<code>decide server install --host 0.0.0.0</code> sets both).</p>`}
      </div>
    </section>
    <section class="block">
      <div class="block-head"><h2>Global webhooks</h2></div>
      <div class="panel">
        <p class="hint" style="margin-top:0">Receive events for <em>every</em> decision${S.space ? ` in <strong>${esc(S.space)}</strong> (the space selected in the sidebar)` : ""}, e.g. to notify Slack or sync to another app. Per-decision webhooks are set by the creating agent.</p>
        ${webhookTable(global)}
        <form class="form-row" id="add-global-hook" style="margin-top:10px">
          <input class="grow" name="url" placeholder="https://example.com/hooks/decisions" required>
          <input name="events" placeholder="events (default: ${esc(cfg.default_webhook_events.join(","))})" style="width:320px">
          <label class="small muted" style="display:flex;align-items:center;gap:4px"><input type="checkbox" name="ack"> re-fire until acked</label>
          <button class="btn primary">Add</button>
        </form>
      </div>
    </section>
    <section class="block">
      <div class="block-head"><h2>Delivery policy</h2></div>
      <div class="panel small">
        Retries with exponential backoff (${w.backoff_base}s × 2ⁿ, max ${w.backoff_max}s), up to ${w.max_attempts || "∞"} attempts, ${w.timeout}s timeout.
        ${w.require_ack ? "Deliveries re-fire" : "Webhooks created with <code>require_ack</code> re-fire"} every ${w.refire_interval}s until acknowledged via <code>ack_url</code>.
        <p class="hint">Change these in <code>decisions.toml</code> or <code>DECISIONS_WEBHOOK_*</code> env vars.</p>
      </div>
    </section>
    <section class="block">
      <div class="block-head"><h2>Audit log</h2></div>
      <div class="panel small">
        ${Object.entries(verify.chains).map(([space, c]) => `<div>${multiSpace() || space === "*" ? `<strong>${space === "*" ? "server" : esc(space)}</strong> · ` : ""}${c.ok
          ? `<span class="badge ok">intact</span> ${c.events} events in an unbroken SHA-256 hash chain. Head <code>${esc(c.head.slice(0, 16))}…</code>`
          : `<span class="badge failed">broken</span> chain fails verification at event #${c.broken_at}.`}</div>`).join("") || `<span class="muted">No events yet.</span>`}
        <p class="hint">Events are append-only: the database rejects updates and deletes on the log. Each space has its own chain.</p>
      </div>
    </section>
  </div>`;
  $("#add-global-hook").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { url: f.get("url") };
    if (f.get("events")) body.events = f.get("events").split(",").map((s) => s.trim()).filter(Boolean);
    if (f.get("ack")) body.require_ack = true;
    if (S.space) body.space = S.space;  // otherwise it hears every space this token sees
    try {
      const sub = await api("POST", "/api/webhooks", body);
      prompt("Webhook created. Signing secret (shown once):", sub.secret);
      renderSettings();
    } catch (err) { fail(err); }
  };
}

const installCommand = (cfg) => `curl -fsSL ${cfg.install_url} | sh` + (cfg.auth && S.token ? ` -s -- --token ${S.token}` : "");

function webhookTable(hooks) {
  if (!hooks.length) return `<div class="muted small">None.</div>`;
  const spaces = multiSpace() && hooks.some((h) => !h.decision_id);
  return `<table class="plain"><tr><th>URL</th>${spaces ? "<th>Space</th>" : ""}<th>Events</th><th>Ack</th><th></th></tr>${hooks.map((h) => `
    <tr><td class="mono">${esc(h.url)}${h.active ? "" : ` <span class="badge">paused</span>`}</td>
    ${spaces ? `<td class="small">${h.space ? esc(h.space) : "all"}</td>` : ""}
    <td class="small">${esc(h.events.join(", "))}</td>
    <td>${h.require_ack === null ? "default" : h.require_ack ? "required" : "no"}</td>
    <td style="text-align:right;white-space:nowrap">
      <button class="btn small ghost" data-action="toggle-hook" data-id="${h.id}" data-active="${h.active}">${h.active ? "Pause" : "Resume"}</button>
      <button class="btn small ghost danger" data-action="delete-hook" data-id="${h.id}">Delete</button></td></tr>`).join("")}</table>`;
}

// ------------------------------------------------------------------ decision page

function renderDecision() {
  const d = S.d;
  if (d.status === "resolved" && d.resolution) return renderResolved(d);
  const u = ui();
  const parts = partition();
  const { metas, visible, filtered, excluded } = parts;
  const view = u.view || (d.options.some((o) => o.image) ? "cards" : "table");
  const single = d.max_select === 1;

  const main = $("#main");
  const scroll = main.scrollTop;
  main.innerHTML = `<div class="page">
    ${headerHtml(d)}
    ${bannerHtml(d)}
    <div id="deadline-card">${deadlineHtml(d)}</div>
    <div id="resume-card">${resumeHtml(d)}</div>
    <section class="block">
      <div class="block-head">
        <h2>Options</h2><span class="muted small">${visible.length} of ${d.options.length} in play</span>
        <span class="spacer"></span>
        <span class="muted small">${single ? "Pick one" : `Pick ${d.min_select}${d.max_select === null ? "+" : d.max_select === d.min_select ? "" : "–" + d.max_select}`}</span>
        <div class="seg"><button data-action="view" data-view="table" class="${view === "table" ? "on" : ""}">Table</button><button data-action="view" data-view="cards" class="${view === "cards" ? "on" : ""}">Cards</button></div>
      </div>
      ${d.options.length ? filtersHtml(metas, filtered) : `<div class="panel muted">No options yet. The agent can add them with <code>POST /api/decisions/${d.id}/options</code>.</div>`}
      ${view === "table" ? tableHtml(parts, single) : cardsHtml(parts, single)}
      ${filtered.length ? `<details class="subtle-list" ${u.openFiltered ? "open" : ""} data-key="openFiltered"><summary>Hidden by filters (${filtered.length})</summary>
        <div class="mini-opts">${filtered.map((o) => miniHtml(o, "filtered")).join("")}</div></details>` : ""}
      ${excluded.length ? `<details class="subtle-list" ${u.openExcluded ? "open" : ""} data-key="openExcluded"><summary>Excluded (${excluded.length})</summary>
        <div class="mini-opts">${excluded.map((o) => miniHtml(o, "excluded")).join("")}</div></details>` : ""}
      ${d.options.length && !closed(d) ? decideBarHtml(d, single) : ""}
    </section>
    <div class="cols">
      <section class="block">${attrRequestHtml(d, metas)}</section>
      <section class="block" id="listeners-panel">${webhooksHtml(d)}</section>
    </div>
    <section class="block">${activityHtml(d)}</section>
  </div>`;
  main.scrollTop = scroll;
  bindDecision();
}

// Resolved decisions lead with a summary (what, which option, why), followed by the full
// record read-only: context, every option with its attributes, and the activity log.
function renderResolved(d) {
  const r = d.resolution;
  const picked = d.selected_options.map((o) => `<strong>${esc(o.label)}</strong>`).join(", ") || "<em>nothing</em>";
  const links = d.links.map((l) => `<a class="chip" href="${esc(l.url)}" target="_blank" rel="noopener">↗ ${esc(l.title || l.url.replace(/^https?:\/\//, "").slice(0, 50))}</a>`).join("");
  const meta = Object.entries(d.metadata).filter(([, v]) => typeof v !== "object").slice(0, 8)
    .map(([k, v]) => `<span class="chip" title="metadata">${esc(k)}: ${esc(String(v).slice(0, 40))}</span>`).join("");
  const main = $("#main");
  const scroll = main.scrollTop;
  main.innerHTML = `<div class="page"><div class="resolved-view">
    <div class="d-sub"><span class="chip id" data-action="copy-ref" title="Copy a markdown reference for Slack / Linear">${d.id} ⧉</span><span class="chip status resolved">resolved</span>
      <span title="${esc(fullTime(r.resolved_at))}">${esc(r.resolved_by)} · ${ago(r.resolved_at)}</span>
      ${spaceChip(d)}
      ${d.source ? `<span>from <strong>${esc(d.source)}</strong></span>` : ""}
      <span title="${esc(fullTime(d.created_at))}">created ${ago(d.created_at)}</span>
      ${d.tags.map((t) => `<span class="chip">#${esc(t)}</span>`).join("")}</div>
    <h1 class="d-title">${esc(d.title)}</h1>
    <div class="banner"><div>Picked ${picked}</div></div>
    <div class="rationale">${r.note ? md(r.note) : `<span class="muted">No rationale given.</span>`}</div>
    <div id="resume-card">${resumeHtml(d)}</div>
    <div class="form-row">
      <button class="btn primary" data-action="copy-provenance">Copy for provenance</button>
      <button class="btn" data-action="copy-md" title="Copy a markdown summary with the options table">Copy summary</button>
      <button class="btn" data-action="reopen">Reopen</button>
    </div>
    ${d.context || links || meta ? `<section class="block"><div class="block-head"><h2>Context</h2></div>
      ${d.context ? `<div class="context">${md(d.context)}</div>` : ""}
      ${links || meta ? `<div class="links">${links}${meta}</div>` : ""}</section>` : ""}
    ${d.options.length ? `<section class="block">${resolvedOptionsHtml(d)}</section>` : ""}
    <section class="block">${activityHtml(d)}</section>
  </div></div>`;
  main.scrollTop = scroll;
  const commentForm = $("#comment-form");
  commentForm.onsubmit = (e) => {
    e.preventDefault();
    const t = new FormData(commentForm).get("text").trim();
    if (t) act(() => api("POST", `/api/decisions/${d.id}/comments`, { text: t }));
  };
}

// Read-only comparison: picked options first, then the rest, then ruled-out ones with their reason.
function resolvedOptionsHtml(d) {
  const metas = attrKeys(d).map((k) => attrMeta(d, k)).filter((m) => m.type !== "image");
  const chosen = new Set(d.resolution.selected);
  const rank = (o) => (chosen.has(o.id) ? 0 : o.excluded ? 2 : 1);
  const opts = [...d.options].sort((a, b) => rank(a) - rank(b));
  const excludedCount = d.options.filter((o) => o.excluded).length;
  return `<div class="block-head"><h2>Options</h2><span class="muted small">${d.options.length} considered${excludedCount ? `, ${excludedCount} ruled out` : ""}</span></div>
    <div class="table-wrap"><table class="opts readonly">
      <thead><tr><th></th><th>Option</th>${metas.map((m) => `<th title="${esc(m.def.description || "")}">${esc(m.label)}</th>`).join("")}</tr></thead>
      <tbody>${opts.map((o) => `
        <tr class="${chosen.has(o.id) ? "selected" : ""} ${o.excluded ? "excluded" : ""}">
          <td class="check">${chosen.has(o.id) ? `<span class="picked" title="Picked">✓</span>` : ""}</td>
          <td>${o.image ? `<img class="thumb" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
            <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.label)}</a>` : esc(o.label)}</div>
            ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}
            ${o.excluded ? `<div class="opt-desc">Ruled out${o.exclude_reason ? `: ${esc(o.exclude_reason)}` : ""}</div>` : ""}</td>
          ${metas.map((m) => {
            const v = o.attributes[m.key];
            return `<td class="${v == null ? "missing" : ""} ${typeof v === "number" ? "num" : ""}">${cellHtml(v, m, o.attribute_meta[m.key])}</td>`;
          }).join("")}
        </tr>`).join("")}</tbody></table></div>`;
}

function headerHtml(d) {
  const links = d.links.map((l) => `<a class="chip" href="${esc(l.url)}" target="_blank" rel="noopener">↗ ${esc(l.title || l.url.replace(/^https?:\/\//, "").slice(0, 50))}</a>`).join("");
  const meta = Object.entries(d.metadata).filter(([, v]) => typeof v !== "object").slice(0, 8)
    .map(([k, v]) => `<span class="chip" title="metadata">${esc(k)}: ${esc(String(v).slice(0, 40))}</span>`).join("");
  if (S.editing) {
    return `<form class="edit-form" id="edit-form">
      <input class="input" name="title" value="${esc(d.title)}" style="font-size:18px;font-weight:600">
      <textarea class="input" name="context" placeholder="Context (markdown)">${esc(d.context)}</textarea>
      <div class="form-row"><button class="btn primary">Save</button><button type="button" class="btn" data-action="cancel-edit">Cancel</button></div>
    </form>`;
  }
  return `<div class="d-head">
      <div>
        <h1 class="d-title">${esc(d.title)}</h1>
        <div class="d-sub">
          <span class="chip id" data-action="copy-ref" title="Copy a markdown reference for Slack / Linear">${d.id} ⧉</span>
          <span class="chip status ${d.status}">${d.status}</span>
          ${spaceChip(d)}
          ${d.source ? `<span>from <strong>${esc(d.source)}</strong></span>` : ""}
          <span title="${esc(fullTime(d.created_at))}">created ${ago(d.created_at)}</span>
          ${d.tags.map((t) => `<span class="chip">#${esc(t)}</span>`).join("")}
        </div>
      </div>
      <div class="d-actions">
        <button class="btn small" data-action="copy-md" title="Copy a markdown summary with the options table">Copy summary</button>
        <button class="btn small ghost" data-action="edit">Edit</button>
        ${d.status === "open" ? `<button class="btn small ghost danger" data-action="cancel-decision">Cancel</button>` : `<button class="btn small ghost" data-action="reopen">Reopen</button>`}
      </div>
    </div>
    ${d.context ? `<div class="context">${md(d.context)}</div>` : ""}
    ${links || meta ? `<div class="links">${links}${meta}</div>` : ""}`;
}

function bannerHtml(d) {
  if (d.status === "resolved" && d.resolution) {
    const labels = d.selected_options.map((o) => `<strong>${esc(o.label)}</strong>`).join(", ") || "<em>nothing</em>";
    return `<div class="banner"><div>Decided ${labels} <span class="muted small">· ${esc(d.resolution.resolved_by)} · ${ago(d.resolution.resolved_at)}</span>
      ${d.resolution.note ? `<div class="note">“${esc(d.resolution.note)}”</div>` : ""}</div>
      <span class="muted small">Change the selection below to revise.</span></div>`;
  }
  if (d.status === "cancelled") {
    const ev = [...S.events].reverse().find((e) => e.type === "decision.cancelled");
    return `<div class="banner cancelled"><div><strong style="color:var(--muted)">Cancelled${ev ? ` by ${esc(ev.actor)}` : ""}.</strong>
      ${ev?.data.note ? `“${esc(ev.data.note)}” ` : ""}Reopen to decide after all.</div></div>`;
  }
  if (d.status === "expired") {
    const days = S.config?.deadline_days;
    return `<div class="banner expired"><div><strong>Expired unanswered</strong>${d.due_at ? ` <span title="${esc(fullTime(d.due_at))}">${ago(d.due_at)}</span>` : ""}.
      Nobody decided before the deadline, so it was closed as not important enough to block on.
      <div class="note">Reopen to decide after all${days ? `; that starts a new ${days}-day deadline` : ""}.</div></div>
      <button class="btn small" data-action="reopen">Reopen</button></div>`;
  }
  return "";
}

// Open decisions expire at their deadline. Keep the countdown and its consequence in plain view.
function deadlineHtml(d) {
  if (d.status !== "open" || !d.due_at) return "";
  const who = esc(d.source || "the requester");
  return `<div class="deadline ${urgency(d)}">
    <div class="deadline-main"><span class="deadline-left">${until(d.due_at)}</span>
      <div>left to decide, until <strong title="${esc(fullTime(d.due_at))}">${esc(whenTime(d.due_at))}</strong>.
        <div class="small muted">If nobody decides by then, it expires and ${who} is told it wasn't important.</div></div></div>
    <div class="deadline-actions">
      <button class="btn small" data-action="extend" data-days="1" title="Move the deadline a day later">+1 day</button>
      <button class="btn small" data-action="extend" data-days="${S.config?.deadline_days || 4}">+${S.config?.deadline_days || 4} days</button></div>
  </div>`;
}

// Sessions are resumed by the resumer on the machine they ran on.
const resumerPath = (d) => "/api/resumer" + (d?.resume?.machine ? `?machine=${encodeURIComponent(d.resume.machine)}` : "");

// Is the requesting agent going to be picked back up? Warn loudly when it isn't.
function resumeHtml(d) {
  const r = d.resume, st = d.resume_state, rs = S.resumer || {};
  if (!r) return "";
  const who = `${esc(r.agent || "agent")} session <code title="${esc(r.session_id)}">${esc(r.session_id.slice(0, 8))}…</code>${r.cwd ? ` in <code>${esc(r.cwd.replace(/^\/(Users|home)\/[^/]+/, "~"))}</code>` : ""}${r.machine ? ` on <strong>${esc(r.machine)}</strong>` : ""}`;
  const where = r.machine ? ` on ${esc(r.machine)}` : "";
  const online = rs.online ? `<span class="live on"></span> resumer${where} online` : `<span class="live off"></span> resumer${where} <strong>offline</strong>${rs.last_seen ? ` (last seen ${ago(rs.last_seen)})` : ""}`;
  const copyBtn = `<button class="btn small" data-action="copy-resume">${d.resume_command ? "Copy resume command" : "Copy session ID"}</button>`;
  const stale = st && st.status === "pending" && (Date.now() - new Date(st.since).getTime() > 60000 || !rs.online);
  const expired = st?.trigger === "decision.expired";
  let cls = "", body;
  switch (st?.status) {
    case "armed":
      cls = rs.online ? "" : "warn";
      body = rs.online ? `When you decide (or it expires), ${who} resumes automatically.`
                       : `${who} is waiting on this, but the resumer is offline, so it <strong>won't resume automatically</strong>. Start it with <code>decide resumer run</code> (or <code>install</code>), or resume it yourself after deciding.`;
      break;
    case "pending":
      cls = stale ? "bad" : "";
      body = stale ? `<strong>Not resumed.</strong> ${who} hasn't been picked up${expired ? " to hear that this expired" : ""}${rs.online ? "" : " because the resumer is offline"}. Resume it yourself:`
                   : expired ? `Telling ${who} it expired…` : `Resuming ${who}…`;
      break;
    case "started": body = `Resumed ${who} ${ago(st.since)}${expired ? " to tell it this expired" : ""}; it's working now.`; break;
    case "finished": cls = "ok"; body = `Resumed ${who} ${ago(st.since)}${expired ? " and told it this expired" : ""}; it finished.`; break;
    case "failed":
      cls = "bad";
      body = `<strong>Resume failed</strong> ${ago(st.since)}${st.detail?.exit_code != null ? ` (exit ${st.detail.exit_code})` : ""}${st.detail?.error ? `: ${esc(st.detail.error)}` : ""}. Resume it yourself:`;
      break;
    case "manual": cls = "ok"; body = `Marked as resumed manually by ${esc(st.actor)} ${ago(st.since)}.`; break;
    default: body = who;
  }
  const detail = st?.detail?.output_tail ? `<details><summary class="small muted">Output</summary><pre class="small">${esc(st.detail.output_tail)}</pre></details>` : "";
  const showMark = st && ["pending", "failed"].includes(st.status);
  return `<div class="resume ${cls}">
    <div class="resume-main"><div>${body}</div><div class="small muted">${online}</div>${detail}</div>
    <div class="resume-actions">${copyBtn}${showMark ? `<button class="btn small ghost" data-action="mark-resumed">Mark resumed</button>` : ""}</div>
  </div>`;
}

function filtersHtml(metas, filtered) {
  const u = ui();
  const fs = metas.filter((m) => m.type !== "url" && m.type !== "image" && (m.distinct.length || m.requested)).map((m) => {
    const f = u.filters[m.key] || {};
    const on = filterActive(f);
    const head = `<label><span>${esc(m.label)}${m.unit ? ` <span style="font-weight:400">(${esc(m.unit)})</span>` : ""}</span>${on ? `<span class="clear" data-action="clear-filter" data-key="${esc(m.key)}">clear</span>` : ""}</label>`;
    let body;
    if (m.type === "number") {
      body = `<div class="range"><input type="number" step="any" data-filter="${esc(m.key)}" data-part="min" placeholder="${m.min ?? "min"}" value="${esc(f.min ?? "")}">–<input type="number" step="any" data-filter="${esc(m.key)}" data-part="max" placeholder="${m.max ?? "max"}" value="${esc(f.max ?? "")}"></div>`;
    } else if (m.type === "boolean") {
      body = `<select data-filter="${esc(m.key)}" data-part="bool">${["any", "yes", "no"].map((v) => `<option ${f.bool === v ? "selected" : ""}>${v}</option>`).join("")}</select>`;
    } else if ((m.type === "enum" || m.type === "tags") && m.distinct.length) {
      body = `<div class="chips">${m.distinct.map((v) => `<span class="fchip ${(f.set || []).includes(v) ? "on" : ""}" data-action="toggle-chip" data-key="${esc(m.key)}" data-value="${esc(v)}">${esc(v)}</span>`).join("")}</div>`;
    } else {
      body = `<input type="text" data-filter="${esc(m.key)}" data-part="text" placeholder="contains…" value="${esc(f.text ?? "")}">`;
    }
    return `<div class="filter ${on ? "active" : ""}">${head}${body}</div>`;
  }).join("");
  const desc = describeFilters(metas);
  return `<div class="filters">
      <div class="filter search-f ${u.text.trim() ? "active" : ""}"><label>Search options</label><input type="text" id="opt-text" placeholder="Type to narrow…  ( / )" value="${esc(u.text)}"></div>
      ${fs}
    </div>
    <div class="filter-summary">
      ${desc ? `<span>Filtering: ${esc(desc)}</span>` : `<span>Filter by any attribute to rule options out.</span>`}
      <label style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="keep-unknown" ${u.keepUnknown ? "checked" : ""}> keep options with unknown values</label>
      ${desc ? `<button class="btn small ghost" data-action="clear-all-filters">Clear filters</button>` : ""}
      ${filtered.length ? `<button class="btn small" data-action="exclude-filtered" title="Persist: mark the ${filtered.length} hidden options as excluded in the log">Exclude ${filtered.length} hidden</button>` : ""}
    </div>`;
}

function tableHtml({ metas, visible, best }, single) {
  if (!visible.length) return S.d.options.length ? `<div class="panel muted" style="margin-top:12px">No options match the filters.</div>` : "";
  const sort = ui().sort;
  const arrow = (k) => (sort && sort.key === k ? (sort.dir === "asc" ? " ↑" : " ↓") : "");
  const cols = metas.filter((m) => m.type !== "image");
  return `<div class="table-wrap"><table class="opts">
    <thead><tr><th></th><th data-sort="__label">Option${arrow("__label")}</th>
      ${cols.map((m) => `<th data-sort="${esc(m.key)}" title="${esc(m.def.description || "")}">${esc(m.label)}${m.requested ? ' <span class="req" title="Requested">●</span>' : ""}${m.better ? (m.better === "lower" ? " ↧" : " ↥") : ""}${arrow(m.key)}</th>`).join("")}
      <th></th></tr></thead>
    <tbody>${visible.map((o, i) => `
      <tr class="opt ${S.selection.has(o.id) ? "selected" : ""} ${S.focus === i ? "focus" : ""}" data-option="${esc(o.id)}">
        <td class="check"><div class="checkbox ${single ? "radio" : ""}">✓</div></td>
        <td>${o.image ? `<img class="thumb" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
          <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(o.label)}</a>` : esc(o.label)}</div>
          ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}</td>
        ${cols.map((m) => {
          const v = o.attributes[m.key];
          const cls = [v == null ? "missing" : "", typeof v === "number" ? "num" : "", best[m.key] !== undefined && v === best[m.key] ? "best" : ""].join(" ");
          return `<td class="${cls}">${cellHtml(v, m, o.attribute_meta[m.key])}</td>`;
        }).join("")}
        <td class="act"><button class="icon-btn" data-action="exclude" data-id="${esc(o.id)}" title="Rule out (x). Alt-click to give a reason.">✕</button></td>
      </tr>`).join("")}</tbody></table></div>`;
}

function cardsHtml({ metas, visible, best }, single) {
  if (!visible.length) return S.d.options.length ? `<div class="panel muted" style="margin-top:12px">No options match the filters.</div>` : "";
  const cols = metas.filter((m) => m.type !== "image");
  return `<div class="cards">${visible.map((o, i) => `
    <div class="card ${S.selection.has(o.id) ? "selected" : ""} ${S.focus === i ? "focus" : ""}" data-option="${esc(o.id)}">
      ${o.image ? `<img class="hero" src="${esc(o.image)}" alt="" loading="lazy">` : ""}
      <button class="icon-btn x" data-action="exclude" data-id="${esc(o.id)}" title="Rule out (x)">✕</button>
      <div class="body">
        <div class="top"><div class="checkbox ${single ? "radio" : ""}">✓</div>
          <div class="opt-label">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(o.label)}</a>` : esc(o.label)}</div></div>
        ${o.description ? `<div class="opt-desc">${esc(o.description)}</div>` : ""}
        ${cols.length ? `<dl>${cols.map((m) => {
          const v = o.attributes[m.key];
          return `<dt>${esc(m.label)}</dt><dd class="${v == null ? "missing" : ""} ${best[m.key] !== undefined && v === best[m.key] ? "best" : ""}">${cellHtml(v, m, o.attribute_meta[m.key])}</dd>`;
        }).join("")}</dl>` : ""}
      </div>
    </div>`).join("")}</div>`;
}

function miniHtml(o, kind) {
  return `<div class="mini-opt"><span class="opt-label">${esc(o.label)}</span>
    <span class="why">${kind === "excluded" ? esc(o.exclude_reason || "") : ""}</span>
    ${kind === "excluded" ? `<button class="btn small ghost" data-action="include" data-id="${esc(o.id)}">Restore</button>`
                          : `<button class="btn small ghost" data-action="exclude" data-id="${esc(o.id)}">Exclude</button>`}</div>`;
}

function decideBarHtml(d, single) {
  const n = S.selection.size;
  const valid = n >= d.min_select && (d.max_select === null || n <= d.max_select);
  const labels = d.options.filter((o) => S.selection.has(o.id)).map((o) => o.label);
  const same = d.resolution && n === d.resolution.selected.length && d.resolution.selected.every((s) => S.selection.has(s));
  const verb = d.status === "resolved" ? (same ? "Decided" : "Update decision") : "Decide";
  return `<div class="decide-bar">
    <div class="sel">${n ? `<strong>${esc(labels.join(", "))}</strong>` : `Select ${single ? "an option" : "options"} <span class="kbd">space</span>`}</div>
    <input id="note" placeholder="Note / rationale (optional)" value="${esc(S.noteDraft ?? (same ? d.resolution.note : "") ?? "")}">
    <button class="btn primary" data-action="resolve" ${valid && !same ? "" : "disabled"} title="⌘/Ctrl + Enter">${verb}</button>
  </div>`;
}

function attrRequestHtml(d, metas) {
  const requested = metas.filter((m) => m.requested);
  const active = d.options.filter((o) => !o.excluded);
  return `<div class="block-head"><h2>Ask for attributes</h2></div>
    <div class="panel">
      <form class="form-row" id="attr-form">
        <input name="key" placeholder="key, e.g. delivery_days" required style="width:160px">
        <input name="description" class="grow" placeholder="What you want to know">
        <button class="btn">Ask</button>
      </form>
      <p class="hint">The ${esc(d.source || "creator")} is notified (<code>attributes.requested</code>) and fills values in, best effort.</p>
      ${requested.length ? `<div class="req-list">${requested.map((m) => {
        const have = active.filter((o) => o.attributes[m.key] != null).length;
        const pct = active.length ? Math.round((have / active.length) * 100) : 0;
        return `<div class="req-item"><span class="k">${esc(m.key)}</span><span class="d">${esc(m.def.description || "")}</span>
          <span class="muted small">${have}/${active.length}</span><span class="progress"><i style="width:${pct}%"></i></span>
          <button class="btn small ghost" data-action="cancel-attr" data-key="${esc(m.key)}" title="Withdraw request">✕</button></div>`;
      }).join("")}</div>` : ""}
    </div>`;
}

function webhooksHtml(d) {
  const rows = S.deliveries.slice(0, 12).map((x) => `<tr>
      <td>${esc(x.event_type)}<div class="muted small">${ago(x.created_at)}</div></td>
      <td><span class="badge ${x.status}">${x.status}</span>${x.require_ack && !x.acked_at && x.status === "delivered" ? `<div class="muted small">awaiting ack</div>` : ""}</td>
      <td class="small">${x.total_attempts}× ${x.last_status ? `· ${x.last_status}` : ""}${x.last_error && !x.last_status ? `<div class="muted" title="${esc(x.last_error)}">${esc(x.last_error.slice(0, 40))}</div>` : ""}
        ${x.next_attempt_at ? `<div class="muted">next ${new Date(x.next_attempt_at).toLocaleTimeString()}</div>` : ""}</td>
      <td style="text-align:right"><button class="btn small ghost" data-action="redeliver" data-id="${x.id}" title="Fire again now">Re-fire</button></td></tr>`).join("");
  // The eye lets you resolve without telling this listener (say, to fix a typo). Global webhooks always hear.
  const canSilence = !closed(d);
  const eye = (id) => {
    if (!canSilence) return "";
    const off = S.silenced.has(id);
    return `<button class="eye ${off ? "off" : ""}" data-action="toggle-silence" data-id="${esc(id)}"
      title="${off ? "Won't be notified when you decide. Click to notify." : "Will be notified when you decide. Click to skip it."}">${off ? EYE_OFF : EYE}</button>`;
  };
  const row = (id, body) => `<div class="listener ${S.silenced.has(id) ? "silenced" : ""}">${body}${id ? eye(id) : ""}</div>`;
  const resumerRow = d.resume ? row("resumer", `<span class="live ${S.resumer?.online ? "on" : "off"}"></span><strong>resumer</strong>
       <span class="muted">${S.resumer?.machine ? `on ${esc(S.resumer.machine)}, ` : ""}${S.resumer?.online ? `online, will resume ${esc(d.resume.agent || "the agent")} session ${esc(d.resume.session_id.slice(0, 8))}…` : "offline: the session will not resume automatically"}</span>`) : "";
  const listeners = resumerRow + (S.listeners || []).map((l) => l.kind === "long-poll"
    ? row(l.id, `<span class="live ${l.connected ? "on" : ""}"></span><strong>${esc(l.actor)}</strong>
         <span class="muted">waiting (long-poll) since ${ago(l.since)} for ${esc(l.events.join(", "))}</span>`)
    : row(l.scope === "global" ? null : l.id, `<span class="live on hook"></span><strong>${l.scope === "global" ? "global webhook" : "webhook"}</strong>
         <span class="mono small">${esc(l.url)}</span> <span class="muted">· ${esc(l.events.join(", "))}${l.require_ack ? " · re-fires until acked" : ""}</span>`)).join("");
  return `<div class="block-head"><h2>Listening</h2><span class="spacer"></span><span class="muted small">${(S.listeners || []).length + (d.resume && S.resumer?.online ? 1 : 0) || "nobody"}</span></div>
    <div class="panel">
      ${listeners || `<div class="muted small">Nobody is listening for this decision right now. It will still be recorded; the requester can check later.</div>`}
      ${rows ? `<div class="hint" style="margin-top:12px">Webhook deliveries</div><table class="plain"><tr><th>Event</th><th>Status</th><th>Attempts</th><th></th></tr>${rows}</table>` : ""}
    </div>`;
}

const EYE = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
const EYE_OFF = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.2 3.2M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/></svg>`;

const EVENT_ICONS = {
  "decision.created": "✦", "decision.updated": "✎", "decision.resolved": "✔", "decision.reopened": "↺",
  "decision.cancelled": "⊘", "decision.expired": "⌛", "decision.commented": "❝", "options.added": "+", "options.updated": "✎",
  "option.removed": "−", "options.excluded": "✕", "options.included": "↺", "attributes.requested": "?",
  "attributes.request_cancelled": "⊘",
};

function eventText(e) {
  const d = S.d;
  const label = (id) => d.options.find((o) => o.id === id)?.label || id;
  const x = e.data;
  switch (e.type) {
    case "decision.created": return `created this decision with ${x.decision.options.length} options`;
    case "decision.updated": {
      const due = x.changes.due_at?.[1];
      const rest = Object.keys(x.changes).filter((k) => k !== "due_at");
      return [due ? `set the deadline to <strong title="${esc(fullTime(due))}">${esc(whenTime(due))}</strong>` : "",
              rest.length ? `updated ${esc(rest.join(", "))}` : ""].filter(Boolean).join(", ");
    }
    case "decision.resolved": return `${x.previous_status === "resolved" ? "changed the decision to" : "decided"} <strong>${esc(x.resolution.selected.map(label).join(", "))}</strong>${x.resolution.note ? ` — “${esc(x.resolution.note)}”` : ""}${x.resolution.filters ? `<div class="muted small">filters: ${esc(x.resolution.filters)}</div>` : ""}${x.silenced ? `<div class="muted small">didn't notify ${esc(x.silenced.join(", "))}</div>` : ""}`;
    case "decision.reopened": return `reopened${x.due_at ? ` with a new deadline, <span title="${esc(fullTime(x.due_at))}">${esc(whenTime(x.due_at))}</span>` : ""}${x.note ? ` — “${esc(x.note)}”` : ""}`;
    case "decision.expired": return `expired it: nobody decided before the deadline`;
    case "decision.cancelled": return `cancelled${x.note ? ` — “${esc(x.note)}”` : ""}`;
    case "decision.commented": return `<div class="comment">${esc(x.text)}</div>`;
    case "options.added": return `added ${x.options.map((o) => `<strong>${esc(o.label)}</strong>`).join(", ")}`;
    case "options.updated": {
      const ids = Object.keys(x.options);
      const attrs = [...new Set(ids.flatMap((id) => Object.keys(x.options[id].attributes || {})))];
      return `updated ${ids.length} option${ids.length > 1 ? "s" : ""}${attrs.length ? ` (${esc(attrs.join(", "))})` : ""}`;
    }
    case "option.removed": return `removed <strong>${esc(x.option.label)}</strong>`;
    case "options.excluded": return `ruled out ${esc(x.option_ids.map(label).join(", "))}${x.reason ? ` — ${esc(x.reason)}` : ""}`;
    case "options.included": return `restored ${esc(x.option_ids.map(label).join(", "))}`;
    case "attributes.requested": return `asked for ${Object.entries(x.attributes).map(([k, v]) => `<strong>${esc(k)}</strong>${v.description ? ` (${esc(v.description)})` : ""}`).join(", ")}`;
    case "attributes.request_cancelled": return `withdrew request for <strong>${esc(x.key)}</strong>`;
    case "webhook.acked": return `acknowledged webhook delivery <code>${esc(x.delivery_id)}</code>`;
    case "webhook.redelivered": return `re-fired webhook delivery <code>${esc(x.delivery_id)}</code>`;
    default: return esc(e.type);
  }
}

function activityHtml(d) {
  const evs = [...S.events].reverse();
  return `<div class="block-head"><h2>Activity & provenance</h2><span class="spacer"></span><span class="muted small">${S.events.length} immutable events</span></div>
    <div class="panel">
      <form class="form-row" id="comment-form"><input name="text" class="grow" placeholder="Add a comment to the log…"><button class="btn">Comment</button></form>
      <ul class="timeline">${evs.map((e) => `<li>
        <span class="ic">${EVENT_ICONS[e.type] || "·"}</span>
        <div><span class="who">${esc(e.actor)}</span> <span class="what">${eventText(e)}</span><span class="when" title="${esc(fullTime(e.ts))}">${ago(e.ts)}</span>
          <details><summary>#${e.seq} · ${esc(e.type)} · ${esc(e.hash.slice(0, 10))}</summary><pre>${esc(JSON.stringify(e.data, null, 2))}</pre></details></div>
      </li>`).join("")}</ul>
    </div>`;
}

// ------------------------------------------------------------------ interactions

function visibleIds() { return partition().visible.map((o) => o.id); }

function toggleSelect(id) {
  const d = S.d;
  if (S.selection.has(id)) S.selection.delete(id);
  else {
    if (d.max_select === 1) S.selection.clear();
    S.selection.add(id);
  }
  S.noteDraft = $("#note")?.value || S.noteDraft;
  renderDecision();
}

async function act(fn, okMsg) {
  try {
    const d = await fn();
    if (d && d.id && d.options) { S.d = d; await loadDecision(d.id, { keepSelection: true }); }
    if (okMsg) toast(okMsg);
    loadList();
  } catch (e) { fail(e); }
}

async function exclude(ids, reason, filters) {
  const d = S.d;
  const labels = ids.map((id) => d.options.find((o) => o.id === id)?.label || id);
  ids.forEach((id) => S.selection.delete(id));
  await act(() => api("POST", `/api/decisions/${d.id}/exclude`, { option_ids: ids, reason, filters }));
  toast(`Ruled out ${labels.length > 2 ? labels.length + " options" : labels.join(", ")}`, {
    undo: () => act(() => api("POST", `/api/decisions/${d.id}/include`, { option_ids: ids })),
  });
}

function bindDecision() {
  const d = S.d;
  const u = ui();
  const main = $("#main");

  main.querySelectorAll("[data-filter]").forEach((el) => {
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
      const f = (u.filters[el.dataset.filter] ||= {});
      f[el.dataset.part] = el.value;
      saveUi();
      rerenderKeepingFocus(el);
    });
  });
  const text = $("#opt-text");
  if (text) text.addEventListener("input", () => { u.text = text.value; saveUi(); rerenderKeepingFocus(text); });
  const keep = $("#keep-unknown");
  if (keep) keep.onchange = () => { u.keepUnknown = keep.checked; saveUi(); renderDecision(); };
  main.querySelectorAll("details[data-key]").forEach((el) => el.addEventListener("toggle", () => { u[el.dataset.key] = el.open; saveUi(); }));
  main.querySelectorAll("th[data-sort]").forEach((th) => th.onclick = () => {
    const k = th.dataset.sort;
    u.sort = !u.sort || u.sort.key !== k ? { key: k, dir: "asc" } : u.sort.dir === "asc" ? { key: k, dir: "desc" } : null;
    saveUi(); renderDecision();
  });
  const note = $("#note");
  if (note) note.addEventListener("input", () => (S.noteDraft = note.value));

  const attrForm = $("#attr-form");
  if (attrForm) attrForm.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(attrForm);
    const key = f.get("key").trim();
    act(() => api("POST", `/api/decisions/${d.id}/attribute-requests`, { key, description: f.get("description").trim() }), `Asked for "${key}"`);
  };
  const commentForm = $("#comment-form");
  commentForm.onsubmit = (e) => {
    e.preventDefault();
    const t = new FormData(commentForm).get("text").trim();
    if (t) act(() => api("POST", `/api/decisions/${d.id}/comments`, { text: t }));
  };
  const editForm = $("#edit-form");
  if (editForm) editForm.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(editForm);
    S.editing = false;
    act(() => api("PATCH", `/api/decisions/${d.id}`, { title: f.get("title"), context: f.get("context") }), "Saved");
  };
}

function rerenderKeepingFocus(el) {
  const sel = el.id ? `#${el.id}` : `[data-filter="${CSS.escape(el.dataset.filter)}"][data-part="${el.dataset.part}"]`;
  const pos = el.selectionStart;
  renderDecision();
  const again = $(sel);
  if (again) { again.focus(); try { again.setSelectionRange(pos, pos); } catch {} }
}

document.addEventListener("click", async (e) => {
  const nav = e.target.closest("a[data-nav]");
  if (nav && !e.metaKey && !e.ctrlKey) { e.preventDefault(); return navigate(nav.getAttribute("href")); }
  const tab = e.target.closest("[data-status]");
  if (tab) { S.status = tab.dataset.status; storage("status", S.status); return renderSidebar(); }
  const spaceTab = e.target.closest("[data-space]");
  if (spaceTab) { S.space = spaceTab.dataset.space; storage("space", S.space); return renderSidebar(); }

  const a = e.target.closest("[data-action]");
  const d = S.d;
  if (!a) {
    const opt = e.target.closest("[data-option]");
    if (opt && d && !closed(d)) toggleSelect(opt.dataset.option);
    return;
  }
  e.stopPropagation();
  const id = a.dataset.id;
  switch (a.dataset.action) {
    case "view": ui().view = a.dataset.view; saveUi(); return renderDecision();
    case "exclude": {
      const reason = e.altKey ? prompt("Why is this not viable?") || undefined : undefined;
      return exclude([id], reason);
    }
    case "include": return act(() => api("POST", `/api/decisions/${d.id}/include`, { option_ids: [id] }));
    case "exclude-filtered": {
      const { metas, filtered } = partition();
      const desc = describeFilters(metas);
      return exclude(filtered.map((o) => o.id), `filtered out: ${desc}`, desc);
    }
    case "toggle-chip": {
      const f = (ui().filters[a.dataset.key] ||= {});
      const set = new Set(f.set || []);
      set.has(a.dataset.value) ? set.delete(a.dataset.value) : set.add(a.dataset.value);
      f.set = [...set]; saveUi(); return renderDecision();
    }
    case "clear-filter": delete ui().filters[a.dataset.key]; saveUi(); return renderDecision();
    case "clear-all-filters": ui().filters = {}; ui().text = ""; saveUi(); return renderDecision();
    case "resolve": return resolve();
    case "toggle-silence": {
      S.silenced.has(id) ? S.silenced.delete(id) : S.silenced.add(id);
      const panel = $("#listeners-panel");
      if (panel) panel.innerHTML = webhooksHtml(d);
      return;
    }
    case "reopen": {
      const note = prompt("Reopen — note (optional):");
      if (note === null) return;
      return act(() => api("POST", `/api/decisions/${d.id}/reopen`, { note }), "Reopened");
    }
    case "cancel-decision": {
      const note = prompt("Cancel this decision — note (optional):");
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
    case "copy-install": copy(installCommand(S.config), "install command"); return;
    case "copy-gui-link": copy(`${location.origin}/#token=${encodeURIComponent(S.token)}`, "GUI link"); return;
    case "copy-resume": {
      await (d.resume_command ? copy(d.resume_command, "resume command") : copy(d.resume.session_id, "session ID"));
      if (d.resume_state && ["pending", "failed"].includes(d.resume_state.status))
        toast("Copied. Paste it in a terminal to resume.", { undo: () => markResumed(), undoLabel: "Mark resumed" });
      return;
    }
    case "mark-resumed": return markResumed();
    case "copy-ref": return copy(`[${d.id}: ${d.title}](${d.url})`, "reference");
    case "copy-md": return copy(await api("GET", `/api/decisions/${d.id}/markdown`), "summary");
    case "copy-provenance": {
      const r = d.resolution;
      const picked = d.selected_options.map((o) => o.label).join(", ") || "nothing";
      return copy([`Decision: ${d.title}`, `Choice: ${picked}`, ...(r.note ? [`Rationale: ${r.note.trim()}`] : []), `Link: ${d.url}`].join("\n"), "provenance");
    }
    case "cancel-attr": return act(() => api("DELETE", `/api/decisions/${d.id}/attribute-requests/${encodeURIComponent(a.dataset.key)}`));
    case "redeliver": return act(async () => { await api("POST", `/api/deliveries/${id}/redeliver`); return null; }, "Re-firing").then(() => d && loadDecision(d.id, { keepSelection: true }));
    case "toggle-hook": try { await api("PATCH", `/api/webhooks/${id}`, { active: a.dataset.active !== "true" }); renderSettings(); } catch (err) { fail(err); } return;
    case "delete-hook": if (confirm("Delete this webhook?")) { try { await api("DELETE", `/api/webhooks/${id}`); renderSettings(); } catch (err) { fail(err); } } return;
  }
});

function markResumed() {
  const d = S.d;
  return act(() => api("POST", `/api/decisions/${d.id}/resume-events`, { status: "manual", trigger_seq: d.resume_state?.trigger_seq }), "Marked as resumed");
}

async function resolve() {
  const d = S.d;
  const note = $("#note")?.value || "";
  const { metas } = partition();
  const filters = describeFilters(metas) || undefined;
  const silence = [...S.silenced];
  await act(() => api("POST", `/api/decisions/${d.id}/resolve`, { selected: [...S.selection], note, filters, silence }),
            `${d.id} decided${silence.length ? ` — ${silence.length} listener${silence.length > 1 ? "s" : ""} not notified` : " — webhook firing"}`);
  S.noteDraft = undefined;
  S.silenced = new Set();
}

document.addEventListener("keydown", (e) => {
  if (!S.d || S.editing || S.d.status === "resolved" || closed(S.d)) return;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    const btn = $('[data-action="resolve"]');
    if (btn && !btn.disabled) { e.preventDefault(); resolve(); }
    return;
  }
  if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
  const ids = visibleIds();
  if (e.key === "/") { e.preventDefault(); $("#opt-text")?.focus(); }
  else if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); S.focus = Math.min(ids.length - 1, S.focus + 1); renderDecision(); scrollFocus(); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); S.focus = Math.max(0, S.focus - 1); renderDecision(); scrollFocus(); }
  else if ((e.key === " " || e.key === "Enter") && ids[S.focus]) { e.preventDefault(); toggleSelect(ids[S.focus]); }
  else if (e.key === "x" && ids[S.focus]) { e.preventDefault(); exclude([ids[S.focus]]); S.focus = Math.min(S.focus, ids.length - 2); }
});
function scrollFocus() { $(".focus")?.scrollIntoView({ block: "nearest" }); }

setInterval(async () => {
  if (!S.d || document.hidden) return;
  try {
    [S.listeners, S.resumer] = await Promise.all([
      api("GET", `/api/decisions/${S.d.id}/listeners`).then((r) => r.listeners),
      api("GET", resumerPath(S.d)),
    ]);
    const panel = $("#listeners-panel");
    if (panel) panel.innerHTML = webhooksHtml(S.d);
    const card = $("#resume-card");
    if (card) card.innerHTML = resumeHtml(S.d);
  } catch {}
}, 5000);

// Countdowns tick without waiting for an event.
setInterval(() => {
  if (document.hidden) return;
  renderSidebar();
  const card = $("#deadline-card");
  if (card && S.d) card.innerHTML = deadlineHtml(S.d);
}, 30000);

$("#search").addEventListener("input", (e) => { S.q = e.target.value; renderSidebar(); });
window.addEventListener("popstate", route);

// ------------------------------------------------------------------ boot

(async function boot() {
  try {
    S.config = await api("GET", "/api/config");
    await loadList();
    await route();
    poll();
  } catch (e) {
    $("#main").innerHTML = `<div class="page"><div class="empty">Could not load: ${esc(e.message)}</div></div>`;
  }
})();
