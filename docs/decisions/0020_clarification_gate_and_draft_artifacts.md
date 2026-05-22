# DR0020: Clarification Gate and Draft Artifacts

Date: 2026-05-20

## Decision

Switchboard supports a clarification gate and task-scoped draft artifacts.

Consult tasks now collect the primary's `user_input_question` plus consultant `suggested_questions`, deduplicate them, present one numbered questionnaire, pause as `awaiting_user_input`, and resume final synthesis after the user's answer is posted.

Final recommendations that include draftable file work are captured as app-owned artifacts under the runtime data directory. File and search/replace edit artifacts can be explicitly applied to the task's `project_path` by the user. Patch artifacts are review/download-only.

## Rationale

The product needs better outcomes when agents identify missing information or propose concrete file outputs while the task permissions do not grant direct project writes. Pausing once for numbered answers improves task quality without forcing a new task. Capturing draft artifacts gives the user a concrete operational handoff without granting agents write authority over the real project.

## Guardrails

- Agents still do not write directly to the user's project in v1.
- Artifact storage is under `user_data_root()/artifacts/`.
- Applying an artifact is an explicit user/API action.
- Apply is constrained to the task's `project_path`.
- Patch artifacts remain review/download-only.

## Consequences

The dashboard now shows draft artifacts alongside final results and can apply supported artifacts. Exports include a draft artifacts section for markdown paths. Resumed prompts include summaries of prior draft artifacts so follow-up rounds can reason about what was already produced.
