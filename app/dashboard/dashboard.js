// AI Switchboard dashboard - vanilla JS, no build step
"use strict";

// ------------------------------------------------------------
// State
// ------------------------------------------------------------
const State = {
  view: "new",                  // "new" | "inbox" | "detail"
  agents: [],                   // available agent names
  selectedAgents: [],           // for the New Task form
  inboxTimer: null,
  detailTimer: null,
  currentTaskId: null,
  currentTaskData: null,        // last fetched detail payload
  terminalStatuses: new Set(["completed", "failed", "cancelled"]),
  attachments: [],              // [{ id, file: File }] pending uploads for New Task form
  decisionEditing: false,       // when true, force the decision panel into the form state even if a decision exists
  decisionDraft: "",            // preserved draft text across re-renders while editing
  followupParentId: null,       // set when "Continue this thread" was used; included as parent_task_id on next submit
  threadCache: {},              // taskId -> last fetched thread response
  liveTickerTimer: null,        // 1s interval for live elapsed-time updates on the active agent run
  liveTickerStartMs: null,      // ms epoch for the active run start, used by the ticker
  // Inbox filters / quantity / search. These persist across the 5s auto-refresh.
  inboxFilters: {
    status: "",                 // "" = all, else one of: pending|running|awaiting_user_input|completed|failed|cancelled
    mode: "",                   // "" = all, else conclave|consult|resolve (client-side filter)
    search: "",                 // case-insensitive substring match against row text + ID
    exported: "",               // "" = any, "true" = exported only, "false" = not-exported only (server-side filter)
  },
  inboxLimit: 50,               // last-N to request from server; persisted in localStorage
  inboxRawTasks: [],            // last server payload (already limit-applied), used for client-side filter rerenders
  inboxSearchDebounce: null,    // debounce timer for the search input
};

const INBOX_LIMIT_KEY = "switchboard.inbox.limit";
const INBOX_LIMIT_CHOICES = [50, 100, 500, 1000, 5000];

const MAX_FILE_BYTES = 20 * 1024 * 1024; // 20 MiB cap, matches server

// Standard ignore set for folder uploads. Mirrors the server-side ignore list
// so we don't bother POSTing files the server will reject. Centralized here so
// future tweaks are obvious — do not scatter individual checks elsewhere.
const FOLDER_IGNORE = {
  dirNames: new Set([
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit",
    ".idea", ".vscode", "coverage", "htmlcov", "data",
  ]),
  fileSuffixes: [
    ".pyc", ".pyo", ".so", ".dll", ".exe",
    ".zip", ".tar", ".gz", ".mp4", ".mov",
  ],
  // Exact file-name matches (case-sensitive on most filesystems; we lowercase
  // when comparing to be forgiving).
  fileNames: new Set([".ds_store", "thumbs.db"]),
};

// Rough sanity guard: refuse to walk a dropped folder if its top-level entry
// count is over this. Catches accidental drags of "Downloads" or "/".
const FOLDER_TOP_LEVEL_LIMIT = 500;

function folderShouldIgnoreDir(name) {
  return FOLDER_IGNORE.dirNames.has(name);
}

function folderShouldIgnoreFile(name) {
  const lower = (name || "").toLowerCase();
  if (FOLDER_IGNORE.fileNames.has(lower)) return true;
  for (const suf of FOLDER_IGNORE.fileSuffixes) {
    if (lower.endsWith(suf)) return true;
  }
  return false;
}

// ------------------------------------------------------------
// Tiny DOM helpers
// ------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else {
      node.setAttribute(k, v);
    }
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch (_) { return iso; }
}

function shortId(id) {
  if (!id) return "";
  return id.length > 14 ? id.slice(0, 14) + "..." : id;
}

// Parse an ISO timestamp into ms-since-epoch, or null if unparseable.
function parseIsoMs(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  const t = d.getTime();
  return isNaN(t) ? null : t;
}

// Compact human-readable duration. msOrSecs may be number-of-ms.
function fmtDurationMs(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "";
  const totalSecs = Math.floor(ms / 1000);
  if (totalSecs < 60) return totalSecs + "s";
  const m = Math.floor(totalSecs / 60);
  const s = totalSecs % 60;
  if (m < 60) return s === 0 ? m + "m" : m + "m " + s + "s";
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm === 0 ? h + "h" : h + "h " + mm + "m";
}

function fmtRelTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  if (diff < 60000)    return "just now";
  if (diff < 3600000)  return Math.floor(diff / 60000) + "m ago";
  if (diff < 86400000) return Math.floor(diff / 3600000) + "h ago";
  return Math.floor(diff / 86400000) + "d ago";
}

// Format an integer with thousands separators. Returns "" for null/undefined.
function fmtInt(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "";
  try { return Number(n).toLocaleString("en-US"); } catch (_) { return String(n); }
}

// Format a USD cost with at most 4 decimal places, dropping trailing zeros.
function fmtUsd(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "";
  if (n === 0) return "$0";
  const abs = Math.abs(n);
  const digits = abs < 0.01 ? 4 : (abs < 1 ? 3 : 2);
  return "$" + n.toFixed(digits).replace(/\.?0+$/, (m) => m === "." ? "" : "");
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

// ------------------------------------------------------------
// Clipboard / copy buttons
// ------------------------------------------------------------
// Single inline SVG clipboard icon. Reused by every copy button via cloneNode.
const COPY_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
  '<rect x="9" y="9" width="11" height="11" rx="2" ry="2"></rect>' +
  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>' +
  '</svg>';

const COPY_CHECK_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
  '<polyline points="20 6 9 17 4 12"></polyline>' +
  '</svg>';

async function copyToClipboard(text) {
  if (text === null || text === undefined) text = "";
  text = String(text);
  // Modern path
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return { ok: true, fallback: false };
    } catch (_) {
      // fall through to fallback
    }
  }
  // Fallback: hidden textarea + execCommand
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.left = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(ta);
    if (ok) return { ok: true, fallback: true };
  } catch (_) { /* ignore */ }
  return { ok: false, fallback: true };
}

function flashCopyFeedback(btn, success) {
  if (!btn) return;
  const originalIconHtml = btn.dataset.iconHtml || COPY_ICON_SVG;
  if (!btn.dataset.iconHtml) btn.dataset.iconHtml = originalIconHtml;

  // Swap in the check icon (or keep clipboard for failure) and add a label tooltip
  const iconSpan = btn.querySelector(".copy-icon");
  if (success) {
    btn.classList.add("copied");
    if (iconSpan) iconSpan.innerHTML = COPY_CHECK_SVG;
  } else {
    btn.classList.add("copy-failed");
  }

  let feedback = btn.querySelector(".copy-feedback");
  if (!feedback) {
    feedback = document.createElement("span");
    feedback.className = "copy-feedback";
    btn.appendChild(feedback);
  }
  feedback.textContent = success ? "Copied" : "Copy failed";
  // Force reflow before adding the .show class so the transition runs
  // eslint-disable-next-line no-unused-expressions
  feedback.offsetHeight;
  feedback.classList.add("show");

  if (btn._copyTimer) clearTimeout(btn._copyTimer);
  btn._copyTimer = setTimeout(() => {
    btn.classList.remove("copied", "copy-failed");
    feedback.classList.remove("show");
    if (iconSpan) iconSpan.innerHTML = originalIconHtml;
  }, 1500);
}

// textOrFn: either a string, or a function returning a string (evaluated at click time
// so callers can capture fresh state if needed).
function makeCopyButton(textOrFn, label, opts) {
  opts = opts || {};
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn" + (opts.extraClass ? " " + opts.extraClass : "");
  const ariaLabel = "Copy " + label;
  btn.setAttribute("aria-label", ariaLabel);
  btn.setAttribute("title", ariaLabel);
  const iconSpan = document.createElement("span");
  iconSpan.className = "copy-icon";
  iconSpan.innerHTML = COPY_ICON_SVG;
  btn.appendChild(iconSpan);
  btn.dataset.iconHtml = COPY_ICON_SVG;

  btn.addEventListener("click", async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const text = (typeof textOrFn === "function") ? textOrFn() : textOrFn;
    const result = await copyToClipboard(text);
    flashCopyFeedback(btn, result.ok);
  });

  return btn;
}

// ------------------------------------------------------------
// Formatters used by copy buttons (plain-text, human-readable)
// ------------------------------------------------------------
function valueToPlainText(value, indent) {
  indent = indent || "";
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map((item) => {
      if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
        return indent + "- " + String(item);
      }
      return indent + "- " + JSON.stringify(item, null, 2).replace(/\n/g, "\n  " + indent);
    }).join("\n");
  }
  if (isPlainObject(value)) {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function formatMessageAsText(m) {
  const lines = [];
  const header = (m.agent_name || "?") + (m.role ? " (" + m.role + ")" : "")
    + (m.message_type ? " [" + m.message_type + "]" : "");
  lines.push(header);
  if (m.created_at) lines.push("Time: " + fmtTime(m.created_at));
  lines.push("");

  const structured = isPlainObject(m.structured) ? m.structured : null;
  if (structured) {
    for (const [key, value] of Object.entries(structured)) {
      if (value === null || value === undefined) continue;
      if (Array.isArray(value) && value.length === 0) continue;
      if (typeof value === "string" && value.trim() === "") continue;
      lines.push(prettifyKey(key).toUpperCase() + ":");
      lines.push(valueToPlainText(value));
      lines.push("");
    }
  } else if (typeof m.content === "string" && m.content.length > 0) {
    lines.push(m.content);
  }
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function formatDisagreementAsText(d) {
  const lines = [];
  if (d.topic) lines.push("Topic: " + d.topic);
  if (d.primary_position) {
    lines.push("");
    lines.push("Primary position:");
    lines.push(valueToPlainText(d.primary_position));
  }
  if (d.consultant_position) {
    lines.push("");
    lines.push("Consultant position:");
    lines.push(valueToPlainText(d.consultant_position));
  }
  for (const [k, v] of Object.entries(d)) {
    if (["topic", "primary_position", "consultant_position"].includes(k)) continue;
    if (v === null || v === undefined) continue;
    lines.push("");
    lines.push(prettifyKey(k) + ":");
    lines.push(valueToPlainText(v));
  }
  return lines.join("\n").trim();
}

// ------------------------------------------------------------
// API
// ------------------------------------------------------------
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_) { /* ignore non-json */ }
  if (!res.ok) {
    const detail = body && (body.detail || body.error || body.message);
    const msg = detail ? (typeof detail === "string" ? detail : JSON.stringify(detail))
                       : `${res.status} ${res.statusText}`;
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

const Api = {
  health:   () => api("/api/health"),
  agents:   () => api("/api/agents"),
  listTasks:(opts) => {
    const params = new URLSearchParams();
    if (opts && opts.status) params.set("status", opts.status);
    if (opts && opts.limit) params.set("limit", String(opts.limit));
    if (opts && (opts.exported === "true" || opts.exported === "false")) {
      params.set("exported", opts.exported);
    }
    if (opts && opts.q) params.set("q", opts.q);
    const qs = params.toString();
    return api("/api/tasks" + (qs ? "?" + qs : ""));
  },
  getTask:  (id) => api(`/api/tasks/${id}`),
  getThread:(id) => api(`/api/tasks/${id}/thread`),
  createTask: (payload) => api("/api/tasks", { method: "POST", body: JSON.stringify(payload) }),
  cancelTask: (id) => api(`/api/tasks/${id}/cancel`, { method: "POST" }),
  answerTask: (id, answer) => api(`/api/tasks/${id}/answer`,
    { method: "POST", body: JSON.stringify({ answer }) }),
  applyArtifact: (taskId, artifactId) => api(`/api/tasks/${taskId}/artifacts/${artifactId}/apply`,
    { method: "POST" }),
  decideTask: (id, decision) => api(`/api/tasks/${id}/decide`,
    { method: "POST", body: JSON.stringify({ decision }) }),
  exportTask: (id) => api(`/api/tasks/${id}/export`, { method: "POST" }),
  getApiKeys: () => api("/api/settings/api-keys"),
  setApiKey: (name, value) => api(`/api/settings/api-keys/${name}`,
    { method: "POST", body: JSON.stringify({ value }) }),
  revealApiKey: (name) => api(`/api/settings/api-keys/${name}/reveal`),
  exportBatchTasks: (body) => api("/api/tasks/export-batch",
    { method: "POST", body: JSON.stringify(body || {}) }),
  usageSummary: () => api("/api/tasks/usage"),
  uploadFile: async (file) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/uploads", { method: "POST", body: fd });
    let body = null;
    try { body = await res.json(); } catch (_) { /* ignore non-json */ }
    if (!res.ok) {
      const detail = body && (body.detail || body.error || body.message);
      const msg = detail ? (typeof detail === "string" ? detail : JSON.stringify(detail))
                         : `${res.status} ${res.statusText}`;
      const err = new Error(msg);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  },
};

// ------------------------------------------------------------
// Sidebar (left rail) — declarative, ordered for future growth.
// Each item: { id, glyph, color, label, pin: 'top'|'bottom', handler, shortcut? }
// `id` doubles as the view name when the handler is switchView(id).
// ------------------------------------------------------------
const SIDEBAR_ITEMS = [
  {
    id: "new",
    glyph: "⌂",
    color: "#84cc16",
    label: "New Task (home)",
    pin: "top",
    handler: () => switchView("new"),
  },
  {
    id: "help",
    glyph: "?",
    color: "#3b82f6",
    label: "Help & Reference",
    pin: "top",
    shortcut: "?",
    handler: () => openHelp(),
  },
  {
    id: "pricing",
    glyph: "$",
    color: "#8b5cf6",
    label: "Model pricing",
    handler: () => switchView("pricing"),
  },
  {
    id: "recent-tasks",
    glyph: "⟳",
    color: "#06b6d4",
    label: "Recent tasks",
    handler: () => switchView("recent-tasks"),
  },
  {
    id: "usage",
    glyph: "∑",
    color: "#f59e0b",
    label: "Usage & spend",
    handler: () => switchView("usage"),
  },
  {
    id: "theme",
    glyph: "☾",
    color: "#a855f7",
    label: "Toggle dark / light theme",
    action: "toggle-theme",
    handler: () => toggleTheme(),
  },
  {
    id: "settings",
    glyph: "⚙",
    color: "#10b981",
    label: "Settings",
    pin: "bottom",
    handler: () => switchView("settings"),
  },
];

function renderSidebar() {
  const rail = $("#left-rail");
  if (!rail) return;
  rail.innerHTML = "";
  const topGroup = document.createElement("div");
  topGroup.className = "rail-group rail-group-top";
  const bottomGroup = document.createElement("div");
  bottomGroup.className = "rail-group rail-group-bottom";
  for (const item of SIDEBAR_ITEMS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rail-btn";
    btn.id = "rail-" + item.id;
    btn.dataset.itemId = item.id;
    btn.style.setProperty("--rail-color", item.color);
    const tip = item.shortcut ? `${item.label} (${item.shortcut})` : item.label;
    btn.title = tip;
    btn.setAttribute("aria-label", item.label);
    btn.setAttribute("data-label", tip);
    btn.innerHTML = `<span aria-hidden="true">${item.glyph}</span>`;
    btn.addEventListener("click", () => toggleSidebarItem(item.id));
    (item.pin === "bottom" ? bottomGroup : topGroup).appendChild(btn);
  }
  rail.appendChild(topGroup);
  rail.appendChild(bottomGroup);
}

// Binary toggle for sidebar items. Clicking (or shortcut-invoking) an already
// active sidebar item returns the user to the view they were on before. Works
// across any number of sidebar items via State.prevView, updated every time we
// enter a sidebar view from elsewhere. Items declared with `action:` (e.g. the
// theme toggle) are not views and just run their handler.
function toggleSidebarItem(id) {
  const item = SIDEBAR_ITEMS.find((x) => x.id === id);
  if (!item) return;
  if (item.action) {
    item.handler();
    return;
  }
  if (State.view === id) {
    switchView(State.prevView || "new");
  } else {
    State.prevView = State.view;
    item.handler();
  }
}

// ------------------------------------------------------------
// Theme (dark / light)
// ------------------------------------------------------------
const THEME_STORAGE_KEY = "switchboard.theme";

function applyTheme() {
  const stored = localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "dark") {
    document.body.classList.add("theme-dark");
  } else {
    document.body.classList.remove("theme-dark");
  }
  updateThemeGlyph();
}

function toggleTheme() {
  const isDark = document.body.classList.toggle("theme-dark");
  localStorage.setItem(THEME_STORAGE_KEY, isDark ? "dark" : "light");
  updateThemeGlyph();
}

function updateThemeGlyph() {
  const btn = document.getElementById("rail-theme");
  if (!btn) return;
  const isDark = document.body.classList.contains("theme-dark");
  // Sun when in dark mode (= "click to go light"), moon when in light mode.
  const span = btn.querySelector("span");
  if (span) span.textContent = isDark ? "☀" : "☾";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.setAttribute("data-label", label);
}

// ------------------------------------------------------------
// View switching
// ------------------------------------------------------------
function switchView(name) {
  State.view = name;
  for (const v of ["new", "inbox", "detail", "help", "pricing", "settings", "recent-tasks", "usage"]) {
    const section = $("#view-" + v);
    if (section) section.hidden = (v !== name);
  }
  for (const t of $$(".tab")) {
    t.classList.toggle("active", t.dataset.view === name);
  }
  // Sync rail-button active state (rail is JS-rendered, ids = `rail-<itemId>`).
  for (const btn of $$(".left-rail .rail-btn")) {
    btn.classList.toggle("active", btn.dataset.itemId === name);
  }

  // Help view releases the .app max-width so the doc + TOC can take the full
  // viewport and the user can drag the splitter wherever.
  document.body.classList.toggle("help-view-active", name === "help");

  // Manage polling lifecycles
  if (name === "inbox") startInboxPolling(); else stopInboxPolling();
  if (name === "detail") startDetailPolling(); else stopDetailPolling();

  // Trigger immediate fetches when entering a view
  if (name === "inbox") refreshInbox();
  if (name === "detail" && State.currentTaskId) refreshDetail();
  if (name === "settings") loadApiKeysSettings();
  if (name === "help") refreshHelpBadge();
  if (name === "pricing") loadPricing();
  if (name === "recent-tasks") loadRecentTasks();
  if (name === "usage") loadUsage();
  // Refresh per-seat readiness when the New Task panel opens. Server caches
  // /api/health for 30s, so re-firing on each panel-open is cheap.
  if (name === "new") loadReadiness();
}

// ------------------------------------------------------------
// Pricing view — fetches /api/agents/pricing and renders a sortable table.
// Default sort: most expensive (per-turn estimate) descending, so the seats
// you'd want to keep an eye on are at the top.
// ------------------------------------------------------------
const PricingState = {
  items: [],
  sortKey: "per_turn_estimate_usd",
  sortDir: "desc",
  basis: null,
  cacheAgeSeconds: null,
};

async function loadPricing() {
  const tbody = $("#pricing-tbody");
  const statusEl = $("#pricing-status");
  const noteEl = $("#pricing-basis-note");
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" class="loading">Loading pricing…</td></tr>';
  try {
    const resp = await fetch("/api/agents/pricing");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    PricingState.items = data.items || [];
    PricingState.basis = data.estimate_basis || null;
    PricingState.cacheAgeSeconds = data.openrouter_cache_age_seconds;
    if (noteEl && PricingState.basis) {
      noteEl.textContent =
        "Per-turn estimate assumes ~" + PricingState.basis.input_tokens +
        " input + " + PricingState.basis.output_tokens + " output tokens (typical conclave turn). " +
        "Subscription seats show no number because they're not billed per-token on this orchestrator.";
    }
    if (statusEl) {
      const parts = ["Click a column header to sort."];
      if (PricingState.cacheAgeSeconds !== null && PricingState.cacheAgeSeconds !== undefined) {
        parts.push("OpenRouter cache age: " + PricingState.cacheAgeSeconds + "s");
      }
      if (data.openrouter_error) {
        parts.push("OpenRouter fetch error: " + data.openrouter_error);
      }
      statusEl.textContent = parts.join(" · ");
    }
    renderPricingTable();
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    tbody.innerHTML = '<tr><td colspan="8" class="loading">Failed to load pricing: ' + escapeHtml(msg) + '</td></tr>';
  }
}

function renderPricingTable() {
  const tbody = $("#pricing-tbody");
  if (!tbody) return;
  const rows = PricingState.items.slice();
  // Sort. Subscription rows (null per_turn_estimate) always sort to the bottom
  // when sorting by a numeric column.
  const key = PricingState.sortKey;
  const dir = PricingState.sortDir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const va = a[key];
    const vb = b[key];
    const aNull = (va === null || va === undefined);
    const bNull = (vb === null || vb === undefined);
    if (aNull && bNull) return 0;
    if (aNull) return 1;   // nulls last regardless of direction
    if (bNull) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });

  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="loading">No agents registered.</td></tr>';
    return;
  }
  for (const it of rows) {
    const tr = el("tr", { class: "pricing-row pricing-row-" + (it.kind || "cli") });
    tr.appendChild(el("td", {}, [el("strong", { text: it.name })]));
    tr.appendChild(el("td", {}, [el("span", { class: "kind-pill kind-" + (it.kind || "cli"), text: it.kind || "—" })]));
    tr.appendChild(el("td", {}, [renderModelInUseCell(it)]));
    tr.appendChild(el("td", { class: "num", text: it.context_length ? Number(it.context_length).toLocaleString() : "—" }));
    tr.appendChild(el("td", { class: "num", text: formatUsdPerMillion(it.input_per_million_usd) }));
    tr.appendChild(el("td", { class: "num", text: formatUsdPerMillion(it.output_per_million_usd) }));
    tr.appendChild(el("td", { class: "num est-cell", text: formatPerTurn(it.per_turn_estimate_usd) }));
    tr.appendChild(el("td", { class: "small muted", text: it.note || "" }));
    tbody.appendChild(tr);
  }
  // Update header sort indicators
  for (const th of $$("#pricing-table thead th")) {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.sort === key) {
      th.classList.add("sorted-" + PricingState.sortDir);
    }
  }
}

