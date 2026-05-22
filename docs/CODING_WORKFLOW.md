# Coding Workflow

This document describes the canonical workflow for using The AI Conclave Switchboard during coding work — design decisions, code review, refactor debates, architecture calls, library choices, debugging hard problems. It is the user-facing companion to the *Conclave Charter*.

## The principle

**The conclave deliberates. The interactive CLI executes. The user decides.**

- **Deliberation** — three labs (Codex, Gemini, Claude) discuss a coding question and produce a structured recommendation
- **Decision** — you record what you actually chose, in your own words, on the task
- **Execution** — your interactive Claude Code session (or your editor of choice) carries out the work
- **Continuation** — findings from execution feed back into the next deliberation if needed

This separation is intentional. Trying to make the conclave *execute* code in parallel with deliberation creates race conditions, audit-trail confusion, and a deliberation-vs-execution category mix that degrades both. See `docs/ROADMAP.md` for the explicit deferral of Layer 2 (in-conclave execution).

## When to invoke the conclave for coding

Strong fit:

- **Architecture choices** where multiple paths are defensible (Postgres vs. MongoDB; monorepo vs. polyrepo; sync vs. async; REST vs. gRPC)
- **Library / framework selection** (FastAPI vs. Litestar; pandas vs. polars; build tools)
- **Refactor debates** where the cost of getting it wrong is real (renaming a public API, splitting a service, picking a state-management pattern)
- **Code review of contentious changes** — when you want a second and third independent opinion on a PR's design
- **Performance trade-offs** — when the right answer depends on assumptions you're not sure about
- **Security / safety calls** — when one model's training cutoff or risk priors might miss what another catches
- **Subjective design** — naming, file layout, public-API shape

Poor fit:

- **Trivial questions** the interactive Claude Code can answer in seconds (typo fix, syntax lookup, single-variable rename)
- **Tasks requiring fresh runtime evidence** (run tests, observe behavior) — gather the evidence first in your terminal, then submit it to the conclave as an attachment or quoted text
- **Anything that needs your environment** (your shell history, your live editor state, your in-progress edits) — that context lives in your terminal, not in the conclave

## The four-step loop

For any non-trivial coding question, the workflow is:

### Step 1 — Frame the question and submit

Use `/conclave` from your Claude Code session, or the dashboard's New Task form. The Conclave Charter's *Standard Brief* applies: a good question names the artifact, audience, success criteria, constraints, time horizon, and risk priorities.

For coding specifically, **attach the relevant code** rather than describing it. Three paths, escalating in scope:

1. **A few files** — drag them into the dashboard's attachment dropzone. `.py`, `.js`, `.ts`, `.md`, `.json` get text-extracted into the prompt; PDFs of specs work the same way; images of UI mockups become first-class visual content for all agents.

2. **The whole codebase** — set `project_path` to your project root AND check "Provide a read-only sandbox copy to the agents" on the New Task form. The orchestrator copies your project to a per-task sandbox dir (skipping `.git/`, `__pycache__/`, `node_modules/`, `.venv/`, `dist/`, binaries, and — depending on permissions — `.env` / `*.key` / `credentials*`). Each agent then gets read-only access to the snapshot via its native CLI tools (Codex shell in `-s read-only`, Gemini included-directory, Claude Read tool). The agents can enumerate the file tree, open files, and reason about the codebase as a whole. They cannot execute anything; the sandbox is a snapshot.

