// trajectory-tools.js
//
// Plugin: surfaces the trajectory exporter (DR0023) in the dashboard.
//
//   - Sidebar tab "Trajectories" with a one-click "export all terminal-task
//     trajectories" button (calls POST /api/trajectories/export-all).
//   - Per-row "Export trajectory" action in the inbox for terminal tasks
//     (calls POST /api/tasks/{id}/trajectory/export).

(function () {
  "use strict";
  if (!window.Plugins || typeof window.Plugins.register !== "function") return;

  const TERMINAL_STATUSES = new Set([
    "completed", "failed", "cancelled", "cannot_resolve",
  ]);

  // Plain download icon for the per-row button; matches the existing icon
  // style in dashboard.js (currentColor stroke, 24x24 viewBox).
  const DOWNLOAD_ICON_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>' +
    '<polyline points="7 10 12 15 17 10"/>' +
    '<line x1="12" y1="15" x2="12" y2="3"/>' +
    '</svg>';

  function summariseExportAll(body) {
    if (!body || typeof body !== "object") return "Done.";
    const written = body.written ?? body.exported ?? body.count ?? 0;
    const skipped = body.skipped ?? 0;
    const errors = Array.isArray(body.errors) ? body.errors : [];
    let msg = "Wrote " + written + " trajectory file" + (written === 1 ? "" : "s");
    if (skipped) msg += "; skipped " + skipped;
    if (errors.length) msg += "; " + errors.length + " error" + (errors.length === 1 ? "" : "s");
    return msg + ".";
  }

  window.Plugins.register({
    name: "trajectory-tools",
    version: "0.1.0",
    sidebarTabs: [
      {
        id: "trajectories",
        label: "Trajectories",
        onActivate: function (rootEl, api) {
          rootEl.appendChild(api.el("h2", { text: "Task trajectories" }));
          rootEl.appendChild(api.el("p", {
            class: "muted",
            text: "Every completed task is auto-exported as a self-contained "
                + "JSONL file under data/exports/trajectories/<task_id>.jsonl "
                + "(DR0023). Use this page to bulk-export every terminal task "
                + "that hasn't been written yet, or re-export them all.",
          }));

          const btn = api.el("button", {
            type: "button",
            class: "btn btn-primary",
            text: "Export all terminal-task trajectories",
          });
          const statusLine = api.el("div", {
            class: "muted",
            style: "margin-top: 12px;",
          });

          btn.addEventListener("click", async () => {
            btn.disabled = true;
            const prev = btn.textContent;
            btn.textContent = "Exporting...";
            statusLine.textContent = "";
            try {
              const resp = await fetch("/api/trajectories/export-all", {
                method: "POST",
              });
              if (!resp.ok) throw new Error("HTTP " + resp.status);
              const body = await resp.json();
              statusLine.textContent = summariseExportAll(body);
            } catch (e) {
              statusLine.textContent = "Export failed: "
                + (e && e.message ? e.message : e);
            } finally {
              btn.disabled = false;
              btn.textContent = prev;
            }
          });

          rootEl.appendChild(btn);
          rootEl.appendChild(statusLine);

          rootEl.appendChild(api.el("p", {
            class: "muted small",
            style: "margin-top: 24px;",
            text: "Trajectories are a portable export of the SQLite history — "
                + "one self-contained JSON record per task, including the "
                + "transcript, per-run timings, final result (with action plan "
                + "and failure-cause tags), and your recorded decision.",
          }));
        },
      },
    ],
    inboxRowActions: [
      {
        id: "export-trajectory",
        label: "Export trajectory",
        icon: DOWNLOAD_ICON_SVG,
        onClick: async function (task, api) {
          if (!task || !TERMINAL_STATUSES.has(task.status)) {
            window.alert(
              "Trajectories are only exportable once a task reaches a "
              + "terminal status (completed / failed / cancelled / cannot_resolve)."
            );
            return;
          }
          try {
            const resp = await fetch(
              "/api/tasks/" + encodeURIComponent(task.id)
                + "/trajectory/export",
              { method: "POST" }
            );
            if (!resp.ok) {
              let detail = "HTTP " + resp.status;
              try {
                const j = await resp.json();
                if (j && j.detail) detail = j.detail;
              } catch (_) { /* ignore */ }
              window.alert("Export failed: " + detail);
              return;
            }
            const body = await resp.json();
            window.alert("Wrote trajectory to:\n" + (body.path || "(unknown path)"));
          } catch (e) {
            window.alert("Export failed: " + (e && e.message ? e.message : e));
          }
        },
      },
    ],
  });
})();
