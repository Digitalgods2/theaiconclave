# Decision Record 0019 - Structured Action Plan

## Status

Accepted

## Date

2026-05-20

## Decision

Switchboard adds a first-class `action_plan` field to `FinalResult` and task detail responses. The action plan is a structured operational handoff compiled from the final synthesized response's `recommended_actions` in `consult` and `resolve` modes.

V1 is advisory only. It classifies each step, records required permissions, evaluates policy status, and preserves policy reasons. It does not execute commands, apply patches, access secrets, create approval rows, pause tasks, or remove blocked steps.

## Framing

The product framing is **Structured Action Plan** for users and **Decision-to-Action Compiler** for the internal system concept. It is not a firewall and it is not in-conclave execution. The feature makes the "deliberate, decide, then act" handoff more legible and auditable before a human or calling agent takes action.

## Consequences

- `FinalResult.action_plan` becomes the preferred user-facing action artifact when present.
- Legacy `recommended_actions`, `commands_requiring_approval`, and `patches_requiring_approval` remain for compatibility.
- Existing safety policy remains authoritative; the compiler exposes policy implications instead of expanding agent authority.
- Exports include the structured action plan so the operational handoff survives archival.
