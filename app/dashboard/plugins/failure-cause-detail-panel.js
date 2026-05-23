// failure-cause-detail-panel.js
//
// Plugin: adds a "Failure causes" panel to the task detail view that lists
// the FailureCause tags stamped by app/services/trace_analyzer.py (DR0022).
//
// The tags ship on `taskData.final_result.failure_cause_tags` (see
// `_row_to_final_result` in app/api/tasks.py). The list is empty for tasks
// that completed cleanly or predate the column migration.

(function () {
  "use strict";
  if (!window.Plugins || typeof window.Plugins.register !== "function") return;

  const LABELS = {
    missing_evidence: "Missing evidence",
    tool_timeout: "Tool timeout",
    bad_json_output: "Bad JSON output",
    premise_conflict: "Premise conflict",
    multimodal_perception_split: "Multimodal perception split",
    unresolved_dissent: "Unresolved dissent",
    repetition_loop_backstop: "Repetition loop",
    clarification_unanswered: "Clarification unanswered",
    permission_denied: "Permission denied",
  };

  const DESCRIPTIONS = {
    missing_evidence: "An agent had no grounding data for a load-bearing claim.",
    tool_timeout: "An agent's tool call or run exceeded its time budget.",
    bad_json_output: "An agent's structured output failed to parse.",
    premise_conflict: "Agents disagreed on starting assumptions.",
    multimodal_perception_split: "Agents reported different observations of the same image/chart.",
    unresolved_dissent: "The conclave ended without convergence.",
    repetition_loop_backstop: "The orchestrator's repetition guard fired.",
    clarification_unanswered: "A clarification gate never received a user answer.",
    permission_denied: "An action was blocked by task permissions.",
  };

  window.Plugins.register({
    name: "failure-cause-detail-panel",
    version: "0.1.0",
    detailPanels: [
      {
        id: "failure-causes",
        title: "Failure causes",
        render: function (taskData, rootEl, api) {
          const final = taskData && taskData.final_result;
          const tags = (final && Array.isArray(final.failure_cause_tags))
            ? final.failure_cause_tags
            : [];
          if (!tags.length) {
            rootEl.appendChild(api.el("p", {
              class: "muted",
              text: "No failure-cause tags. The deliberation ran clean.",
            }));
            return;
          }
          const list = api.el("ul", { class: "failure-cause-list" });
          for (const tag of tags) {
            const label = LABELS[tag] || tag;
            const desc = DESCRIPTIONS[tag] || "";
            const item = api.el("li", { class: "failure-cause-item" });
            item.appendChild(api.el("span", {
              class: "failure-cause-chip",
              text: label,
            }));
            if (desc) {
              item.appendChild(api.el("span", {
                class: "failure-cause-desc muted",
                text: " " + desc,
              }));
            }
            list.appendChild(item);
          }
          rootEl.appendChild(list);
          rootEl.appendChild(api.el("p", {
            class: "muted small",
            text: "Tags are stamped by app/services/trace_analyzer.py (DR0022) "
                + "from the deliberation's signals — rule-based, no LLM calls, "
                + "no per-task cost.",
          }));
        },
      },
    ],
  });
})();
