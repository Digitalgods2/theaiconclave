# Decision Record 0013 — Pre-fetched URL attachments (network access for the conclave, scoped)

**Date**: 2026-05-16
**Mode**: Glen-directed (proposal-and-spec; ratification pending)
**Keeper**: claude-code

> **Status: NOT RATIFIED — pending v2 rework.** This proposal was pressure-tested in `tsk_01KRR4B0MWTCN95TEAPYQ2RS4M` (conclave mode, codex + gemini + claude-code, minor_disagreement). All three participants refused to ratify as written. The conclave's verdict is summarized in `docs/ROADMAP.md` § "Next" item 1 and is the input to a v2 rework. Reading this document for current direction: jump to the ROADMAP entry. Reading it for audit history: the original proposal text below is preserved verbatim.

## What Was Chosen

The conclave gets bounded access to live web content via **user-named, pre-fetched URLs**, not via per-agent web tool use. The user adds one or more URLs to a New Task submission; Switchboard fetches each URL *once*, server-side, before dispatching the conclave; the fetched content lands as a normal text attachment that every participant sees identically. The conclave then deliberates over that shared, fixed snapshot.

This is the **shared-snapshot shape** — it preserves the "same stable situation" property the conclave depends on. It is explicitly *not* the alternative shape, in which each agent independently exercises a WebFetch / WebSearch tool mid-deliberation (see Rejected, below).

Concretely:

- **Request schema** — `TaskRequest.context.urls: list[UrlAttachment]` where each entry is `{url: str, label: str | None}`. The dashboard's New Task form gets a small "Fetch these URLs" textarea + a "fetch & preview" button so the user can verify what was retrieved before submit. Slash commands accept a `--url <url>` repeatable flag.
- **Server-side fetcher** — `app/services/url_fetcher.py`: one fetch per URL using `httpx` with a strict allowlist of response types (`text/html`, `text/plain`, `text/markdown`, `application/pdf`, `application/json`, `application/xml`, `application/rss+xml`). Hard caps: **2 MiB per URL**, **5 URLs per task**, **10 s timeout per fetch**. Total fetched bytes are added to the existing prompt-budget accounting so a big page can't displace the sandbox silently.
- **Content extraction** — HTML goes through `readability-lxml` (already considered for PDF extraction in earlier work; new dependency for this feature) → main-content text. PDFs reuse the existing `pypdf` path. Plain text / markdown / JSON / XML pass through verbatim.
- **Storage** — fetched content is written to `data/uploads/<task_id>/<sha256>.txt` so it lives in the same place as user-uploaded attachments and follows the same retention rules. Original URL + fetch timestamp + final response status + content-type recorded in `agent_runs`-adjacent `task_url_fetches` table (one row per fetched URL).
- **Inlining** — fetched content is included in each participant's prompt under a clearly labelled section: `### Live web content (fetched 2026-05-16T14:23:11Z)` followed by per-URL blocks with the URL, label, and content. Same content for every participant.
- **Failure handling** — a fetch that times out, returns non-2xx, or exceeds the size cap is recorded with the error in the task record and inlined into the prompt as a labelled stub: `### URL not retrieved: https://… (HTTP 503)`. The conclave still runs; participants are explicitly told the URL was attempted but failed.
- **No mid-deliberation network calls.** Adapters keep their current network-denied configurations (`claude --tools "Read"`, `codex -s read-only`, `gemini --approval-mode plan`). Once the conclave starts, no further HTTP is performed on the agents' behalf.
- **Charter alignment** — this is a *capability addition*. Operability Impact section below addresses the Charter v1.2 requirement.

## Why It Was Chosen

The previous Layer 2 deferral (decision 0004) declined to grant agents *in-conclave write/execute* access on the filesystem. The arguments there were: race conditions, output-discipline regression, redundancy with the interactive CLI, audit-trail divergence, and the load-bearing one — *deliberation needs a stable situation*.

