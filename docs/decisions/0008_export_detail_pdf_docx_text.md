# Decision Record 0008 — Export task detail as PDF / DOCX / Markdown / Text

**Date**: 2026-05-12
**Mode**: Glen-directed (no separate conclave)
**Keeper**: claude-code

## What Was Chosen

The dashboard's task detail page can now download the full task detail — header, recorded decision, final result, and the complete transcript — in **PDF, Word (.docx), Markdown (.md), or plain text (.txt)**. The user picks folder + filename via the browser's own Save dialog (the modern `showSaveFilePicker()` native dialog in Chromium browsers, falling back to a normal anchor download elsewhere).

Concretely:

- **New service module** `app/services/doc_export.py` — `render_pdf(...)` (reportlab) and `render_docx(...)` (python-docx), plus `filename_stem(task)` for a filesystem-safe suggested filename (`<mode>-<question-slug>-<task_id>`). Markdown/text reuse the existing `exporter.export_to_markdown`.
- **New endpoint** `GET /api/tasks/{task_id}/download?format=pdf|docx|md|txt` (default `pdf`). Streams the file with `Content-Disposition: attachment; filename="..."`. Read-only — does not write to disk, does not modify the task row, and (unlike `POST /export`) works on any task status, not just terminal ones.
- **Refactor** — extracted `_load_task_bundle(task_id)` in `app/api/tasks.py` (the task → messages → final_result → agent_runs assembly), used by the new endpoint. (`POST /export` was left untouched to keep churn minimal; a future cleanup could route it through the same helper.)
- **Dashboard** — the post-task action bar gains an "Export detail as [PDF ▾] [Download…]" control next to the existing "Export to decision record" button. The JS (`onDownloadDetail`) fetches the blob, then uses `showSaveFilePicker()` if available (native Save dialog, suggested filename pre-filled), otherwise triggers an anchor download. Success/cancel/error surface through the existing export-feedback area.
- **Dependencies** — `reportlab>=4.2.0` and `python-docx>=1.1.0` added to `requirements.txt`. Both are pure-Python with no native deps.
- **Tests** — 12 new tests in `tests/test_doc_export.py` (format coverage with magic-byte checks, default format, unsupported-format 400, nonexistent-task 404, non-terminal-task allowed, `filename_stem` safety, render-with-no-final-result). 121 tests total, all pass.

## Why It Was Chosen

The existing `POST /export` endpoint writes a markdown decision record to `data/exports/` — that's the *Tier 2 archive* path (decision 0005), tied to retention. It's not a "give me a copy of this in a format I can hand to someone" path: it only produces markdown, only writes to a fixed directory, and is gated on terminal status.

Glen liked the standalone PDF generated ad-hoc for `tsk_01KRDEY1...` and asked for that as a first-class dashboard feature, with format choice and an explorer-style picker for destination. PDF and Word are the formats that travel — you email a PDF, you edit a .docx. Markdown/text round out the set for the "I want the raw content" case. The browser Save dialog *is* the explorer interface; a custom server-side directory picker was considered and rejected (exposes the filesystem over HTTP, clunky to build) — see *What Was Rejected*.

## What Was Rejected

- **Server-side directory picker** (the service renders a folder-tree UI and writes the file directly). Rejected: exposing the local filesystem over an HTTP API — even bound to 127.0.0.1 — is a footgun, and the UX is worse than the OS-native Save dialog. The browser download path gives the user folder + filename selection for free.
- **RTF instead of DOCX** (zero-dependency, Word opens it). Considered as a way to avoid adding `python-docx`. Glen approved the dependency, so we went with real `.docx` — it round-trips cleanly in Word/LibreOffice/Google Docs and supports proper headings/styles.
- **Stripping markdown formatting for the `.txt` variant.** Decided against — the markdown is already perfectly readable as plain text, and stripping it would lose structure (headings, bullets). `.txt` and `.md` therefore share content; only the extension and MIME type differ.
- **Marking the task as exported when downloaded.** Rejected — download is a transient "give me a copy" action, not the Tier 2 archive event. Only `POST /export` sets `exported_at`. Conflating them would pollute the retention signal.
- **Routing `POST /export` through the new `_load_task_bundle` helper now.** Deferred — it works, and the task was "add a feature," not "refactor." Noted as a future cleanup.