3. **Mixed** — attach a few critical files explicitly via the dropzone (they get inlined in the prompt directly, which agents can't miss) AND enable the sandbox (so agents can browse for context they need beyond the attached files). Best for focused reviews of one module where ambient project context matters.

The sandbox is automatically cleaned up when the task completes. Orphan sandboxes from crashes get swept on service startup.

When permissions matter — e.g., you want the agents to reason about `.env` configuration or credential handling — open the **Permissions** section of the New Task form and grant `can_read_env_files` or `can_read_secrets` explicitly. The default is read-files-only.

### Step 2 — Read the deliberation, record your decision

The conclave returns a structured result. Read the transcript at least once — the *deliberation* often surfaces points that the final answer compresses away.

Pay attention to:

- **agreement_level** — `consensus` is strong agreement, `minor_disagreement` is wording-divergent but substantively unified, `major_disagreement` is real fork, `unresolved` means none of them committed
- **Disagreements list** — non-empty disagreements are not flaws in the conclave; they're information for you
- **resolution_status** (resolve mode) or **convergence** (conclave mode) — tells you how settled each agent considers the question

Once you've decided what you actually want to do, record it:

```
/decide latest "Going with Postgres. Decision driven by managed-instance availability and team familiarity. Rejected MongoDB primarily because we don't have schema-flexibility requirements. Revisit if multi-region replication becomes a need."
```

This is your authoritative call. The conclave's final answer is a recommendation; your decision is the binding artifact. It will appear:

- In the dashboard's Detail view as the "Your Decision" panel
- In any follow-up task's prompt context (so the next conclave sees what you actually decided)
- Via `/decision <task_id>` from any future Claude Code session

### Step 3 — Execute in your Claude Code session

Now switch back to your interactive Claude Code (the one you're already running). You have all the context: the question, the conclave's reasoning, your decision. Use it.

A typical execution prompt at this point:

> *"Implement the Postgres decision from `tsk_01KR...`. Use the SQLAlchemy patterns we already have in `app/database.py`. Start with the schema migration."*

Claude Code reads, edits, runs tests, iterates — using its native tools (Read, Edit, Bash, etc.). This is exactly what these CLIs were built for. Trying to do it from inside a parallel conclave would be slower, more error-prone, and harder to supervise.

You can also reach into the conclave context explicitly:

```
/decision tsk_01KRBHKCMF0TX06VFDSVBCYHCG
```

That prints the full context (original question, conclave's final answer, your decision) inline, with a trailing `>>> Decision is in scope. Proceed with execution as authorized.` marker. Then ask Claude Code to act on it.

### Step 4 — When findings emerge, continue the thread

Execution will surface things the deliberation didn't anticipate — a test failure, a missing dependency, a performance regression, a subtle bug in one of the proposed approaches.

When that happens, **continue the thread** rather than starting fresh:

```
/continue latest "Implementing the Postgres migration revealed that our current ORM relies on a SQLite-specific JSON type. Need a portable approach. What's the cleanest path?"
```

The new task inherits the parent's mode and agents, and the orchestrator automatically injects the prior thread's context (question + final answer + your decision) into every participant's prompt. The conclave continues from *where you left off*, not from a blank slate.

The thread depth cap is 5 ancestors. Beyond that, the breadcrumb on the dashboard's Detail view shows the chain.

## Three-step pattern: deliberate → decide → act

The most common coding workflow is:

1. **`/conclave` or dashboard form** — submit the question with relevant attachments
2. **`/decide`** — record your call after reading the result
3. **Ask Claude Code to act on it** — using the recorded decision as binding context

For tightly-bounded second opinions (a quick sanity check rather than a full deliberation), use **`/consult`** mode instead — bounded to three rounds, faster, cheaper.

For complex problems where you want one agent driving with others as checks, use **`/conclave` in `resolve` mode** instead — the primary agent decides when it's done, with consultants influencing per round.

The mode picker is in the dashboard's New Task form, or via the helper script's first argument.

## What this workflow does NOT require

- **In-conclave code execution.** Deferred per `docs/ROADMAP.md`. The interactive Claude Code session you're already using is your execution layer.
- **Multiple agents writing to your files in parallel.** Never enabled. One agent (Claude Code interactive) writes at a time, under your supervision.
- **An IDE plugin.** The slash commands work from any Claude Code session.

## What this workflow does require

- **The AI Conclave Switchboard service running** at `127.0.0.1:8787`. Start it with: `cd 'C:/Users/gosmo/Desktop/Conclave AI' && python -m uvicorn app.main:app --host 127.0.0.1 --port 8787`
- **Codex, Gemini, and Claude Code CLIs installed and authenticated.** Each draws from your provider subscription quota, not API tokens (assuming default OAuth login).
- **Recording decisions.** A conclave without a recorded decision is half a workflow. The decision panel exists to close the loop.

## A worked example

You're working on the `Conclave AI` codebase and considering whether to add a Sentry integration for production error tracking. You're not sure if it's worth the dependency.

1. **Frame and submit**:
   ```
   /conclave Should The AI Conclave add Sentry for production error tracking?
   Currently using stdlib logging. Single-user local-only service. ~5 deps in requirements.txt.
   Risks of adding: deps, complexity, potential PII in error reports. Benefits: structured
   error surface, alerting if I ever deploy multi-user.
   ```

2. **Conclave deliberates** (~2 minutes, three labs may genuinely disagree on this)

3. **You decide**:
   ```
   /decide latest "Skipping Sentry for now. Single-user local-only doesn't justify the
   dep. Re-deciding if we ever expose this service to non-local clients. Add a TODO in
   docs/MVP_PLAN.md to revisit when network access ships."
   ```

4. **You execute in Claude Code**:
   > *"Add the TODO to docs/MVP_PLAN.md per my decision on the Sentry task. Reference the task ID in the comment."*

   Claude Code reads MVP_PLAN.md, finds an appropriate section, edits it, and confirms.

5. **Two months later**, you actually consider exposing the service:
   ```
   /continue tsk_<sentry_task_id> Now planning to expose this to a small team on internal
   network. Worth revisiting the Sentry decision?
   ```

   The new conclave sees the original question, the prior answer, and your earlier "skipping for now" decision. It deliberates over the *changed* context.

That is the workflow. Five lines of CLI, three labs deliberating, one human making the call, one execution agent doing the work, one continuous thread.