// Render the "Model in use" cell, which can show:
//   - The detected slug (from the CLI's own config) + a small "detected" tag
//   - The declared slug (from config.yaml) when there's nothing detected
//   - A drift warning chip when declared and detected disagree
//   - "—" when neither is known (e.g., Gemini in subscription mode)
function renderModelInUseCell(it) {
  const wrap = el("div", { class: "model-cell" });
  if (it.drift && it.declared_model_slug && it.detected_model_slug) {
    // Drift: show detected (load-bearing for pricing) prominently + a warning
    wrap.appendChild(el("div", { class: "model-slug mono small", text: it.detected_model_slug }));
    const drift = el("div", { class: "model-drift-chip", title:
      "Your CLI is set to " + it.detected_model_slug + " (from " + (it.detected_source || "?") + "), " +
      "but config.yaml declares " + it.declared_model_slug + ". Pricing reflects the detected model. " +
      "Run the CLI's model-selection command to align them (e.g., /model in Claude Code) or update config.yaml."
    });
    drift.appendChild(el("span", { class: "model-drift-badge", text: "drift" }));
    drift.appendChild(el("span", {
      class: "model-drift-text",
      text: "declared " + it.declared_model_slug,
    }));
    wrap.appendChild(drift);
  } else if (it.detected_model_slug) {
    wrap.appendChild(el("div", { class: "model-slug mono small", text: it.detected_model_slug }));
    if (it.detected_source) {
      wrap.appendChild(el("div", {
        class: "model-source small muted",
        text: "detected · " + it.detected_source,
      }));
    }
  } else if (it.model_id) {
    // OpenRouter seats — no detection layer needed.
    wrap.appendChild(el("div", { class: "model-slug mono small", text: it.model_id }));
  } else if (it.declared_model_slug) {
    wrap.appendChild(el("div", { class: "model-slug mono small", text: it.declared_model_slug }));
    wrap.appendChild(el("div", { class: "model-source small muted", text: "declared · config.yaml" }));
  } else {
    wrap.appendChild(el("span", { class: "muted", text: "—" }));
  }
  return wrap;
}

function formatUsdPerMillion(v) {
  if (v === null || v === undefined) return "—";
  // Show 2 decimals if >= 1, else 4 decimals for tiny rates.
  return v >= 1 ? "$" + v.toFixed(2) : "$" + v.toFixed(4);
}
function formatPerTurn(v) {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "$0";
  if (v < 0.001) return "$" + v.toFixed(5);
  if (v < 0.1) return "$" + v.toFixed(4);
  return "$" + v.toFixed(3);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function setupPricingTableHeaders() {
  for (const th of $$("#pricing-table thead th")) {
    const key = th.dataset.sort;
    if (!key) continue;
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      if (PricingState.sortKey === key) {
        PricingState.sortDir = (PricingState.sortDir === "asc") ? "desc" : "asc";
      } else {
        PricingState.sortKey = key;
        PricingState.sortDir = (key === "name" || key === "kind" || key === "model_id") ? "asc" : "desc";
      }
      renderPricingTable();
    });
  }
}

// ------------------------------------------------------------
// Help view — lazy-loaded fragment + version metadata badge
// ------------------------------------------------------------
let _helpLoaded = false;
let _helpScrollspyInit = false;

async function openHelp() {
  await ensureHelpLoaded();
  switchView("help");
}

async function ensureHelpLoaded() {
  if (_helpLoaded) return;
  const mount = $("#view-help");
  if (!mount) return;
  try {
    const r = await fetch("/static/help.html", { cache: "no-cache" });
    if (r.ok) {
      mount.innerHTML = await r.text();
      _helpLoaded = true;
      initHelpScrollspy();
      const printBtn = document.getElementById("help-print-btn");
      if (printBtn) printBtn.addEventListener("click", () => window.print());
    } else {
      mount.innerHTML = `<p class="loading">Failed to load help: HTTP ${r.status}</p>`;
    }
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    mount.innerHTML = `<p class="loading">Failed to load help: ${msg}</p>`;
  }
}

async function refreshHelpBadge() {
  if (!_helpLoaded) return;
  const setT = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const badge = document.getElementById("help-doc-currency");
  const showError = (reason) => {
    setT("help-doc-version-tag", "v?");
    setT("help-doc-covered-val", "—");
    setT("help-doc-updated-val", "—");
    setT("help-doc-appver-val", "—");
    if (badge) {
      badge.setAttribute("data-state", "unknown");
      badge.textContent = "Metadata unavailable";
      badge.title = reason + " — try restarting the service or refreshing the page.";
    }
  };
  let r;
  try {
    r = await fetch("/api/help/metadata");
  } catch (e) {
    showError("Network error: " + (e && e.message ? e.message : e));
    return;
  }
  if (!r.ok) {
    showError(`Endpoint returned HTTP ${r.status}` + (r.status === 404 ? " (service may need a restart to pick up the new /api/help/metadata route)" : ""));
    return;
  }
  let m;
  try {
    m = await r.json();
  } catch (e) {
    showError("Could not parse metadata response: " + (e && e.message ? e.message : e));
    return;
  }
  setT("help-doc-version-tag", "v" + m.doc_version);
  setT("help-doc-covered-val", "v" + m.covered_app_version);
  setT("help-doc-updated-val", m.last_updated);
  setT("help-doc-appver-val", "v" + m.app_version);
  if (badge) {
    const labels = {
      current:   { text: "Current",      title: "Documentation is current for this app version (major.minor match)." },
      app_newer: { text: "App is newer", title: `This doc was written for app v${m.covered_app_version}; you're running v${m.app_version}. Some recent features may not be documented yet.` },
      older_app: { text: "Older app",    title: `This doc was written for app v${m.covered_app_version}; you're running an older v${m.app_version}.` },
      unknown:   { text: "Unknown",      title: "Could not determine documentation currency." },
    };
    const info = labels[m.currency] || labels.unknown;
    badge.setAttribute("data-state", m.currency || "unknown");
    badge.textContent = info.text;
    badge.title = info.title;
  }
}

function initHelpScrollspy() {
  if (_helpScrollspyInit) return;
  initHelpSplitter();
  const tocLinks = Array.from(document.querySelectorAll(".help-doc-toc a"));
  if (tocLinks.length === 0) return;

  // Smooth-scroll on TOC click, update URL hash.
  for (const link of tocLinks) {
    link.addEventListener("click", (e) => {
      const href = link.getAttribute("href");
      if (!href || !href.startsWith("#")) return;
      e.preventDefault();
      const target = document.querySelector(href);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        history.replaceState(null, "", href);
      }
    });
  }

  // Scrollspy via IntersectionObserver — highlights nearest section in TOC.
  const linkByHref = new Map();
  for (const l of tocLinks) linkByHref.set(l.getAttribute("href"), l);
  const sections = Array.from(document.querySelectorAll(".help-section, .help-subsection"));
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        const id = "#" + entry.target.id;
        const match = linkByHref.get(id);
        if (match) {
          for (const l of tocLinks) l.classList.remove("active-toc");
          match.classList.add("active-toc");
        }
      }
    }
  }, { rootMargin: "-20% 0px -65% 0px", threshold: 0 });
  for (const s of sections) {
    if (s.id) observer.observe(s);
  }

  // Honor initial hash if user deep-linked.
  if (location.hash) {
    const t = document.querySelector(location.hash);
    if (t) requestAnimationFrame(() => t.scrollIntoView({ behavior: "auto", block: "start" }));
  }

  _helpScrollspyInit = true;
}

// Drag-to-resize the splitter between the help TOC and content.
// Stores the chosen TOC width in localStorage so the layout persists.
const HELP_TOC_WIDTH_KEY = "switchboard.help.toc-width";
const HELP_TOC_MIN = 160;
const HELP_TOC_MAX = 520;

function initHelpSplitter() {
  const splitter = document.getElementById("help-doc-splitter");
  const toc = document.querySelector(".help-doc-toc");
  if (!splitter || !toc) return;

  // Apply saved width if present.
  const saved = parseInt(localStorage.getItem(HELP_TOC_WIDTH_KEY) || "0", 10);
  if (saved >= HELP_TOC_MIN && saved <= HELP_TOC_MAX) {
    toc.style.setProperty("--help-toc-width", saved + "px");
    toc.style.width = saved + "px";
  }

  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  const onMove = (e) => {
    if (!dragging) return;
    const delta = e.clientX - startX;
    let next = startWidth + delta;
    if (next < HELP_TOC_MIN) next = HELP_TOC_MIN;
    if (next > HELP_TOC_MAX) next = HELP_TOC_MAX;
    toc.style.width = next + "px";
    toc.style.setProperty("--help-toc-width", next + "px");
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const w = parseInt(toc.style.width, 10);
    if (!isNaN(w)) localStorage.setItem(HELP_TOC_WIDTH_KEY, String(w));
  };

  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    startX = e.clientX;
    startWidth = toc.getBoundingClientRect().width;
    splitter.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);

  // Keyboard accessibility: arrow keys nudge the TOC width by 16px steps.
  splitter.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const cur = toc.getBoundingClientRect().width;
    let next = cur + (e.key === "ArrowRight" ? 16 : -16);
    if (next < HELP_TOC_MIN) next = HELP_TOC_MIN;
    if (next > HELP_TOC_MAX) next = HELP_TOC_MAX;
    toc.style.width = next + "px";
    toc.style.setProperty("--help-toc-width", next + "px");
    localStorage.setItem(HELP_TOC_WIDTH_KEY, String(next));
    e.preventDefault();
  });
}

// Global `?` keyboard shortcut opens help (ignored while typing in inputs).
function _isTextFocused() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}
function _onGlobalKeydown(e) {
  if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey && !_isTextFocused()) {
    e.preventDefault();
    toggleSidebarItem("help");
  }
}

// ------------------------------------------------------------
// Health badge
// ------------------------------------------------------------
// Per-seat readiness map populated from /api/health.seats.
// Keyed by seat name -> { available: bool, reason: string, hint: string, kind: string }.
// Populated by pollHealth() (which is the single shared /api/health fetcher) and
// re-populated by loadReadiness() when the New Task panel opens.
const _seatReadiness = {};

function _updateSeatReadinessFromHealth(h) {
  if (!h || !Array.isArray(h.seats)) return;
  // Clear and refill so seats that disappear server-side don't linger as stale.
  for (const k of Object.keys(_seatReadiness)) delete _seatReadiness[k];
  for (const s of h.seats) {
    if (s && typeof s.name === "string") {
      _seatReadiness[s.name] = {
        available: !!s.available,
        reason: s.reason || "",
        hint: s.hint || "",
        kind: s.kind || "",
      };
    }
  }
}

async function loadReadiness() {
  // Lightweight refetch invoked when the New Task panel opens. Shares the same
  // endpoint as pollHealth() (server has a 30s cache, so this is cheap), but
  // we re-render the agents list afterwards so indicators update immediately.
  try {
    const h = await Api.health();
    _updateSeatReadinessFromHealth(h);
  } catch (_e) {
    // Soft failure: leave whatever map state we had. The indicator falls back
    // to an "unknown" dot, which is fine — the submit-time guard only blocks
    // when we have a definite available:false signal.
  }
  if (State.view === "new") renderAgentsList();
}

async function pollHealth() {
  const ind = $("#health-indicator");
  let detailMsg = "";
  try {
    const h = await Api.health();
    _updateSeatReadinessFromHealth(h);
    if (h && h.status === "ok") {
      ind.textContent = "healthy";
      ind.className = "health ok";
    } else if (h && h.status === "degraded") {
      ind.textContent = "degraded (why?)";
      ind.className = "health degraded clickable";
      detailMsg = h.error || "Service reported degraded.";
    } else {
      ind.textContent = "unknown";
      ind.className = "health";
    }
    // If we're on the New Task view, refresh the agent indicators to reflect
    // any seat-readiness changes from this poll.
    if (State.view === "new") renderAgentsList();
  } catch (e) {
    ind.textContent = "offline (why?)";
    ind.className = "health down clickable";
    detailMsg = (e && e.message) ? e.message : "Could not reach the service.";
  }
  ind.title = detailMsg || "service health";
  ind.dataset.detail = detailMsg;
  // Hide any previously-expanded detail block when state changes.
  const expanded = document.getElementById("health-detail");
  if (expanded && !detailMsg) expanded.hidden = true;
  // Sync health dot on the Settings rail button.
  const settingsBtn = document.getElementById("rail-settings");
  if (settingsBtn) {
    const cls = ind.className;
    settingsBtn.dataset.health = cls.includes("ok") ? "ok"
      : cls.includes("degraded") ? "degraded"
      : cls.includes("down") ? "down"
      : "unknown";
  }
}

function setupHealthIndicator() {
  const ind = $("#health-indicator");
  if (!ind) return;
  ind.addEventListener("click", () => {
    const msg = ind.dataset.detail || "";
    if (!msg) return;
    let detail = document.getElementById("health-detail");
    if (!detail) {
      detail = document.createElement("div");
      detail.id = "health-detail";
      detail.className = "health-detail";
      ind.insertAdjacentElement("afterend", detail);
    }
    if (detail.hidden || detail.textContent !== msg) {
      detail.textContent = msg;
      detail.hidden = false;
    } else {
      detail.hidden = true;
    }
  });
}

// ------------------------------------------------------------
// New Task view
// ------------------------------------------------------------
async function loadAgents() {
  try {
    const resp = await Api.agents();
    State.agents = Array.isArray(resp.agents) ? resp.agents : [];
  } catch (e) {
    State.agents = [];
    $("#agents-list").innerHTML = "";
    $("#agents-list").appendChild(el("p", { class: "form-status error", text: "Failed to load agents: " + e.message }));
    return;
  }
  renderAgentsList();
}

function renderAgentsList() {
  const container = $("#agents-list");
  container.innerHTML = "";
  if (State.agents.length === 0) {
    container.appendChild(el("p", { class: "muted", text: "No agents available." }));
    return;
  }
  const mode = $("#mode").value;
  const orderMatters = (mode === "consult" || mode === "resolve");
  for (const name of State.agents) {
    const checked = State.selectedAgents.includes(name);
    const position = checked ? State.selectedAgents.indexOf(name) + 1 : 0;
    const isPrimary = orderMatters && checked && position === 1;
    const label = el("label", { class: "agent-check" + (isPrimary ? " primary" : "") });
    const cb = el("input", { type: "checkbox", value: name });
    cb.checked = checked;
    cb.addEventListener("change", () => {
      if (cb.checked) {
        if (!State.selectedAgents.includes(name)) State.selectedAgents.push(name);
      } else {
        State.selectedAgents = State.selectedAgents.filter((a) => a !== name);
      }
      renderAgentsList();
    });
    label.appendChild(cb);
    // Seat readiness indicator (DR0017 follow-on). One small dot to the left of
    // the agent name, sourced from /api/health.seats. Unknown seats render a
    // neutral dot — we don't want to falsely flag an unavailable state when we
    // simply haven't fetched health yet.
    const readiness = _seatReadiness[name];
    let indClass = "seat-indicator";
    let indTitle;
    if (readiness === undefined) {
      indClass += " seat-unknown";
      indTitle = "Readiness unknown — service has not reported yet.";
    } else if (readiness.available) {
      indClass += " seat-ok";
      indTitle = readiness.hint || "Available.";
    } else {
      indClass += " seat-unavail";
      indTitle = readiness.hint || "Currently reported unavailable.";
    }
    const indicator = el("span", { class: indClass, title: indTitle, text: "●" });
    indicator.setAttribute("aria-hidden", "true");
    label.appendChild(indicator);
    // In consult/resolve, prefix with position number so the "first-picked = primary" rule is visible.
    const prefix = (orderMatters && checked) ? " " + position + ". " : " ";
    const primaryTag = isPrimary ? " (primary — first picked)" : "";
    label.appendChild(document.createTextNode(prefix + name + primaryTag));
    container.appendChild(label);
  }
}

function updateModeHint() {
  const mode = $("#mode").value;
  const hints = {
    conclave: "All selected agents deliberate together as peers.",
    consult:  "The first selected agent is the primary; the rest are consultants.",
    resolve:  "The first selected agent resolves the task; consultants are optional.",
  };
  $("#mode-hint").textContent = hints[mode] || "";
  $("#agents-hint").textContent = mode === "conclave"
    ? "Pick two or more agents."
    : "Pick at least one. The first becomes the primary agent.";
}

// ------------------------------------------------------------
// Permissions (New Task form)
// ------------------------------------------------------------
const PERMISSION_KEYS = [
  "can_read_files",
  "can_write_files",
  "can_run_commands",
  "can_access_network",
  "can_install_packages",
  "can_apply_patches",
  "can_read_env_files",
  "can_read_secrets",
];

const DEFAULT_PERMISSIONS = Object.freeze({
  can_read_files: true,
  can_write_files: false,
  can_run_commands: false,
  can_access_network: false,
  can_install_packages: false,
  can_apply_patches: false,
  can_read_env_files: false,
  can_read_secrets: false,
});

function permCheckbox(key) {
  return document.getElementById("perm-" + key);
}

function readPermissions() {
  const out = {};
  for (const k of PERMISSION_KEYS) {
    const cb = permCheckbox(k);
    out[k] = !!(cb && cb.checked);
  }
  return out;
}

function writePermissions(perms) {
  const source = perms || DEFAULT_PERMISSIONS;
  for (const k of PERMISSION_KEYS) {
    const cb = permCheckbox(k);
    if (cb) cb.checked = !!source[k];
  }
  updatePermissionsSummary();
}

function setPermissionsToDefaults() {
  writePermissions(DEFAULT_PERMISSIONS);
}

function permsEqual(a, b) {
  for (const k of PERMISSION_KEYS) {
    if (!!a[k] !== !!b[k]) return false;
  }
  return true;
}

