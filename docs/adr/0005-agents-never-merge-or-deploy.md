# ADR-0005: Agents never hold merge or deploy capability

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

A model that can be steered by the text it reads must not be the last step before production.

## Decision

No agent tool profile grants merging, and agents hold no deploy credentials. Deterministic jobs merge after a policy evaluated in code, and GitHub completes the merge only when the required `hub / ci-gate` check passes. Production deploys run only from a published release behind environment reviewers.

## Consequences

Every merge is attributable to a policy decision or a human approval. Automatic merging is opt-in and limited.

## Alternatives considered

Letting the Reviewer agent merge directly was rejected.
