# Roadmap

This roadmap shows direction, not promises. Dates are not committed. Items move through public issues and, for significant changes, [ADRs](adr/index.md).

## Phase 1: open-source developer preview (0.1)

**Done in the repository**

- Python package and `squadhq` CLI (`init`, `validate`, `render`, `doctor`, `onboard`, `drill`).
- `squadhq.yml` with schema, validator and renderer; local-time schedules with an explicit DST policy.
- GitHub App creation through the manifest flow; rulesets requiring `hub / ci-gate`.
- Hardened Actions pipeline: SHA-pinned actions, `permissions: {}`, repo-scoped agent tokens, tool profiles, branch and label guards, trigger gates, fork isolation.
- SSDLC gates (gitleaks, Semgrep, OSV-Scanner, licences, Trivy, SBOM), deterministic risk floor and a fail-closed merge policy with human approval by default.
- Untrusted-input fencing, skill catalogue, 17 stack packs, eval harness with a prompt-injection set.
- Release workflow with SBOM, provenance, signed image and trusted publishing; developer releases `0.1.0.dev2` to `0.1.0.dev5` on PyPI.
- `squadhq.yml` settings acted on in Actions mode: `wip_limit`, `review_fix_rounds`, `merge.always_human`, `merge.risk_based`, budgets (monthly across hub loops and agents, per project, per run, `on_exceed`), and escalation and digest notifications to Slack, Teams, Discord, PagerDuty and webhooks.
- Hub loops routed to any configured provider (`providers.routing` and `hub_loops` tiers, with fallback).
- Workflow isolation: bookkeeping in separate jobs, `PYTHONSAFEPATH`, hashed dependency locks for workflow tools (`requirements/*.lock`), redaction of agent output in run summaries, and a `gh` wrapper in every agent job that refuses posts containing a secret.

**Remaining before 0.1.0**

- The 0.1.0 release itself.
- A fresh organisation reaches its first agent PR in under 15 minutes, measured end to end.

## Phase 2: hosted GitHub App (Cloud beta)

Planned: one-click install with no YAML, hosted sandboxed workers or dispatch into your own Actions, a web onboarding wizard, and the drill run automatically.

## Phase 3: more providers, executors and trackers

In progress: provider adapters and Jira, Linear, Trello and GitLab adapters exist and are tested against fixtures. Planned: Codex CLI, Gemini CLI and aider in Actions mode, cost-aware routing validated by evals, more trackers and code hosts, notifications wired to `squadhq.yml`.

## Phase 4: self-hosted server mode

In progress: API, worker, scheduler, state stores, audit log, Compose, Helm and Terraform examples exist. Planned: validation against real cloud accounts, more secrets backends, and a recovery drill as a release criterion.

## Phase 5: Enterprise

Planned: SSO and SCIM, fine-grained RBAC and approver groups, policy as code, separation of duties between author, reviewer and security agents, audit export to SIEM, data residency and air-gapped operation with local models.

## Phase 6: dashboard and mobile

Preview: a web dashboard and a mobile app for approvals, runs, costs and briefs exist as clients of the server API. Planned: releases of both, push notifications end to end, and voice-to-ticket intake.

## Not planned

- Agents that merge or deploy on their own authority.
- Moving security gates out of the open-source edition.