Those arguments transfer *partially* to per-agent network reads. They transfer cleanly for *writes* (which network access doesn't do) and they transfer cleanly for *stable situation* (since search-result ranking and live-page mutability would make each agent see different things). They do *not* transfer for "the agents' internal knowledge already differs" — which is true, but doesn't justify widening the divergence further.

The case for *some* form of network access is real:
- Training cutoffs make the conclave confidently wrong about library versions, API pricing, security advisories, and anything else that changed in the last 6–18 months.
- Users currently work around this by manually pasting fresh information into the question, which is friction and easy to forget.
- For a "should we adopt X" question, current data is often what makes the deliberation worth having.

The shared-snapshot shape captures the value (currency) while paying very little of the cost:
- **Same situation property preserved** — all three agents see the same fetched bytes. No search-result divergence; no per-agent perception of "the latest article."
- **Audit trail simple** — one entry per fetched URL per task, stored alongside other attachments. No tool-call traces interleaved in the transcript.
- **Cost bounded** — N fetches × ≤2 MiB, paid once before the conclave runs. No runaway tool-call loops.
- **Cherry-picking constrained** — the user chose the URLs, not the agent.
- **Hallucination risk reduced** — agents reason over the actual fetched content (in their prompt), not over their memory of having "read" a URL.

The per-agent independent-web-access shape was considered and rejected (below). It can become a future decision if the shared-snapshot shape proves insufficient in practice.

## What Was Rejected

- **Per-agent independent web tool access (WebFetch / WebSearch granted to each adapter).** This was the alternative considered. Rejected for v1, for the reasons in the *Multimodal Disagreement* clause applied to search: different search backends return different ranking, "the most recent article on X" cites different articles for different agents, and the synthesized consensus would smooth over a real divergence in perceptual input. Auditability suffers: tool-call traces interleave in the transcript and inflate it 5–10×. Cost becomes unbounded. Hallucinated tool results become a new failure mode. This shape may be revisited as a separate decision after the shared-snapshot shape has been used long enough to identify gaps it genuinely cannot cover. The arguments against are not "this is wrong forever" — they are "this is the wrong starting point."

- **HTML rendering / JavaScript execution.** No headless browser. `httpx` GET only, raw response. Pages that hide content behind JS render are documented as out-of-scope; users are advised to find a static alternative (e.g. the article's RSS feed, the doc site's `.md` source, the PyPI page instead of the project's marketing site).

- **Authenticated URLs / cookie support.** No. Bearer tokens / cookies / form auth are not supported in v1. Users who need authenticated content paste it into the question manually. Future decision if needed; raises secrets-handling concerns that aren't worth coupling to this feature.

- **Search.** No search-engine integration. The user names URLs; they don't ask Switchboard to search. Search introduces ranking opacity (which is the per-agent shape's first failure mode anyway) and provider lock-in.

- **Automatic URL detection in the question text.** Considered; rejected for v1. The user explicitly adds URLs in a separate field. Auto-detection makes the surprise too high (e.g. you paste an email containing a URL you didn't intend to fetch). Can be revisited if friction is real.

- **Caching across tasks.** No. Each task fetches its URLs fresh. A URL fetched at 10:00 and again at 14:00 is two distinct fetches with two distinct content snapshots. This preserves auditability (every task's record is self-contained) and avoids the "stale cache vs. live page" failure mode. If a URL is repeatedly fetched and cost becomes a concern, caching can be added with a clear TTL — out of scope here.

- **OpenRouter / Ollama-Cloud seats getting different content from CLI seats.** No. Every seat in the conclave gets the same inlined content, same size budget. The API seats already pay an inline-sandbox cost (decision 0012); the additional URL content is added to the same budget and trimmed by the same priority rules if a model's context can't hold it all.

## Operability Impact

(Capability addition under Charter v1.2 §Decision Records.)

- **Observability**: positive. Each fetched URL records `url`, `label`, `fetched_at`, `status_code`, `content_type`, `byte_length`, `sha256` in a new `task_url_fetches` table. The agents' final transcript names every URL they reasoned over. Users can see exactly what each conclave consumed.
- **Durability**: low-impact addition. One new SQLite table (`task_url_fetches`), one new directory pattern (`data/uploads/<task_id>/`). Both follow existing retention rules. No schema change to existing tables.
- **Recoverability**: a failed fetch is a labelled stub in the prompt, not a hard failure of the task. A timed-out URL doesn't break the conclave — it runs without that source and the failure is recorded.
- **Audit trail**: positive. Single fetch-per-URL means the conclave reasons over exact bytes that are preserved on disk. A future re-read of the task transcript can reference the on-disk snapshot.
- **Retention/export**: positive. Fetched-URL attachments are exported in decision-record markdown alongside the user's original attachments. Tier-based retention applies as for any uploaded attachment.
- **Complexity**: moderate. New service module (`url_fetcher.py`), new request-schema field, new dashboard control, new prompt-builder section, new test file. New runtime dependency: `readability-lxml` for HTML main-content extraction. ~400 lines including tests.
- **Accepted risks**:
  - **A pre-fetched page can be wrong / outdated / adversarial.** The agents reason over the bytes; if the page lied or was tampered with in transit, the conclave is misled. Mitigation: the URL is part of the audit record, so retrospective verification is possible. Users are reminded in the dashboard hint that they are responsible for source quality. This is the same risk a user takes when pasting content manually today.
  - **Content-extraction errors.** `readability-lxml` occasionally returns navigation chrome instead of article text, or strips important sidebars. Mitigation: dashboard's "fetch & preview" button shows extracted content before submit so the user can see what the agents will see and re-fetch the raw HTML if extraction failed.
  - **Cost on metered seats.** Each fetched URL adds to the inlined prompt for OpenRouter / Ollama seats. 5 URLs × 2 MiB ÷ 3 chars-per-token ≈ 3.3M tokens — exceeds every current model's window. The hard caps (per-URL + per-task) plus the existing inline-sandbox trimmer enforce a real ceiling, but a careless user could still inflate cost by attaching big pages. Mitigation: dashboard surfaces the total fetched-byte count before submit; per-URL caps prevent runaway.
  - **Privacy framing.** Fetched content traverses Switchboard's server (locally) before reaching each agent's provider. The privacy framing in section 1.2 of the help doc still holds: the orchestration plane is local; the deliberation content (including fetched URL bytes) still traverses providers' APIs. The new feature adds Switchboard itself as a network client; the user's IP appears in the target server's logs.
- **Exceptions to "Operability before capability"**: **none.** The shared-snapshot shape adds operability (more inspectable audit trail) rather than degrading it. The bounded-priority-window test is satisfied: no named operability gap is displaced; the existing operability "Next" items (crash-safe worker reaper, tool-loop for API seats, Tier 2 trim after export) are not affected.
- **Follow-up review point**: after the feature has been used on ~10 real tasks, decide whether (a) the shared-snapshot shape is sufficient or (b) a follow-up decision on per-agent web access is warranted because there are real questions the shared-snapshot shape cannot serve.

## Known Risks

(Operability Impact covers the categories. Two additional notes.)

- **Open-weight seats with smaller effective windows may have to omit URL content.** The same priority-ordered inline-sandbox trimmer (decision 0012) applies to URL attachments. If a fetched URL doesn't fit, it's omitted with a note in the prompt — same pattern as omitted source files. The user sees this in the per-agent prompt-budget tooltip on the task detail page.
- **The "URL labelled in the request, content shown to agents" pattern is a phishing surface.** A user could be tricked into adding a URL with deceptive content. This is the same risk as pasting deceptive content into the question manually, but the URL-fetch shape automates it. The dashboard's "fetch & preview" pane shows the extracted text before submit, which is the primary mitigation. The fetched-content stub also names the URL explicitly so the agents (and the user reading the transcript later) can see what was claimed to be from where.

## Open Questions

- **Should fetched URL content default to participating in the conclave context, or be opt-in per agent?** Default = participate. The current architecture treats attachments as shared by default. No reason to diverge.
- **Caching policy.** None in v1. If real usage shows the same URL being fetched repeatedly within minutes, a short TTL (5 minutes, per-URL, in-process) could be added without a new decision. If long-TTL persistent caching is wanted, that requires its own decision (it introduces "what content was actually shown" auditability concerns).
- **MIME types beyond the v1 allowlist.** Adding `.epub`, `.docx`, `.csv` is a one-line allowlist edit + an extractor. Punted to follow-up; the allowlist is the conservative starting set.
- **A future per-agent web-access decision.** If shared-snapshot proves insufficient (e.g. real questions need the agent to *itself* decide what to look up), the conversation re-opens. The Charter v1.2 *Operability before capability* clause and the *Multimodal Disagreement* clause both apply to that future decision; the mitigations sketched in the conclave's prior deliberation (tool calls logged in the transcript, per-round tool-call budget, search-result divergence escalation) become specs to refine.

## Who Is Keeping Continuity

**`claude-code`** as keeper. Implementation will live at:

- `app/services/url_fetcher.py` — fetch + extract + cap enforcement
- `app/protocol/validators.py` — `UrlAttachment` model, added to `Context.urls`
- `app/services/prompt_builder.py` — new "Live web content (fetched …)" section, inserted before the sandbox-inline section in each agent's prompt
- `app/database.py` — `task_url_fetches` table (id, task_id, url, label, fetched_at, status_code, content_type, byte_length, sha256, error)
- `app/api/uploads.py` or new `app/api/urls.py` — `POST /api/urls/preview` endpoint backing the dashboard's "fetch & preview" button
- `app/dashboard/index.html` + `app/dashboard/dashboard.js` — New Task form addition: a "Fetch URLs" textarea (one URL per line, optional `label = url` syntax) + preview button + per-URL fetched-bytes counter
- `clients/claude-code-commands/conclave.md` (+ Codex / Gemini equivalents) — `--url <url>` flag, repeatable
- `requirements.txt` — `readability-lxml>=0.8.1` added
- `tests/test_url_fetcher.py` — new test file: fetch happy path, timeout, oversize, disallowed content-type, extraction success/failure, allowlist enforcement, per-task URL count cap
- `docs/SWITCHBOARD_PROTOCOL.md` — protocol bumped to v1.1 (additive); `Context.urls` documented; backward-compatible (existing tasks with no `urls` field work unchanged)
- `docs/CONCLAVE_CHARTER.md` — no amendment required; the shared-snapshot shape doesn't change deliberation norms
- `docs/ROADMAP.md` — moved from "Considered" to "Shipped"; the rejected per-agent shape stays in "Considered and Intentionally Not Built" with a pointer to this decision

This decision is **proposal-and-spec**. Implementation begins after Glen's `/decide` ratification on the deliberation task that adopts it.