function describePermissions(perms) {
  // Specific named states that take priority over generic counts.
  const base = Object.assign({}, DEFAULT_PERMISSIONS);
  if (permsEqual(perms, base)) return "Permissions: read-only (default)";

  const readPlusEnv = Object.assign({}, base, { can_read_env_files: true });
  if (permsEqual(perms, readPlusEnv)) return "Permissions: read + .env";

  const readPlusSecrets = Object.assign({}, base, { can_read_secrets: true });
  if (permsEqual(perms, readPlusSecrets)) return "Permissions: read + secrets";

  const readEverything = Object.assign({}, base, {
    can_read_env_files: true,
    can_read_secrets: true,
  });
  if (permsEqual(perms, readEverything)) return "Permissions: read everything";

  let n = 0;
  for (const k of PERMISSION_KEYS) if (perms[k]) n += 1;
  return "Permissions: " + n + " enabled";
}

function updatePermissionsSummary() {
  const summary = $("#permissions-summary");
  if (!summary) return;
  summary.textContent = describePermissions(readPermissions());
}

function applyInstallImpliesRule(changedKey) {
  // Pydantic validator: can_install_packages requires can_run_commands AND can_access_network.
  // Mirror that here so the UI can't submit an invalid combination.
  const install = permCheckbox("can_install_packages");
  const run = permCheckbox("can_run_commands");
  const net = permCheckbox("can_access_network");
  if (!install || !run || !net) return;

  if (changedKey === "can_install_packages" && install.checked) {
    run.checked = true;
    net.checked = true;
  } else if (
    install.checked
    && (changedKey === "can_run_commands" || changedKey === "can_access_network")
    && (!run.checked || !net.checked)
  ) {
    // Reverse direction: unchecking a dependency while install is on also drops install.
    install.checked = false;
  }
}

function applyPermissionPreset(preset) {
  const next = Object.assign({}, DEFAULT_PERMISSIONS);
  if (preset === "read_only") {
    // already correct
  } else if (preset === "read_env") {
    next.can_read_env_files = true;
  } else if (preset === "read_all") {
    next.can_read_env_files = true;
    next.can_read_secrets = true;
  }
  writePermissions(next);
}

function setupPermissionsUI() {
  for (const k of PERMISSION_KEYS) {
    const cb = permCheckbox(k);
    if (!cb) continue;
    cb.addEventListener("change", () => {
      applyInstallImpliesRule(k);
      updatePermissionsSummary();
    });
  }
  for (const btn of $$(".permission-preset")) {
    btn.addEventListener("click", () => {
      applyPermissionPreset(btn.dataset.preset);
    });
  }
  updatePermissionsSummary();
}

function readProjectPath() {
  const inp = $("#project-path");
  return inp ? inp.value.trim() : "";
}

function readIncludeSandbox() {
  const cb = $("#include-sandbox");
  return !!(cb && cb.checked);
}

function setProjectPath(value) {
  const inp = $("#project-path");
  if (inp) inp.value = value || "";
}

function setIncludeSandbox(value) {
  const cb = $("#include-sandbox");
  if (cb) cb.checked = !!value;
}

function setSandboxWarning(text) {
  const w = $("#sandbox-warning");
  if (!w) return;
  w.textContent = text || "";
  w.className = "form-status" + (text ? " error" : "");
}

// Live pre-flight: show the sandbox warning as soon as the user creates an
// invalid combination, not only at submit time.
function validateSandboxState() {
  const cb = $("#include-sandbox");
  if (!cb || !cb.checked) {
    setSandboxWarning("");
    return;
  }
  const projectPath = readProjectPath();
  const canReadFiles = $("#perm-can_read_files");
  const missing = [];
  if (!projectPath) missing.push("Project path");
  if (canReadFiles && !canReadFiles.checked) missing.push("can_read_files in Permissions");
  if (missing.length > 0) {
    setSandboxWarning("Sandbox needs: " + missing.join(" + ") + ".");
  } else {
    setSandboxWarning("");
  }
}

function setupProjectSourceUI() {
  const pathInput = $("#project-path");
  const sandboxCb = $("#include-sandbox");
  const permReadCb = $("#perm-can_read_files");
  if (pathInput) pathInput.addEventListener("input", validateSandboxState);
  if (sandboxCb) sandboxCb.addEventListener("change", validateSandboxState);
  if (permReadCb) permReadCb.addEventListener("change", validateSandboxState);
}

function buildPayload(mode, agents, question) {
  const projectPath = readProjectPath();
  const base = {
    protocol_version: "1.0",
    source: "dashboard",
    source_agent: null,
    mode,
    task_type: "general_consultation",
    user_request: question,
    project_path: projectPath || null,
    context: { files: [], error: null, git_diff: null, extra: {} },
    permissions: readPermissions(),
    limits: {
      max_rounds: 5, timeout_seconds: 360, max_seconds: 1200,
      max_context_tokens: null, convergence_threshold: 1.0,
    },
  };

  if (mode === "conclave") {
    base.primary_agent = null;
    base.consultants = agents.slice();
  } else if (mode === "consult") {
    base.primary_agent = agents[0];
    base.consultants = agents.slice(1);
  } else if (mode === "resolve") {
    base.primary_agent = agents[0];
    base.consultants = agents.slice(1);
  }
  return base;
}

// ------------------------------------------------------------
// Attachments (New Task form)
// ------------------------------------------------------------
function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return n + " B";
  const kib = n / 1024;
  if (kib < 1024) return kib.toFixed(kib < 10 ? 2 : 1) + " KiB";
  const mib = kib / 1024;
  return mib.toFixed(mib < 10 ? 2 : 1) + " MiB";
}

function setAttachmentsWarning(text, kind) {
  const w = $("#attachments-warning");
  if (!w) return;
  w.textContent = text || "";
  w.className = "form-status" + (text ? " " + (kind || "error") : "");
}

function renderAttachmentsList() {
  const list = $("#attachments-list");
  if (!list) return;
  list.innerHTML = "";
  if (State.attachments.length === 0) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  for (const att of State.attachments) {
    const pill = el("div", { class: "attachment-pill" });
    pill.appendChild(el("span", { class: "att-name", text: att.file.name, title: att.file.name }));
    pill.appendChild(el("span", { class: "att-size", text: formatBytes(att.file.size) }));
    const removeBtn = el("button", {
      type: "button",
      class: "att-remove",
      title: "Remove",
      "aria-label": "Remove " + att.file.name,
      text: "×",
    });
    removeBtn.addEventListener("click", () => {
      State.attachments = State.attachments.filter((a) => a.id !== att.id);
      renderAttachmentsList();
    });
    pill.appendChild(removeBtn);
    list.appendChild(pill);
  }
}

function addAttachmentFiles(fileList) {
  if (!fileList || fileList.length === 0) return;
  const rejected = [];
  for (const f of Array.from(fileList)) {
    if (f.size > MAX_FILE_BYTES) {
      rejected.push(f.name);
      continue;
    }
    const dup = State.attachments.some((a) =>
      a.file.name === f.name && a.file.size === f.size && a.file.lastModified === f.lastModified);
    if (dup) continue;
    State.attachments.push({
      id: "att_" + Math.random().toString(36).slice(2, 10) + "_" + Date.now().toString(36),
      file: f,
    });
  }
  renderAttachmentsList();
  if (rejected.length > 0) {
    setAttachmentsWarning(
      "Skipped (over 20 MiB): " + rejected.join(", "), "error");
  } else {
    setAttachmentsWarning("", null);
  }
}

function clearAttachments() {
  State.attachments = [];
  const input = $("#attachments-input");
  if (input) input.value = "";
  setAttachmentsWarning("", null);
  renderAttachmentsList();
}

function setupAttachmentsUI() {
  const dz = $("#dropzone");
  const input = $("#attachments-input");
  const browseBtn = $("#browse-btn");
  if (!dz || !input) return;

  browseBtn && browseBtn.addEventListener("click", () => input.click());
  dz.addEventListener("click", (e) => {
    // Avoid double-firing when the inner button is clicked
    if (e.target && (e.target === browseBtn || e.target.closest("button"))) return;
    input.click();
  });
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.click();
    }
  });

  input.addEventListener("change", () => {
    addAttachmentFiles(input.files);
    // Reset native input so the same filename can be re-added after removal
    input.value = "";
  });

  const prevent = (e) => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      prevent(e);
      dz.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    dz.addEventListener(ev, (e) => {
      prevent(e);
      dz.classList.remove("dragover");
    });
  });
  dz.addEventListener("drop", (e) => {
    // Inspect DataTransferItemList first so we can detect a directory drop and
    // route it through the recursive folder-walk path. Falls back to the
    // existing single-file flow when no items are directories.
    const items = e.dataTransfer && e.dataTransfer.items;
    const dirEntries = [];
    const looseFiles = [];
    if (items && items.length) {
      for (let i = 0; i < items.length; i++) {
        const it = items[i];
        if (!it || it.kind !== "file") continue;
        const entry = (typeof it.webkitGetAsEntry === "function") ? it.webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) {
          dirEntries.push(entry);
        } else {
          const f = it.getAsFile();
          if (f) looseFiles.push(f);
        }
      }
    }
    if (dirEntries.length > 0) {
      // Loose files dropped alongside a folder still go through the regular path.
      if (looseFiles.length) addAttachmentFiles(looseFiles);
      // Walk each directory sequentially (each runs its own upload pipeline).
      (async () => {
        for (const d of dirEntries) {
          await handleFolderDrop(d);
        }
      })();
      return;
    }
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) addAttachmentFiles(files);
  });
  // Avoid hijacking page-wide drag operations
  ["dragover", "drop"].forEach((ev) => {
    window.addEventListener(ev, (e) => {
      if (!dz.contains(e.target)) e.preventDefault();
    });
  });
}

// ------------------------------------------------------------
// Folder drop: recursive walk + sequential upload
// ------------------------------------------------------------
function setFolderUploadStatus(text, kind) {
  const el2 = $("#folder-upload-status");
  if (!el2) return;
  el2.textContent = text || "";
  el2.className = "form-status" + (text ? " " + (kind || "muted") : " muted");
}

// Promise wrappers for the callback-based FileSystem API.
function readEntriesPromise(reader) {
  return new Promise((resolve, reject) => {
    reader.readEntries((entries) => resolve(entries), (err) => reject(err));
  });
}

function entryFilePromise(fileEntry) {
  return new Promise((resolve, reject) => {
    fileEntry.file((f) => resolve(f), (err) => reject(err));
  });
}

// Read every entry under a directory (readEntries may chunk and must be called
// repeatedly until it returns an empty list).
async function readAllEntries(dirEntry) {
  const reader = dirEntry.createReader();
  const out = [];
  while (true) {
    let batch;
    try {
      batch = await readEntriesPromise(reader);
    } catch (_) {
      break;
    }
    if (!batch || batch.length === 0) break;
    for (const e of batch) out.push(e);
  }
  return out;
}

// Recursively gather { fileEntry, relPath } pairs under root, applying the
// standard ignore set to both directories and files.
async function gatherFilesUnder(dirEntry, relPrefix) {
  const collected = [];
  const stack = [{ entry: dirEntry, rel: relPrefix || dirEntry.name }];
  while (stack.length > 0) {
    const { entry, rel } = stack.pop();
    const children = await readAllEntries(entry);
    for (const child of children) {
      const childRel = rel + "/" + child.name;
      if (child.isDirectory) {
        if (folderShouldIgnoreDir(child.name)) continue;
        stack.push({ entry: child, rel: childRel });
      } else if (child.isFile) {
        if (folderShouldIgnoreFile(child.name)) continue;
        collected.push({ entry: child, relPath: childRel });
      }
    }
  }
  return collected;
}

async function handleFolderDrop(dirEntry) {
  setAttachmentsWarning("", null);
  setFolderUploadStatus("Scanning " + dirEntry.name + "...", "muted");

  // Top-level sanity guard: if the dropped folder itself has too many direct
  // children, bail out before walking. This catches accidental Downloads/root drops.
  let topLevel;
  try {
    topLevel = await readAllEntries(dirEntry);
  } catch (err) {
    setFolderUploadStatus("Failed to read folder: " + (err && err.message ? err.message : err), "error");
    return;
  }
  if (topLevel.length > FOLDER_TOP_LEVEL_LIMIT) {
    setFolderUploadStatus(
      "Refusing to walk \"" + dirEntry.name + "\": " + topLevel.length +
      " top-level entries (limit " + FOLDER_TOP_LEVEL_LIMIT +
      "). Pick a smaller folder.", "error");
    return;
  }

  // Walk the whole tree applying the ignore set.
  let collected;
  try {
    collected = await gatherFilesUnder(dirEntry, dirEntry.name);
  } catch (err) {
    setFolderUploadStatus("Failed during walk: " + (err && err.message ? err.message : err), "error");
    return;
  }

  if (collected.length === 0) {
    setFolderUploadStatus("No eligible files in " + dirEntry.name + " (all filtered by ignore set).", "muted");
    return;
  }

  const total = collected.length;
  let uploaded = 0;
  let skipped = 0;
  let failed = 0;

  for (let i = 0; i < total; i++) {
    const { entry, relPath } = collected[i];
    let file;
    try {
      file = await entryFilePromise(entry);
    } catch (err) {
      failed++;
      // eslint-disable-next-line no-console
      console.warn("Folder upload: failed to read", relPath, err);
      setFolderUploadStatus(
        "Uploading " + (uploaded + 1) + " of " + total +
        " (skipped " + skipped + (failed ? ", failed " + failed : "") + ")...", "muted");
      continue;
    }
    if (file.size > MAX_FILE_BYTES) {
      skipped++;
      setFolderUploadStatus(
        "Uploading " + (uploaded + 1) + " of " + total +
        " (skipped " + skipped + (failed ? ", failed " + failed : "") + ")...", "muted");
      continue;
    }

    setFolderUploadStatus(
      "Uploading " + (uploaded + 1) + " of " + total +
      " (skipped " + skipped + (failed ? ", failed " + failed : "") + "): " + relPath, "muted");

    try {
      // Tag the File with the relative path so the pill shows the project-relative
      // name rather than just the bare basename. The server gets the basename
      // either way via the multipart form filename field.
      const renamed = new File([file], relPath, { type: file.type, lastModified: file.lastModified });
      const dup = State.attachments.some((a) =>
        a.file.name === renamed.name && a.file.size === renamed.size);
      if (dup) {
        skipped++;
      } else {
        State.attachments.push({
          id: "att_" + Math.random().toString(36).slice(2, 10) + "_" + Date.now().toString(36),
          file: renamed,
        });
        uploaded++;
        renderAttachmentsList();
      }
    } catch (err) {
      failed++;
      // eslint-disable-next-line no-console
      console.warn("Folder upload: failed to queue", relPath, err);
    }

    // Yield so the page stays responsive even on very large folders.
    // eslint-disable-next-line no-await-in-loop
    await new Promise((r) => setTimeout(r, 0));
  }

  const summary = "Folder \"" + dirEntry.name + "\": queued " + uploaded +
    ", skipped " + skipped + (failed ? ", failed " + failed : "") +
    " of " + total + " files.";
  setFolderUploadStatus(summary, "muted");
  // Clear the summary after a short delay so the dropzone returns to a clean state.
  if (handleFolderDrop._timer) clearTimeout(handleFolderDrop._timer);
  handleFolderDrop._timer = setTimeout(() => setFolderUploadStatus("", "muted"), 8000);
}

// ------------------------------------------------------------
// Attach current git diff (button next to Question label)
// ------------------------------------------------------------
function setGitDiffStatus(text, kind) {
  const node = $("#git-diff-status");
  if (!node) return;
  node.textContent = text || "";
  node.className = "form-status" + (text ? " " + (kind || "muted") : "");
}

function updateGitDiffButtonEnabled() {
  const btn = $("#attach-git-diff-btn");
  const caption = $("#git-diff-caption");
  if (!btn) return;
  const projectPath = readProjectPath();
  if (projectPath) {
    btn.disabled = false;
    btn.title = "Click to append `git diff` (uncommitted + staged) of " + projectPath + " to the question.";
    if (caption) {
      caption.innerHTML = "Attaches a <code>git diff</code> of your in-progress changes (uncommitted + staged) to the question.";
      caption.classList.remove("git-diff-caption-disabled");
    }
  } else {
    btn.disabled = true;
    btn.title = "Set Project path above to enable.";
    if (caption) {
      caption.innerHTML = "Attaches a <code>git diff</code> of your in-progress changes (uncommitted + staged) to the question. Set <strong>Project path</strong> above to enable.";
      caption.classList.add("git-diff-caption-disabled");
    }
  }
}

// Best-effort parse of the server's stat_summary to produce a one-line
// "12 files, 87 + / 23 -" headline. Falls back to "" if it can't read it.
function summarizeGitDiffStat(statSummary) {
  if (!statSummary || typeof statSummary !== "string") return "";
  let files = 0;
  let plus = 0;
  let minus = 0;
  // Lines look like "  app/main.py | 4 +-" or "  N files changed, X insertions(+), Y deletions(-)".
  const lines = statSummary.split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    // Per-file row: "path | N <chars>"
    const m = trimmed.match(/^[^|]+\|\s*(\d+)\s*([+\-]*)/);
    if (m) {
      files += 1;
      const chars = m[2] || "";
      for (const ch of chars) {
        if (ch === "+") plus += 1;
        else if (ch === "-") minus += 1;
      }
    }
  }
  if (files === 0) return "";
  return files + " file" + (files === 1 ? "" : "s") + ", " + plus + " + / " + minus + " -";
}

async function onAttachGitDiff() {
  const btn = $("#attach-git-diff-btn");
  if (!btn || btn.disabled) return;
  const projectPath = readProjectPath();
  if (!projectPath) {
    setGitDiffStatus("Set project_path to enable.", "error");
    return;
  }
  btn.disabled = true;
  setGitDiffStatus("Fetching git diff...", "muted");
  try {
    const res = await fetch("/api/git/diff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_path: projectPath, include_staged: true }),
    });
    let body = null;
    try { body = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) {
      const detail = body && (body.detail || body.error || body.message);
      const msg = detail ? (typeof detail === "string" ? detail : JSON.stringify(detail))
                         : (res.status + " " + res.statusText);
      if (res.status === 400) {
        setGitDiffStatus(msg, "error");
      } else {
        setGitDiffStatus("git diff failed: " + msg, "error");
      }
      return;
    }
    // Success: append a separator block + stat_summary + raw diff to the textarea.
    const ta = $("#question");
    if (!ta) return;
    const branch = (body && body.branch) ? body.branch : "?";
    const head = (body && body.head) ? body.head : "?";
    const stat = (body && body.stat_summary) ? body.stat_summary : "";
    const diff = (body && body.diff) ? body.diff : "";
    const sep = "\n\n=== Current git diff (branch: " + branch + ", head: " + head + ") ===\n";
    ta.value = (ta.value || "") + sep + stat + "\n\n" + diff;
    // Bring the appended block into view.
    ta.scrollTop = ta.scrollHeight;

    const headline = summarizeGitDiffStat(stat);
    const bytes = (body && Number.isFinite(body.diff_bytes)) ? body.diff_bytes : (diff ? diff.length : 0);
    const sizeStr = formatBytes(bytes);
    const msg = headline
      ? "Attached git diff: " + headline + " (" + sizeStr + ")"
      : "Attached diff (" + sizeStr + ")";
    setGitDiffStatus(msg, "ok");
  } catch (err) {
    setGitDiffStatus("git diff failed: " + (err && err.message ? err.message : String(err)), "error");
  } finally {
    // Always re-enable, respecting current project_path state.
    updateGitDiffButtonEnabled();
  }
}

function setupGitDiffUI() {
  const btn = $("#attach-git-diff-btn");
  if (btn) btn.addEventListener("click", onAttachGitDiff);
  const pathInput = $("#project-path");
  if (pathInput) {
    pathInput.addEventListener("input", updateGitDiffButtonEnabled);
  }
  updateGitDiffButtonEnabled();
}

