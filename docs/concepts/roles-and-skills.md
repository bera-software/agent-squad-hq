# Roles and skills

A **role** is an identity with authority in `config/flows.yml`: which labels it may apply and which transitions it may make. A **skill** is a packaged unit of work in `skills/<id>/`: a manifest (`skill.yml`), usually a prompt, and evals. Most roles are implemented by one skill.

## Core pipeline roles

| Role | Job key(s) | Runs | Tool profile | Does |
|---|---|---|---|---|
| Product Owner | `po-shape`, `po-continue` | Issue opened by a trusted author; trusted reply | `comment` | Classifies SHAPED or posts a proposal, sets priority and verdict, cuts tickets |
| Team-thread assessors (Architect, Engineer, Legal, Finance, Growth) | `team-thread` | PO posts `@team-thread` | `comment` | Short assessments of a proposal, then a consensus |
| Architect / Designer | `design` | `needs-design` | `edit-test-push` on `design/*` | Design doc and ADR as a PR with `Gate-for: #N` |
| Engineer | `implement`, `review-fix` | `agent:implement`; `review:changes-requested` | `edit-test-push` on `agent/*` | Implements on a branch, opens a PR with `Closes #N`; fixes review findings (max 2 rounds) |
| Reviewer | `review` | Every non-draft PR with code changes | `comment` plus inline review comments | Verdict (APPROVE or REQUEST CHANGES) and a risk label |
| Mention handler | `mention` | `@squad` (or `@claude`) from a trusted author | `edit-test-push` on the PR branch | Answers or acts on the request |
| Concierge | `concierge` (hub only) | `@squad` from a trusted author on a hub issue | Claude Code | Acts as your operator for the squad: answers questions and acts on plain-language requests made in the hub |

The implement job can hand the ticket to one specialist engineering skill (for example `frontend-engineer`, `database-migration-engineer`, `iac-devops-engineer`) instead of the generic Engineer.

## Scheduled roles

| Role | Schedule key | How it runs |
|---|---|---|
| Chief of Staff | `chief_of_staff` | Hub loop (`agent_runner cos`): triage, dispatch, debt intake, escalations |
| Daily Brief, Weekly Rollup | `daily_brief`, `weekly_rollup` | Hub loops; post a report issue in the hub |
| Growth, SEO, Legal, Finance | `growth`, `seo`, `legal`, `finance` | Hub loops; drafts and reports only, external content waits for `awaiting-approval` |
| Debt scanners | `debt_scan` | Coding agent per project (`project-scan.yml`), read-only, proposes `debt-proposal` issues |
| Architect quarterly review | `architect_quarterly` | Coding agent per project |
| Ecosystem Watch, Lessons Harvester, OKR Tracker | `ecosystem_watch`, `lessons_harvester`, `okr_tracker` | Hub workflows |
| Stuck sweeper, hub janitor | `stuck_sweeper`, `hub_janitor` | Deterministic, no model |
| Skill scheduler | `uptime_watch`, `weekly_dependencies`, `release_train`, … | `skill-scheduler.yml` runs each enabled scheduled skill |

**Hub loops** make one model call and return a JSON list of actions. The runner executes only `create_issue`, `comment`, `add_labels`, `remove_labels` and `close_issue`, each checked against the role's authority. They never push code, merge or deploy.

## The skill catalogue

The catalogue has 80 skills across eight phases (plan, design, build, verify, release, operate, business, platform). The generated [catalogue page](../skills/CATALOGUE.md) lists each skill's trigger, tool profile, model tier, outputs and gate.

Each manifest declares:

- **Triggers:** a GitHub event, a label, a schedule key, a command (`@squad <command>`), or manual only.
- **Tool profile:** `read-only`, `comment`, `edit-test` or `edit-test-push`. See [tool profiles](../security/security-model.md#tool-profiles).
- **Model tier:** `heavy`, `light`, or `none` for deterministic skills.
- **Gate:** an optional blocking label or a `squadhq/<name>` commit status that `hub / ci-gate` enforces once the skill has posted it.
- **Tier:** `core` (enabled by default unless marked otherwise), `pro` or `enterprise`. Pro and enterprise skills are present in this repository and off by default; there is no licence check. See [editions](../editions.md).
- **`untrusted_inputs`:** whether the skill reads text an outsider could write. Such skills must ship an injection eval.

To write one, follow [authoring skills](../skills/authoring-skills.md). Evals are described in [agent evals](../skills/evals.md).

## Switching roles and skills off

Two mechanisms exist today, and they do not cover the same names:

1. **`squadhq.yml` `agents:`**, globally or per project. Catalogue skills read this map directly, by skill id (`threat-modeler`) or its snake_case form (`threat_modeler`). The top-level map is also rendered into `SQUADHQ_DISABLED_AGENTS`.
2. **`SQUADHQ_DISABLED_AGENTS`**, a comma-separated repository variable. Workflows compare it against **job keys and schedule keys**: `po-shape`, `po-continue`, `team-thread`, `implement`, `design`, `review`, `review-fix`, `mention`, `qa-preview`, `graph-refresh`, `concierge`, and every schedule key (`chief_of_staff`, `daily_brief`, `growth`, …) or hub-loop mode (`cos`, `brief`, …).

So `agents: { growth: { enabled: false } }` stops the Growth hub loop, because `growth` is also its schedule key. But `agents: { product_owner: { enabled: false } }` does **not** stop the `po-shape` job, because that job checks for `po-shape`. To switch off a core router job, add its job key to `SQUADHQ_DISABLED_AGENTS` yourself. `squadhq doctor` will then report that the variable differs from the rendered value; that warning is expected.

## Models per role

Every job maps to a tier (`heavy` or `light`), and each tier to a primary and a fallback model in `providers.routing`. `squadhq validate --explain` prints the resolved model for every job. See [providers](providers.md).
