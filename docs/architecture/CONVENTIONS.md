# Engineering conventions

This file is the contract that every part of Agent Squad HQ follows. Change it only through a PR that also updates every affected component.

## Names

| Thing | Value |
|---|---|
| Product (display) | **Agent Squad HQ** |
| Repository / distribution | `agent-squad-hq` (PyPI dist name, GitHub repo, Docker image `ghcr.io/<owner>/agent-squad-hq`) |
| Python import package | `squadhq` (under `src/squadhq/`) |
| CLI | `squadhq` |
| User config file | `squadhq.yml` at the hub repo root (`squadhq.example.yml` ships as the template) |
| Default hub repo name | `agent-squad-hq`. Always overridable, never assumed |
| Default bot (GitHub App) slug | `squadhq-ops`, so the login is `squadhq-ops[bot]`. Always read from config/vars |
| Env var prefix | `SQUADHQ_` |
| Hub checkout path inside workflows | `.squadhq/` (relative to the project checkout) |
| Default trigger phrase | `@squad` (configurable; `@claude` accepted for backward compatibility) |

## Runtime configuration resolution (Actions mode)

Workflows never hardcode the org, the hub repo, the bot login or a model. Every workflow resolves:

| Value | Source | Fallback |
|---|---|---|
| Hub repo | `vars.SQUADHQ_HUB_REPO` | `${{ github.repository_owner }}/agent-squad-hq` |
| Hub ref | `vars.SQUADHQ_HUB_REF` | `main`; release installs pin a tag |
| Ops App client id | `vars.SQUADHQ_APP_CLIENT_ID` | none |
| Ops App private key | `secrets.SQUADHQ_APP_PRIVATE_KEY` | none |
| Ops bot login | `vars.SQUADHQ_BOT_LOGIN` | `squadhq-ops[bot]` |
| Agent App client id | `vars.SQUADHQ_AGENT_APP_CLIENT_ID` | none: **single-app mode** |
| Agent App private key | `secrets.SQUADHQ_AGENT_APP_PRIVATE_KEY` | none |
| Coding-agent credential | `secrets.ANTHROPIC_API_KEY` (`auth: api`) / `secrets.CLAUDE_CODE_OAUTH_TOKEN` (`auth: subscription`) | none |
| Account pool credentials | `secrets.ANTHROPIC_API_KEY_1..5` / `secrets.CLAUDE_CODE_OAUTH_TOKEN_1..5`, selected per job by `config/auth.json` `pool` | the single credential above |
| Hand-off key | `secrets.SQUADHQ_HANDOFF_KEY` (required with a pool) | the single LLM credential |
| Agent bot login | `vars.SQUADHQ_AGENT_BOT_LOGIN` | `squadhq-agent[bot]` (the ops login in single-app mode) |
| Trigger phrase | `vars.SQUADHQ_TRIGGER` | `@squad` |
| Debug transcripts | `vars.SQUADHQ_DEBUG_TRANSCRIPTS == 'true'` | off |

Python code reads the same values from the environment through `squadhq.settings`:

| Python setting | Environment variable |
|---|---|
| `settings.hub_repo()` | `SQUADHQ_HUB_REPO` (legacy `HQ_REPO`, `HUB_REPO` accepted) |
| `settings.bot_login()` | `SQUADHQ_BOT_LOGIN` |
| `settings.org()` | `SQUADHQ_ORG` (falls back to the hub repo owner) |

The hub checkout location comes from `squadhq.paths.hub_root()`, which honours `SQUADHQ_HOME`.

## Account pool contract

- `squadhq.yml` `providers.anthropic.pool` (max 5 `{id, auth, secret}`, `pool_strategy: ordered | round_robin`) renders to `config/auth.json` `pool` / `pool_strategy`; absent means the single credential, unchanged.
- Breaker state: `state/capacity.json` `accounts.<id>.{tripped_until, tripped_at, tripped_by}` (Actions mode) or the store's kv `capacity/accounts` (server mode). An expired `tripped_until` is available again; the top-level `paused_until` is the single-credential breaker and is ignored while a pool is configured.
- Selection (`squadhq.account_pool.select`) runs in the prep job only (`capacity_gate resolve`), which writes `auth_mode`, `account` (the id) and `auth_secret` (the secret **name**). These cross jobs as `needs.<prep>.outputs.account` / `auth-secret`; secret values never do. The agent job maps the name with a static expression over the ten declared slots and hands only that value to the executor input.
- `capacity_gate check-trip` trips only the account in the post job's `account` input (passed to `normalize --account` and recorded in `ExecutionResult.account`), and only on the normalized `capacity_tripped` verdict, never on transcript text.
- Every reusable workflow on the path (`router.yml`, `agent-run.yml`, `agent-exec.yml`, `squadhq-agent.yml`, `project-scan.yml`) declares the ten slots, and every stub or caller passes them by name. `.github/lint/isolation.py` allows pool slots in agent jobs only inside the run-agent step's executor inputs.

