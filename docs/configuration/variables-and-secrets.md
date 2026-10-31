# Variables and secrets

Actions-mode workflows never hard-code the organisation, the hub, the bot or a model. They read GitHub Actions **variables** (`vars.SQUADHQ_*`) and **secrets**. This page lists every one the workflows and composite actions reference, with the default used when it is unset.

Set them at **organisation** level with visibility "all repositories" (what `squadhq init --apply` does with the default `--scope org`). Project workflows read them in the project's context, so repository-level values on the hub alone are not enough.

`squadhq render` prints the values derived from `squadhq.yml` and the `gh variable set` commands. `squadhq doctor` (checks `secrets`, `variables`, `token-expiry`) compares what is set with what is expected.

## Secrets

| Secret | Required | Used for |
|---|---|---|
| `SQUADHQ_APP_PRIVATE_KEY` | **Yes**, unless `SQUADHQ_TOKEN_BROKER` is set | The GitHub App's private key (PEM). Every job mints short-lived installation tokens from it. Without it (and without a token broker) nothing runs |
| `ANTHROPIC_API_KEY` | Yes with the default config | Coding agents with `providers.anthropic.auth: api`, and every hub loop that uses an Anthropic model |
| `CLAUDE_CODE_OAUTH_TOKEN` | With `auth: subscription` | Coding agents on subscription auth (`claude setup-token`). `doctor` warns as it nears its one-year lifetime |
| `CLAUDE_CODE_OAUTH_TOKEN_1` .. `_5` | With `providers.anthropic.pool` subscription accounts | One per pool account (default name by list position). Only the selected account's token is handed to the executor |
| `ANTHROPIC_API_KEY_1` .. `_5` | With `providers.anthropic.pool` api accounts | One per pool account, as above |
| `SQUADHQ_HANDOFF_KEY` | Recommended; **required with an account pool** | Key material for the sealed prep-to-agent hand-off (HKDF-SHA256). Without it the key derives from the single LLM credential, which a pool may not set. Any random 32+ byte value: `openssl rand -hex 32 \| gh secret set SQUADHQ_HANDOFF_KEY --org <org> --visibility all` |
| `OPENAI_API_KEY` | If any hub-loop tier uses OpenAI | Passed to hub loops and agent jobs |
| `GEMINI_API_KEY` | If any hub-loop tier uses Gemini | Passed to hub loops and agent jobs |
| `SQUADHQ_SERVER_TOKEN` | Server `byo-compute` mode | Lets the dispatched `squadhq-agent.yml` run report its result to `POST /api/v1/runs`. Use a **per-project reporter token** (`python -m squadhq.server token create --role reporter --project <owner/repo> --name <repo>-ci`), never an admin token: it can only report runs for that one repository |

The project stub passes the LLM credentials (including the ten pool slots), the App keys, the hand-off key and the notification webhooks to the router by name; no other secret in a project reaches the hub. Inside the router only the agent job's executor step receives a pool slot, and only the one prep selected (`.github/lint/isolation.py` enforces this).

**Production deploy** (only if you copy the optional `onboarding/optional/deploy-production.yml` into a project; store them as `production` environment secrets): `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` (Vercel), `RAILWAY_TOKEN` (Railway), `FLY_API_TOKEN` (Fly.io) or `DEPLOY_TOKEN` (custom command).

## Variables

`R` marks variables that `squadhq render` derives from `squadhq.yml`. Set the others by hand when you need them.

### Identity and wiring

