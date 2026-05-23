# Decision Record 0024 — Dashboard Plugin System (frontend-only, v1)

**Date**: 2026-05-23  
**Status**: Ratified by Glen  
**Mode**: Glen-directed

## What was chosen

The dashboard gains a small **frontend-only plugin system** with a manifest loader, four extension-point arrays, and four delimited hooks in `dashboard.js`. Plugin behavior is purely additive: with an empty manifest, dashboard rendering is byte-identical to the pre-plugin version.

`app/dashboard/plugin_loader.js` defines `window.Plugins` with `register(spec)` and four extension-point arrays: `sidebarTabs`, `inboxRowActions`, `inboxFilters`, `detailPanels`. On `DOMContentLoaded` the loader fetches `/static/plugins/manifest.json`, dynamically loads each listed plugin script in order, and each script calls `Plugins.register(...)` to contribute UI.

Four small, clearly-delimited hooks land in `app/dashboard/dashboard.js`: sidebar render, `switchView` dispatch, `renderInbox` row build, `setupInboxFiltersUI`, and `renderDetail`. Each consults `window.Plugins` and renders contributions. The hooks no-op when no plugins are registered.

Each plugin callback receives a per-plugin `api` object: `el`, `fetchJSON`, `refreshInbox`, `openDetail`, read-only `state.currentTaskId` and `state.view`, and a namespaced `log`. A reserved-id guard prevents a plugin sidebarTab id from colliding with a core view (`new`, `inbox`, `detail`, `help`, `pricing`, `settings`, `recent-tasks`, `usage`, `theme`).

A demo plugin at `app/dashboard/plugins/example-hello.js` provides the copy-paste template. Two plugins shipped on the same branch use this system against real features: a failure-cause detail panel (DR0022) and a trajectory tools plugin with a sidebar tab and per-row export button (DR0023).

The inspiration is NousResearch/hermes-example-plugins (self-registering manifest with tab injection, slot injection, theming). Ours is much smaller and frontend-only in v1. Pattern referenced as inspiration only.

## Why

Two features shipped on this branch (failure-cause detail panel, trajectory tools) wanted dashboard UI surface area without bloating `dashboard.js` further (already ~4400 lines). A minimal plugin loader lets feature UI live in its own file, makes the dashboard's extension points explicit, and validates the abstraction against two real consumers before generalizing further.

## What was rejected

- **Backend route registration by plugins.** V1 plugins use existing endpoints only. Deferred because it requires module-loading at startup and a sandbox model we do not need yet.
- **Plugin sandboxing.** Single-user local tool; plugin code runs in the same JS realm as the dashboard. Reasonable for now; revisit if third-party plugins become a thing.
- **Full plugin filter-predicate API.** V1 plugins can add a filter dropdown but the predicate must filter client-side via their own re-render. Core inbox filters that need server cooperation stay inline in `dashboard.js` — the failure-cause inbox filter is one of those.

## Known risks

- Plugins share the JS realm with the dashboard; a buggy plugin can take down the page. Acceptable in single-user local context.
- Hook surface area is small; new extension points will require extending the loader and the `dashboard.js` hooks together.

## Open questions

- Should v2 add a permissioned filter-predicate API so failure-cause-style filters become plugins too?
- Should plugins declare a backend route slot once we have a use case?

## Who is keeping continuity

Claude Code, as keeper.

## Operability Impact

- **DB**: none.
- **Audit trail**: none.
- **Recoverability**: unchanged.
- **Retention**: unchanged.
- **Empty-manifest behavior**: identical to the pre-plugin dashboard (verified by the implementing agent).
- **Risk**: a single buggy plugin can break dashboard render; revert by removing it from `manifest.json`.
