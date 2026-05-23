# Decision Record 0023 — Trajectory Exporter

**Date**: 2026-05-23  
**Status**: Ratified by Glen  
**Mode**: Glen-directed

## What was chosen

Switchboard gains a **per-task trajectory exporter** that writes a single-line JSONL file containing the entire deliberation — prompts, transcript, per-run timings/tokens/cost, final result (including `action_plan` and `failure_cause_tags`), recorded decision, confidence aggregate, `parent_task_id`, `protocol_version`, and `exported_at`.

`app/services/trajectory_exporter.py::export_trajectory(task_id) -> Path` writes to `<exports_root>/trajectories/<task_id>.jsonl`. The schema is versioned with `_TRAJECTORY_SCHEMA_VERSION = 1` so downstream consumers can dispatch on shape changes. A bulk sweep `export_all_terminal()` iterates terminal tasks with per-task error isolation. A new `trajectories_root()` helper lives in `app/utils/paths.py`.

The orchestrator's `_post_finalize_hooks` (introduced alongside DR0022) calls the exporter after each task hits terminal status. The call is try/except wrapped — an export failure never fails the task.

Two HTTP endpoints: `POST /api/tasks/{task_id}/trajectory/export` (per-task) and `POST /api/trajectories/export-all` (bulk). The bulk endpoint lives on a sibling `trajectories_router` mounted from `app/main.py`.

Coverage: 9 tests in `tests/test_trajectory_exporter.py` and 3 in `tests/test_trajectory_export_endpoint.py`.

The inspiration is Hermes Agent's batch trajectory generation and trajectory-compression-for-training pattern, plus Atropos's `ScoredDataGroup` payload shape. Architectures not adopted; pattern referenced as inspiration only.

## Why

The SQLite database has been the only readable copy of any deliberation. A per-task JSONL file is easier to inspect, share, diff, re-export idempotently, and feed into downstream tooling (grep, `jq`, training pipelines if the user ever wants that). Per-task files trade a small amount of disk for substantial inspection ergonomics and an out-of-band recovery surface.

## What was rejected

- **Aggregating all trajectories into a single rolling JSONL file.** Per-task files are easier to inspect, share, and re-export idempotently; concatenation is one `cat data/exports/trajectories/*.jsonl` away if anyone wants the bulk form.
- **Auto-pushing trajectories to a remote sink in v1.** Local-only by default; remote sinks would touch the privacy framing in DR0013 and the help doc.
- **Including raw uploaded file bytes in the trajectory.** Size and secrets risk; the export references upload IDs only.

## Known risks

- Trajectories under `data/exports/trajectories/` accumulate without bound; retention does not currently sweep them. Acceptable for v1 since per-task files are small and the user owns the directory.
- The schema is versioned; future shape changes bump `_TRAJECTORY_SCHEMA_VERSION` so downstream consumers can dispatch.

## Open questions

- Should the retention worker also enforce a budget on the trajectories directory?
- Should we ship a small `tools/trajectory_query.py` for grepping the files, or leave that to `jq`?

## Who is keeping continuity

Claude Code, as keeper.

## Operability Impact

- **DB**: no schema change.
- **Audit trail**: net-additive — a second, file-based form of the existing data.
- **Recoverability**: improved. The DB is no longer the only readable copy of any deliberation.
- **Retention**: introduces a new disk-accumulating surface (flagged above).
- **Cost**: zero per task (file write only).
- **Risk**: export call is wrapped in try/except in the post-finalize hook; an exporter bug cannot fail a task.
