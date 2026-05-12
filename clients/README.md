# Switchboard Clients — Slash Command Parity

This directory is the **source of truth** for Switchboard's user-facing slash commands and skills across all three CLI integrations. Everything in `~/.claude/commands/`, `~/.codex/skills/switchboard-conclave/`, and the installed Gemini `switchboard-conclave` extension is built from these files.

## Layout

```
clients/
├── README.md                    (this file)
├── install.py                   (deploy script — copies files into each tool's home dir)
├── claude-code-commands/        (Claude Code: 8 slash command .md files)
│   ├── conclave.md
│   ├── decide.md
│   ├── decision.md
│   ├── continue.md
│   ├── thread.md
│   ├── consult.md
│   ├── secondopinion.md
│   └── answer.md
├── codex-skill/                 (Codex: single SKILL.md, trigger-phrase activated)
│   └── SKILL.md
└── gemini-extension/            (Gemini: custom-commands extension, 8 slash commands)
    ├── gemini-extension.json
    └── commands/
        ├── conclave.toml
        ├── decide.toml
        ├── decision.toml
        ├── continue.toml
        ├── thread.toml
        ├── consult.toml
        ├── secondopinion.toml
        └── answer.toml
```

## Why three different shapes?

Because the three CLIs have different extensibility models:

| Tool | User-facing layer | Source file format |
|---|---|---|
| **Claude Code** | Literal `/<name>` slash commands | One markdown file per command in `~/.claude/commands/` |
| **Codex** | **Trigger phrases recognized by the agent.** Codex has no literal `/<name>` invocation; the user types natural language ("ask the conclave"), Codex matches it against the skill's `description` field. | Single `SKILL.md` in `~/.codex/skills/<name>/` |
| **Gemini** | Literal `/<name>` slash commands via the **custom-commands extension template** | TOML files in `commands/` of a Gemini extension, installed via `gemini extensions link <path>` |

The `claude-code-commands/` and `gemini-extension/commands/` directories therefore each contain 8 small files (one per slash command). The Codex skill is a single file that lists every trigger phrase up front.

## Provenance

Every command/skill passes `--invoked-by <tool-name>` to `switchboard.py`. The CLI helper sets the `source_agent` column on the resulting task, so the dashboard's inbox can show which CLI submitted each task.

The full provenance values are:

- `claude-code` — invoked from a Claude Code session
- `codex` — invoked from a Codex CLI session
- `gemini` — invoked from a Gemini CLI session
- `dashboard` — submitted from the web dashboard at `127.0.0.1:8787/`
- `api` — direct API POST (rare; usually scripts)

## Deploying

```bash
# All three (most common)
python clients/install.py

# Just one
python clients/install.py claude
python clients/install.py codex
python clients/install.py gemini

# Status check (read-only)
python clients/install.py --check
```

For Claude and Codex, install is a plain file copy. For Gemini it runs `gemini extensions link <path>`, which makes the extension live and reflect any edits in this directory immediately — no reinstall needed for prompt-text changes.

## Editing

Edit files **here, in `clients/`**, then rerun `install.py`. Do not edit the installed copies under `~/.claude/`, `~/.codex/`, or the Gemini extension store — those are downstream and will be overwritten on next install.

## Adding a new command

1. Add the prose to `clients/claude-code-commands/<name>.md`.
2. Add a corresponding `clients/gemini-extension/commands/<name>.toml`.
3. Append the trigger phrases to `clients/codex-skill/SKILL.md`'s `description` frontmatter field, and add a "How to handle /<name>" section to the body.
4. Run `python clients/install.py`.
5. Smoke-test from each CLI.

## Charter constraint (v1.2)

Per the *Operability before capability* principle, additions to this directory that expand capability (new modes, new agent types, new permission layers) must be assessed against operability foundations. The current shape (slash commands as thin clients over a stable Switchboard service + DB) does not degrade observability, durability, recoverability, audit trail, retention, or export — adding new slash commands here is generally an operability-neutral capability addition.

If a future client surface DOES affect those foundations (e.g., a client that writes locally without going through the service, or one that adds a new persistence layer), the Decision Record for the change must include an Operability Impact field per Charter v1.2 §Decision Records.
