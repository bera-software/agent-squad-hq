# Templates

Generic documents for the software lifecycle that Agent Squad HQ runs. Copy a template into the project repo, at the path shown, and fill in every `<placeholder>`.

| Template | Purpose | SSDLC phase | Typical location in a project repo |
|---|---|---|---|
| `spec.md` | Requirements: user stories, Gherkin acceptance criteria, NFRs | Requirements | `docs/specs/<n>.md` |
| `threat-model.md` | STRIDE per trust boundary, plus OWASP Top 10 for LLM Applications | Threat modelling / design | `docs/threats/<n>.md` |
| `dpia.md` | Data protection impact assessment | Privacy | `docs/dpia/<n>.md` |
| `test-plan.md` | Test scope, levels, entry and exit criteria, traceability | Verify | `docs/test-plans/<n>.md` |
| `release-notes.md` | Release record with rollback plan and SBOM links | Release | `docs/releases/<version>.md` |
| `incident.md` | Live incident record | Operate | `docs/incidents/<date>-<slug>.md` or an issue |
| `postmortem.md` | Blameless postmortem | Learn | `docs/postmortems/<date>-<slug>.md` |
| `AGENTS.md.template` | Instructions for other coding tools (Codex, Cursor, Copilot, Gemini, and others) | Build | `AGENTS.md` at the repo root |
| `lessons.seed.md` | Generic starter lessons to adopt into `org/lessons.md` | Learn | hub `org/lessons.md` (copy the lessons you agree with) |
| `PRE-FLIGHT-CHECKLIST.md` | Setup and safety checklist to work through before agents run unattended | Setup | Hub repo (read it, don't copy it) |

## Seeded automatically by onboarding

`squadhq` onboarding copies these files from `onboarding/` into each project. They are not in this directory.

| Source | Destination in project |
|---|---|
| `onboarding/CLAUDE.md.template` | `CLAUDE.md` |
| `onboarding/adr-0000-template.md` | `docs/adr/0000-template.md` (the ADR template) |
| `onboarding/adr-0001.md` | `docs/adr/0001-record-architecture-decisions.md` |
| `onboarding/lessons.md.template` | `docs/lessons.md` |

Placeholders in this directory always use `<angle brackets>`. Never use the double-underscore UPPERCASE form, which is reserved for onboarding substitution and fails the onboarding run if left unfilled.
