# Decision Record 0026 — Remove the `poll` and `handoff` modes

**Date**: 2026-08-29
**Status**: Ratified by Glen (scope reduction)
**Mode**: Glen-directed, following a static + dynamic reachability audit
**Source**: Codebase cleanup review (no source task; findings produced by direct analysis)

## What was chosen

**Remove** `poll` and `handoff` from the protocol entirely, rather than implementing them or leaving them declared. The AI Conclave ships three deliberation modes — `conclave`, `resolve`, `consult` — and the wire format now says so.

Removed: `TaskMode.POLL`, `TaskMode.HANDOFF`, `AgentRole.PEER`, `MessageType.PEER_ANSWER`, the `PeerAnswer` model, `BaseAdapter.run_peer` and its six concrete implementations, `build_peer_prompt`, the `peer_answer` branch of `_format_prior_message`, `examples/peer_answer_poll.json`, and the poll-mode clause of `TaskRequest._check_mode_requirements`.

## Why

Both modes were declared in `TaskMode` and accepted by `POST /api/tasks`, but the orchestrator dispatched neither. A task submitted in either mode was validated, persisted, claimed by the worker, set to `running`, and then answered with a hardcoded `FinalResult` of `"(mode not implemented in MVP)"`. The schema advertised a capability the runtime refused — a trap that cost a real task row and a worker cycle to discover.

The scaffolding hanging off that trap was substantial and entirely inert: `run_peer` was implemented on **all six** adapters — with real prompt-building and validation logic, not stubs — purely to satisfy an `@abstractmethod` that nothing ever called. Every new adapter (`antigravity_adapter.py`, added the same week as this record) paid that tax again.

Deleting rather than implementing, because:

- The product is three modes everywhere it counts. `README.md`, `docs/ROADMAP.md`, and the dashboard's mode selector all offer exactly `conclave` / `resolve` / `consult`. Only `docs/SWITCHBOARD_PROTOCOL.md` and `help.html` claimed five — the docs were already contradicting each other, and this reconciles them.
- `poll` is subsumed. It was specified as "each agent answers independently, no critique loop" — which `conclave` covers with more rigour, since it adds convergence detection, preserved dissent, and a judge pass. Building `poll` would ship a weaker version of a mode that already exists.
- `handoff` is a routing concern, not a deliberation shape. "Let Codex drive" is `consult` with a different `primary_agent`; it needed no mode of its own.
- Nothing was ever built on them. In 394 tasks across the production database, **zero** used `poll` or `handoff`; across 2,000+ stored messages there is **zero** `peer` role and **zero** `peer_answer` message type. There is no historical row that removing these enum members could orphan.

## Operability Impact

*(Required by Charter v1.2 — "operability before capability".)*

Net positive, and this is a capability *reduction*, so the bar is about not breaking what works.

- **Failure surface shrinks.** Two modes that failed at runtime now fail at the API boundary with a 422 schema error. Verified: `POST /api/tasks` with `mode=poll` returns 422 where it previously returned 200 and failed minutes later.
- **No migration risk.** Confirmed against the live 9MB database — no task row and no message row carries a removed value, so no stored record becomes unloadable.
- **No runtime behaviour change for live modes.** The full suite passes; the five deleted tests all exercised the removed surface exclusively.
- **Adapter contract gets cheaper.** Each new seat implements four `run_*` methods instead of five, and the one it no longer writes is the one that could never run.
- **One reversal cost.** If `poll` is ever genuinely wanted, it comes back as a fresh implementation against the current protocol rather than as five-year-old scaffolding resurrected from git. Given that `conclave` already covers the use case, that is the correct trade.

## Audit basis

This record rests on three checks rather than on reading the code and forming an impression:

1. **Static reachability** — no call site for `run_peer` exists anywhere in the repo.
2. **Dynamic reachability** — the codebase has exactly one computed-attribute dispatch, `getattr(adapter, method)(ctx)` in `orchestrator._call_adapter_method`. Its `method` argument is one of four hardcoded literals across all seven call sites, and `"run_peer"` is not among them. No `eval`/`exec`, no `globals()` dispatch, no discriminated unions, no `f"run_{role}"` construction exists in the repo.
3. **Data reachability** — the production database query above.

`tools/audit_dead_code.py`, added alongside this work, makes checks 1 and 2 repeatable; it reports dynamic-dispatch sites as caveats rather than pretending to see through them.

## What was NOT changed

- `docs/decisions/0018_no_sandbox_manifest_for_cli_seats.md` references `build_peer_prompt` as one of "all five public builders". Decision records are append-only; it stays as written, now historically inaccurate in the same way DR0011 is about the removed Ollama adapter. This paragraph is the pointer for a future reader who greps for `build_peer_prompt` and finds only DR0018.
- The orchestrator's `else` fallback in `run_task` is kept, reworded off the "MVP" framing. It is unreachable today — every `TaskMode` has a flow — but it costs three lines and makes a future mode added without a flow fail loudly with a real result row instead of an `UnboundLocalError`.
