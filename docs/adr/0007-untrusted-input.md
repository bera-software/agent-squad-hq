# ADR-0007: All external text is untrusted, and trigger eligibility is decided by code

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Issues, comments, diffs, web pages and tickets can carry instructions aimed at the model.

## Decision

All such text is treated as data: prompts start with the `_untrusted-input.md` preamble, and code fences interpolated text with `squadhq.untrusted.fence()`, neutralising fence tags inside it. Whether an event may start an LLM job is decided in workflow conditions and server code from the author's association, the bot identity and fork status, never by a model.

## Consequences

Injection is mitigated, not eliminated; the other controls assume a model can be steered within its tools. Untrusted authors cannot start paid runs.

## Alternatives considered

Asking a model to classify whether a request is safe was rejected.
