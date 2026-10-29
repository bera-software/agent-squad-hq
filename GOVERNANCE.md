# Governance

Agent Squad HQ is an open-source project maintained by Bera Software & Consultancy Ltd together with community maintainers. This document describes who decides what and how to become a maintainer.

## Roles

| Role | Who | Can |
|---|---|---|
| **User** | Anyone using the project | Open issues and discussions, ask for help, vote with reactions |
| **Contributor** | Anyone whose signed-off PR was merged | Everything a user can, plus be suggested as a reviewer |
| **Maintainer** | Members of the `@bera-software/maintainers` GitHub team | Review and merge PRs, triage issues, cut releases, handle security reports |
| **Lead maintainer** | A maintainer chosen by the maintainers | Break ties, own the roadmap and the release calendar, represent the project |

The current maintainers are the members of the `@bera-software/maintainers` team, which owns every path in [`.github/CODEOWNERS`](.github/CODEOWNERS).

## Decisions

- **Day-to-day changes** are decided in pull requests. A PR merges when a maintainer other than the author approves it and CI is green.
- **Security-sensitive changes** need two maintainer approvals. They cover `src/squadhq/policy/`, `src/squadhq/merge_policy.py`, `src/squadhq/label_guard.py`, `src/squadhq/untrusted.py`, `src/squadhq/executors/tool_profiles.py`, `src/squadhq/server/auth.py`, `.github/actions/run-agent/`, the trigger conditions in `.github/workflows/router.yml`, and the release workflow.
- **Significant design changes** (a new public interface, a change to `squadhq.yml` that is not backward compatible, a new runtime, a change to the trust model) are written up as an Architecture Decision Record in [`docs/adr/`](docs/adr/index.md) and proposed in a PR. The ADR is accepted when the maintainers reach lazy consensus: no maintainer objects within 7 days. If a maintainer objects and discussion does not resolve it, the maintainers vote and a simple majority decides. The lead maintainer breaks ties.
- **Licensing and the open-core boundary** ([ADR-0010](docs/adr/0010-open-core-boundary.md)) change only with the agreement of the copyright holder and a maintainer vote. Security gates and the safety controls described in [docs/security/security-model.md](docs/security/security-model.md) always stay in the open-source edition.

## Becoming a maintainer

A contributor who has made sustained, high-quality contributions (code, reviews, docs or triage) over at least three months can be nominated by any maintainer. The nomination passes if no maintainer objects within 14 days. New maintainers must enable two-factor authentication and commit signing on their GitHub account.

Maintainers who have been inactive for six months may be moved to emeritus status after a private conversation. They can return by asking.

## Code of Conduct

Everyone in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Maintainers enforce it.

## Changes to this document

Changes to this document follow the ADR process above.
