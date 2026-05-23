// The AI Conclave dashboard - plugin loader (v1: frontend-only plugins).
//
// What this is:
//   A tiny shim that lets future UI features attach themselves to the
//   dashboard without editing dashboard.js's core. Plugin scripts live in
//   app/dashboard/plugins/ and are listed in plugins/manifest.json. Each
//   script calls window.Plugins.register({...}) on load.
//
// What it is NOT (v1):
//   - No backend route registration. Plugins use the existing FastAPI
//     endpoints. Backend route plugins are deferred.
//   - No isolation / sandboxing. Plugins run with full window access; only
//     load ones you trust.
//
// Wire-up:
//   1. index.html loads /static/plugin_loader.js BEFORE /static/dashboard.js
//      so window.Plugins exists when dashboard.js starts inspecting it.
//   2. On DOMContentLoaded the loader fetches /static/plugins/manifest.json,
//      then for each listed name appends a <script src="/static/plugins/<name>">
//      tag. Each plugin script registers itself synchronously on parse.
//   3. dashboard.js consults window.Plugins.<extensionPoint> at the four
//      hook sites (sidebar, inbox rows, inbox filters, detail panels) and
//      renders any contributions.
//
// Extension point arrays are populated by Plugins.register(). The dashboard
// reads them via the small helper getters at the bottom of this file.
//
"use strict";