| Variable | Default when unset | R | Meaning |
|---|---|---|---|
| `SQUADHQ_HUB_REPO` | `<repo owner>/agent-squad-hq` | R | The hub repository |
| `SQUADHQ_HUB_REF` | `main` | R | Hub ref that onboarding pins new stubs to (resolved to a SHA) |
| `SQUADHQ_APP_CLIENT_ID` | none (required without a token broker) | R | The GitHub App's client id |
| `SQUADHQ_TOKEN_BROKER` | unset (App keys) | R | Token-broker mode ([ADR-0011](../adr/0011-token-broker-mode.md)): an https URL. Jobs that mint App tokens trade their GitHub OIDC token for them at `<url>/v1/broker/token`, and post jobs report usage to `<url>/v1/broker/usage`. No App private key or client id is needed; agent tokens come from the broker's agent App (never single-app mode). From `hub.token_broker` |
| `SQUADHQ_TOKEN_BROKER_AUDIENCE` | the broker URL | R | OIDC audience requested for the broker. From `hub.token_broker.audience` |
| `SQUADHQ_BOT_LOGIN` | `squadhq-ops[bot]` | R | The App's bot login; used to recognise the squad's own comments and issues |
| `SQUADHQ_ORG` | owner of the hub repo | R | Organisation for board sync and settings |
| `SQUADHQ_TIMEZONE` | | R | `org.timezone`, for reference (crons are already rendered to UTC) |

### Triggers

| Variable | Default | R | Meaning |
|---|---|---|---|
| `SQUADHQ_TRIGGER` | `@squad` | R | Phrase that summons the squad in comments. `@claude` is always accepted too |
| `SQUADHQ_TRUSTED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | R | Author associations allowed to start LLM jobs (issue intake, mentions, replies, skill commands) |
| `SQUADHQ_MERGE_ASSOCIATIONS` | `OWNER,MEMBER` | | Associations whose PR approval arms auto-merge |
| `SQUADHQ_SKILL_LABELS` | empty | | Extra label names (comma-separated) that may start a catalogue skill, beyond the built-in list |

### Agents and gates

| Variable | Default | R | Meaning |
|---|---|---|---|
| `SQUADHQ_DISABLED_AGENTS` | empty | R | Comma-separated job keys, schedule keys, hub-loop modes or skill ids to skip. See [switching roles off](../concepts/roles-and-skills.md#switching-roles-and-skills-off) |
| `SQUADHQ_EXECUTOR` | `claude-code` | | Coding executor. Only `claude-code` runs in Actions mode |
| `SQUADHQ_GATES` | `secrets,sast,sca,license,iac,sbom` | R | Which SSDLC gates run. Rendered from `security.gates` |
| `SQUADHQ_REQUIRED_GATES` | empty | | Skill gate statuses (for example `squadhq/threat-model`) that `ci-gate` requires even if the skill did not post them |
| `SQUADHQ_CI_GATE_WAIT` | `1200` | | Maximum seconds `ci-gate` polls for checks still running on the head commit before deciding |

### Gate tuning

| Variable | Default | Meaning |
|---|---|---|
| `SQUADHQ_SAST_CONFIG` | `p/default` | Semgrep ruleset (`--config`) |
| `SQUADHQ_SAST_SEVERITY` | `ERROR` | Comma-separated Semgrep severities that fail the gate |
| `SQUADHQ_SCA_MODE` | `new` | `new`: fail only on vulnerabilities this PR introduces. `all`: fail on any known vulnerability |
| `SQUADHQ_LICENSE_DENYLIST` | AGPL, GPL-2.0/3.0 variants, SSPL-1.0, BUSL-1.1, Commons-Clause | Denied dependency licences. An `A OR B` licence passes if any alternative is allowed |
| `SQUADHQ_LICENSE_ALLOWLIST` | empty | If set, strict mode: every licence must be on this list |
| `SQUADHQ_IAC_SEVERITY` | `HIGH,CRITICAL` | Trivy config severities that fail the IaC gate |
| `SQUADHQ_MIN_CHANGED_COVERAGE` | `0` | Minimum coverage on changed files for the coverage comment |

### Limits, merging and budget

| Variable | Default | R | Meaning |
|---|---|---|---|
| `SQUADHQ_MONTHLY_BUDGET_USD` | `50` | R | Monthly cap for hub loops ([budgets](../concepts/budgets-and-costs.md)) |
| `SQUADHQ_WIP_LIMIT` | | R | Rendered from `policies.wip_limit`. Not read by the workflows yet |
| `SQUADHQ_REVIEW_FIX_ROUNDS` | | R | Rendered from `policies.review_fix_rounds`. Not read by the workflows yet |
| `SQUADHQ_MERGE_MODE` | | R | Rendered from `policies.merge.mode`. The workflows read the per-project mode from `config/projects.json` instead |
| `SQUADHQ_APPROVERS` | | R | Rendered from `org.approvers`. Not read by the workflows yet |

### Preview QA and deploy

| Variable | Default | Meaning |
|---|---|---|
| `SQUADHQ_PREVIEW_ENVIRONMENT` | `Preview` | `qa-preview` runs when a successful deployment's environment name contains this |
| `SQUADHQ_E2E_DIR` | first of `frontend/e2e`, `e2e`, `web/e2e`, `apps/web/e2e` | Playwright directory for the preview smoke test |
| `SQUADHQ_E2E_COMMAND` | Playwright on Chromium | Custom smoke-test command |
| `SQUADHQ_PRODUCTION_ENABLED` | unset | `true` enables the optional production deploy workflow |
| `SQUADHQ_DEPLOY_TARGET` | | `vercel`, `railway`, `fly` or `custom` |
| `SQUADHQ_DEPLOY_DIR` | `.` | Working directory to deploy from |
| `SQUADHQ_DEPLOY_SERVICE` | | Railway service or Fly app name |
| `SQUADHQ_DEPLOY_COMMAND` | | Command for `custom` |
| `SQUADHQ_HEALTH_URLS` | | Space-separated URLs that must answer 2xx after a deploy |

### Server mode (BYO-compute)

| Variable | Meaning |
|---|---|
| `SQUADHQ_SERVER_URL` | The server's public `https://` URL, used by `squadhq-agent.yml` to report results |

