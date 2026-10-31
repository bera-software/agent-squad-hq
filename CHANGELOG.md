# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0.0, minor releases may contain breaking changes; each one is called out under **Changed** with upgrade steps.

## [Unreleased]

### Added
- `squadhq.post_body`: agents post comments, issues and PRs whose body quotes code, prices or spans several paragraphs from a file in `.squadhq-out/` (Write tool), since `$` and backticks are refused in shell arguments and `--body-file` stays denied. Only regular files inside `.squadhq-out/` are accepted; the directory is git-excluded in every agent run. (A Product Owner comment quoting `next/font/local` was refused, leaving a ticket shaped without its "Why this matters".)
- Stuck sweeper `merge_recheck`: a `risk:low` approved PR that self-merge sent to an approver only for failing checks has its merge decision re-run once every check's latest run passes (a transient failure no longer parks it for good).
- Deterministic debt intake in the stuck sweeper: a bot-written `debt-proposal` with a "Why this matters" section becomes `debt` with a priority (severity:high/critical P0, else its P-label or P2), or is closed as a duplicate of open or recently settled debt (same file/class anchors). Left to the Chief of Staff's model, proposals sat unaccepted.
- Uptime watch checks every project environment URL (`url`, `api_url`, `urls.*`) when `squadhq.yml` has no `uptime.checks`: any answer below 500 is up, incidents open in the project's repository, non-production environments are P2.
- `squadhq.label_sync`, run by Stub Sync (also when `config/flows.yml` or `config/projects.json` changes): creates the registry's labels missing on each project, and the hub-only and `project:<name>` labels on the hub. Projects added without `squadhq onboard` (Agent Squad HQ Cloud) were missing them; a label GitHub refuses now fails the run instead of being skipped silently.
- Code graph in the agent's own PR: implement, design, review-fix and mention agents may run `graphify query/path/explain` and `graphify update .`, and commit the three graph files with their change (`prompts/_shared/delivery.md`). `graph-refresh` skips pushes that already carry `graphify-out/graph.json` in its `if:`, so no runner starts; it only catches up after changes made without the graph. The reviewer leaves the graph diff out.
- Stub sync: opt-in `SQUADHQ_STUB_FOLLOW` (a hub branch such as `main`) makes projects track that branch instead of a pinned commit, so hub changes reach them without pin pull requests. Unset keeps the default SHA pin.