async function onSubmitNewTask(ev) {
  ev.preventDefault();
  const status = $("#form-status");
  const btn = $("#submit-btn");
  status.className = "form-status";
  status.textContent = "";
  setSandboxWarning("");

  const mode = $("#mode").value;
  const question = $("#question").value.trim();
  const agents = State.selectedAgents.slice();
  const projectPath = readProjectPath();
  const includeSandbox = readIncludeSandbox();

  if (!question) {
    status.className = "form-status error";
    status.textContent = "Please enter a question.";
    return;
  }
  if (agents.length === 0) {
    status.className = "form-status error";
    status.textContent = "Select at least one agent.";
    return;
  }
  if (mode === "conclave" && agents.length < 2) {
    status.className = "form-status error";
    status.textContent = "Conclave mode requires at least two agents.";
    return;
  }
  if (mode === "consult" && agents.length < 2) {
    status.className = "form-status error";
    status.textContent = "Consult mode requires a primary plus at least one consultant.";
    return;
  }

  // Seat-readiness soft gate (DR0017 follow-on). If any selected agent's seat
  // is currently reported unavailable, confirm before submitting. The dashboard
  // does NOT disable unavailable checkboxes — the user may have just installed
  // the CLI and the 30s server cache hasn't refreshed yet — but we surface the
  // warning here so accidental submissions are caught. Multiple unavailable
  // seats are collapsed into a single confirm() so the user gets one decision.
  const unavailableSelected = [];
  for (const a of agents) {
    const r = _seatReadiness[a];
    if (r && r.available === false) {
      unavailableSelected.push({ name: a, hint: r.hint || r.reason || "no further detail" });
    }
  }
  if (unavailableSelected.length > 0) {
    const lines = unavailableSelected.map((s) => s.name + " is currently reported unavailable: " + s.hint);
    const msg = lines.join("\n\n") + "\n\nSubmit anyway?";
    // Browser confirm() — intentionally a native dialog per the brief; keeps
    // this change small and avoids a custom in-page modal.
    if (!window.confirm(msg)) {
      status.className = "form-status";
      status.textContent = "";
      return;
    }
  }

  // Sandbox cross-field checks: must have a project_path, and can_read_files must be on.
  if (includeSandbox) {
    if (!projectPath) {
      setSandboxWarning("Set project_path before enabling the sandbox");
      status.className = "form-status error";
      status.textContent = "Fix the sandbox configuration before submitting.";
      return;
    }
    const perms = readPermissions();
    if (!perms.can_read_files) {
      setSandboxWarning("Enable can_read_files in Permissions before using the sandbox");
      status.className = "form-status error";
      status.textContent = "Fix the sandbox configuration before submitting.";
      return;
    }
  }

  const payload = buildPayload(mode, agents, question);

  // Flag the sandbox request in context.extra when all preconditions are met.
  if (includeSandbox && projectPath && payload.permissions.can_read_files) {
    if (!isPlainObject(payload.context.extra)) payload.context.extra = {};
    payload.context.extra.include_sandbox = true;
  }

  btn.disabled = true;
  try {
    // Step 1: upload any attachments
    const uploaded = [];
    const total = State.attachments.length;
    if (total > 0) {
      for (let i = 0; i < total; i++) {
        const att = State.attachments[i];
        status.className = "form-status";
        status.textContent = `Uploading ${i + 1} of ${total}: ${att.file.name}`;
        try {
          const result = await Api.uploadFile(att.file);
          uploaded.push({
            file_id: result.file_id,
            filename: result.filename,
            mime_type: result.mime_type,
          });
        } catch (uerr) {
          status.className = "form-status error";
          status.textContent = `Upload failed for ${att.file.name}: ${uerr.message}`;
          btn.disabled = false;
          return;
        }
      }
      if (!isPlainObject(payload.context.extra)) payload.context.extra = {};
      payload.context.extra.attachments = uploaded;
    }

    // Include parent_task_id if the user came in via "Continue this thread".
    if (State.followupParentId) {
      payload.parent_task_id = State.followupParentId;
    }

    // Step 2: create the task
    status.className = "form-status";
    status.textContent = "Submitting...";
    const resp = await Api.createTask(payload);
    status.className = "form-status ok";
    status.textContent = "Created " + resp.task_id;
    State.currentTaskId = resp.task_id;
    State.currentTaskData = null;
    // Reset the form and clear the parent linkage now that it's been consumed.
    $("#question").value = "";
    clearAttachments();
    clearFollowupParent();
    openDetail(resp.task_id);
  } catch (e) {
    status.className = "form-status error";
    status.textContent = "Submit failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------
// Inbox view
// ------------------------------------------------------------
function startInboxPolling() {
  stopInboxPolling();
  State.inboxTimer = setInterval(refreshInbox, 5000);
}
function stopInboxPolling() {
  if (State.inboxTimer) { clearInterval(State.inboxTimer); State.inboxTimer = null; }
}

async function refreshInbox() {
  const tbody = $("#tasks-tbody");
  try {
    const resp = await Api.listTasks({
      status: State.inboxFilters.status || undefined,
      limit: State.inboxLimit,
      exported: State.inboxFilters.exported || undefined,
      q: (State.inboxFilters.search || "").trim() || undefined,
    });
    const tasks = Array.isArray(resp.tasks) ? resp.tasks : [];
    tasks.sort((a, b) => {
      const ad = a.created_at || "";
      const bd = b.created_at || "";
      return bd.localeCompare(ad);
    });
    State.inboxRawTasks = tasks;
    renderInbox();
  } catch (e) {
    tbody.innerHTML = "";
    tbody.appendChild(el("tr", {}, [
      el("td", { colspan: 5, class: "form-status error", text: "Failed to load tasks: " + e.message }),
    ]));
    State.inboxRawTasks = [];
    updateInboxCounter(0, 0);
  }
}

// Render the inbox table from State.inboxRawTasks, applying the client-side
// mode + search filters. Called by refreshInbox after a server fetch and also
// directly when only client-side filters change (no refetch needed).
function renderInbox() {
  const tbody = $("#tasks-tbody");
  if (!tbody) return;
  const all = Array.isArray(State.inboxRawTasks) ? State.inboxRawTasks : [];
  const modeFilter = (State.inboxFilters.mode || "").toLowerCase();
  // Search is server-side (id + user_request + user_decision + final_answer);
  // see /api/tasks?q=… in app/api/tasks.py. We don't filter here.

  const filtered = all.filter((t) => {
    if (modeFilter && (t.mode || "").toLowerCase() !== modeFilter) return false;
    return true;
  });

  tbody.innerHTML = "";
  if (filtered.length === 0) {
    const emptyText = all.length === 0 ? "No tasks yet." : "No tasks match the current filters.";
    tbody.appendChild(el("tr", {}, [
      el("td", { colspan: 5, class: "muted", text: emptyText }),
    ]));
    updateInboxCounter(0, all.length);
    return;
  }
  for (const t of filtered) {
    const agentsList = [];
    if (t.primary_agent) agentsList.push(t.primary_agent);
    if (Array.isArray(t.consultants)) {
      for (const c of t.consultants) if (c && !agentsList.includes(c)) agentsList.push(c);
    }
    const isExported = !!t.exported_at;
    const trAttrs = { title: t.id };
    if (isExported) trAttrs.class = "exported-row";
    const tr = el("tr", trAttrs);
    tr.addEventListener("click", () => openDetail(t.id));
    const idCell = el("td", { class: "id-cell" });
    idCell.appendChild(document.createTextNode(shortId(t.id)));
    const fullId = t.id;
    idCell.appendChild(makeCopyButton(fullId, "task ID",
      { extraClass: "copy-btn-inline" }));
    if (isExported) {
      const exportedLabel = "Exported at " + fmtTime(t.exported_at)
        + (t.export_path ? " (" + t.export_path + ")" : "");
      idCell.appendChild(el("span", {
        class: "export-dot",
        title: exportedLabel,
        "aria-label": exportedLabel,
      }));
    }
    tr.appendChild(idCell);
    tr.appendChild(el("td", {}, [statusBadge(t.status)]));
    tr.appendChild(el("td", {}, [modeBadge(t.mode)]));
    tr.appendChild(el("td", { text: agentsList.join(", ") || "-" }));
    tr.appendChild(el("td", { text: fmtTime(t.created_at) }));
    tbody.appendChild(tr);
  }
  updateInboxCounter(filtered.length, all.length);
}

function updateInboxCounter(shown, total) {
  const counter = $("#inbox-counter");
  if (!counter) return;
  counter.textContent = "Showing " + shown + " of " + total + " tasks";
}

function loadInboxLimitFromStorage() {
  try {
    const raw = localStorage.getItem(INBOX_LIMIT_KEY);
    const n = parseInt(raw, 10);
    if (Number.isFinite(n) && INBOX_LIMIT_CHOICES.includes(n)) {
      State.inboxLimit = n;
    }
  } catch (_) { /* localStorage may be unavailable */ }
}

function saveInboxLimitToStorage(n) {
  try { localStorage.setItem(INBOX_LIMIT_KEY, String(n)); } catch (_) { /* ignore */ }
}

function setupInboxFiltersUI() {
  loadInboxLimitFromStorage();

  const statusSel = $("#filter-status");
  const modeSel = $("#filter-mode");
  const exportedSel = $("#filter-exported");
  const searchInput = $("#filter-search");
  const limitSel = $("#filter-limit");
  const clearBtn = $("#inbox-clear-btn");
  const bulkExportBtn = $("#inbox-bulk-export-btn");

  if (limitSel) {
    limitSel.value = String(State.inboxLimit);
    limitSel.addEventListener("change", () => {
      const n = parseInt(limitSel.value, 10);
      if (Number.isFinite(n) && INBOX_LIMIT_CHOICES.includes(n)) {
        State.inboxLimit = n;
        saveInboxLimitToStorage(n);
        refreshInbox();
      }
    });
  }

  if (statusSel) {
    statusSel.value = State.inboxFilters.status;
    statusSel.addEventListener("change", () => {
      State.inboxFilters.status = statusSel.value;
      // Status is a server-side filter, so refetch.
      refreshInbox();
    });
  }

  if (modeSel) {
    modeSel.value = State.inboxFilters.mode;
    modeSel.addEventListener("change", () => {
      State.inboxFilters.mode = modeSel.value;
      // Client-side filter, no refetch needed.
      renderInbox();
    });
  }

  if (exportedSel) {
    exportedSel.value = State.inboxFilters.exported;
    exportedSel.addEventListener("change", () => {
      State.inboxFilters.exported = exportedSel.value;
      // Server-side filter (passed as ?exported=true|false), so refetch.
      refreshInbox();
    });
  }

  if (bulkExportBtn) {
    bulkExportBtn.addEventListener("click", onBulkExportUnexported);
  }

  if (searchInput) {
    searchInput.value = State.inboxFilters.search;
    searchInput.addEventListener("input", () => {
      // Debounce ~250ms so each keystroke doesn't fire a server request.
      if (State.inboxSearchDebounce) clearTimeout(State.inboxSearchDebounce);
      State.inboxSearchDebounce = setTimeout(() => {
        State.inboxFilters.search = searchInput.value;
        // Search runs server-side now (id + user_request + user_decision +
        // final_answer) so we must refetch, not just re-render.
        refreshInbox();
      }, 250);
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      State.inboxFilters.status = "";
      State.inboxFilters.mode = "";
      State.inboxFilters.search = "";
      State.inboxFilters.exported = "";
      if (statusSel) statusSel.value = "";
      if (modeSel) modeSel.value = "";
      if (exportedSel) exportedSel.value = "";
      if (searchInput) searchInput.value = "";
      if (State.inboxSearchDebounce) {
        clearTimeout(State.inboxSearchDebounce);
        State.inboxSearchDebounce = null;
      }
      // Clearing status changes the server query, so refetch.
      refreshInbox();
    });
  }
}

// ------------------------------------------------------------
// Bulk export (inbox)
// ------------------------------------------------------------
function setBulkExportStatus(text, kind) {
  const node = $("#inbox-bulk-export-status");
  if (!node) return;
  if (!text) {
    node.hidden = true;
    node.textContent = "";
    node.className = "inbox-bulk-export-status";
    return;
  }
  node.hidden = false;
  node.textContent = text;
  node.className = "inbox-bulk-export-status" + (kind ? " " + kind : "");
}

function setBulkExportErrors(errors) {
  const node = $("#inbox-bulk-export-errors");
  if (!node) return;
  node.innerHTML = "";
  if (!Array.isArray(errors) || errors.length === 0) {
    node.hidden = true;
    return;
  }
  const head = el("div", { text: "First errors:" });
  node.appendChild(head);
  const ul = el("ul");
  for (const err of errors.slice(0, 3)) {
    let line;
    if (typeof err === "string") {
      line = err;
    } else if (err && typeof err === "object") {
      const id = err.task_id ? err.task_id + ": " : "";
      const msg = err.error || err.message || err.detail || JSON.stringify(err);
      line = id + msg;
    } else {
      line = String(err);
    }
    ul.appendChild(el("li", { text: line }));
  }
  node.appendChild(ul);
  node.hidden = false;
}

async function onBulkExportUnexported() {
  // Render an inline confirm UI (no native confirm() dialog). The actual work
  // runs only after the user clicks Continue.
  const statusEl = $("#inbox-bulk-export-status");
  if (!statusEl) return;
  setBulkExportErrors([]);
  statusEl.hidden = false;
  statusEl.className = "inbox-bulk-export-status confirming";
  statusEl.innerHTML = "";
  const msg = document.createElement("span");
  msg.textContent = "Write a markdown decision record to data/exports/ for every unexported completed / failed / cancelled task? ";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "btn btn-primary";
  yes.textContent = "Continue";
  yes.style.cssText = "padding: 3px 12px; margin-left: 6px; font-size: 12px;";
  const no = document.createElement("button");
  no.type = "button";
  no.className = "btn btn-secondary";
  no.textContent = "Cancel";
  no.style.cssText = "padding: 3px 12px; margin-left: 4px; font-size: 12px;";
  no.addEventListener("click", () => {
    statusEl.hidden = true;
    statusEl.innerHTML = "";
    statusEl.className = "inbox-bulk-export-status";
  });
  yes.addEventListener("click", () => {
    statusEl.innerHTML = "";
    statusEl.className = "inbox-bulk-export-status";
    _runBulkExport();
  });
  statusEl.appendChild(msg);
  statusEl.appendChild(yes);
  statusEl.appendChild(no);
}

async function _runBulkExport() {
  const btn = $("#inbox-bulk-export-btn");
  setBulkExportStatus("Exporting...", null);
  if (btn) {
    btn.disabled = true;
    btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
    btn.textContent = "Exporting...";
  }
  try {
    // Empty body uses the default filter=unexported_terminal on the server.
    const resp = await Api.exportBatchTasks({});
    const exported = Number(resp && resp.exported_count) || 0;
    const skipped = Number(resp && resp.skipped_count) || 0;
    const errs = Number(resp && resp.error_count) || 0;
    const summary = "Exported " + exported + " task" + (exported === 1 ? "" : "s")
      + ". " + skipped + " skipped, " + errs + " error" + (errs === 1 ? "" : "s") + ".";
    setBulkExportStatus(summary, errs > 0 ? "error" : "ok");
    if (errs > 0 && Array.isArray(resp.errors)) {
      setBulkExportErrors(resp.errors);
    } else {
      setBulkExportErrors([]);
    }
    // Refresh the inbox so freshly-exported tasks show their exported_at value.
    await refreshInbox();
  } catch (e) {
    setBulkExportStatus("Bulk export failed: " + (e && e.message ? e.message : String(e)),
      "error");
    setBulkExportErrors([]);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || "Bulk export unexported";
    }
  }
}

function statusBadge(status) {
  const safe = (status || "unknown").toLowerCase();
  return el("span", { class: "badge status-" + safe, text: safe });
}
function modeBadge(mode) {
  const safe = (mode || "").toLowerCase();
  return el("span", { class: "badge mode-" + safe, text: safe || "?" });
}
function roleBadge(role) {
  return el("span", { class: "badge role", text: role || "" });
}

// ------------------------------------------------------------
// Detail view
// ------------------------------------------------------------
function openDetail(taskId) {
  State.currentTaskId = taskId;
  State.currentTaskData = null;
  State.decisionEditing = false;
  State.decisionDraft = "";
  // Hide any stale breadcrumb from a prior task until the new fetch resolves.
  hideThreadBreadcrumb();
  // Drop any live activity ticker from the prior task until the new fetch resolves.
  hideLiveActivity();
  // Drop any export feedback from a prior task so it doesn't bleed across views.
  hideExportFeedback();
  switchView("detail");
}

function startDetailPolling() {
  stopDetailPolling();
  State.detailTimer = setInterval(() => {
    const data = State.currentTaskData;
    if (!data) { refreshDetail(); return; }
    const status = data.task && data.task.status;
    if (status && !State.terminalStatuses.has(status)) refreshDetail();
  }, 3000);
}
function stopDetailPolling() {
  if (State.detailTimer) { clearInterval(State.detailTimer); State.detailTimer = null; }
  // The live activity ticker is only meaningful while the detail view is active.
  stopLiveTicker();
}

async function refreshDetail() {
  if (!State.currentTaskId) return;
  try {
    const data = await Api.getTask(State.currentTaskId);
    State.currentTaskData = data;
    renderDetail(data);
    // Fetch thread ancestry only when this task is part of a thread.
    const task = data.task || {};
    if (task.parent_task_id) {
      refreshThreadBreadcrumb(State.currentTaskId);
    } else {
      hideThreadBreadcrumb();
    }
  } catch (e) {
    const header = $("#detail-header");
    header.innerHTML = "";
    header.appendChild(el("p", { class: "form-status error", text: "Failed to load task: " + e.message }));
    $("#detail-body").hidden = true;
  }
}

async function refreshThreadBreadcrumb(taskId) {
  try {
    const resp = await Api.getThread(taskId);
    // Guard against stale responses if the user has navigated away.
    if (State.currentTaskId !== taskId) return;
    State.threadCache[taskId] = resp;
    renderThreadBreadcrumb(resp);
  } catch (_) {
    // Non-fatal: just hide the breadcrumb if the thread fetch fails.
    hideThreadBreadcrumb();
  }
}

function hideThreadBreadcrumb() {
  const section = $("#thread-breadcrumb");
  if (section) section.hidden = true;
  const chain = $("#thread-breadcrumb-chain");
  if (chain) chain.innerHTML = "";
}

function renderThreadBreadcrumb(resp) {
  const section = $("#thread-breadcrumb");
  const chain = $("#thread-breadcrumb-chain");
  if (!section || !chain) return;
  const thread = Array.isArray(resp && resp.thread) ? resp.thread : [];
  // Only show when there's at least one ancestor in addition to the current task.
  if (thread.length <= 1) {
    hideThreadBreadcrumb();
    return;
  }
  chain.innerHTML = "";
  const currentId = (resp && resp.task_id) || State.currentTaskId;
  thread.forEach((entry, idx) => {
    if (idx > 0) {
      chain.appendChild(el("span", { class: "thread-arrow", "aria-hidden": "true", text: "→" }));
    }
    const isCurrent = entry.id === currentId;
    const preview = (entry.user_request || "").replace(/\s+/g, " ").trim().slice(0, 80);
    const tooltip = entry.id + (preview ? " — " + preview : "");
    if (isCurrent) {
      chain.appendChild(el("span", {
        class: "thread-pill current",
        title: tooltip,
        text: shortId(entry.id),
      }));
    } else {
      const pill = el("button", {
        type: "button",
        class: "thread-pill",
        title: tooltip,
        text: shortId(entry.id),
      });
      pill.addEventListener("click", () => openDetail(entry.id));
      chain.appendChild(pill);
    }
  });
  section.hidden = false;
}

function renderDetail(data) {
  const task = data.task || {};
  const messages = Array.isArray(data.messages) ? data.messages : [];
  const finalResult = data.final_result || null;
  // agent_runs is the new backend envelope key; may be undefined for older
  // tasks/API responses. Treat anything non-array as "not available" so the
  // live activity + usage features degrade gracefully.
  const agentRuns = Array.isArray(data.agent_runs) ? data.agent_runs : null;

  // Header
  const header = $("#detail-header");
  header.innerHTML = "";
  const fullTaskId = task.id || State.currentTaskId || "";
  const idRow = el("div", { class: "detail-id-row" });
  idRow.appendChild(el("span", { class: "detail-id-code", text: fullTaskId }));
  if (fullTaskId) {
    idRow.appendChild(makeCopyButton(fullTaskId, "task ID",
      { extraClass: "copy-btn-prominent" }));
  }
  const titleRow = el("div", { class: "detail-meta" }, [idRow]);
  const metaRow = el("div", { class: "detail-meta" });
  metaRow.appendChild(statusBadge(task.status));
  metaRow.appendChild(modeBadge(task.mode));
  if (task.task_type) {
    metaRow.appendChild(el("span", { class: "muted", text: "type: " + task.task_type }));
  }
  const agents = [];
  if (task.primary_agent) agents.push(task.primary_agent + " (primary)");
  if (Array.isArray(task.consultants)) for (const c of task.consultants) agents.push(c);
  if (agents.length) metaRow.appendChild(el("span", { class: "muted", text: "agents: " + agents.join(", ") }));
  if (task.created_at) metaRow.appendChild(el("span", { class: "muted", text: "created: " + fmtTime(task.created_at) }));
  if (task.updated_at) metaRow.appendChild(el("span", { class: "muted", text: "updated: " + fmtTime(task.updated_at) }));
  header.appendChild(titleRow);
  header.appendChild(metaRow);
  if (task.error_message) {
    header.appendChild(el("p", { class: "form-status error", text: "error: " + task.error_message }));
  }

  // Usage summary (terminal tasks only). Gracefully omitted when there is no data.
  const usageNode = renderUsageSummary(task, agentRuns);
  if (usageNode) header.appendChild(usageNode);

  $("#detail-body").hidden = false;

  // Live activity panel (running/pending tasks only).
  renderLiveActivity(task, agentRuns);

  // User request
  const userRequestText = task.user_request || "";
  $("#detail-user-request").textContent = userRequestText;
  const userReqSlot = $("#user-request-copy-slot");
  if (userReqSlot) {
    userReqSlot.innerHTML = "";
    if (userRequestText) {
      userReqSlot.appendChild(makeCopyButton(userRequestText, "user request"));
    }
  }

  // Prior Art panel — Phase 2.5 of post-DR plan. Shows the past decision records
  // the keeper surfaced as relevant when this task was created. Frozen — what the
  // agents actually saw, not what they'd see today.
  renderPriorArt(task.prior_art || []);

  // Answer prompt if awaiting user input
  renderAnswerPrompt(task, messages);

  // Transcript
  renderTranscript(task, messages, agentRuns);

  // Final result
  renderFinalResult(finalResult);

  // Draft artifacts
  renderArtifactPanel(task, Array.isArray(data.artifacts) ? data.artifacts : []);

  // Errors
  renderErrors(finalResult);

  // Cancel button visibility
  const cancelBtn = $("#cancel-btn");
  if (task.status && !State.terminalStatuses.has(task.status)) {
    cancelBtn.hidden = false;
  } else {
    cancelBtn.hidden = true;
  }

  // Decision panel (terminal status only) - rendered before the post-task bar
  renderDecisionPanel(task, messages);

  // Post-task action bar (terminal status only)
  const postBar = $("#post-task-bar");
  if (postBar) {
    if (task.status && State.terminalStatuses.has(task.status)) {
      postBar.hidden = false;
      // Reflect whether this task has already been exported in the button label
      // and the small muted "Last exported" hint.
      updateExportButtonState(task);
    } else {
      postBar.hidden = true;
      // Drop any stale export feedback if the bar is being hidden.
      hideExportFeedback();
      updateExportButtonState(null);
    }
  }
}

// Update the export button label + auxiliary "Last exported" hint based on
// whether task.exported_at is set. Called from renderDetail and after a
// successful single-task export so the UI reflects the new state immediately.
function updateExportButtonState(task) {
  const btn = $("#export-btn");
  const info = $("#export-last-info");
  if (!btn) return;

  const exportedAt = task && task.exported_at ? task.exported_at : null;
  const exportPath = task && task.export_path ? task.export_path : "";

  if (exportedAt) {
    btn.textContent = "Re-export";
    btn.dataset.originalText = "Re-export";
    btn.title = "Re-export this task to data/exports/, overwriting the previous file.";
    if (info) {
      info.hidden = false;
      info.innerHTML = "";
      const shortPath = exportPath
        ? (exportPath.length > 48
            ? "..." + exportPath.slice(exportPath.length - 45)
            : exportPath)
        : "";
      let label = "Last exported: " + fmtTime(exportedAt);
      if (shortPath) label += " at ";
      info.appendChild(document.createTextNode(label));
      if (exportPath) {
        const code = el("code", { text: shortPath, title: exportPath });
        info.appendChild(code);
        // Reuse the standard copy button factory so users can grab the full path.
        info.appendChild(makeCopyButton(exportPath, "export path"));
      }
    }
  } else {
    btn.textContent = "Export to decision record";
    btn.dataset.originalText = "Export to decision record";
    btn.title = "Export this task's question, transcript, final result, and decision to a markdown file in data/exports/.";
    if (info) {
      info.hidden = true;
      info.innerHTML = "";
    }
  }
}

// ------------------------------------------------------------
// Live activity panel (Feature A)
// ------------------------------------------------------------
function stopLiveTicker() {
  if (State.liveTickerTimer) {
    clearInterval(State.liveTickerTimer);
    State.liveTickerTimer = null;
  }
  State.liveTickerStartMs = null;
}

function startLiveTicker(startMs) {
  stopLiveTicker();
  if (!Number.isFinite(startMs)) return;
  State.liveTickerStartMs = startMs;
  State.liveTickerTimer = setInterval(() => {
    const target = document.getElementById("live-activity-elapsed");
    if (!target || !Number.isFinite(State.liveTickerStartMs)) return;
    const elapsed = Date.now() - State.liveTickerStartMs;
    target.textContent = fmtDurationMs(elapsed) + " ago";
  }, 1000);
}

function hideLiveActivity() {
  stopLiveTicker();
  const section = $("#live-activity");
  const inner = $("#live-activity-inner");
  if (inner) inner.innerHTML = "";
  if (section) section.hidden = true;
}

function findActiveRun(runs) {
  // Most recent "running" + no finished_at; prefer the one with the latest started_at.
  if (!Array.isArray(runs) || runs.length === 0) return null;
  let best = null;
  let bestT = -Infinity;
  for (const r of runs) {
    if (!r || r.status !== "running" || r.finished_at) continue;
    const t = parseIsoMs(r.started_at) || 0;
    if (t >= bestT) { best = r; bestT = t; }
  }
  return best;
}

function maxRoundNumber(runs) {
  if (!Array.isArray(runs) || runs.length === 0) return null;
  let m = null;
  for (const r of runs) {
    if (!r) continue;
    const n = Number(r.round_number);
    if (Number.isFinite(n) && (m === null || n > m)) m = n;
  }
  return m;
}

function recentFinishedRuns(runs, limit) {
  if (!Array.isArray(runs) || runs.length === 0) return [];
  const finished = runs.filter((r) => r && r.finished_at);
  finished.sort((a, b) => {
    const at = parseIsoMs(a.finished_at) || 0;
    const bt = parseIsoMs(b.finished_at) || 0;
    return bt - at;
  });
  return finished.slice(0, limit || 5);
}

function renderLiveActivity(task, agentRuns) {
  const section = $("#live-activity");
  const inner = $("#live-activity-inner");
  if (!section || !inner) return;

  const status = task && task.status;
  // Hide for terminal statuses or awaiting_user_input (the answer form takes over).
  if (!status || status === "awaiting_user_input"
      || State.terminalStatuses.has(status)) {
    hideLiveActivity();
    return;
  }

  // agent_runs may be missing on older API responses; if pending status, still show
  // a "waiting" line. If running but no runs array at all, gracefully omit the panel.
  const runsAvailable = Array.isArray(agentRuns);

  if (status === "pending") {
    stopLiveTicker();
    inner.innerHTML = "";
    inner.appendChild(el("div", { class: "live-activity-row" }, [
      el("span", { class: "live-pulse-dot", "aria-hidden": "true" }),
      el("span", { class: "live-activity-main",
        text: "Waiting for worker to claim..." }),
    ]));
    section.hidden = false;
    return;
  }

  if (status !== "running") {
    hideLiveActivity();
    return;
  }

  if (!runsAvailable) {
    // Older API: gracefully omit instead of breaking.
    hideLiveActivity();
    return;
  }

  inner.innerHTML = "";

  const active = findActiveRun(agentRuns);
  const round = maxRoundNumber(agentRuns);

  // Active row
  const row = el("div", { class: "live-activity-row" });
  row.appendChild(el("span", { class: "live-pulse-dot", "aria-hidden": "true" }));

  if (active) {
    const agentName = active.agent_name || "?";
    const roundN = Number.isFinite(Number(active.round_number)) ? Number(active.round_number) : null;
    const headText = "Calling " + agentName + (roundN !== null ? " (round " + roundN + ")" : "");
    row.appendChild(el("span", { class: "live-activity-main", text: headText }));
    row.appendChild(el("span", { class: "live-activity-sep", text: "—" }));
    row.appendChild(el("span", { class: "live-activity-prefix", text: "started" }));
    const startedMs = parseIsoMs(active.started_at);
    const elapsedText = startedMs !== null
      ? (fmtDurationMs(Date.now() - startedMs) + " ago")
      : "just now";
    row.appendChild(el("span", {
      id: "live-activity-elapsed",
      class: "live-activity-elapsed",
      text: elapsedText,
    }));
    if (startedMs !== null) startLiveTicker(startedMs);
    else stopLiveTicker();
  } else {
    // No active run: distinguish "haven't started any agent yet" (prep / first
    // dispatch) from "between rounds" (some finished runs already on file).
    const noRunsYet = agentRuns.length === 0;
    const text = noRunsYet
      ? "Preparing the task (sandbox + prompt) and dispatching to agents…"
      : "Between rounds…";
    row.appendChild(el("span", { class: "live-activity-main", text }));
    stopLiveTicker();
    // For the prep phase, show an elapsed counter from task.created_at so the
    // user can see something is moving rather than feeling stuck.
    if (noRunsYet && task.created_at) {
      const createdMs = parseIsoMs(task.created_at);
      if (createdMs !== null) {
        row.appendChild(el("span", { class: "live-activity-sep", text: "—" }));
        row.appendChild(el("span", { class: "live-activity-prefix", text: "elapsed" }));
        row.appendChild(el("span", {
          id: "live-activity-elapsed", class: "live-activity-elapsed",
          text: fmtDurationMs(Date.now() - createdMs),
        }));
        startLiveTicker(createdMs);
      }
    }
  }
  inner.appendChild(row);

  // Round progress line
  if (round !== null) {
    inner.appendChild(el("div", {
      class: "live-activity-round",
      text: "Round " + round + " in progress",
    }));
  }

  // Recent activity summary (last 3-5 finished runs)
  const recent = recentFinishedRuns(agentRuns, 5);
  if (recent.length > 0) {
    const recentWrap = el("div", { class: "live-activity-recent" });
    recentWrap.appendChild(el("div", {
      class: "live-activity-recent-label",
      text: "Recent activity",
    }));
    const list = el("ul", { class: "live-activity-recent-list" });
    for (const r of recent) {
      const agentName = r.agent_name || "?";
      const role = r.role || "";
      const rn = Number.isFinite(Number(r.round_number)) ? Number(r.round_number) : null;
      const dur = Number.isFinite(Number(r.duration_ms)) ? Number(r.duration_ms) : null;
      let suffix;
      if (r.status === "completed") {
        suffix = dur !== null ? "completed in " + fmtDurationMs(dur) : "completed";
      } else if (r.status === "failed") {
        suffix = "failed" + (r.error_code ? " (" + r.error_code + ")" : "");
      } else if (r.status === "timed_out") {
        suffix = "timed out" + (dur !== null ? " after " + fmtDurationMs(dur) : "");
      } else {
        suffix = r.status || "finished";
      }
      const line = agentName
        + (role ? " (" + role + ")" : "")
        + (rn !== null ? " round " + rn : "")
        + " — " + suffix;
      list.appendChild(el("li", { text: line }));
    }
    recentWrap.appendChild(list);
    inner.appendChild(recentWrap);
  }

  section.hidden = false;
}

// ------------------------------------------------------------
// Usage aggregation (Feature B)
// ------------------------------------------------------------
function aggregateUsage(agentRuns) {
  const result = {
    runs: 0,
    inputTokens: 0,
    outputTokens: 0,
    hasTokenData: false,
    cost: 0,
    hasCostData: false,
    fullCostCoverage: true,   // false if at least one run lacks cost_usd
    totalDurationMs: 0,
    hasDuration: false,
  };
  if (!Array.isArray(agentRuns)) return result;
  for (const r of agentRuns) {
    if (!r) continue;
    result.runs += 1;
    const inp = Number(r.input_tokens);
    const out = Number(r.output_tokens);
    if (Number.isFinite(inp)) { result.inputTokens += inp; result.hasTokenData = true; }
    if (Number.isFinite(out)) { result.outputTokens += out; result.hasTokenData = true; }
    const cost = Number(r.cost_usd);
    if (r.cost_usd !== null && r.cost_usd !== undefined && Number.isFinite(cost)) {
      result.cost += cost;
      result.hasCostData = true;
    } else {
      result.fullCostCoverage = false;
    }
    const dur = Number(r.duration_ms);
    if (Number.isFinite(dur)) { result.totalDurationMs += dur; result.hasDuration = true; }
  }
  return result;
}

// Build the per-run cost detail string used in the transcript message header.
// Returns "" when neither tokens, cost, nor duration are available.
function formatRunDetails(run) {
  if (!run) return "";
  const parts = [];
  const dur = Number(run.duration_ms);
  if (Number.isFinite(dur)) parts.push(fmtDurationMs(dur));
  const inp = Number(run.input_tokens);
  const out = Number(run.output_tokens);
  if (Number.isFinite(inp) || Number.isFinite(out)) {
    const inT = Number.isFinite(inp) ? fmtInt(inp) : "?";
    const outT = Number.isFinite(out) ? fmtInt(out) : "?";
    parts.push(inT + " in / " + outT + " out");
  }
  const cost = Number(run.cost_usd);
  if (run.cost_usd !== null && run.cost_usd !== undefined && Number.isFinite(cost)) {
    parts.push(fmtUsd(cost));
  }
  return parts.join(" · ");
}

// Pick the agent_run that best matches a given transcript message. Matching is
// best-effort: by agent_name + round_number, falling back to agent_name + role.
function findRunForMessage(agentRuns, message) {
  if (!Array.isArray(agentRuns) || !message) return null;
  const agent = message.agent_name;
  if (!agent) return null;
  const round = Number(message.round_number);
  const role = message.role || null;
  let byAgentRound = null;
  let byAgentRole = null;
  let byAgent = null;
  for (const r of agentRuns) {
    if (!r || r.agent_name !== agent) continue;
    byAgent = byAgent || r;
    if (role && r.role === role && !byAgentRole) byAgentRole = r;
    if (Number.isFinite(round) && Number(r.round_number) === round) {
      // Prefer one whose role also matches when known.
      if (!byAgentRound || (role && r.role === role)) byAgentRound = r;
    }
  }
  return byAgentRound || byAgentRole || byAgent;
}

function renderUsageSummary(task, agentRuns) {
  // Returns a node to append to the detail header, or null if there's nothing
  // meaningful to show yet (e.g., pre-terminal with no data).
  const isTerminal = task && task.status && State.terminalStatuses.has(task.status);
  if (!isTerminal) return null;
  if (!Array.isArray(agentRuns) || agentRuns.length === 0) return null;
  const agg = aggregateUsage(agentRuns);
  if (agg.runs === 0) return null;
  if (!agg.hasTokenData && !agg.hasCostData && !agg.hasDuration) return null;

  const parts = [];
  if (agg.hasTokenData) {
    parts.push(fmtInt(agg.inputTokens) + " input + "
      + fmtInt(agg.outputTokens) + " output tokens");
  }
  if (agg.hasCostData) {
    const costStr = "~" + fmtUsd(agg.cost)
      + (agg.fullCostCoverage ? "" : " (partial coverage)");
    parts.push(costStr);
  }
  parts.push(agg.runs + " agent call" + (agg.runs === 1 ? "" : "s"));
  if (agg.hasDuration) parts.push(fmtDurationMs(agg.totalDurationMs) + " total");

  const text = "Usage: " + parts.join(" · ");
  return el("p", { class: "detail-usage muted", text });
}

function hasUnansweredInputRequest(messages) {
  let latestRequest = -1;
  let latestResponse = -1;
  const list = Array.isArray(messages) ? messages : [];
  for (let i = 0; i < list.length; i++) {
    const m = list[i] || {};
    if (m.message_type === "user_input_request") latestRequest = i;
    if (m.message_type === "user_input_response") latestResponse = i;
  }
  return latestRequest >= 0 && latestRequest > latestResponse;
}

function renderDecisionPanel(task, messages) {
  const panel = $("#decision-panel");
  const inner = $("#decision-panel-inner");
  if (!panel || !inner) return;

  const isTerminal = task && task.status && State.terminalStatuses.has(task.status);
  const unresolvedPrompt = hasUnansweredInputRequest(messages);
  if (!isTerminal || unresolvedPrompt) {
    panel.hidden = true;
    inner.innerHTML = "";
    // Reset editing state when leaving terminal status
    State.decisionEditing = false;
    State.decisionDraft = "";
    return;
  }

  inner.innerHTML = "";
  panel.hidden = false;

  const existing = (task && typeof task.user_decision === "string") ? task.user_decision : "";
  const hasDecision = existing.trim().length > 0;
  const showForm = !hasDecision || State.decisionEditing;

  if (showForm) {
    renderDecisionForm(inner, task, existing);
  } else {
    renderDecisionDisplay(inner, task, existing);
  }
}

function renderDecisionForm(inner, task, existing) {
  inner.appendChild(el("h3", { class: "decision-title", text: "Your Decision" }));
  inner.appendChild(el("p", {
    class: "decision-help",
    text: "The conclave produced a recommendation. Record your authoritative call here — what you decided, in your own words.",
  }));

  const form = el("form", { class: "decision-form", id: "decision-form" });
  const initial = State.decisionEditing
    ? (State.decisionDraft || existing || "")
    : (existing || "");
  const textarea = el("textarea", {
    id: "decision-text",
    class: "decision-textarea",
    rows: 5,
    placeholder: "Record your decision...",
    required: true,
  });
  textarea.value = initial;
  textarea.addEventListener("input", () => {
    State.decisionDraft = textarea.value;
  });
  form.appendChild(textarea);

  const actions = el("div", { class: "decision-actions" });
  const submitBtn = el("button", {
    type: "submit",
    class: "btn btn-decision",
    text: State.decisionEditing && existing ? "Update Decision" : "Record Decision",
  });
  actions.appendChild(submitBtn);

  if (State.decisionEditing && existing) {
    const cancelBtn = el("button", {
      type: "button",
      class: "btn btn-secondary",
      text: "Cancel",
    });
    cancelBtn.addEventListener("click", () => {
      State.decisionEditing = false;
      State.decisionDraft = "";
      renderDecisionPanel(task);
    });
    actions.appendChild(cancelBtn);
  }

  const status = el("span", { class: "form-status", id: "decision-status" });
  actions.appendChild(status);
  form.appendChild(actions);

  form.addEventListener("submit", onSubmitDecision);
  inner.appendChild(form);
}

function renderDecisionDisplay(inner, task, decisionText) {
  // Card-level copy button anchored to the upper-right of the decision panel,
  // matching the consistent placement used across the dashboard.
  if (decisionText) {
    inner.appendChild(makeCopyButton(decisionText, "decision",
      { extraClass: "copy-btn-card" }));
  }
  const head = el("div", { class: "decision-display-head" });
  head.appendChild(el("h3", { class: "decision-title", text: "Your Decision" }));
  const actions = el("div", { class: "detail-meta" });
  const editBtn = el("button", {
    type: "button",
    class: "decision-edit-btn",
    text: "Edit",
    "aria-label": "Edit decision",
  });
  editBtn.addEventListener("click", () => {
    State.decisionEditing = true;
    State.decisionDraft = decisionText;
    renderDecisionPanel(task);
    const ta = $("#decision-text");
    if (ta) {
      ta.focus();
      try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (_) { /* ignore */ }
    }
  });
  actions.appendChild(editBtn);
  head.appendChild(actions);
  inner.appendChild(head);

  inner.appendChild(el("div", {
    class: "decision-text-block",
    text: decisionText,
  }));

  const ts = task && task.user_decided_at ? task.user_decided_at : "";
  if (ts) {
    inner.appendChild(el("p", {
      class: "decision-timestamp",
      text: "Decided at: " + fmtTime(ts),
    }));
  }
}

async function onSubmitDecision(ev) {
  ev.preventDefault();
  const status = $("#decision-status");
  if (status) {
    status.className = "form-status";
    status.textContent = "";
  }
  const ta = $("#decision-text");
  const text = ta ? ta.value.trim() : "";
  if (!text) {
    if (status) {
      status.className = "form-status error";
      status.textContent = "Please enter a decision.";
    }
    return;
  }
  const form = $("#decision-form");
  const submitBtn = form ? form.querySelector("button[type='submit']") : null;
  if (submitBtn) submitBtn.disabled = true;
  try {
    if (status) {
      status.className = "form-status";
      status.textContent = "Recording...";
    }
    await Api.decideTask(State.currentTaskId, text);
    State.decisionEditing = false;
    State.decisionDraft = "";
    await refreshDetail();
  } catch (e) {
    if (status) {
      status.className = "form-status error";
      status.textContent = "Failed: " + e.message;
    }
    if (submitBtn) submitBtn.disabled = false;
  }
}

function resetNewTaskForm() {
  $("#question").value = "";
  clearAttachments();
  setProjectPath("");
  setIncludeSandbox(false);
  setSandboxWarning("");
  const status = $("#form-status");
  if (status) {
    status.className = "form-status";
    status.textContent = "";
  }
  // Default permissions whenever the form is reset.
  setPermissionsToDefaults();
}

function showFollowupBanner(parentId) {
  const banner = $("#followup-banner");
  const text = $("#followup-banner-text");
  if (!banner || !text) return;
  text.innerHTML = "";
  text.appendChild(document.createTextNode("Continuing thread from task "));
  const code = el("code", { text: shortId(parentId) });
  text.appendChild(code);
  text.appendChild(document.createTextNode(
    ". Mode and agents pre-selected; you can change them. The conclave will see this thread's prior context automatically."
  ));
  banner.hidden = false;
}

function hideFollowupBanner() {
  const banner = $("#followup-banner");
  if (banner) banner.hidden = true;
}

function clearFollowupParent() {
  State.followupParentId = null;
  hideFollowupBanner();
}

function onStartNewTask() {
  // "Start New Task" is the clean-slate path: drop any pending parent linkage.
  clearFollowupParent();
  resetNewTaskForm();
  switchView("new");
  const q = $("#question");
  if (q) q.focus();
}

function onSubmitFollowup() {
  const data = State.currentTaskData;
  const task = (data && data.task) || {};
  const finalResult = (data && data.final_result) || null;
  const taskId = task.id || State.currentTaskId || "";
  const finalAnswer = (finalResult && typeof finalResult.final_answer === "string")
    ? finalResult.final_answer : "";
  const preview = finalAnswer.length > 200 ? finalAnswer.slice(0, 200) : finalAnswer;
  const userDecision = (typeof task.user_decision === "string") ? task.user_decision : "";
  const decisionPreview = userDecision.length > 200 ? userDecision.slice(0, 200) : userDecision;
  // If a decision is recorded, it's the authoritative call — feed it to the next
  // conclave as primary context, not just the prior conclave's recommendation.
  const decisionLine = decisionPreview
    ? `\nMy decision on that task: "${decisionPreview}"`
    : "";
  const prefill =
    `Follow-up to task ${taskId}. Previous final answer: "${preview}"${decisionLine}\n\nNew question: `;

  // Reset the form but DON'T clear followupParentId — we're setting it next.
  resetNewTaskForm();

  // Record parent for the next submission.
  if (taskId) {
    State.followupParentId = taskId;
    showFollowupBanner(taskId);
  } else {
    clearFollowupParent();
  }

  // Pre-select mode to match the parent task.
  const parentMode = task.mode;
  const modeSelect = $("#mode");
  if (modeSelect && parentMode) {
    const hasOption = Array.from(modeSelect.options).some((o) => o.value === parentMode);
    if (hasOption) modeSelect.value = parentMode;
  }

  // Pre-select agents to match the parent task. For consult/resolve/handoff,
  // the primary agent goes first so the agent ordering matches buildPayload's
  // "first agent is primary" convention.
  const parentAgents = [];
  const isPrimaryFirst = parentMode === "consult" || parentMode === "resolve" || parentMode === "handoff";
  if (isPrimaryFirst && task.primary_agent) {
    parentAgents.push(task.primary_agent);
  }
  if (Array.isArray(task.consultants)) {
    for (const c of task.consultants) {
      if (c && !parentAgents.includes(c)) parentAgents.push(c);
    }
  }
  // For modes without a "primary first" ordering, still include the primary if present.
  if (!isPrimaryFirst && task.primary_agent && !parentAgents.includes(task.primary_agent)) {
    parentAgents.push(task.primary_agent);
  }
  State.selectedAgents = parentAgents.filter((a) => State.agents.includes(a));
  updateModeHint();
  renderAgentsList();

  // Pre-fill permissions from the parent task if present, otherwise keep
  // the defaults that resetNewTaskForm() applied above.
  if (task && isPlainObject(task.permissions)) {
    const merged = Object.assign({}, DEFAULT_PERMISSIONS);
    for (const k of PERMISSION_KEYS) {
      if (k in task.permissions) merged[k] = !!task.permissions[k];
    }
    writePermissions(merged);
  }

  // Pre-fill project_path and include_sandbox from the parent task.
  if (task && typeof task.project_path === "string") {
    setProjectPath(task.project_path);
  }
  const parentExtra = task && isPlainObject(task.context) && isPlainObject(task.context.extra)
    ? task.context.extra : null;
  if (parentExtra && parentExtra.include_sandbox === true) {
    setIncludeSandbox(true);
  }

  switchView("new");
  const q = $("#question");
  if (q) {
    q.value = prefill;
    q.focus();
    // Move caret to end
    const end = q.value.length;
    try { q.setSelectionRange(end, end); } catch (_) { /* ignore */ }
  }
}

function renderAnswerPrompt(task, messages) {
  const wrap = $("#answer-prompt");
  if (task.status !== "awaiting_user_input") {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  // The actual question lives in structured.user_input_question (conclave) or
  // structured.question on a turn — the bare "user_input_request" marker
  // messages are usually empty, so DON'T stop at one; keep walking back until
  // we find real question text. Take the most recent one.
  let questionText = null, askedBy = null, context = null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const s = (m && typeof m.structured === "object" && m.structured) ? m.structured : {};
    const q = s.user_input_question || s.question
      || (m.message_type === "user_input_request" ? m.content : null);
    if (q && String(q).trim()) {
      questionText = String(q).trim();
      askedBy = m.agent_name || null;
      context = (s.summary && String(s.summary).trim())
        || (s.position && String(s.position).trim())
        || (s.analysis && String(s.analysis).trim())
        || null;
      break;
    }
  }

  const qEl = $("#answer-question");
  const questionnaireEl = $("#answer-questionnaire");
  const askedByEl = $("#answer-asked-by");
  const ctxWrap = $("#answer-context");
  const ctxText = $("#answer-context-text");

  if (questionText) {
    qEl.textContent = questionText;
    qEl.classList.remove("answer-question-missing");
    renderQuestionnaire(questionnaireEl, questionText);
  } else {
    qEl.textContent = "An agent paused the deliberation for your input, but no question text was recorded — check the latest agent turn in the Transcript below.";
    qEl.classList.add("answer-question-missing");
    renderQuestionnaire(questionnaireEl, "");
  }

  if (askedBy) {
    askedByEl.hidden = false;
    askedByEl.textContent = `Asked by ${askedBy}`;
  } else {
    askedByEl.hidden = true;
  }

  if (context) {
    ctxWrap.hidden = false;
    ctxText.textContent = context;
  } else {
    ctxWrap.hidden = true;
    ctxText.textContent = "";
  }
}

function parseNumberedQuestions(text) {
  const lines = String(text || "").split(/\r?\n/);
  const questions = [];
  for (const line of lines) {
    const m = line.match(/^\s*(\d+)[.)]\s+(.+?)\s*$/);
    if (m && m[2]) questions.push(m[2]);
  }
  return questions;
}