### Debugging

| Variable | Default | R | Meaning |
|---|---|---|---|
| `SQUADHQ_DEBUG_TRANSCRIPTS` | `false` | R | `true` prints full agent transcripts in run logs. Never on public repositories |
| `SQUADHQ_RUN_MAP` | unset | | `true` adds a job that prints every event fact the router's gates read |

### Set automatically

| Variable | Set by | Meaning |
|---|---|---|
| `MODEL_OVERRIDE_<JOB>` | `squadhq.failure_handler` | One-shot model for the retry after a model-specific failure. `squadhq.resolve_model` deletes it after use |

## Environment variables for Python modules

Inside workflows, `setup-hub` exports `SQUADHQ_HOME` (the hub checkout), `SQUADHQ_HUB_REPO`, `SQUADHQ_ORG` and `SQUADHQ_BOT_LOGIN`. Other modules read:

| Variable | Read by | Meaning |
|---|---|---|
| `SQUADHQ_CONFIG` | `squadhq` CLI | Path to `squadhq.yml` |
| `SQUADHQ_HOME` | everything | Hub root |
| `SQUADHQ_STUB_REF` | onboarding, stub sync | Hub ref to pin stubs to |
| `SQUADHQ_STUB_FOLLOW` | stub sync | Opt-in: a hub branch (e.g. `main`) projects track instead of a pinned SHA; no pin PRs. See [Upgrading](../getting-started/upgrading.md) |
| `SQUADHQ_DEFAULT_PROVIDER` | providers | Provider for bare model ids (default `anthropic`) |
| `SQUADHQ_PRICES_FILE` | providers | Replacement price catalogue |
| `SQUADHQ_GH_TIMEOUT` | `squadhq.github` | Timeout for `gh` calls |
| `BOARD_TITLE`, `BOARD_ORG` | board sync | Pick a Projects board when there are several |

Provider and tracker credentials are listed in [providers](../concepts/providers.md#provider-adapters-hub-loops) and [trackers](../concepts/trackers.md#credentials). Server settings (`SQUADHQ_DB_URL`, `SQUADHQ_API_TOKENS`, `SQUADHQ_MODE`, `SQUADHQ_WEBHOOK_SECRET_*`, `SQUADHQ_JOB_*` and others) are documented in `deploy/.env.example` and the [self-hosting guides](../deploy/docker-compose.md).
