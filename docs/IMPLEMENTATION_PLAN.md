# Repository improvement implementation

Authorized 2026-09-05. Preserve the existing working tree and manual deliberation modes.

## Milestones

1. Reliability: preserve dissent and citations in every result/export; unify authentication;
   reconcile interrupted work and provide explicit retry; pin evidence connections to
   validated public addresses; record exact evidence excerpts; fix project validation and
   conservative retention. Add focused regressions.
2. Value measurement: transactional feedback migration, terminal-task feedback API,
   content-free mode aggregates, run/token/cost coverage, and dashboard feedback/metrics.
   Feedback must never launch inference.
3. Project workflow: inspect/edit/archive projects, explicitly select reusable snapshots,
   show evidence omissions, and preserve project context in CLI/dashboard follow-ups.
4. Maintenance: shared result serialization and HTTP transport, Windows/Linux CI,
   browser smoke coverage, complete dependency locking, and editable landing sources
   with a reproducible build/check command.

## Verification

Run focused tests while implementing, then full pytest, Ruff, Pyright, JavaScript syntax,
reachability audit, landing build check, and browser smoke tests against an isolated fake
service. Do not run paid agents or change the user's live database. Record actual results
and platform limitations here. Automatic routing remains deferred pending outcome data.