function renderQuestionnaire(container, questionText) {
  if (!container) return;
  container.innerHTML = "";
  const questions = parseNumberedQuestions(questionText);
  if (questions.length < 2) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  questions.forEach((question, idx) => {
    const row = el("label", { class: "answer-questionnaire-item" });
    row.appendChild(el("span", { class: "answer-questionnaire-number", text: String(idx + 1) }));
    const body = el("span", { class: "answer-questionnaire-body" });
    body.appendChild(el("span", { class: "answer-questionnaire-question", text: question }));
    body.appendChild(el("textarea", {
      class: "answer-questionnaire-input",
      rows: "2",
      "data-question-index": String(idx + 1),
      placeholder: "Answer question " + (idx + 1),
    }));
    row.appendChild(body);
    container.appendChild(row);
  });
}

function groupMessagesByRound(messages) {
  // Each round is delimited by a turn from every conclave participant; simpler:
  // group consecutive messages until the same agent appears again.
  const rounds = [];
  let current = [];
  const seenAgents = new Set();
  for (const m of messages) {
    if (m.message_type === "conclave_turn") {
      if (seenAgents.has(m.agent_name) && current.length > 0) {
        rounds.push(current);
        current = [];
        seenAgents.clear();
      }
      seenAgents.add(m.agent_name);
    }
    current.push(m);
  }
  if (current.length) rounds.push(current);
  return rounds;
}