### Fixed
- A design ticket whose implementation the PO cut as a separate ticket (`After: #<design>`) got the Engineer dispatched onto it when its design PR merged, and stayed open, so the implementation ticket waited forever. It now closes, which releases the follow-up.
- Type errors in the uptime and coverage changes (Hub CI mypy).
- Stuck sweeper: a review job that finished without recording a verdict for the PR's head (for example every tool call denied) now counts as a missing review and is re-requested; before, any successful review job counted as reviewed.
- Merge policy: a check run superseded by a newer run of the same check (often CANCELLED by its concurrency group after a re-run or reopen) no longer counts as failing; only each check's latest run does.
- Docs-only PRs from our agent skipped review and then sat without any label, since only a review verdict leads to self-merge or the approver. They are reviewed now; a human's docs-only PR still skips review.
- Graph refresh opened a PR after every merge: `graphify update` rewrites all three files even when nothing changed (checkout mtimes in the manifest, randomly numbered communities). The refresh now publishes only when an indexed file's content hash, or the set of files, changed.
- Agents wrote hand-off files with shell heredocs, which are refused; prompts now say to use the Write tool (it creates `.squadhq-out/`).
- Workflows pass `SQUADHQ_AGENT_BOT_ALIASES` too, so tickets and PRs an earlier agent App wrote (for example the Claude App before moving to Agent Squad HQ) count as ours: their "Why this matters" is read and they can be dispatched. Only the ops aliases reached the jobs before.
- Re-shape of a ticket missing "Why this matters" told the Product Owner to append it to the issue body, an inline edit refused whenever the body has backticks or `$`; the ticket bounced until it escalated. The PO now posts it as a comment, which dispatch reads.
- Loops that hand results back through files (tech-debt scan and architect quarterly: findings; OKR tracker and lessons harvester: PR title and body) could not write them: Claude Code does not write outside the checkout in these runs. The hand-off files now live in `.squadhq-out/` inside the checkout (never part of a patch), and every profile may write exactly those three.
- Ecosystem watch could never file its report: bodies quoting prices (`$`) or code are refused as inline shell arguments. It now writes its tickets and report to the findings file and the run creates them; a finding may name its `repo` (the hub or a configured project only).
- `agent-run` deliver (OKR tracker, lessons harvester) ended silently when the agent wrote no `pr-title.txt`: under `pipefail` the failing `head` stopped the step. The branch name is the fallback title.
- Jobs with a sparse hub tooling checkout (ecosystem watch, which must not see `org/` or `state/`) could not install the package: its build bundles those folders. Setup creates empty placeholders for bundled paths the checkout left out.
- A graph-refresh PR whose merge was refused stayed open for good: the stuck sweeper's retry (re-applying `ready-to-merge`) only reaches self-merge, which skips generated-only PRs. trivial-merge now answers it too.
- Pushes: agents may use the usual spellings for their allowed branch (`git push origin <branch>`, `-u`, `-q`, `--set-upstream`, and `git push`/`git push origin HEAD` on a PR branch), not only `HEAD:refs/heads/<branch>`. The pre-push hook still enforces which branches. A mention run edited the file, then stopped silently when `git push -q origin agent/…` was refused.
- Delivery rules tell agents which commands their shell accepts (one command per call, plain `git commit -m`, the edit tool instead of `sed -i`) and to always finish with a comment saying what they did or why they stopped.
- Uptime watch failed on its first run: with no `state/uptime.json` yet, the 404 body was piped into `base64 -d`. The state is read only when the request succeeds.
- `agent-run` loops (OKR tracker, lessons harvester, ecosystem watch, tech-debt scan, architect quarterly) failed in `prep` with "Can't find 'action.yml' … .squadhq/.github/actions/agent-prep": checking the subject out into the workspace root deleted the hub tooling checked out before it. The tooling is restored after the subject checkout.
- Merges: GitHub refuses `gh pr merge --auto` on a PR that is already mergeable but has an optional check failing or pending ("unstable status"), so a ready graph-refresh PR stayed open. Trivial-merge, self-merge and approved-review merges now merge such a PR directly; the ruleset still enforces the required checks.
- `iac-scan` fails only on findings the PR introduces (trivy on the base branch and the PR head, compared by file, check and resource). Findings already on the base branch are listed in the summary as a warning; before, any PR touching `infra/` was blocked by them.
- Agents skipped `graphify update .` on docs- or comment-only PRs ("after changing code"), so a refresh PR followed their merge. The delivery rules now run it whatever changed.
- Dispatch: a ticket the Product Owner shaped (its "Why this matters" posted as a comment, as the prompt asks, so the author's text stays theirs) was never dispatched: the stuck sweeper only read the section from the issue body. A "Why this matters:" section in one of our comments now counts; the implement prompt checks the comment too.
- Product Owner: a title that is a pasted paragraph is replaced with a short summary.
- Agents could not read any file on GitHub-hosted runners: the deny rule `Read(~/**)` (also Grep and Glob) covered `$HOME`, and the checkout lives in `/home/runner/work`, inside it. Every agent stopped after a few denied reads while its run stayed green. Only the home directory's hidden entries (`~/.*`) are denied now.
- Agent runs report denied tool calls: a `::warning` and a job-summary list, and a `denials` output, instead of finishing silently.
- Stuck Sweeper: the sweep token reads checks and commit statuses (every PR was skipped with "Resource not accessible by integration" while the run stayed green), and a repository it cannot read now fails the run.
- Stuck Sweeper: mentions older than 7 days are not asked about again (it re-asked about mentions from a previous setup). Set `SQUADHQ_BOT_ALIASES` to an earlier bot login so its comments still count as the sweeper's.
- Packs `node-next` and `node-api`: the test command adds `--coverage` only when the project's test script does not already pass it; Vitest 4 stopped with "Expected a single value for option --coverage" on the second flag.
- Router: the merge token can read checks, commit statuses and Actions (workflow names in the check rollup), so `trivial-merge` and `self-merge` can evaluate the merge policy; they read the PR's check rollup, failed to, and declined every unattended merge ("could not read the PR — failing closed").
- Packs `node-next` and `node-api`: tests also write `coverage/lcov.info` (Vitest `--coverage.reporter=lcov`, Jest `--coverageReporters=lcov`), so coverage gates and comments work when the project's own reporters leave lcov out.
- Pack `java-spring`, multi-module Gradle builds: the arch gate no longer fails with "No tests found for given includes: [*ArchitectureTest]" in modules without ArchUnit tests (a Gradle init script turns off `failOnNoMatchingTests`, as Maven's `failIfNoSpecifiedTests=false` already did).
- Coverage gate, multi-module builds: when the configured report is missing or has no line data (the empty root project of a Gradle or Maven multi-module build), the gate merges every module's report at the same relative path (`coverage.py overall`) instead of reporting 0%. The changed-lines job and the PR comment use the same report.

## [0.1.0.dev5] - 2026-10-31

### Added
- Providers: `stream_reply()` yields text as it is written, then the whole reply, for every provider; Anthropic streams natively, the others answer in one piece.
- Providers: messages can carry images (`squadhq.interfaces.Image`) for Anthropic, OpenAI (chat and responses), Azure OpenAI, Gemini and Bedrock; the price catalogue marks vision-capable models.
- Price catalogue: Claude Opus 5.5.

### Fixed
- Token-broker mode: the token client accepts GitHub's stateless App installation tokens (`ghs_<app id>_<JWT>`, about 520 characters). Every broker-mode job failed with "token broker answered 200 without a usable token" once GitHub made them the default.
- Router: `update-prs`, `update-prs-publish` and `graph-publish` ask for `contents: read`, so they can check out private projects instead of failing with "repository not found".
- Router: `graph-build` runs graphify's code-only `update`, which needs no AI key; the flags it passed do not exist in the locked graphify and the fallback asked for an LLM key.
- Release: dev, alpha, beta and rc tags are published as GitHub prereleases.

### Upgrading
- Hubs: take the hub upgrade pull request (or Stub Sync) for this version, then move each project's hub pin. In token-broker mode also set the repository variables `SQUADHQ_BOT_LOGIN` and `SQUADHQ_AGENT_BOT_LOGIN` to your Apps' bot logins (Agent Squad HQ Cloud sets them for you), so graph refreshes and the squad's own pull requests are recognised.

## [0.1.0.dev4] - 2026-10-30

### Fixed
- Container image: the build context keeps `docs/FLOW-DIAGRAM.md`. First release with the container image and a GitHub release.

## [0.1.0.dev3] - 2026-10-30

### Fixed
- Release: the container image is tagged with the PEP 440 version.

## [0.1.0.dev2] - 2026-10-30

First published developer preview: everything listed under 0.1.0 below.

## [0.1.0] - Unreleased

First public developer preview. The code was imported from an earlier private deployment with a fresh history, then generalised, packaged and hardened.

### Added

**Packaging and CLI**
- Python package `agent-squad-hq` (import name `squadhq`, Python 3.11+) with the `squadhq` command: `init`, `validate`, `render`, `doctor`, `onboard`, `drill`, `packs`, `skills`, `evals`, `server` and `version`. Optional extras: `server`, `postgres`, `bedrock`, `dev`.
- `squadhq.yml`, the single user-facing configuration file, with a JSON Schema (`schema/squadhq.schema.json`) for editor autocomplete and a validator that reports every problem with a dotted path and a hint. `squadhq.example.yml` documents every section.
- `squadhq render` generates `config/models.json`, `config/auth.json`, `config/projects.json` and the UTC cron lines of every workflow carrying a `# squadhq:schedule:<key>` marker, and prints the repository variables and secrets to set. `--check` fails CI when generated files are stale.
- Human-readable local-time schedules ("weekdays 07:30", "every 30m during working_hours", "monthly 1st 05:00") converted to UTC, with an explicit daylight-saving policy (`org.dst`).
- `squadhq init`: interactive or flag-driven setup that writes `squadhq.yml`, creates the GitHub App through the manifest flow (`--create-app`), stores the App key, sets variables, creates labels from `config/flows.yml` and applies a default-branch ruleset requiring `hub / ci-gate`. Dry run unless `--apply`.
- `squadhq doctor` with 13 checks (configuration, rendered files, schedule markers, DST, model ids, `gh` login, secrets, token age, variables, App installation, rulesets), each with the exact fix.
- `squadhq drill`: opens a test issue in a project and waits for the bot to reply.
- `squadhq.broker_notice`: when the token broker refuses an agent token (plan, quota, paused, repo not on), `agent-prep` posts one comment per reason and month on the issue or PR, with the broker's link to fix it. The token client writes the refusal code and `help-url` as step outputs.
- `squadhq.hub_upgrade`: computes the managed files of a hub for this version (template plus files rendered from its `squadhq.yml`) and the change set that takes an existing hub there, keeping files the owner changed once `.github/squadhq-manifest.json` exists. Agent Squad HQ Cloud uses it to open hub upgrade pull requests; Stub Sync also runs when the manifest changes.
- `squadhq repo-settings --hub OWNER/HUB [--project OWNER/REPO ...] [--apply]`: applies only the admin repository settings with your own `gh` login and writes no `squadhq.yml`: the hub's Actions access level (so the owner's repositories can call its reusable workflows), allow auto-merge and squash merging on each project, and the `squadhq` ruleset `init --apply` creates. Dry run by default, idempotent; on the Free plan a missing ruleset on a private repository is a warning, not a failure.
- Token-broker mode ([ADR-0011](docs/adr/0011-token-broker-mode.md)): with `vars.SQUADHQ_TOKEN_BROKER` (`hub.token_broker`, `squadhq init --token-broker URL`) jobs trade their GitHub Actions OIDC token for App installation tokens at a broker instead of using App private keys. One client (`.github/actions/squadhq-token`) requesting the same narrowed permissions as App-key mode, tokens revoked at job end, `id-token: write` on minting jobs only (enforced by the isolation lint), best-effort usage reports from post jobs, and a `token-broker` doctor check. Without the variable, tokens are minted exactly as before.

**Pipeline (Actions mode)**
- Reusable `router.yml` behind a pinned per-project `agents.yml` stub: change classification, pack-driven stack CI, coverage comments, Product Owner intake, team-thread assessment, design, implement, review, bounded review-fix loop, mention handling (`@squad`, with `@claude` still accepted), catalogue skills by label or `@squad <command>`, preview QA, merge jobs, ticket transitions, board sync and branch updates.
- Composite actions `setup-hub`, `run-agent`, `agent-post`, `escalate`, `ci-plan`, `skill-resolve` and `update-prs`.
- Scheduled hub workflows: Chief of Staff, Daily Brief, Weekly Rollup, Growth, SEO, Legal, Finance, Ecosystem Watch, Lessons Harvester, OKR Tracker, debt scans, quarterly architecture review, stuck sweeper, hub janitor and a generic skill scheduler.
- `config/flows.yml` as the label registry and state machine, validated by `python -m squadhq.flow_reader validate` and rendered to `docs/FLOW-DIAGRAM.md`.
- Stub sync, which opens a PR in each project when the stub or the pinned hub release changes.

**Security and SSDLC**
- SSDLC gates on pull requests: gitleaks (secrets), Semgrep (SAST, new findings only), OSV-Scanner (newly introduced vulnerabilities and dependency licences), Trivy (IaC). A CycloneDX SBOM is attached to each published project release. Gates are selected with `SQUADHQ_GATES`.
- A single required check, `hub / ci-gate`, that evaluates which checks applied to a PR and whether they passed, and enforces skill gates (`squadhq/*` statuses) when they were posted or listed in `SQUADHQ_REQUIRED_GATES`.
- Deterministic path-based risk floor (`squadhq.policy.risk`): final risk is `max(model risk, floor)`. Packs add their own `risk_paths`.
- Merge policy evaluator (`squadhq.merge_policy`) that fails closed. The default merge mode is `approvers`.
- Untrusted-input fencing (`squadhq.untrusted.fence`) and the `prompts/_untrusted-input.md` preamble added to every agent prompt.
- Trigger gates on author association (`SQUADHQ_TRUSTED_ASSOCIATIONS`, default `OWNER,MEMBER,COLLABORATOR`); untrusted issues are labelled `needs-triage` for a human instead.
- Tool profiles (`read-only`, `comment`, `edit-test`, `edit-test-push`) defined once in `squadhq.executors.tool_profiles`; no profile grants an unrestricted shell, `gh pr merge` or force pushes.
- Branch guard (pre-push hook and `git` wrapper) and label guard (`gh` wrapper) for agent jobs.
- Per-job GitHub App tokens: a bookkeeping token for deterministic steps and a separate least-privilege agent token for the repository being worked on.
- Fork PRs get CI and gates but never an agent job.

**Models, executors and adapters**
- Provider adapters for hub loops behind one interface: Anthropic, OpenAI (Chat Completions and Responses), Azure OpenAI, Google Gemini, AWS Bedrock (SigV4 or bearer token) and OpenAI-compatible servers (Ollama, vLLM, LM Studio, OpenRouter, Together, generic). Retry with backoff and a single cross-model or cross-provider fallback.
- Price and capability catalogue (`src/squadhq/providers/prices.yml`).
- Neutral `ExecutionResult` schema and normalisers for Claude Code, Codex CLI, Gemini CLI and aider output, consumed by the cost ledger, failure handler and capacity breaker. Only Claude Code is wired into Actions mode.
- Tracker adapters for GitHub Issues, Jira Cloud, Linear, Trello and GitLab Issues, with native status maps; code-host adapters for GitHub (including GitHub App authentication) and GitLab; notifiers for GitHub issues, Slack, Microsoft Teams, email (SMTP), generic signed webhooks, PagerDuty and Expo push.

**Packs, skills and evals**
- 17 stack packs with detection rules, CI commands, coverage settings, review and debt rules, risk paths and narrow tool allowlists.
- Skill catalogue (`skills/`) with manifests validated by `python -m squadhq.skills validate` and a generated `docs/skills/CATALOGUE.md`.
- Agent eval harness (`python -m squadhq.skills.evals`) with golden tickets, a prompt-injection red-team set and thresholds; dry-run scoring runs in hub CI.

**Quality gates**
- Architecture gate: optional pack `arch` block (`tool`, `config`, `command`, `local`, `snippet`, `requires`), run as the `arch` step of the tokenless stack-ci job and required by `hub / ci-gate` as sub-check `arch`. Starter configs and pinned tools: dependency-cruiser 18.5.0 (Node, Next.js, Vite, Expo), import-linter 2.15 (FastAPI, Django), arch-go v1.7.0 (Go), ArchUnit (Spring; the existing snippet), NetArchTest.Rules 1.3.2 (.NET), deptrac 4.7.2 (Laravel). Skipped with a notice when the project has no config, unless `projects[].quality.arch: required`.
- Lint/format gate: optional pack `ci.lint` (`command`, `local`, `configs`, `requires_config`, default `snippet`), its own stack-ci step and ci-gate sub-check `lint`. `projects[].quality.lint: default` uses the pack's default config (Ruff, golangci-lint, `.editorconfig`) when the repo has none.
- Changed-lines coverage: new `changed-coverage` job computes, per stack, the coverage of the PR's added and modified lines from the lcov, Cobertura or JaCoCo report and the merge commit's diff (`squadhq.quality.coverage`), without running PR code and treating the reports as size-capped, entity-free data. `projects[].quality.changed_coverage: enforce` makes it a required ci-gate check against the pack's `min_changed`; the default `report` only writes the summary.
- `projects[].quality` in `squadhq.yml` (`arch`, `lint`, `changed_coverage`), rendered into `config/projects.json`; `python -m squadhq.packs ci-plan --checkout DIR` annotates each CI entry with the gates that apply, and `python -m squadhq.packs gates` lists the commands agents run.
- Implement and review prompts list the project's arch and lint commands under `QUALITY GATES`; the Engineer runs them before pushing and the Reviewer cites failing `arch`, `lint` and `changed-coverage` output. Pack `tools` grant the commands (`Bash(npx --no-install depcruise *)`, `Bash(lint-imports*)` ...).
- Onboarding proposes a starter `CLAUDE.md` pre-filled from the detected packs (architecture overview, layering rules, coding standards, test policy and a do-not list, with `TODO(owner)` markers), ADR 0001 in Nygard format, each pack's starter architecture config and, when the repo has none, its default lint config. Nothing is overwritten; the PR body is a checklist.

**Server mode (self-host)**
- FastAPI REST API v1 with bearer-token roles (`viewer`, `approver`, `admin`), signed webhook intake for GitHub, Jira, Linear, Trello and GitLab, approvals, runs, costs, briefs, jobs and an append-only hash-chained audit log with verification and export.
- Queue worker with leases and retries, a leader-elected timezone-aware scheduler, SQLite and Postgres state stores, `byo-compute` and sandboxed `worker` modes.
- Structured JSON logs, Prometheus metrics and optional OpenTelemetry tracing.
- Docker image, Docker Compose files and installer, Helm chart, and Terraform examples for AWS ECS Fargate, GCP Cloud Run and Azure Container Apps.

**Client apps (preview)**
- Next.js web dashboard and Expo mobile app, both clients of the server API, sharing a typed API package.

**Project**
- Apache-2.0 licence, NOTICE, security policy with private reporting, contributing guide with DCO sign-off, Contributor Covenant 2.1, governance, support guide, CODEOWNERS, issue and PR templates.
- MkDocs documentation site, including architecture, security model, threat model, SSDLC, configuration reference and ADRs 0001 to 0010.
- Hub CI: ruff, mypy, pytest with coverage, configuration and catalogue validation, actionlint, zizmor, a pinned-actions check and a grep guard against references to the private predecessor.
- Release workflow: wheel and sdist with SLSA build provenance and an SBOM attestation, PyPI trusted publishing, a signed (cosign keyless) and attested container image on GHCR, and a GitHub release.
- Dependabot for GitHub Actions, pip, npm and Docker, with a 7-day cooldown.

**Account pool**
- `providers.anthropic.pool` (up to five `{id, auth, secret}` accounts, `pool_strategy: ordered | round_robin`): coding agents rotate across several Claude Code OAuth tokens or Anthropic API keys. The capacity breaker is per account; a trip pauses only the account that ran, the failure handler reruns the run on the next available account, and the existing pause or metered fallback applies only when every account is tripped. Selection happens in the prep job, which passes only the account id and secret name; the agent job hands only the selected credential to the executor. New optional secrets `ANTHROPIC_API_KEY_1..5` / `CLAUDE_CODE_OAUTH_TOKEN_1..5` are declared by the reusable workflows and passed by the stubs. `squadhq render` prints their `gh secret set` lines, `squadhq validate --explain` and `squadhq doctor --check pool` show the pool, and server mode selects per job from the same pool (or `ANTHROPIC_API_KEY_n` / `CLAUDE_CODE_OAUTH_TOKEN_n` in the worker environment) with the breaker in the store and `GET /api/v1/capacity`. A pool requires `SQUADHQ_HANDOFF_KEY`. Pooling personal consumer subscriptions for automated use may violate the provider's terms.

### Changed

- Configuration that used to be spread over `config/models.json`, `config/auth.json` and `config/projects.json` is now generated from `squadhq.yml`. Edit `squadhq.yml` and run `squadhq render` instead of editing those files.
- Workflows no longer hard-code the organisation, hub repository, bot login or model. They read `SQUADHQ_*` repository variables with documented defaults.
- Scripts moved into the `squadhq` package; workflows call `python -m squadhq.<module>`.
- The default model-credential mode is metered API-key auth (`providers.anthropic.auth: api`). Subscription auth is opt-in.
- Project stubs pin the hub by commit SHA and pass named secrets only (no `secrets: inherit`).
- Packs no longer lint inside `build`: `npm run lint`, `ruff check`, `cargo fmt`/`clippy`, RuboCop, Pint, `dart format`/`flutter analyze`, `terraform fmt` and Android lint moved to `ci.lint`. Where those ran unconditionally before, they still do (`requires_config: false` or a config marker that matches the same projects). The stack-ci step "Install, build, test" is now "Install", then `arch`, `lint`, then "Build, test"; build and test still run when a gate fails.

### Known limitations

- Several `squadhq.yml` keys are validated and rendered but not yet enforced in Actions mode (`budgets.per_project_usd`, `budgets.per_run_usd`, `budgets.on_exceed`, `policies.wip_limit`, `policies.review_fix_rounds`, `policies.release`, `policies.merge.always_human`, `policies.merge.risk_based.*`, `notifications`). The WIP limit (5) and the review-fix round cap (2) currently come from `config/flows.yml` and the prompts. See the configuration reference.
- `agents.<name>.enabled: false` switches off catalogue skills and the jobs whose key matches the name. Core router jobs (`po-shape`, `implement`, `review`, …) are switched off by listing their job key in `SQUADHQ_DISABLED_AGENTS`.
- Executors other than Claude Code cannot run in Actions mode yet.
- The deployment examples have been linted and validated but not applied to real cloud accounts.

[Unreleased]: https://github.com/bera-software/agent-squad-hq/compare/v0.1.0.dev5...HEAD
[0.1.0.dev5]: https://github.com/bera-software/agent-squad-hq/releases/tag/v0.1.0.dev5
[0.1.0.dev4]: https://github.com/bera-software/agent-squad-hq/releases/tag/v0.1.0.dev4
[0.1.0.dev3]: https://github.com/bera-software/agent-squad-hq/releases/tag/v0.1.0.dev3
[0.1.0.dev2]: https://github.com/bera-software/agent-squad-hq/releases/tag/v0.1.0.dev2
[0.1.0]: https://github.com/bera-software/agent-squad-hq/releases/tag/v0.1.0
