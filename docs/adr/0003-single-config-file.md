# ADR-0003: One user-facing configuration file

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Users had to edit several JSON files (`models.json`, `auth.json`, `projects.json`) and workflow crons by hand, and hard-coded names leaked into workflows.

## Decision

Users edit only `squadhq.yml`. It has a JSON Schema for editors and an authoritative validator. `squadhq render` generates `config/*.json` and the marked cron lines; `render --check` guards drift in CI. Engine files such as `config/flows.yml` ship with each release and are not normally edited by users.

## Consequences

One place to change behaviour, reviewed as a commit. Some keys are validated before the runtime enforces them; the reference marks each one.

## Alternatives considered

Reading `squadhq.yml` directly from every workflow was rejected: GitHub cron must be literal UTC in the workflow file.