function renderTranscript(task, messages, agentRuns) {
  const container = $("#transcript-container");
  container.innerHTML = "";
  if (!messages.length) {
    container.appendChild(el("p", { class: "muted", text: "No messages yet." }));
    return;
  }

  if (task.mode === "conclave") {
    const rounds = groupMessagesByRound(messages);
    rounds.forEach((roundMsgs, idx) => {
      const block = el("div", { class: "round-block" });
      block.appendChild(el("div", { class: "round-label", text: "Round " + (idx + 1) }));
      for (const m of roundMsgs) block.appendChild(renderMessage(m, agentRuns));
      container.appendChild(block);
    });
  } else {
    for (const m of messages) container.appendChild(renderMessage(m, agentRuns));
  }
}

// DR0015: tool-loop events render as compact, click-to-expand ribbons inline
// within the agent's round-block, so the full file-reading trail is visible
// but doesn't visually drown the actual structured turn.
function renderToolMessage(m) {
  const structured = isPlainObject(m.structured) ? m.structured : {};
  const isCall = m.message_type === "tool_call";
  const fn = structured.function || "?";
  const node = el("div", {
    class: "tool-event tool-" + (isCall ? "call" : "result"),
  });
  const head = el("div", { class: "tool-event-head" });
  head.appendChild(el("span", {
    class: "tool-arrow",
    text: isCall ? "→" : "←",
    title: isCall ? "agent → tool" : "tool → agent",
  }));
  head.appendChild(el("span", { class: "tool-fn mono", text: fn }));
  if (isCall) {
    let argsPreview = "";
    try {
      const parsed = JSON.parse(structured.arguments || "{}");
      argsPreview = Object.entries(parsed)
        .map(([k, v]) => k + "=" + JSON.stringify(v))
        .join(", ");
    } catch (_) {
      argsPreview = String(structured.arguments || "");
    }
    head.appendChild(el("span", { class: "tool-args mono", text: argsPreview.slice(0, 120) }));
  } else {
    const ok = !!structured.ok;
    head.appendChild(el("span", {
      class: "tool-status " + (ok ? "tool-ok" : "tool-err"),
      text: ok ? "ok" : "error",
    }));
    const result = structured.result || {};
    let summary = "";
    if (!ok) {
      summary = String(result.error || "").slice(0, 140);
    } else if (typeof result.content === "string") {
      summary = result.content.length + " chars" + (result.truncated ? " (truncated)" : "");
    } else if (Array.isArray(result.entries)) {
      summary = result.entries.length + " entries";
    } else if (Array.isArray(result.paths)) {
      summary = result.paths.length + " paths" + (result.truncated ? " (cap hit)" : "");
    }
    if (summary) head.appendChild(el("span", { class: "tool-summary muted", text: summary }));
    if (typeof structured.bytes === "number") {
      head.appendChild(el("span", { class: "tool-bytes muted", text: structured.bytes + " B" }));
    }
  }
  node.appendChild(head);

  // Click to expand the full structured payload (collapsed by default).
  const details = el("details", { class: "tool-details" });
  const summaryEl = el("summary", { class: "tool-details-summary", text: "show payload" });
  details.appendChild(summaryEl);
  const pre = el("pre", { class: "tool-payload mono" });
  pre.textContent = JSON.stringify(structured, null, 2);
  details.appendChild(pre);
  node.appendChild(details);
  return node;
}

function renderMessage(m, agentRuns) {
  // DR0015: tool-loop events render as compact ribbons, not full cards.
  if (m.message_type === "tool_call" || m.message_type === "tool_result") {
    return renderToolMessage(m);
  }

  const classes = ["msg"];
  const structured = isPlainObject(m.structured) ? m.structured : null;

  if (m.message_type === "conclave_turn" && structured && structured.convergence) {
    classes.push("conv-" + structured.convergence);
  }
  if (m.message_type === "consultant_critique" && structured && structured.agreement) {
    classes.push("agree-" + structured.agreement);
  }

  const node = el("div", { class: classes.join(" ") });

  // Card-level copy button anchored to the upper-right corner of the .msg card.
  // (Consistent placement with every other panel/card copy button.)
  const copyLabel = "message from " + (m.agent_name || "agent");
  node.appendChild(makeCopyButton(
    () => formatMessageAsText(m),
    copyLabel,
    { extraClass: "copy-btn-card" }
  ));

  // Header (badges only — copy button lives in the card corner, timestamp
  // drops to its own muted line just below so neither overlaps the corner btn).
  const head = el("div", { class: "msg-header" }, [
    el("span", { class: "msg-agent", text: m.agent_name || "?" }),
    roleBadge(m.role),
    el("span", { class: "msg-type", text: m.message_type || "" }),
  ]);
  if (m.direction) head.appendChild(el("span", { class: "muted", text: m.direction }));
  node.appendChild(head);
  if (m.created_at) {
    const timeRow = el("div", { class: "msg-time" }, [
      document.createTextNode(fmtTime(m.created_at)),
    ]);
    // Per-run cost/usage details (when an agent_run can be matched to this message).
    const runDetails = formatRunDetails(findRunForMessage(agentRuns, m));
    if (runDetails) {
      timeRow.appendChild(el("span", { class: "msg-run-sep", text: " · " }));
      timeRow.appendChild(el("span", { class: "msg-run-details", text: runDetails }));
    }
    node.appendChild(timeRow);
  }

  // Fields
  const fieldsWrap = el("div", { class: "msg-fields" });
  if (structured) {
    for (const [key, value] of Object.entries(structured)) {
      if (value === null || value === undefined) continue;
      if (Array.isArray(value) && value.length === 0) continue;
      if (typeof value === "string" && value.trim() === "") continue;
      fieldsWrap.appendChild(renderField(key, value));
    }
  }
  if (typeof m.content === "string" && m.content.length > 0
      && (!structured || Object.keys(structured).length === 0)) {
    fieldsWrap.appendChild(renderField("content", m.content));
  }
  if (fieldsWrap.children.length === 0) {
    fieldsWrap.appendChild(el("p", { class: "muted", text: "(no content)" }));
  }
  node.appendChild(fieldsWrap);

  return node;
}

function renderField(key, value) {
  const wrap = el("div", { class: "field" });
  // For arrays of primitives (rendered as a <ul>) and plain objects (rendered
  // as a JSON <pre>) we surface a copy button on the label row so the whole
  // structured field can be copied as JSON in one click. Single primitives
  // and plain strings don't need a button here.
  const needsCopy = (Array.isArray(value) && value.length > 0)
    || (isPlainObject(value) && Object.keys(value).length > 0);
  if (needsCopy) {
    const labelRow = el("div", { class: "field-label-row" });
    labelRow.appendChild(el("div", { class: "field-label", text: prettifyKey(key) }));
    labelRow.appendChild(makeCopyButton(
      () => JSON.stringify(value, null, 2),
      prettifyKey(key) + " as JSON"
    ));
    wrap.appendChild(labelRow);
  } else {
    wrap.appendChild(el("div", { class: "field-label", text: prettifyKey(key) }));
  }
  wrap.appendChild(renderFieldValue(value));
  return wrap;
}

function prettifyKey(k) {
  return String(k).replace(/_/g, " ");
}

function wrapWithCopy(innerNode, copyText, label) {
  const wrap = el("div", { class: "copyable-box" });
  wrap.appendChild(innerNode);
  wrap.appendChild(makeCopyButton(copyText, label));
  return wrap;
}

function renderFieldValue(value) {
  if (typeof value === "number" || typeof value === "boolean") {
    return el("div", { class: "field-value mono", text: String(value) });
  }
  if (typeof value === "string") {
    return el("div", { class: "field-value", text: value });
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return el("div", { class: "field-value muted", text: "(empty)" });
    }
    // If every item is an object carrying a `description` string, render as
    // human-readable cards (recommended_actions, risks, etc.).
    if (value.every(v => isPlainObject(v) && typeof v.description === "string")) {
      return renderObjectList(value);
    }
    // All primitives → flat ul.
    if (value.every(v => typeof v === "string" || typeof v === "number" || typeof v === "boolean")) {
      const ul = el("ul", { class: "field-list" });
      for (const item of value) ul.appendChild(el("li", { text: String(item) }));
      return ul;
    }
    // All plain objects (without `description`) → each rendered as a DL
    // inside an <li>. Recursive: nested objects/arrays render through
    // renderFieldValue again.
    if (value.every(v => isPlainObject(v))) {
      const ul = el("ul", { class: "field-list field-list-objects" });
      for (const item of value) {
        const li = el("li", { class: "field-list-object-item" });
        li.appendChild(renderObjectAsDl(item));
        ul.appendChild(li);
      }
      return ul;
    }
    // Mixed list — render each item by recursing.
    const ul = el("ul", { class: "field-list" });
    for (const item of value) {
      if (item === null || item === undefined) continue;
      const li = el("li");
      li.appendChild(renderFieldValue(item));
      ul.appendChild(li);
    }
    return ul;
  }
  if (isPlainObject(value)) {
    // Recursive readable rendering instead of a JSON dump.
    return renderObjectAsDl(value);
  }
  return el("div", { class: "field-value", text: String(value) });
}

// Render a plain object as a definition list (key → value). Values recurse
// through renderFieldValue so nested objects / arrays render readably too.
// The `code` key gets its usual code-block treatment.
function renderObjectAsDl(obj) {
  const dl = el("dl", { class: "object-dl" });
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined) continue;
    dl.appendChild(el("dt", { text: prettifyKey(k) }));
    const dd = el("dd");
    if (k === "code" && typeof v === "string" && v.includes("\n")) {
      const pre = el("pre", { class: "object-card-code", text: v });
      dd.appendChild(wrapWithCopy(pre, v, "code"));
    } else {
      dd.appendChild(renderFieldValue(v));
    }
    dl.appendChild(dd);
  }
  return dl;
}

// ------------------------------------------------------------
// Human-readable renderer for arrays of objects that share a `description`
// field (the shape of recommended_actions, risks, etc.). Each item becomes a
// small card: description prominent, simple meta keys in a row, payload
// rendered as a definition list or code block depending on shape.
// ------------------------------------------------------------
function renderObjectList(items) {
  const wrap = el("div", { class: "object-list" });
  for (const item of items) {
    wrap.appendChild(renderObjectCard(item));
  }
  return wrap;
}

