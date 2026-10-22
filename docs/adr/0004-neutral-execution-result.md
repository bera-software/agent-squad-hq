# ADR-0004: Provider and executor interfaces with a neutral execution result

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

The ledger, failure handler and capacity gate parsed one coding agent's native output, which blocked other executors.

## Decision

Contracts for LLM providers, executors, trackers, code hosts and notifiers live in `src/squadhq/interfaces.py`. Every executor's output is normalised into one `ExecutionResult` (outcome, error class, cost, tokens, turns, model, transcript reference). Downstream consumers read only that. Tool profiles are defined once and translated per executor.

## Consequences

New executors need a normaliser and a profile translation, not changes to the ledger or failure handler. Only Claude Code is wired into Actions mode so far.

## Alternatives considered

None.
