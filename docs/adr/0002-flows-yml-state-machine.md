# ADR-0002: config/flows.yml is the state machine

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Labels, transitions, role authority and limits were scattered across prompts and workflows, and drifted.

## Decision

`config/flows.yml` is the single source of truth for the label registry, flow states and transitions, role authority (`applied_by`), idempotency rules and numeric limits. Code reads it through `squadhq.flow_reader`; prompts reference it for mechanical rules. Tracker-native states (Jira statuses, Linear states, Trello lists) map onto its state labels through status maps; other labels act as modifiers.

## Consequences

`flow_reader validate --strict` runs in hub CI; `docs/FLOW-DIAGRAM.md` is generated from the file. Every entry names the code or prompt that enforces it, which makes prompt-only limits visible.

## Alternatives considered

A database-backed workflow engine was rejected for Actions mode: labels are visible, auditable and work without a server.
