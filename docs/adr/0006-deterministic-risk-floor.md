# ADR-0006: Risk is the maximum of model risk and a path-based floor

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

A model's risk label can be manipulated or wrong.

## Decision

Final risk is `max(llm_risk, floor)`. `squadhq.policy.risk` computes the floor from the changed files: security-sensitive code, migrations, dependency manifests, CI/CD, infrastructure and secrets configuration force `high`. Packs add `risk_paths`. A model can raise risk, never lower it.

## Consequences

Over-inclusive matching costs extra human clicks, which we accept. The floor is the main protection if automatic merging is enabled.

## Alternatives considered

Model-only risk assessment was rejected.