function renderObjectCard(obj) {
  const card = el("div", { class: "object-card" });

  // 1. Description leads — the most important user-facing text.
  if (typeof obj.description === "string" && obj.description.length > 0) {
    card.appendChild(el("div", { class: "object-card-desc", text: obj.description }));
  }

  // 2. Meta row — primitive keys other than description / payload. Severity
  //    gets a colored pill; everything else gets a small "key: value" chip.
  const metaItems = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === "description" || k === "payload") continue;
    if (v === null || v === undefined) continue;
    if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") continue;
    metaItems.push([k, v]);
  }
  if (metaItems.length > 0) {
    const meta = el("div", { class: "object-card-meta" });
    for (const [k, v] of metaItems) {
      if (k === "severity") {
        const sev = String(v).toLowerCase();
        meta.appendChild(el("span", { class: "object-card-meta-item" }, [
          el("span", { class: "object-card-meta-key", text: "severity " }),
          el("span", { class: "severity-pill severity-" + sev, text: sev }),
        ]));
      } else if (k === "requires_approval") {
        meta.appendChild(el("span", { class: "object-card-meta-item" }, [
          el("span", { class: "object-card-meta-key", text: "approval " }),
          el("span", {
            class: "approval-pill approval-" + (v ? "required" : "not-required"),
            text: v ? "required" : "not required",
          }),
        ]));
      } else {
        meta.appendChild(el("span", { class: "object-card-meta-item" }, [
          el("span", { class: "object-card-meta-key", text: prettifyKey(k) + " " }),
          el("span", { class: "object-card-meta-val", text: String(v) }),
        ]));
      }
    }
    card.appendChild(meta);
  }

  // 3. Payload — render the inside, not the JSON wrapper.
  if (isPlainObject(obj.payload) && Object.keys(obj.payload).length > 0) {
    card.appendChild(renderObjectCardPayload(obj.payload));
  }

  // 4. Any other non-primitive keys (objects / arrays we didn't recognize)
  //    fall back to the generic value renderer with a small label.
  for (const [k, v] of Object.entries(obj)) {
    if (k === "description" || k === "payload") continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") continue;
    if (v === null || v === undefined) continue;
    const sub = el("div", { class: "object-card-subfield" });
    sub.appendChild(el("div", { class: "object-card-subfield-label", text: prettifyKey(k) }));
    sub.appendChild(renderFieldValue(v));
    card.appendChild(sub);
  }

  return card;
}

function renderObjectCardPayload(payload) {
  const wrap = el("div", { class: "object-card-payload" });
  const keys = Object.keys(payload);

  // Common case: payload is a single `code` field. Render as a code block.
  if (keys.length === 1 && keys[0] === "code" && typeof payload.code === "string") {
    wrap.appendChild(el("div", { class: "object-card-payload-label", text: "Code" }));
    const pre = el("pre", { class: "object-card-code", text: payload.code });
    wrap.appendChild(wrapWithCopy(pre, payload.code, "code"));
    return wrap;
  }

  // Otherwise: defer to the recursive DL renderer so nested objects and
  // arrays inside the payload render readably too (not as JSON blobs).
  wrap.appendChild(el("div", { class: "object-card-payload-label", text: "Payload" }));
  wrap.appendChild(renderObjectAsDl(payload));
  return wrap;
}

