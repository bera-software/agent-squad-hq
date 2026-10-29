# Secure software development lifecycle

There are two lifecycles here: **(A)** the one Agent Squad HQ enforces on the software its agents build, and **(B)** the one we follow to build Agent Squad HQ itself. Both are mapped to the [NIST Secure Software Development Framework (SP 800-218)](https://csrc.nist.gov/pubs/sp/800/218/final). Each row says whether it is a deterministic gate (code decides) or an agent step (a model helps), and whether it is in place today.

## (A) What the squad enforces on your projects

| Phase | Agent or skill | Deterministic control | Status |
|---|---|---|---|
| Intake | Product Owner, Requirements Analyst | Trigger gate on author association; untrusted authors get `needs-triage` | In place |
| Security requirements | Security Requirements skill | Spec completeness status `squadhq/spec-complete` when the skill runs | Skill in catalogue |
| Design | Architect, Threat Modeler, Security Architect assessor | `needs-threat-model` blocks; `squadhq/threat-model` status enforced by `ci-gate` once posted | Skills in catalogue |
| Privacy | Privacy / DPIA skill | `needs-dpia` label; `squadhq/dpia` status | Skill in catalogue (enterprise tier, off by default) |
| Build | Engineer and stack packs | Tool profiles, branch guard, repo-scoped token, no merge or deploy capability | In place |
| Verify: secrets | Secrets-response skill | **gitleaks** on the PR's commits (`--redact`) | In place, gate `secrets` |
| Verify: SAST | SAST-triage skill | **Semgrep**, new findings only against the merge base (`SQUADHQ_SAST_CONFIG`, `SQUADHQ_SAST_SEVERITY`) | In place, gate `sast` |
| Verify: SCA | Dependency Steward | **OSV-Scanner**, vulnerabilities introduced by the PR (`SQUADHQ_SCA_MODE=all` for any) | In place, gate `sca` |
| Verify: licences | License-compliance skill | **OSV-Scanner** licences against a deny list or strict allow list | In place, gate `license` |
| Verify: IaC | IaC-scanner-triage skill | **Trivy config**, HIGH and CRITICAL by default | In place, gate `iac` |
| Verify: architecture | Engineer and Reviewer (pack rules) | Pack architecture tool in stack CI (dependency-cruiser, import-linter, arch-go, ArchUnit, NetArchTest, deptrac) when its config exists, or always with `quality.arch: required` | In place, ci-gate sub-check `arch` |
| Verify: lint / format | Engineer (pack rules) | Pack lint step in stack CI, separate from the build (`quality.lint`) | In place, ci-gate sub-check `lint` |
| Verify: tests | QA / Test Planner, Preview QA | Pack-driven stack CI with an overall coverage minimum and coverage comments; changed-lines coverage against `min_changed`, computed without running PR code (`quality.changed_coverage: enforce` makes it blocking); Playwright smoke on preview deployments | In place, ci-gate sub-check `changed-coverage` when enforced |
| Review | Reviewer, Security Reviewer | Risk floor (`max(model, paths)`); merge policy fails closed; human approval by default | In place |
| Single gate | | **`hub / ci-gate`**: works out which checks applied to the PR and whether they passed, including skill statuses that were posted or listed in `SQUADHQ_REQUIRED_GATES` | In place |
| Release | Release Manager, SBOM & provenance skill | **CycloneDX SBOM** attached to each published release (gate `sbom`) | SBOM in place; release skills in catalogue |
| Deploy | Deploy orchestrator, IaC/DevOps skill | Optional `deploy-production.yml`: only on a published release, behind the `production` environment's reviewers, with health checks | Template in place |
| Operate | Uptime watcher, SRE incident responder, Postmortem writer | `incident` label; revert PRs need a human | Skills in catalogue |
| Maintain | Debt scanners, Dependency Steward, Ecosystem Watch | Weekly scans propose tickets; the Chief of Staff accepts or declines | In place |
| Learn | Lessons Harvester, OKR Tracker | Lessons land only through a human-merged PR | In place |

Gates are chosen with `security.gates` (`SQUADHQ_GATES`). Every gate is configurable; the defaults are on. The quality gates (architecture, lint, changed-lines coverage) are set per project with `projects[].quality`; see [Quality gates](../concepts/quality-gates.md). `dast` and `container` are accepted in the configuration but have no router job yet.

## (B) How we build Agent Squad HQ

| Practice | What we do | Status |
|---|---|---|
| Threat modelling | [Threat model](threat-model.md) (STRIDE + OWASP LLM Top 10), reviewed on architecture changes | In place |
| Design records | [ADRs](../adr/index.md) for significant decisions | In place |
| Secure coding rules | [CONVENTIONS](../architecture/CONVENTIONS.md): timeouts on every network call, UTC times, fencing of untrusted input, typed new subpackages | In place |
| Lint and types | `ruff` (with `flake8-bandit` `S` rules) and `mypy` (blocking on typed subpackages) in hub CI | In place |
| Tests | `pytest` with coverage; adapters tested against recorded fixtures; no network calls | In place |
| Workflow security | `actionlint`, `zizmor` (high and above fails), a check that every action is SHA-pinned and that `secrets: inherit` and `Bash(*)` never appear | In place |
| Generated-file drift | `squadhq render --check`, `flow_reader validate --strict`, schema test, catalogue `--check` | In place |
| Agent evals | Golden tickets and a prompt-injection red-team set; thresholds in `evals/thresholds.yml` (injection set 100%, zero policy violations); dry-run scoring in CI | Dry run in CI; live runs on demand |
| Review | CODEOWNERS; two maintainer approvals on security-sensitive paths ([GOVERNANCE](https://github.com/berasoftware/agent-squad-hq/blob/main/GOVERNANCE.md)) | Process in place |
| Contribution provenance | DCO sign-off on every commit | Process in place; no DCO check bot yet |
| Dependencies | Dependabot for Actions, pip, npm and Docker with a 7-day cooldown | In place |
| Release | SBOM, SLSA build provenance, cosign-signed image, PyPI trusted publishing ([supply chain](supply-chain.md)) | Workflow in place; no release published yet |
| Vulnerability handling | Private reporting through GitHub advisories, 90-day coordinated disclosure ([SECURITY.md](https://github.com/berasoftware/agent-squad-hq/blob/main/SECURITY.md)) | In place |
| Planned | CodeQL, OpenSSF Scorecard workflow, secret scanning with push protection on the public repository, webhook-parser fuzzing, an external penetration test before 1.0 | Planned |

## Release gate

Before each release, maintainers check:

- [ ] Hub CI is green: lint, types, tests, configuration and catalogue validation, actionlint, zizmor.
- [ ] Evals at or above thresholds: overall pass rate at least 85%, injection set 100%, zero policy violations, no more than 2 points of regression against the baseline.
- [ ] No open critical or high findings from SAST, SCA or secret scanning without a written justification.
- [ ] Threat model and ADRs updated if the architecture changed.
- [ ] CHANGELOG and upgrade notes written; breaking configuration changes have a migration path.
- [ ] Documentation builds (`mkdocs build --strict`).

## NIST SSDF mapping

| SSDF practice | Where it is covered |
|---|---|
| **PO.1** Define security requirements | (A) Security Requirements skill and spec gate; (B) CONVENTIONS, this page |
| **PO.2** Roles and responsibilities | Role authority in `config/flows.yml`; GOVERNANCE; CODEOWNERS |
| **PO.3** Supporting toolchains | Pinned scanners verified by SHA-256; SHA-pinned actions; Dependabot |
| **PO.4** Security checks criteria | `hub / ci-gate`; `evals/thresholds.yml`; release gate above |
| **PO.5** Secure environments | Ephemeral GitHub-hosted runners; least-privilege tokens; `permissions: {}` by default; server `worker` sandbox |
| **PS.1** Protect code from unauthorised change | Default-branch rulesets requiring `hub / ci-gate`; branch guard; agents cannot merge |
| **PS.2** Integrity verification for releases | SLSA build provenance and SBOM attestations; cosign-signed image |
| **PS.3** Archive and protect releases | GitHub releases with SBOM; GHCR image by digest; PyPI |
| **PW.1** Design to meet requirements and mitigate risk | Threat Modeler and Architect skills; ADRs; our threat model |
| **PW.2** Review the design | Design PRs with `Gate-for:`; team thread |
| **PW.4** Reuse well-secured software | SCA and licence gates; Dependency Steward |
| **PW.5** Secure coding practices | Packs' review rules and security checklists; CONVENTIONS |
| **PW.6** Configure build processes securely | Packs forbid deploy and publish commands; tool allowlists |
| **PW.7** Review and analyse code | Reviewer agent plus human approval; Semgrep; gitleaks |
| **PW.8** Test executable code | Stack CI, coverage, preview smoke tests; our pytest suite and evals |
| **PW.9** Secure defaults | `approvers` merge mode, narrow trigger associations, transcripts off, gates on |
| **RV.1** Identify vulnerabilities continuously | Weekly debt and dependency scans; Dependabot; Ecosystem Watch |
| **RV.2** Assess and remediate | Escalation issues; triage skills; SECURITY.md process |
| **RV.3** Root-cause analysis | Postmortem writer; Lessons Harvester |