(function () {
  if (window.Plugins) return; // already initialized — defensive

  // ------------------------------------------------------------------
  // Plugin registry storage
  // ------------------------------------------------------------------
  const registered = [];             // raw specs in registration order
  const sidebarTabs = [];            // flattened per-extension-point arrays
  const inboxRowActions = [];        // for fast iteration by dashboard.js
  const inboxFilters = [];
  const detailPanels = [];
  // Map sidebarTab.id -> { plugin, tab } so switchView() can dispatch by id.
  const sidebarTabIndex = Object.create(null);

  // Reserved view ids in core dashboard.js — plugins must not collide.
  const RESERVED_IDS = new Set([
    "new", "inbox", "detail", "help", "pricing",
    "settings", "recent-tasks", "usage", "theme",
  ]);

  function _log(name, msg, level) {
    const prefix = "[plugin:" + name + "]";
    if (level === "warn" && console.warn) console.warn(prefix, msg);
    else if (level === "error" && console.error) console.error(prefix, msg);
    else console.log(prefix, msg);
  }

  // ------------------------------------------------------------------
  // Plugin API (passed to every plugin callback)
  // ------------------------------------------------------------------
  // We build this lazily per-plugin so each call can carry the plugin name
  // for namespaced logging. Plugins should NOT cache the api object across
  // plugins — it's intentionally per-instance.
  function _buildApi(pluginName) {
    return {
      // Mirror of dashboard.js's `el` helper. Pulled from window at call time
      // so this loader doesn't have to import or duplicate it.
      el: function (tag, attrs, children) {
        if (typeof window.el === "function") {
          return window.el(tag, attrs || {}, children || []);
        }
        // Minimal fallback if core el isn't on window for some reason.
        const node = document.createElement(tag);
        if (attrs && typeof attrs === "object") {
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
        }
        for (const c of [].concat(children || [])) {
          if (c === null || c === undefined) continue;
          node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        }
        return node;
      },

      // Small fetch-and-parse-JSON helper. Throws on non-2xx.
      fetchJSON: async function (url, opts) {
        const resp = await fetch(url, opts);
        if (!resp.ok) {
          const err = new Error("HTTP " + resp.status + " from " + url);
          err.status = resp.status;
          throw err;
        }
        const ct = resp.headers.get("content-type") || "";
        if (ct.indexOf("application/json") === -1) {
          // Tolerate servers that omit content-type — try anyway.
          return resp.json();
        }
        return resp.json();
      },

      // Re-fetch the inbox. Delegates to dashboard.js.
      refreshInbox: function () {
        if (typeof window.refreshInbox === "function") {
          return window.refreshInbox();
        }
      },

      // Switch to detail view for a task. Delegates to dashboard.js.
      openDetail: function (taskId) {
        if (typeof window.openDetail === "function") {
          return window.openDetail(taskId);
        }
      },

      // Read-only access to a curated subset of dashboard State. We expose
      // only what plugins legitimately need; extend deliberately.
      state: {
        get currentTaskId() {
          return (window.State && window.State.currentTaskId) || null;
        },
        get view() {
          return (window.State && window.State.view) || null;
        },
      },

      // Namespaced console logging — easier to grep when debugging.
      log: function (msg, level) {
        _log(pluginName, msg, level);
      },
    };
  }

  // ------------------------------------------------------------------
  // register(spec)
  // ------------------------------------------------------------------
  function register(spec) {
    if (!spec || typeof spec !== "object") {
      console.warn("[Plugins] register() called with non-object; ignored.");
      return false;
    }
    if (typeof spec.name !== "string" || !spec.name.trim()) {
      console.warn("[Plugins] register() rejected: missing .name");
      return false;
    }
    const name = spec.name.trim();
    if (registered.some((p) => p.name === name)) {
      console.warn("[Plugins] register() rejected: duplicate plugin name '" + name + "'");
      return false;
    }
    registered.push(spec);

    // sidebarTabs
    if (Array.isArray(spec.sidebarTabs)) {
      for (const tab of spec.sidebarTabs) {
        if (!tab || typeof tab.id !== "string" || !tab.id.trim()) {
          _log(name, "sidebarTab without an id ignored", "warn");
          continue;
        }
        const tabId = tab.id.trim();
        if (RESERVED_IDS.has(tabId)) {
          _log(name, "sidebarTab id '" + tabId + "' collides with a core view id; ignored", "warn");
          continue;
        }
        if (sidebarTabIndex[tabId]) {
          _log(name, "sidebarTab id '" + tabId + "' already registered by another plugin; ignored", "warn");
          continue;
        }
        const entry = { plugin: name, tab };
        sidebarTabs.push(entry);
        sidebarTabIndex[tabId] = entry;
      }
    }

    // inboxRowActions
    if (Array.isArray(spec.inboxRowActions)) {
      for (const action of spec.inboxRowActions) {
        if (!action || typeof action.id !== "string") {
          _log(name, "inboxRowAction without an id ignored", "warn");
          continue;
        }
        inboxRowActions.push({ plugin: name, action });
      }
    }

    // inboxFilters
    if (Array.isArray(spec.inboxFilters)) {
      for (const filter of spec.inboxFilters) {
        if (!filter || typeof filter.id !== "string") {
          _log(name, "inboxFilter without an id ignored", "warn");
          continue;
        }
        inboxFilters.push({ plugin: name, filter });
      }
    }

    // detailPanels
    if (Array.isArray(spec.detailPanels)) {
      for (const panel of spec.detailPanels) {
        if (!panel || typeof panel.id !== "string") {
          _log(name, "detailPanel without an id ignored", "warn");
          continue;
        }
        detailPanels.push({ plugin: name, panel });
      }
    }

    // init hook — fire-and-forget; errors don't break the registry.
    if (typeof spec.init === "function") {
      try {
        spec.init(_buildApi(name));
      } catch (e) {
        _log(name, "init() threw: " + (e && e.message ? e.message : e), "error");
      }
    }

    _log(name, "registered" + (spec.version ? " v" + spec.version : ""));
    return true;
  }

  // ------------------------------------------------------------------
  // Per-extension-point getters used by dashboard.js
  // Each returns an array of { plugin, <itemKey> } records. dashboard.js
  // calls these at render time; the loader does no rendering itself.
  // ------------------------------------------------------------------
  function getSidebarTabs()      { return sidebarTabs.slice(); }
  function getInboxRowActions()  { return inboxRowActions.slice(); }
  function getInboxFilters()     { return inboxFilters.slice(); }
  function getDetailPanels()     { return detailPanels.slice(); }

  // Dispatcher for plugin-contributed sidebar tabs. dashboard.js calls this
  // when switchView() is invoked with an id that isn't a core view but IS a
  // registered plugin tab. The loader mounts a fresh root <div> and hands it
  // to the plugin's onActivate(rootEl, api).
  function activateSidebarTab(tabId, mountEl) {
    const entry = sidebarTabIndex[tabId];
    if (!entry) return false;
    const tab = entry.tab;
    if (!mountEl) return false;
    mountEl.innerHTML = "";
    const root = document.createElement("div");
    root.className = "plugin-view-root";
    root.dataset.pluginTabId = tabId;
    mountEl.appendChild(root);
    if (typeof tab.onActivate === "function") {
      try {
        tab.onActivate(root, _buildApi(entry.plugin));
      } catch (e) {
        _log(entry.plugin, "onActivate threw: " + (e && e.message ? e.message : e), "error");
      }
    }
    return true;
  }

  // Convenience: build the api object for use by core dashboard.js code
  // (e.g. when invoking an inbox row action's onClick).
  function apiFor(pluginName) {
    return _buildApi(pluginName);
  }

  // ------------------------------------------------------------------
  // Manifest loader. Called on DOMContentLoaded.
  //
  // manifest.json shape:
  //   { "plugins": ["example-hello.js", "another.js"] }
  //
  // Behavior:
  //   - Missing or empty manifest is a no-op (zero plugins).
  //   - A failed individual script load is logged but doesn't abort the rest.
  //   - Scripts are appended sequentially so registration order is stable.
  // ------------------------------------------------------------------
  async function _loadManifest() {
    let manifest = null;
    try {
      const resp = await fetch("/static/plugins/manifest.json", {
        cache: "no-cache",
      });
      if (!resp.ok) {
        // 404 means no manifest exists yet — that's fine, just skip silently.
        if (resp.status === 404) return;
        console.warn("[Plugins] manifest.json fetch failed: HTTP " + resp.status);
        return;
      }
      manifest = await resp.json();
    } catch (e) {
      console.warn("[Plugins] manifest.json fetch error:", e && e.message ? e.message : e);
      return;
    }
    if (!manifest || !Array.isArray(manifest.plugins) || manifest.plugins.length === 0) {
      return; // empty manifest, nothing to do
    }
    for (const name of manifest.plugins) {
      if (typeof name !== "string" || !name.trim()) continue;
      await _loadOne(name.trim());
    }
  }

  function _loadOne(scriptName) {
    return new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = "/static/plugins/" + scriptName;
      s.async = false; // preserve registration order
      s.onload = () => resolve(true);
      s.onerror = () => {
        console.warn("[Plugins] failed to load script: " + scriptName);
        resolve(false);
      };
      document.head.appendChild(s);
    });
  }

  // ------------------------------------------------------------------
  // Expose the public surface
  // ------------------------------------------------------------------
  window.Plugins = {
    register: register,
    // Extension-point arrays exposed both directly (raw read access for the
    // dashboard) and through getters for callers that want a defensive copy.
    sidebarTabs: sidebarTabs,
    inboxRowActions: inboxRowActions,
    inboxFilters: inboxFilters,
    detailPanels: detailPanels,
    getSidebarTabs: getSidebarTabs,
    getInboxRowActions: getInboxRowActions,
    getInboxFilters: getInboxFilters,
    getDetailPanels: getDetailPanels,
    activateSidebarTab: activateSidebarTab,
    apiFor: apiFor,
    // Useful for debugging from the devtools console.
    _registered: registered,
  };

  // Kick off manifest loading once DOM is ready. We delay slightly so that
  // dashboard.js (loaded after this) has installed its globals (el, State,
  // refreshInbox, openDetail) before plugin init callbacks may reference them.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _loadManifest);
  } else {
    // Already loaded (script was inserted late) — fire immediately.
    _loadManifest();
  }
})();