## Identities (two GitHub Apps)

| App | Who uses it | Permissions | Trusted for |
|---|---|---|---|
| **Ops** (`deploy/github-app/app-manifest.json`) | Deterministic code: router steps, hub loops, sweeper, janitor, merge jobs | contents, pull requests, issues, workflows, actions, Actions variables (write); checks, statuses, members (read) | Protocol markers, state comments, cost ledger, label bookkeeping, the policy approval that lets a PR merge |
| **Agent** (`deploy/github-app/app-manifest-agent.json`) | LLM coding agents (`run-agent`) | contents, pull requests, issues (write); checks, metadata (read). No workflows, administration or actions | Nothing beyond "this is agent work": its comments are role output, never protocol |

Python: `settings.bot_logins()` and `untrusted.by_bot()`/`is_bot_login()` mean the **ops** identity only. `settings.agent_bot_logins()` and `untrusted.by_agent()`/`is_agent_login()` mean the agent identity; `untrusted.by_automation()` means either (agent role comments such as `[Engineer]`, agent-authored PRs). Merge authorship rules: trivial lanes need an **ops**-authored PR; `auto_merge`'s `pr_author_type: bot` needs an **agent**-authored PR.

**Single-app mode** (`SQUADHQ_AGENT_APP_CLIENT_ID` unset, `hub.single_app: true`) is a supported fallback: the agents run as the ops App, `settings.single_app_mode()` is true, and `squadhq.merge_policy` returns "human required" for `auto_merge` and `trivial_merge` (and prints `single_app=true`, which the router's `auto` merge mode honours). `squadhq doctor --check agent-app` warns.

**Ruleset** (`squadhq init --apply`): on each default branch, no deletion or force push, a PR with **one approving review**, **approval after the last push** and stale approvals dismissed, the required check `hub / ci-gate`, and **no bypass actors**. The agent App pushes, so its own approval can never count; in `risk_based`/`auto` modes the ops App approves only after `squadhq.merge_policy` passes. Fork PRs never auto-merge; turn on "Require approval for all outside collaborators" in Actions settings.

## Invoking Python from workflows

The `setup-hub` composite action:
1. checks the hub out to `.squadhq`;
2. runs `pip install ./.squadhq` (pinned, hashed requirements);
3. exports `SQUADHQ_HOME=$GITHUB_WORKSPACE/.squadhq`.

Steps then call `python -m squadhq.<module> …` (or the `squadhq` CLI). No workflow references `scripts/*.py`; that directory no longer exists.

## Schedules

Every scheduled workflow carries its cron line with a machine-readable marker:

```yaml
on:
  schedule:
    - cron: "30 6 * * 1-5"  # squadhq:schedule:daily_brief
```

`squadhq render` rewrites only lines carrying a `squadhq:schedule:<key>` marker. It converts the local-time schedule from `squadhq.yml` (`org.timezone`) to UTC.

Canonical schedule keys:
- `chief_of_staff`, `daily_brief`, `weekly_rollup`
- `finance`, `growth`, `legal`, `seo`
- `stuck_sweeper`, `hub_janitor`, `debt_scan`
- `architect_quarterly`, `ecosystem_watch`, `lessons_harvester`, `okr_tracker`

## Repository variables (Actions mode)

All variables are optional. The defaults are in brackets.

**Identity and wiring**
- `SQUADHQ_HUB_REPO`, `SQUADHQ_HUB_REF`
- Ops App: `SQUADHQ_APP_CLIENT_ID`, `SQUADHQ_BOT_LOGIN` (+ secret `SQUADHQ_APP_PRIVATE_KEY`)
- Agent App: `SQUADHQ_AGENT_APP_CLIENT_ID`, `SQUADHQ_AGENT_BOT_LOGIN` (+ secret `SQUADHQ_AGENT_APP_PRIVATE_KEY`); unset client id = single-app mode
- `SQUADHQ_ORG`, `SQUADHQ_TIMEZONE`

**Triggers**
- `SQUADHQ_TRIGGER` [`@squad`]
- `SQUADHQ_TRUSTED_ASSOCIATIONS` [`OWNER,MEMBER,COLLABORATOR`]

**Agents and gates**
- `SQUADHQ_DISABLED_AGENTS`: comma list of job keys, schedule keys, agent names and skill ids (`squadhq render` expands each `agents:` name to its job keys, e.g. `engineer` -> `engineer,implement,review-fix`)
- `SQUADHQ_GATES` [`secrets,sast,sca,license,iac,sbom`]

**Limits and merging**
- `SQUADHQ_MONTHLY_BUDGET_USD` [`50`]
- `SQUADHQ_REVIEW_FIX_ROUNDS` [`2`]: read by router.yml's fix-round prompts
- `SQUADHQ_WIP_LIMIT`, `SQUADHQ_MERGE_MODE`, `SQUADHQ_APPROVERS`: informational copies; the runtime reads `policies.wip_limit` and each project's `merge` block from `config/projects.json`

**Notifications**
- `SQUADHQ_NOTIFY_ESCALATIONS`, `SQUADHQ_NOTIFY_DIGEST`: `;`-separated targets for `python -m squadhq.notify send --targets`

**Deploy and debugging**
- `SQUADHQ_DEPLOY_TARGET`
- `SQUADHQ_DEPLOYMENT` [`actions`]: `server` makes onboarding and stub-sync also seed the server-mode stub. Distinct from the server's own `SQUADHQ_MODE` (`byo-compute` | `worker`); the old `SQUADHQ_MODE=server` spelling is still accepted by stub-sync
- `SQUADHQ_DEBUG_TRANSCRIPTS` [`false`]

`squadhq render` prints the exact `gh variable set` commands.

The GitHub App manifest is `deploy/github-app/app-manifest.json`. `squadhq init --create-app` uses it.

## Packs (stack-specific behaviour)

Core prompts in `prompts/` are language-neutral. Stack-specific rules live in `packs/<pack-id>/`:

```
packs/<pack-id>/
  pack.yml          # manifest, schema below
  implement.md      # extra rules for the Engineer
  review.md         # extra review rules / checklist
  debt.md           # what the debt scanner looks for
  design.md         # optional
  snippets/         # optional build-integration files seeded by onboarding
```

`pack.yml` schema (validated by `squadhq.packs`):

```yaml
id: java-spring
name: Java / Spring Boot
detect:                      # any match proposes this pack during init/onboard
  files: ["pom.xml", "build.gradle", "build.gradle.kts"]
paths:                       # change-detection globs, relative to the pack's root dir
  - "**/*.java"
  - "**/*.kt"
  - "pom.xml"
root: backend                # default working dir; overridable per project
ci:
  setup:                     # one of: java, node, python, go, dotnet, rust, ruby, php, none
    kind: java
    version: "21"
  install: ""                # optional
  build: "./gradlew build --continue || mvn -B verify"
  test: ""                   # optional if build runs tests
  lint:                      # optional; its own stack-ci step and ci-gate sub-check `lint`
    command: "if [ -f pom.xml ]; then mvn -B -q spotless:check; else ./gradlew spotlessCheck; fi"
    local: "./gradlew spotlessCheck"   # optional: what agents run (default: command); must fit `tools`
    configs: ["pom.xml:spotless-maven-plugin", "build.gradle*:com.diffplug.spotless"]
    requires_config: true    # false: the linter runs on its defaults, so the gate always applies
    snippet: ""              # optional default config in snippets/ (used by quality.lint: default)
    snippet_path: ""         # where the default goes, root-relative (default: the snippet's name)
  coverage:
    format: jacoco           # jacoco | lcov | cobertura | none
    path: "build/reports/jacoco/test/jacocoTestReport.xml"
    min_overall: 60          # stack-ci fails below this
    min_changed: 70          # changed-lines minimum (changed-coverage job; blocking with quality.changed_coverage: enforce)
arch:                        # optional architecture gate (ci-gate sub-check `arch`)
  tool: ArchUnit
  config: "**/*ArchitectureTest.java"   # root-relative path or glob; the gate runs when it exists
  command: "./gradlew test --tests '*ArchitectureTest'"
  local: ""                  # optional, as for lint
  snippet: HexagonalArchitectureTest.java   # starter in snippets/
  snippet_path: ""           # where onboarding seeds it (default: config, when config is a plain path)
  requires: "ArchUnit test dependency"      # optional: what the project must add (shown in the onboarding PR)
  installed: 'package.json:"dependency-cruiser"'  # optional marker; under quality.arch: auto the gate is skipped until it matches
risk_paths:                  # globs that force risk >= high (merged with core list)
  - "**/security/**"
  - "**/db/migration/**"
tools:                       # extra Bash allowlist entries for coding agents
  - "Bash(./gradlew *)"
  - "Bash(mvn *)"
```

A project selects packs in `config/projects.json` (rendered from `squadhq.yml`), for example `"packs": ["java-spring", "node-next"]`, with optional per-pack `root` overrides: `"pack_roots": {"node-next": "frontend"}`.

Config markers in `lint.configs` (and `arch.config`) are globs relative to the pack root, optionally `<glob>:<text>` to require the file to contain `<text>` (`pyproject.toml:[tool.ruff`); symlinks never count. Whether `arch` and `lint` run is decided per project by `projects[].quality` (`squadhq.yml`, rendered into `projects.json`) and the files present in the PR's checkout; see [Quality gates](../concepts/quality-gates.md).

`python -m squadhq.packs files --repo <owner/name> --role <implement|review|debt|design>` prints the pack prompt files to append. `python -m squadhq.packs ci-plan --repo <owner/name> [--checkout DIR]` prints a JSON matrix for CI (with `--checkout`, each entry carries `gates`: which arch/lint gates apply to that checkout). `python -m squadhq.packs gates --repo <owner/name>` prints the arch and lint commands agents run before pushing (appended to implement and review prompts as `QUALITY GATES`).

## Untrusted input

Any text a human outside the approver set, or the web, could author is **untrusted**. That covers issue and PR bodies, comments, diffs, commit messages, code comments, web pages and tracker tickets.

- Prompts include `prompts/_untrusted-input.md` first.
- Code that interpolates untrusted text wraps it as:

  ```
  <untrusted source="issue#12" author="octocat" association="NONE">
  ...text...
  </untrusted>
  ```

  Use `squadhq.untrusted.fence(text, source=..., author=..., association=...)`, which also neutralises any closing tag inside the text.
- Whether an event may trigger an LLM job is decided by code (author association and configured approvers), never by a model.

## Risk

Final risk is `max(llm_risk, path_floor)`. The path floor comes from `squadhq.policy.risk` using the core risk paths plus the selected packs' `risk_paths`. A model can raise risk; it can never lower it below the floor.

## Interfaces

Provider, tracker, code-host, notifier and execution-result contracts are defined in `src/squadhq/interfaces.py`. Adapters implement them; nothing else imports an adapter directly. Use the registries in each subpackage's `__init__` instead.

## Server REST API (v1)

The base path is `/api/v1`. Every endpoint except `/healthz` and `/webhooks/*` requires a bearer token.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness |
| POST | `/webhooks/github` · `/webhooks/jira` · `/webhooks/linear` · `/webhooks/trello` | signed event intake |
| GET | `/projects` | registered projects |
| GET | `/tickets?project=&state=` | normalised tickets with flow state |
| GET | `/runs?project=&limit=` | agent runs (role, outcome, cost, duration) |
| GET | `/approvals` | items awaiting a human (merge, proposal, deploy, escalation) |
| POST | `/approvals/{id}/decision` | `{ "decision": "approve" \| "reject", "comment": "..." }` |
| GET | `/costs?month=YYYY-MM` | spend by project / role / model |
| GET | `/audit?since=` | append-only audit events (hash-chained) |
| GET | `/briefs/latest` | latest daily brief (markdown) |

## Code style

- Python ≥ 3.11. `ruff check` must pass. New subpackages are fully typed (`mypy` strict overrides in `pyproject.toml`).
- Standard-library HTTP (`urllib.request`) for adapters unless a dependency is unavoidable. Optional dependencies go in `[project.optional-dependencies]`.
- Every network call has a timeout. Every list call paginates or warns when it hits its cap.
- Times are UTC (`datetime.now(timezone.utc)`).
- Tests live in `tests/` and mirror `src/squadhq/` (`tests/providers/test_openai.py` …). There are no network calls in tests.