## Operability Impact

(Third decision under Charter v1.2 §Decision Records, after 0006 and 0007.)

- **Observability**: neutral. No new state, no new logging surface.
- **Durability**: neutral. The endpoint never writes to disk; documents are rendered on demand and streamed.
- **Recoverability**: neutral.
- **Audit trail**: neutral-to-slightly-positive. Downloaded documents include an "Exported `<timestamp>`. Copyright © 2026 digitalgods.ai. All rights reserved." footer and the task's metadata (id, mode, invoked-by, timestamps), so a document handed to someone else carries enough provenance to trace it back. But the download itself is not recorded server-side (by design — it's not an archive event).
- **Retention/export**: neutral. Deliberately decoupled from the Tier 2 `exported_at` mechanism (decision 0005) — see "What Was Rejected."
- **Complexity**: low-moderate. One new service module (~330 lines), one new endpoint, one extracted helper, two new pure-Python dependencies, ~90 lines of dashboard JS + a small HTML/CSS block. No new processes, no new persistence, no schema changes.
- **Accepted risks**:
  - `reportlab` and `python-docx` are now load-bearing imports in `app/services/doc_export.py` — a broken install fails at service startup, not silently. That's intentional (fail loud). Both are mature, widely-used, pure-Python.
  - `showSaveFilePicker()` is Chromium-only; other browsers get the anchor-download fallback, which may not show a Save dialog depending on browser settings (could go straight to the downloads folder). Acceptable — the user runs this on localhost in Chrome/Edge in practice, and the fallback still works.
  - PDF/DOCX rendering of a very large transcript could be slow and memory-heavy (everything is built in memory before streaming). Acceptable at current scale (a conclave transcript is a few dozen messages); revisit if transcripts grow to hundreds of turns.
- **Exceptions to "Operability before capability"**: **none**. This is a capability addition that touches no operability foundation. The bounded-priority-window test is satisfied — no named operability gap was displaced; this was bundled alongside no competing operability work.
- **Follow-up review point**: if transcript sizes grow such that PDF/DOCX rendering latency becomes noticeable, switch to streaming generation or add a size cap with a "transcript truncated — download markdown for the full text" note.

## Known Risks

(Operability Impact covers the categories. One additional non-operability note.)

- **Two export paths now coexist** ("Export to decision record" → `POST /export` → `data/exports/*.md`, Tier-2 archive; "Export detail as…" → `GET /download` → browser Save dialog, transient copy). The dashboard help text explains the difference, but users might still conflate them. Mitigation: the buttons are visually grouped but separated by a divider, with distinct labels and tooltips.

## Open Questions

- **Should the markdown decision-record export (`POST /export`) gain the same format options?** Currently it's markdown-only. Could be unified with the new download endpoint's format machinery. Deferred — the two have different purposes (archive vs. copy) and unifying them risks muddying the retention contract.
- **Should the download include the project sandbox listing or git-diff attachment if the task had one?** Currently it includes only what's in `agent_messages` + `final_results`. Attachments aren't surfaced. Probably fine — attachments are inputs, not deliberation content — but worth revisiting if a user asks.
- **A "download all messages as JSON" option?** Not built. The API already exposes the raw JSON at `GET /api/tasks/{id}`; a download button for it would be trivial but nobody's asked.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation lives at:

- `app/services/doc_export.py` — PDF + DOCX renderers, `filename_stem`
- `app/api/tasks.py` — `_load_task_bundle` helper, `GET /{task_id}/download` endpoint, `_DOWNLOAD_FORMATS` map
- `app/dashboard/index.html` — "Export detail as…" control in the post-task bar + updated help text
- `app/dashboard/dashboard.js` — `onDownloadDetail`, `DOWNLOAD_FORMAT_META`, `filenameFromContentDisposition`, wired in `init()`
- `app/dashboard/dashboard.css` — `.download-detail-group` / `-label` / `-select` styles
- `requirements.txt` — `reportlab`, `python-docx`
- `tests/test_doc_export.py` — 12 tests