// ------------------------------------------------------------
// Prior Art panel — Phase 2.5 of post-DR plan tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
// Renders the past decision records the keeper matched at task-creation time.
// ------------------------------------------------------------
function renderPriorArt(matches) {
  const section = document.getElementById("prior-art-section");
  const list = document.getElementById("prior-art-list");
  if (!section || !list) return;
  list.innerHTML = "";
  if (!matches || matches.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  for (const m of matches) {
    const card = el("div", { class: "prior-art-card" + (m.superseded ? " prior-art-card-superseded" : "") });
    const head = el("div", { class: "prior-art-head" });
    // Strip leading zeros: "0011" -> "11" for readability.
    const num = String(m.number).replace(/^0+/, "") || m.number;
    head.appendChild(el("span", { class: "prior-art-number", text: "Decision " + num }));
    head.appendChild(el("span", { class: "prior-art-title", text: m.title || "" }));
    if (m.date) head.appendChild(el("span", { class: "prior-art-date muted", text: m.date }));
    head.appendChild(el("span", {
      class: "prior-art-score",
      title: "TF-IDF cosine similarity (0.0–1.0)",
      text: "match " + Number(m.score || 0).toFixed(2),
    }));
    if (m.superseded) {
      const supBy = (m.superseded_by || []).map(n => "Decision " + String(n).replace(/^0+/, "")).join(", ");
      head.appendChild(el("span", {
        class: "prior-art-superseded-badge",
        title: supBy ? "Superseded by " + supBy : "Superseded — see docs/decisions/INDEX.md",
        text: supBy ? "superseded by " + supBy : "superseded",
      }));
    }
    card.appendChild(head);
    if (m.summary) {
      card.appendChild(el("div", { class: "prior-art-summary", text: m.summary }));
    }
    if (m.path) {
      card.appendChild(el("div", { class: "prior-art-path muted", text: m.path }));
    }
    list.appendChild(card);
  }
}

// ------------------------------------------------------------
// Confidence panel — Phase 2 of post-DR plan tsk_01KRSW6AS3M66B4RRJE3JFAPRV.
// Aggregate band (min/max/mean) + per-agent trajectory (round-by-round
// confidence + convergence state). Read-only transparency layer.
// ------------------------------------------------------------
function _confBand(score) {
  // Color band: red (low) -> amber (mid) -> green (high). Threshold at 0.7 / 0.85.
  if (score === null || score === undefined) return "muted";
  if (score >= 0.85) return "high";
  if (score >= 0.70) return "mid";
  return "low";
}

function _fmtConf(score) {
  if (score === null || score === undefined) return "—";
  return Number(score).toFixed(2);
}

function renderConfidencePanel(agg, trajectory) {
  const panel = el("div", { class: "confidence-panel" });
  panel.appendChild(el("div", { class: "field-label", text: "CONFIDENCE" }));

  // Aggregate stat row
  if (agg) {
    const stats = el("div", { class: "conf-stats" });
    const mkStat = (label, value, score) => {
      const cell = el("div", { class: "conf-stat conf-" + _confBand(score) });
      cell.appendChild(el("div", { class: "conf-stat-label", text: label }));
      cell.appendChild(el("div", { class: "conf-stat-value", text: _fmtConf(value) }));
      return cell;
    };
    stats.appendChild(mkStat("min",  agg.min,  agg.min));
    stats.appendChild(mkStat("mean", agg.mean, agg.mean));
    stats.appendChild(mkStat("max",  agg.max,  agg.max));
    const countCell = el("div", { class: "conf-stat conf-count" });
    countCell.appendChild(el("div", { class: "conf-stat-label", text: "participants" }));
    const countText = agg.missing_count
      ? agg.count + " (" + agg.missing_count + " missing)"
      : String(agg.count);
    countCell.appendChild(el("div", { class: "conf-stat-value", text: countText }));
    stats.appendChild(countCell);
    panel.appendChild(stats);

    // Spread caveat — flags weak/conformist convergence
    const spread = (agg.max ?? 0) - (agg.min ?? 0);
    if (spread >= 0.30 && agg.count >= 2) {
      panel.appendChild(el("div", {
        class: "conf-caveat",
        text: "Wide spread (" + _fmtConf(spread) + "). Some participants " +
              "signaled done with materially lower confidence than others — " +
              "consensus may be conformist drift rather than robust agreement.",
      }));
    }
  }

  // Per-agent trajectory
  if (trajectory && trajectory.length > 0) {
    const trajWrap = el("div", { class: "conf-trajectory" });
    trajWrap.appendChild(el("div", { class: "conf-trajectory-label", text: "Round-by-round" }));
    for (const t of trajectory) {
      const row = el("div", { class: "conf-traj-row" });
      row.appendChild(el("span", { class: "conf-traj-agent", text: t.agent }));
      const dots = el("span", { class: "conf-traj-dots" });
      for (let i = 0; i < (t.rounds || []).length; i++) {
        const r = t.rounds[i];
        const dot = el("span", {
          class: "conf-traj-dot conf-" + _confBand(r.confidence),
          title: "Round " + r.round + ": confidence " + _fmtConf(r.confidence)
                  + (r.convergence ? " · " + r.convergence : ""),
          text: _fmtConf(r.confidence),
        });
        dots.appendChild(dot);
        if (i < t.rounds.length - 1) {
          dots.appendChild(el("span", { class: "conf-traj-arrow", text: "→" }));
        }
      }
      row.appendChild(dots);
      // Final convergence state
      const last = t.rounds && t.rounds[t.rounds.length - 1];
      if (last && last.convergence) {
        row.appendChild(el("span", {
          class: "conf-traj-final convergence-" + last.convergence,
          text: last.convergence,
        }));
      }
      trajWrap.appendChild(row);
    }
    panel.appendChild(trajWrap);
  }

  return panel;
}

function renderActionPlan(steps) {
  const wrap = el("div", { class: "action-plan" });
  wrap.appendChild(el("div", { class: "action-plan-title", text: "Structured Action Plan" }));
  const list = el("div", { class: "action-plan-list" });
  for (const step of steps) {
    if (!isPlainObject(step)) continue;
    const status = String(step.policy_status || "unknown");
    const card = el("div", { class: "action-plan-step action-plan-" + status });
    const head = el("div", { class: "action-plan-step-head" });
    head.appendChild(el("span", {
      class: "action-plan-number",
      text: String(step.step_number || "?"),
    }));
    head.appendChild(el("span", {
      class: "action-plan-summary",
      text: step.summary || "(no summary)",
    }));
    head.appendChild(el("span", {
      class: "action-plan-status action-plan-status-" + status,
      text: prettifyKey(status),
    }));
    card.appendChild(head);

    const meta = el("div", { class: "action-plan-meta" });
    if (step.action_type) {
      meta.appendChild(el("span", { text: "type: " + step.action_type }));
    }
    if (step.target) {
      meta.appendChild(el("span", { class: "action-plan-target", text: "target: " + step.target }));
    }
    if (Array.isArray(step.required_permissions) && step.required_permissions.length > 0) {
      meta.appendChild(el("span", { text: "permissions: " + step.required_permissions.join(", ") }));
    }
    if (step.source_action_kind) {
      meta.appendChild(el("span", { text: "source: " + step.source_action_kind }));
    }
    if (meta.childNodes.length > 0) card.appendChild(meta);

    if (Array.isArray(step.policy_reasons) && step.policy_reasons.length > 0) {
      const reasons = el("ul", { class: "action-plan-reasons" });
      for (const reason of step.policy_reasons) {
        reasons.appendChild(el("li", { text: String(reason) }));
      }
      card.appendChild(reasons);
    }
    list.appendChild(card);
  }
  wrap.appendChild(list);
  return wrap;
}

function renderArtifactPanel(task, artifacts) {
  const section = $("#artifact-panel-section");
  const container = $("#artifact-panel-container");
  if (!section || !container) return;
  container.innerHTML = "";
  if (!Array.isArray(artifacts) || artifacts.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const list = el("div", { class: "artifact-list" });
  for (const artifact of artifacts) {
    if (!isPlainObject(artifact)) continue;
    const metadata = isPlainObject(artifact.metadata) ? artifact.metadata : {};
    const card = el("div", { class: "artifact-card artifact-kind-" + (artifact.kind || "unknown") });
    const head = el("div", { class: "artifact-head" });
    head.appendChild(el("div", { class: "artifact-title", text: artifact.title || artifact.filename || artifact.id }));
    head.appendChild(el("span", { class: "artifact-kind", text: artifact.kind || "artifact" }));
    card.appendChild(head);

    const meta = el("div", { class: "artifact-meta" });
    meta.appendChild(el("span", { text: "file: " + (artifact.filename || "(unknown)") }));
    if (metadata.target_path) meta.appendChild(el("span", { text: "target: " + metadata.target_path }));
    if (metadata.apply_mode) meta.appendChild(el("span", { text: "mode: " + metadata.apply_mode }));
    if (metadata.agent) meta.appendChild(el("span", { text: "agent: " + metadata.agent }));
    card.appendChild(meta);

    if (artifact.content) {
      const details = el("details", { class: "artifact-preview" });
      details.appendChild(el("summary", { text: "Preview" }));
      details.appendChild(el("pre", { class: "field-value mono", text: artifact.content }));
      card.appendChild(details);
    }

    const actions = el("div", { class: "artifact-actions" });
    const download = el("a", {
      class: "btn btn-secondary",
      href: `/api/tasks/${task.id}/artifacts/${artifact.id}/download`,
      text: "Download",
    });
    actions.appendChild(download);
    const canApply = metadata.apply_mode === "write_file" || metadata.apply_mode === "search_replace";
    if (canApply) {
      const applyBtn = el("button", {
        type: "button",
        class: "btn btn-primary",
        text: metadata.applied_at ? "Apply again" : "Apply to project",
      });
      applyBtn.addEventListener("click", async () => {
        const target = metadata.target_path || artifact.filename || artifact.id;
        const ok = window.confirm("Apply this draft artifact to " + target + "?");
        if (!ok) return;
        applyBtn.disabled = true;
        applyBtn.textContent = "Applying...";
        try {
          await Api.applyArtifact(task.id, artifact.id);
          await refreshDetail();
        } catch (e) {
          alert("Apply failed: " + e.message);
          applyBtn.disabled = false;
          applyBtn.textContent = metadata.applied_at ? "Apply again" : "Apply to project";
        }
      });
      actions.appendChild(applyBtn);
    }
    if (metadata.applied_at) {
      actions.appendChild(el("span", { class: "artifact-applied", text: "applied " + fmtTime(metadata.applied_at) }));
    }
    card.appendChild(actions);
    list.appendChild(card);
  }
  container.appendChild(list);
}

function renderFinalResult(fr) {
  const section = $("#final-result-section");
  const container = $("#final-result-container");
  container.innerHTML = "";
  const finalSlot = $("#final-result-copy-slot");
  if (finalSlot) finalSlot.innerHTML = "";
  if (!fr) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  if (finalSlot && typeof fr.final_answer === "string" && fr.final_answer.length > 0) {
    finalSlot.appendChild(makeCopyButton(fr.final_answer, "final answer"));
  }

  const grid = el("div", { class: "final-grid" });

  // Agreement + resolution status row
  const agreement = (fr.agreement_level || "unknown").toString();
  const agreementRow = el("div", { class: "agreement-display" }, [
    el("span", { class: "agreement-label", text: "Agreement:" }),
    el("span", { class: "agreement-pill agreement-" + agreement, text: agreement }),
  ]);
  if (fr.resolution_status) {
    agreementRow.appendChild(el("span", { class: "agreement-label", text: "Resolution:" }));
    agreementRow.appendChild(el("span", { class: "badge status-" + fr.resolution_status, text: fr.resolution_status }));
  }
  grid.appendChild(agreementRow);

  // Confidence aggregate + per-agent trajectory (Phase 2 of post-DR plan).
  // Surfaces whether "consensus" was 4x0.95 or 1x0.95 + 3x0.4, plus each
  // participant's confidence path round-by-round.
  if (fr.confidence_aggregate || (fr.confidence_trajectory && fr.confidence_trajectory.length > 0)) {
    grid.appendChild(renderConfidencePanel(fr.confidence_aggregate, fr.confidence_trajectory));
  }

  // Final answer
  grid.appendChild(el("div", { class: "field" }, [
    el("div", { class: "field-label", text: "FINAL ANSWER" }),
    el("div", { class: "final-answer-block", text: fr.final_answer || "(no final answer)" }),
  ]));

  // Disagreements - never summarize away
  if (Array.isArray(fr.disagreements) && fr.disagreements.length > 0) {
    const dis = el("div", { class: "disagreements" });
    dis.appendChild(el("div", { class: "disagreements-title", text: "Disagreements (" + fr.disagreements.length + ")" }));
    for (const d of fr.disagreements) {
      const item = el("div", { class: "disagreement-item" });
      // Card-level copy button in the upper-right corner of this disagreement card.
      item.appendChild(makeCopyButton(
        () => formatDisagreementAsText(d),
        "disagreement",
        { extraClass: "copy-btn-card" }
      ));
      const headRow = el("div", { class: "disagreement-head" });
      if (d.topic) {
        headRow.appendChild(el("div", { class: "disagreement-topic", text: d.topic }));
      } else {
        headRow.appendChild(el("div", { class: "disagreement-topic muted", text: "Disagreement" }));
      }
      item.appendChild(headRow);
      // (topic already rendered in headRow above)
      if (d.primary_position) {
        item.appendChild(renderField("primary position", d.primary_position));
      }
      if (d.consultant_position) {
        item.appendChild(renderField("consultant position", d.consultant_position));
      }
      // Surface any extra fields verbatim
      for (const [k, v] of Object.entries(d)) {
        if (["topic", "primary_position", "consultant_position"].includes(k)) continue;
        if (v === null || v === undefined) continue;
        item.appendChild(renderField(k, v));
      }
      dis.appendChild(item);
    }
    grid.appendChild(dis);
  }

  const hasActionPlan = Array.isArray(fr.action_plan) && fr.action_plan.length > 0;
  if (hasActionPlan) {
    grid.appendChild(renderActionPlan(fr.action_plan));
  }

  // Recommended actions
  if (!hasActionPlan && Array.isArray(fr.recommended_actions) && fr.recommended_actions.length > 0) {
    grid.appendChild(renderField("recommended actions", fr.recommended_actions));
  } else if (hasActionPlan && Array.isArray(fr.recommended_actions) && fr.recommended_actions.length > 0) {
    const raw = el("details", { class: "raw-recommendations" });
    raw.appendChild(el("summary", { text: "Raw recommendations" }));
    raw.appendChild(renderField("recommended actions", fr.recommended_actions));
    grid.appendChild(raw);
  }
  // Risks
  if (Array.isArray(fr.risks) && fr.risks.length > 0) {
    grid.appendChild(renderField("risks", fr.risks));
  }
  // Approval items
  if (Array.isArray(fr.commands_requiring_approval) && fr.commands_requiring_approval.length > 0) {
    grid.appendChild(renderField("commands requiring approval", fr.commands_requiring_approval));
  }
  if (Array.isArray(fr.patches_requiring_approval) && fr.patches_requiring_approval.length > 0) {
    grid.appendChild(renderField("patches requiring approval", fr.patches_requiring_approval));
  }

  container.appendChild(grid);
}

function renderErrors(fr) {
  const section = $("#errors-section");
  const list = $("#errors-list");
  list.innerHTML = "";
  if (!fr || !Array.isArray(fr.errors) || fr.errors.length === 0) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  for (const err of fr.errors) {
    if (typeof err === "string") {
      list.appendChild(el("li", { text: err }));
    } else {
      list.appendChild(el("li", {}, [
        el("pre", { class: "field-value mono", text: JSON.stringify(err, null, 2) }),
      ]));
    }
  }
}

// ------------------------------------------------------------
// Cancel + Answer
// ------------------------------------------------------------
async function onCancelTask() {
  if (!State.currentTaskId) return;
  const ok = window.confirm("Cancel this task?");
  if (!ok) return;
  try {
    await Api.cancelTask(State.currentTaskId);
    refreshDetail();
  } catch (e) {
    alert("Cancel failed: " + e.message);
  }
}

async function onSubmitAnswer(ev) {
  ev.preventDefault();
  const status = $("#answer-status");
  status.className = "form-status";
  status.textContent = "";
  let text = $("#answer-text").value.trim();
  const questionInputs = $$(".answer-questionnaire-input");
  const answeredQuestions = [];
  for (const input of questionInputs) {
    const value = (input.value || "").trim();
    const idx = input.getAttribute("data-question-index") || String(answeredQuestions.length + 1);
    if (value) answeredQuestions.push(idx + ". " + value);
  }
  if (answeredQuestions.length > 0) {
    text = answeredQuestions.join("\n") + (text ? "\n\nAdditional context:\n" + text : "");
  }
  if (!text) {
    status.className = "form-status error";
    status.textContent = "Please enter an answer.";
    return;
  }
  try {
    status.textContent = "Sending...";
    await Api.answerTask(State.currentTaskId, text);
    status.className = "form-status ok";
    status.textContent = "Answer sent.";
    $("#answer-text").value = "";
    for (const input of questionInputs) input.value = "";
    refreshDetail();
  } catch (e) {
    status.className = "form-status error";
    status.textContent = "Failed: " + e.message;
  }
}

// ------------------------------------------------------------
// Export to decision record
// ------------------------------------------------------------
function hideExportFeedback() {
  const fb = $("#export-feedback");
  if (!fb) return;
  fb.hidden = true;
  fb.className = "export-feedback";
  fb.innerHTML = "";
}

function showExportFeedback(path) {
  const fb = $("#export-feedback");
  if (!fb) return;
  fb.className = "export-feedback";
  fb.innerHTML = "";

  const row = el("div", { class: "export-feedback-row" });
  row.appendChild(el("span", {
    class: "export-feedback-message",
    text: "Exported to",
  }));
  row.appendChild(el("code", { class: "export-feedback-path", text: path }));
  // Reuse the existing copy-button factory so the look matches the rest of the UI.
  row.appendChild(makeCopyButton(path, "export path"));
  fb.appendChild(row);

  fb.appendChild(el("p", {
    class: "export-feedback-note",
    html: "Future versions may also write to <code>docs/decisions/</code> for "
      + "consciously-archived task threads.",
  }));
  fb.hidden = false;
}

function showExportError(message) {
  const fb = $("#export-feedback");
  if (!fb) return;
  fb.className = "export-feedback error";
  fb.innerHTML = "";
  fb.appendChild(el("div", { class: "export-feedback-row" }, [
    el("span", {
      class: "export-feedback-message",
      text: "Export failed: " + (message || "unknown error"),
    }),
  ]));
  fb.hidden = false;
}

async function onExportTask() {
  if (!State.currentTaskId) return;
  const btn = $("#export-btn");
  // Reset prior feedback before kicking off a new export.
  hideExportFeedback();
  if (btn) {
    btn.disabled = true;
    btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
    btn.textContent = "Exporting...";
  }
  let succeeded = false;
  try {
    const resp = await Api.exportTask(State.currentTaskId);
    const path = (resp && resp.export_path) ? resp.export_path : "(unknown path)";
    showExportFeedback(path);
    succeeded = true;
  } catch (e) {
    showExportError(e && e.message ? e.message : String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      // On success, refreshDetail() below will re-render and set the correct
      // "Re-export" label via updateExportButtonState. Restore the original
      // text now so the "Exporting..." spinner state doesn't linger if the
      // refresh fails or hasn't run yet.
      btn.textContent = btn.dataset.originalText || "Export to decision record";
    }
  }
  if (succeeded) {
    // Pick up exported_at + export_path from the backend so the button flips
    // to "Re-export" and the "Last exported" hint appears.
    await refreshDetail();
  }
}

// ------------------------------------------------------------
// Download task detail (PDF / DOCX / MD / TXT) via the browser Save dialog
// ------------------------------------------------------------
const DOWNLOAD_FORMAT_META = {
  pdf:  { mime: "application/pdf", desc: "PDF document", ext: ".pdf" },
  docx: { mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", desc: "Word document", ext: ".docx" },
  md:   { mime: "text/markdown", desc: "Markdown file", ext: ".md" },
  txt:  { mime: "text/plain", desc: "Text file", ext: ".txt" },
};

function filenameFromContentDisposition(header, fallback) {
  if (!header) return fallback;
  // Handle: attachment; filename="name.ext"  and  filename*=UTF-8''name.ext
  const star = /filename\*=(?:UTF-8'')?["']?([^"';]+)/i.exec(header);
  if (star && star[1]) { try { return decodeURIComponent(star[1]); } catch (e) { return star[1]; } }
  const plain = /filename=["']?([^"';]+)/i.exec(header);
  if (plain && plain[1]) return plain[1];
  return fallback;
}

async function onDownloadDetail() {
  if (!State.currentTaskId) return;
  const sel = $("#download-format");
  const fmt = (sel && sel.value) || "pdf";
  const meta = DOWNLOAD_FORMAT_META[fmt] || DOWNLOAD_FORMAT_META.pdf;
  const btn = $("#download-detail-btn");

  hideExportFeedback();
  if (btn) { btn.disabled = true; btn.dataset.originalText = btn.dataset.originalText || btn.textContent; btn.textContent = "Preparing…"; }

  try {
    const resp = await fetch(`/api/tasks/${encodeURIComponent(State.currentTaskId)}/download?format=${encodeURIComponent(fmt)}`);
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { const j = await resp.json(); if (j && j.detail) detail = j.detail; } catch (e) { /* not json */ }
      throw new Error(detail);
    }
    const blob = await resp.blob();
    const suggestedName = filenameFromContentDisposition(
      resp.headers.get("Content-Disposition"),
      `${State.currentTaskId}${meta.ext}`,
    );

    // Preferred: native Save dialog (Chromium). The user picks folder + filename.
    if (window.showSaveFilePicker) {
      try {
        const handle = await window.showSaveFilePicker({
          suggestedName,
          types: [{ description: meta.desc, accept: { [meta.mime]: [meta.ext] } }],
        });
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
        showExportFeedback(`Saved as “${handle.name || suggestedName}”.`);
        return;
      } catch (e) {
        // AbortError = user cancelled the dialog. Anything else: fall through to <a download>.
        if (e && e.name === "AbortError") {
          showExportFeedback("Download cancelled.");
          return;
        }
      }
    }

    // Fallback: anchor download. The browser's own Save As dialog (if enabled)
    // still lets the user choose the location.
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = suggestedName;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    showExportFeedback(`Downloaded “${suggestedName}”. Check your browser's downloads folder.`);
  } catch (e) {
    showExportError(e && e.message ? e.message : String(e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.originalText || "Download…"; }
  }
}

// ------------------------------------------------------------
// Settings view — API keys (parameterized by key name)
// ------------------------------------------------------------
// Each entry: the DOM ids for that key's row + its env-var name + a label.
const API_KEY_FIELDS = {
  openrouter: {
    label: "OpenRouter", envName: "OPENROUTER_API_KEY",
    input: "#openrouter-key-input", eyeball: "#openrouter-key-eyeball",
    status: "#openrouter-key-status", feedback: "#openrouter-key-feedback",
    save: "#openrouter-key-save", clear: "#openrouter-key-clear",
  },
};
// Per-key transient state: where the current value comes from, and whether the
// field already holds a revealed value (so the eyeball doesn't re-fetch).
const ApiKeyState = {};  // name -> { source: "env"|"db"|"none", revealed: bool }

function _kf(name) { return API_KEY_FIELDS[name]; }

function _keyFeedback(name, msg, kind) {
  const cfg = _kf(name); if (!cfg) return;
  const fb = $(cfg.feedback); if (!fb) return;
  fb.hidden = false;
  fb.className = "api-key-feedback " + (kind || "ok");
  fb.textContent = msg;
}
function _clearKeyFeedback(name) {
  const cfg = _kf(name); if (!cfg) return;
  const fb = $(cfg.feedback);
  if (fb) { fb.hidden = true; fb.textContent = ""; }
}

async function loadApiKeysSettings() {
  // Reset all rows to a clean state, then populate from one GET.
  for (const name of Object.keys(API_KEY_FIELDS)) {
    const cfg = _kf(name);
    _clearKeyFeedback(name);
    const input = $(cfg.input), eyeball = $(cfg.eyeball), statusEl = $(cfg.status);
    if (input) { input.value = ""; input.type = "password"; }
    if (eyeball) eyeball.classList.remove("revealed");
    ApiKeyState[name] = { source: "none", revealed: false };
    if (statusEl) { statusEl.textContent = "Loading…"; statusEl.className = "api-key-status"; }
  }
  let data = null;
  try { data = await Api.getApiKeys(); }
  catch (e) {
    for (const name of Object.keys(API_KEY_FIELDS)) {
      const statusEl = $(_kf(name).status);
      if (statusEl) { statusEl.textContent = "Could not load settings: " + (e && e.message ? e.message : e); statusEl.className = "api-key-status"; }
    }
    return;
  }
  for (const name of Object.keys(API_KEY_FIELDS)) {
    const cfg = _kf(name);
    const o = (data && data[name]) || { set: false, source: "none", masked: null };
    ApiKeyState[name] = { source: o.source || "none", revealed: false };
    const input = $(cfg.input), statusEl = $(cfg.status);
    if (input) input.placeholder = o.masked ? o.masked : "(not set)";
    if (!statusEl) continue;
    if (o.source === "env") {
      statusEl.textContent = `Currently set via the ${cfg.envName} environment variable — the stored key is ignored. (Saving here changes the stored key but won't take effect until the env var is unset.)`;
      statusEl.className = "api-key-status is-env";
    } else if (o.source === "db") {
      statusEl.textContent = `Currently using the stored database key (${o.masked || "set"}). Click the eyeball to reveal it.`;
      statusEl.className = "api-key-status is-set";
    } else {
      statusEl.textContent = `Not set. Paste a ${cfg.label} API key and click Save, or export ${cfg.envName} in the environment.`;
      statusEl.className = "api-key-status";
    }
  }
}

async function onToggleKeyVisibility(name) {
  const cfg = _kf(name); if (!cfg) return;
  const input = $(cfg.input), eyeball = $(cfg.eyeball);
  if (!input) return;
  const labelEl = eyeball ? eyeball.querySelector(".eyeball-label") : null;
  const st = ApiKeyState[name] || { source: "none", revealed: false };
  if (input.type === "text") {
    input.type = "password";
    if (eyeball) {
      eyeball.classList.remove("revealed");
      eyeball.setAttribute("aria-pressed", "false");
    }
    if (labelEl) labelEl.textContent = "Show";
    return;
  }
  if (!input.value && st.source === "db" && !st.revealed) {
    try {
      const r = await Api.revealApiKey(name);
      if (r && typeof r.value === "string") { input.value = r.value; st.revealed = true; ApiKeyState[name] = st; }
      else if (r && r.note) { _keyFeedback(name, r.note, "ok"); }
    } catch (e) {
      _keyFeedback(name, "Could not reveal: " + (e && e.message ? e.message : e), "error");
      return;
    }
  } else if (!input.value && st.source === "env") {
    _keyFeedback(name, `This key is set via the ${cfg.envName} environment variable; its value isn't stored here and can't be shown.`, "ok");
    return;
  }
  input.type = "text";
  if (eyeball) {
    eyeball.classList.add("revealed");
    eyeball.setAttribute("aria-pressed", "true");
  }
  if (labelEl) labelEl.textContent = "Hide";
}

async function onSaveKey(name) {
  const cfg = _kf(name); if (!cfg) return;
  const input = $(cfg.input), btn = $(cfg.save);
  if (!input) return;
  const value = input.value.trim();
  if (!value) {
    _keyFeedback(name, "Nothing to save — the field is empty. Use “Clear stored key” to remove a stored key.", "error");
    return;
  }
  if (btn) { btn.disabled = true; btn.dataset.originalText = btn.dataset.originalText || btn.textContent; btn.textContent = "Saving…"; }
  try {
    const r = await Api.setApiKey(name, value);
    if (r && r.ok) { _keyFeedback(name, "Saved to the database.", "ok"); await loadApiKeysSettings(); }
    else { _keyFeedback(name, "Save failed: " + (r && r.error ? r.error : "unknown error"), "error"); }
  } catch (e) {
    _keyFeedback(name, "Save failed: " + (e && e.message ? e.message : e), "error");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.originalText || "Save"; }
  }
}

async function onClearKey(name) {
  const cfg = _kf(name); if (!cfg) return;
  if (!confirm(`Remove the stored ${cfg.label} API key from the database?`)) return;
  const btn = $(cfg.clear);
  if (btn) { btn.disabled = true; }
  try {
    const r = await Api.setApiKey(name, "");
    if (r && r.ok) { _keyFeedback(name, "Stored key cleared.", "ok"); await loadApiKeysSettings(); }
    else { _keyFeedback(name, "Clear failed: " + (r && r.error ? r.error : "unknown error"), "error"); }
  } catch (e) {
    _keyFeedback(name, "Clear failed: " + (e && e.message ? e.message : e), "error");
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

// ------------------------------------------------------------
// Recent Tasks view
// ------------------------------------------------------------
async function loadRecentTasks() {
  const list = document.getElementById("recent-tasks-list");
  const status = document.getElementById("recent-tasks-status");
  if (!list) return;
  list.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const data = await Api.listTasks({ limit: 15 });
    const tasks = (data && data.tasks) || [];
    if (status) status.textContent = tasks.length + " most recent";
    if (tasks.length === 0) {
      list.innerHTML = '<p class="muted" style="padding:16px 0">No tasks yet.</p>';
      return;
    }
    list.innerHTML = "";
    for (const t of tasks) {
      const row = el("div", { class: "rt-row", role: "button", tabindex: "0" });
      row.dataset.taskId = t.id;
      const meta = el("div", { class: "rt-meta" });
      meta.appendChild(el("span", { class: "badge status-" + t.status, text: t.status }));
      meta.appendChild(el("span", { class: "badge mode-" + t.mode, text: t.mode }));
      meta.appendChild(el("span", { class: "rt-time muted", text: fmtRelTime(t.created_at) }));
      const snippet = el("div", {
        class: "rt-snippet",
        text: t.user_request_snippet || t.id,
      });
      row.appendChild(meta);
      row.appendChild(snippet);
      row.addEventListener("click", () => openDetail(t.id));
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") openDetail(t.id);
      });
      list.appendChild(row);
    }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    list.innerHTML = '<p class="loading">Failed to load: ' + escapeHtml(msg) + "</p>";
  }
}

// ------------------------------------------------------------
// Usage & Spend view
// ------------------------------------------------------------
async function loadUsage() {
  const content = document.getElementById("usage-content");
  if (!content) return;
  content.innerHTML = '<p class="loading">Loading…</p>';
  try {
    const data = await Api.usageSummary();
    renderUsage(data);
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    content.innerHTML = '<p class="loading">Failed to load: ' + escapeHtml(msg) + "</p>";
  }
}

function renderUsage(data) {
  const content = document.getElementById("usage-content");
  if (!content) return;
  content.innerHTML = "";

  const todayAgents = (data && data.today_by_agent) || [];
  const daily       = (data && data.daily_7d) || [];
  const allTime     = (data && data.all_time) || {};

  const todayTotal = todayAgents.reduce((s, a) => s + (a.cost_usd || 0), 0);
  const weekly7d   = daily.reduce((s, d) => s + (d.cost_usd || 0), 0);

  // Summary cards
  const cards = el("div", { class: "usage-cards" });
  cards.appendChild(_usageCard("Today", todayTotal > 0 ? fmtUsd(todayTotal) : "$0", "OpenRouter spend"));
  cards.appendChild(_usageCard("Last 7 days", weekly7d > 0 ? fmtUsd(weekly7d) : "$0", "OpenRouter spend"));
  cards.appendChild(_usageCard("All-time tasks", String(allTime.task_count || 0), "tasks run"));
  const totalTok = (allTime.input_tokens || 0) + (allTime.output_tokens || 0);
  cards.appendChild(_usageCard("All-time tokens", _fmtTok(totalTok), "input + output"));
  content.appendChild(cards);

  // 7-day bar chart
  const chartSec = el("div", { class: "usage-section" });
  chartSec.appendChild(el("h3", { text: "Daily OpenRouter spend — last 7 days" }));
  if (daily.length > 0) {
    const maxCost = Math.max(...daily.map((d) => d.cost_usd || 0), 0.00001);
    const chart = el("div", { class: "usage-chart" });
    for (const d of daily) {
      const cost = d.cost_usd || 0;
      const pct  = Math.max((cost / maxCost) * 100, cost > 0 ? 3 : 0);
      const col  = el("div", { class: "usage-bar-col" });
      const fill = el("div", {
        class: "usage-bar-fill",
        title: d.day + ": " + (cost > 0 ? fmtUsd(cost) : "$0"),
      });
      fill.style.height = pct + "%";
      col.appendChild(fill);
      col.appendChild(el("div", { class: "usage-bar-label", text: d.day.slice(5) }));
      chart.appendChild(col);
    }
    chartSec.appendChild(chart);
  } else {
    chartSec.appendChild(el("p", { class: "muted", text: "No spend data in the last 7 days." }));
  }
  content.appendChild(chartSec);

  // Today per-agent breakdown
  if (todayAgents.length > 0) {
    const agentSec = el("div", { class: "usage-section" });
    agentSec.appendChild(el("h3", { text: "Today — by agent" }));
    const table = el("table", { class: "usage-agent-table" });
    const thead = el("thead");
    thead.innerHTML = "<tr><th>Agent</th><th class='num'>Runs</th><th class='num'>Input tok</th><th class='num'>Output tok</th><th class='num'>Spend</th></tr>";
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const a of todayAgents) {
      const tr = el("tr");
      tr.appendChild(el("td", { text: a.agent_name }));
      tr.appendChild(el("td", { class: "num", text: String(a.run_count || 0) }));
      tr.appendChild(el("td", { class: "num", text: fmtInt(a.input_tokens || 0) }));
      tr.appendChild(el("td", { class: "num", text: fmtInt(a.output_tokens || 0) }));
      tr.appendChild(el("td", { class: "num", text: a.cost_usd > 0 ? fmtUsd(a.cost_usd) : "—" }));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    agentSec.appendChild(table);
    content.appendChild(agentSec);
  }

  content.appendChild(el("p", {
    class: "muted usage-note",
    text: "CLI seats (Codex, Gemini, Claude Code) run on subscriptions — their turns appear in token counts only, not in spend.",
  }));
}

function _usageCard(title, value, subtitle) {
  const card = el("div", { class: "usage-card" });
  card.appendChild(el("div", { class: "usage-card-title", text: title }));
  card.appendChild(el("div", { class: "usage-card-value", text: value }));
  card.appendChild(el("div", { class: "usage-card-sub",   text: subtitle }));
  return card;
}

function _fmtTok(n) {
  if (!n || n === 0) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

// ------------------------------------------------------------
// Wire-up
// ------------------------------------------------------------
function init() {
  applyTheme();
  renderSidebar();
  // renderSidebar created the theme button — refresh its glyph now.
  updateThemeGlyph();
  document.addEventListener("keydown", _onGlobalKeydown);

  for (const t of $$(".tab")) {
    t.addEventListener("click", () => switchView(t.dataset.view));
  }
  for (const name of Object.keys(API_KEY_FIELDS)) {
    const cfg = _kf(name);
    const saveBtn = $(cfg.save), clearBtn = $(cfg.clear), eyeball = $(cfg.eyeball);
    if (saveBtn) saveBtn.addEventListener("click", () => onSaveKey(name));
    if (clearBtn) clearBtn.addEventListener("click", () => onClearKey(name));
    if (eyeball) eyeball.addEventListener("click", () => onToggleKeyVisibility(name));
  }

  $("#mode").addEventListener("change", () => {
    updateModeHint();
    renderAgentsList();
  });
  updateModeHint();

  $("#new-task-form").addEventListener("submit", onSubmitNewTask);
  $("#answer-form").addEventListener("submit", onSubmitAnswer);
  $("#cancel-btn").addEventListener("click", onCancelTask);

  setupAttachmentsUI();
  setupPermissionsUI();
  setupProjectSourceUI();
  setupGitDiffUI();
  setupInboxFiltersUI();
  const startNewBtn = $("#start-new-btn");
  const followupBtn = $("#followup-btn");
  const exportBtn = $("#export-btn");
  if (startNewBtn) startNewBtn.addEventListener("click", onStartNewTask);
  if (followupBtn) followupBtn.addEventListener("click", onSubmitFollowup);
  if (exportBtn) exportBtn.addEventListener("click", onExportTask);
  const downloadDetailBtn = $("#download-detail-btn");
  if (downloadDetailBtn) downloadDetailBtn.addEventListener("click", onDownloadDetail);
  const followupDismiss = $("#followup-banner-dismiss");
  if (followupDismiss) followupDismiss.addEventListener("click", clearFollowupParent);

  const usageRefreshBtn = document.getElementById("usage-refresh-btn");
  if (usageRefreshBtn) usageRefreshBtn.addEventListener("click", loadUsage);

  loadAgents();
  setupHealthIndicator();
  setupPricingTableHeaders();
  pollHealth();
  setInterval(pollHealth, 15000);

  switchView("new");
}

document.addEventListener("DOMContentLoaded", init);
