// example-hello.js — minimal demo plugin for the dashboard plugin system.
//
// Purpose:
//   Prove that the loader wiring works and serve as a copy-paste template
//   for new plugins. This plugin contributes ONE sidebar tab labeled
//   "Hello". When the user clicks it, the main content area shows a tiny
//   greeting panel.
//
// To add your own plugin:
//   1. Create app/dashboard/plugins/<your-name>.js that calls
//      window.Plugins.register({...}).
//   2. Add "<your-name>.js" to plugins/manifest.json.
//   3. Reload the dashboard.
//
"use strict";

(function () {
  if (!window.Plugins || typeof window.Plugins.register !== "function") {
    console.warn("[example-hello] window.Plugins missing — plugin_loader.js not loaded?");
    return;
  }

  window.Plugins.register({
    name: "example-hello",
    version: "0.1.0",

    sidebarTabs: [
      {
        id: "example-hello",
        label: "Hello (demo plugin)",
        // Simple inline icon. Anything renderable as innerHTML works here.
        icon: "<span aria-hidden=\"true\">★</span>",
        onActivate: function (rootEl, api) {
          api.log("activated");
          rootEl.appendChild(api.el("div", { class: "view-header" }, [
            api.el("h2", { text: "Hello from a plugin" }),
          ]));
          rootEl.appendChild(api.el("p", {
            text: "Plugins are loaded from app/dashboard/plugins/. "
                + "See app/dashboard/plugins/example-hello.js for the template.",
          }));
          rootEl.appendChild(api.el("p", { class: "muted" }, [
            "This whole view was rendered by a plugin. Core dashboard.js was untouched ",
            "to add it — the only change to core was the four extension-point hooks.",
          ]));
        },
      },
    ],

    // Optional init hook fires once at registration time (before any tab is
    // clicked). Use it for one-time setup. Receives the plugin api.
    init: function (api) {
      api.log("registered and ready");
    },
  });
})();
