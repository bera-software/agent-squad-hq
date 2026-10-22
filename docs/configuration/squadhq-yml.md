# squadhq.yml reference

`squadhq.yml` at the root of your hub is the one file you edit ([ADR-0003](../adr/0003-single-config-file.md)). It is validated by `squadhq validate`, turned into runtime files by `squadhq render`, and read directly by server mode. Start from [`squadhq.example.yml`](https://github.com/bera-software/agent-squad-hq/blob/main/squadhq.example.yml) or `squadhq init`.

Editor autocomplete: add this first line (the example file already has it):

```yaml
# yaml-language-server: $schema=./schema/squadhq.schema.json
```

The JSON Schema in `schema/squadhq.schema.json` is generated from the same registry as the validator. The validator (`squadhq.config.loader`) is authoritative: unknown keys are **errors** with a "did you mean" hint, while unknown agent, job and schedule names are **warnings** so a newer runtime never breaks an older CLI.

Only `version`, `org` (with `name` and `approvers`) and `hub` (with `repo`) are required. Everything else has defaults.

## How to read the "Effect" column

- **Enforced**: the runtime reads it and acts on it.
- **Rendered**: written into `config/*.json` or a `SQUADHQ_*` variable, and read by the runtime.
- **Context**: given to agents as information (their prompts read the project config); no code enforces it.
- **Not enforced yet**: validated and possibly rendered, but nothing acts on it today. Planned; see the [roadmap](../ROADMAP.md).

## `version`

| Key | Type | Default | Effect |
|---|---|---|---|
| `version` | `1` | required | Schema version. Anything else is an error |

## `org`

| Key | Type | Default | Effect |
|---|---|---|---|
| `name` | string | required | Display name. Not enforced yet (not rendered) |
| `timezone` | IANA zone | `UTC` | Enforced: all `schedules` are local time in this zone. Rendered to `SQUADHQ_TIMEZONE` |
| `working_hours` | `HH:MM-HH:MM` | `09:00-18:00` | Enforced: the window for `... during working_hours` schedules (end exclusive) |
| `working_days` | `daily` \| `weekdays` \| `weekends` \| `mon-fri` \| `sun,wed` … | `mon-fri` | Enforced: the days of the working-hours window |
| `approvers` | list of `@user` or `@org/team` | required, at least one | Rendered into `config/projects.json` (`merge.approvers`) and `SQUADHQ_APPROVERS`. **Not enforced yet**: in Actions mode, who may merge is decided by `SQUADHQ_MERGE_ASSOCIATIONS` (default `OWNER,MEMBER`) and by who can apply labels |
| `operating_model` | `solo` \| `team` | `team` | Not enforced yet |
| `dst` | `daylight` \| `standard` | `daylight` | Enforced: which UTC offset GitHub crons are rendered for. See [schedules and time zones](schedules-and-timezones.md) |

## `hub`

| Key | Type | Default | Effect |
|---|---|---|---|
| `repo` | `owner/name` | required | Rendered: `SQUADHQ_HUB_REPO`; its owner becomes `SQUADHQ_ORG`. Any name works |
| `ref` | string | `main` | Rendered: `SQUADHQ_HUB_REF`, the ref onboarding pins project stubs to. Release installs pin a tag |
| `trigger` | `@word` | `@squad` | Rendered: `SQUADHQ_TRIGGER`. `@claude` is always accepted too |
| `bot` | string (App slug) or mapping | `squadhq-ops` | See below |
| `bot.app` | App slug | `squadhq-ops` | Used by `squadhq doctor` (App installation check) and `drill` |
| `bot.login` | string | `<app>[bot]` | Rendered: `SQUADHQ_BOT_LOGIN` |
| `bot.client_id` | string | empty | Rendered: `SQUADHQ_APP_CLIENT_ID` when set. `init --create-app` sets the variable directly |
| `agent_bot` | string (App slug) or mapping | `<bot.app>-agent` (`squadhq-ops` -> `squadhq-agent`) | The **agent** App the coding agents run as. Must differ from `bot.app` |
| `agent_bot.login` / `agent_bot.client_id` | string | `<app>[bot]` / empty | Rendered: `SQUADHQ_AGENT_BOT_LOGIN`, `SQUADHQ_AGENT_APP_CLIENT_ID`; `render` and `doctor` also require the secret `SQUADHQ_AGENT_APP_PRIVATE_KEY` |
| `single_app` | bool | `false` | Fallback with one App: agents share the ops identity and every unattended merge (`auto`, `risk_based`, trivial lanes) needs an approver. Cannot be combined with `agent_bot`. `doctor --check agent-app` warns |
| `token_broker` | https URL, or mapping `{url, audience}` | unset | Token-broker mode ([ADR-0011](../adr/0011-token-broker-mode.md)): App tokens come from this broker in exchange for the job's GitHub OIDC token, so no App private key is stored. Rendered: `SQUADHQ_TOKEN_BROKER` (and `SQUADHQ_TOKEN_BROKER_AUDIENCE` when `audience` is set); `render` and `doctor` then require neither App key. https only, without credentials, query or fragment. Cannot be combined with `single_app` |

## `providers`

| Key | Type | Default | Effect |
|---|---|---|---|
| `default` | provider id | first configured | The provider for bare model ids **in squadhq.yml**. `render` writes non-Anthropic routes as `provider/model` in `models.json`, so the runtime never guesses |
| `<provider>` | mapping | | One block per provider you use: `anthropic`, `openai`, `gemini`, `openrouter`, `azure_openai`, `bedrock`, `together`, `openai_compatible`, `ollama`, `vllm`, `lmstudio`. `vertex` is rejected (no adapter) |
| `<provider>.auth` | see below | first allowed | `anthropic`: `api` \| `subscription`; `bedrock`: `iam` \| `api`; `ollama`/`lmstudio`: `none`; others: `api`. Enforced for `anthropic` (coding-agent auth via `config/auth.json`) |
| `<provider>.secret` | `UPPER_SNAKE` | per provider (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `AZURE_OPENAI_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`) | Used by `render` and `doctor` to list required secrets |
| `<provider>.base_url` | http(s) URL | empty | Rendered into `config/auth.json`; hub-loop adapters use it (`squadhq.providers.configured_kwargs`), as they do `region` (Bedrock) and the value of the `secret` environment variable when set |
| `<provider>.region` | string | empty | As `base_url` |
| `routing` | mapping of tiers | `heavy` and `light` on Anthropic models | Rendered into `config/models.json`. If present, both `heavy` and `light` are required; you may add more tiers |
| `routing.<tier>.primary` | `provider/model-id` | required | The model for the tier. The provider must have a block above |
| `routing.<tier>.fallback` | `provider/model-id` | none | Used after model-specific failures ([providers](../concepts/providers.md#models-and-tiers)) |
| `jobs` | `job: tier` | every known job has a default tier | Rendered. Overrides one job's tier. `squadhq validate --explain` lists all jobs |
| `executor` | `claude-code` \| `codex` \| `aider` \| `gemini-cli` | `claude-code` | Rendered into `config/*.json`. Workflows read `SQUADHQ_EXECUTOR` instead, and only `claude-code` runs in Actions mode today |
| `fallback_on_capacity_trip` | bool | `false` | Enforced: when a subscription capacity trip pauses coding jobs, `true` switches them to metered API auth instead. With an account pool this applies only once **every** pool account is tripped |
| `anthropic.pool` | list (max 5) of `{id, auth, secret}` | none | Enforced. Coding-agent account pool: each job runs on one account, a capacity trip pauses only that account, and the next job uses the next available one. See [account pool](#account-pool). Absent = the single `anthropic.secret` credential, unchanged |
| `anthropic.pool[].id` | slug | required | Unique account id, shown in run summaries, the cost ledger (`account`) and `state/capacity.json` |
| `anthropic.pool[].auth` | `api` \| `subscription` | `subscription` | Which executor input the credential is handed to |
| `anthropic.pool[].secret` | `^[A-Z][A-Z0-9_]*$` | `CLAUDE_CODE_OAUTH_TOKEN_<n>` / `ANTHROPIC_API_KEY_<n>` (n = position) | Secret holding the credential. Actions mode can only pass `ANTHROPIC_API_KEY[_1..5]` (api) and `CLAUDE_CODE_OAUTH_TOKEN[_1..5]` (subscription), because reusable workflows declare secrets statically; other names validate with a warning and work in server mode only |
| `anthropic.pool_strategy` | `ordered` \| `round_robin` | `ordered` | `ordered`: the first untripped account in list order. `round_robin`: the first untripped account from a rotating start (a stable hash of the run id, attempt and job, so selection never writes state) |

!!! warning
    In Actions mode, route hub-loop tiers to Anthropic models only for now. See [current limitations](../concepts/providers.md#current-limitations).

### Account pool

```yaml
providers:
  anthropic:
    auth: api                      # hub loops keep using ANTHROPIC_API_KEY
    pool:
      - { id: team-a, auth: subscription }            # secret CLAUDE_CODE_OAUTH_TOKEN_1
      - { id: team-b, auth: subscription }            # secret CLAUDE_CODE_OAUTH_TOKEN_2
      - { id: org-key, auth: api, secret: ANTHROPIC_API_KEY_3 }
    pool_strategy: ordered         # or round_robin
  fallback_on_capacity_trip: false # applies only when EVERY pool account is tripped
```

- The prep job picks the account (`python -m squadhq.capacity_gate resolve`) and passes only its id and the secret **name** to the agent job, which maps the name to the secret with a static expression over the declared slots. No other pool credential reaches the executor.
- A capacity trip on a run trips only the account that ran, for 60 minutes (`state/capacity.json` `accounts.<id>.tripped_until`). If another account is available, the failure handler reruns the whole run, and the rerun picks the next account. When every account is tripped, the existing behaviour applies: jobs pause (with one escalation issue), or fall back to metered `ANTHROPIC_API_KEY` with `fallback_on_capacity_trip: true`.
- API-key accounts trip on credit or usage-limit exhaustion ("credit balance is too low", "reached your specified API usage limits"). Rate limits (429) and overloads stay transient and are retried, not tripped.
- With a pool, set **`SQUADHQ_HANDOFF_KEY`**: the prep job never holds the pool credentials, so the sealed hand-off can no longer derive its key from the run's LLM credential. Without it the key falls back to the single `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`.
- `squadhq render` prints a `gh secret set` line per pool secret, `squadhq validate --explain` lists the pool and `squadhq doctor --check pool` lists the accounts, warns when api and subscription accounts are mixed and (online) when a pool secret is missing. The workflows cannot see which secrets are set, so a job that selects an account whose secret is missing fails; it is not skipped.

!!! warning "Provider terms"
    Rotating several API keys or organisation (team / enterprise) accounts is fine. Pooling or rotating personal consumer subscriptions for automated use may violate the provider's terms; whether your use is permitted is your responsibility.

## `budgets`

| Key | Type | Default | Effect |
|---|---|---|---|
| `monthly_usd` | number ≥ 0 | `100` | Enforced for hub loops: rendered to `SQUADHQ_MONTHLY_BUDGET_USD`. Coding agents are recorded, not capped ([budgets](../concepts/budgets-and-costs.md)) |
| `per_project_usd` | `name: number` (`default` allowed) | none | Rendered into `config/projects.json` `budget_usd`. Not enforced yet. Warns when a value exceeds `monthly_usd` or names no project |
| `per_run_usd` | `job: number` | none | Not enforced yet. Warns on unknown job names |
| `on_exceed` | `pause` \| `downgrade` \| `notify` | `pause` | Not enforced yet (the hub-loop cap always pauses) |

## `trackers`

| Key | Type | Default | Effect |
|---|---|---|---|
| `default` | `github` \| `jira` \| `linear` \| `trello` | `github` | Validated; a non-GitHub default needs its block. Actions mode always uses GitHub Issues. Server mode picks the adapter from the webhook source |
| `jira` | `{site, project, status_map}` | | `site` and `project` required |
| `linear` | `{team}` | | `team` required |
| `trello` | `{board}` | | `board` required |

Tracker credentials are environment variables ([trackers](../concepts/trackers.md#credentials)), never this file.

## `notifications`

| Key | Type | Default | Effect |
|---|---|---|---|
| `escalations` | list of channels | `[github_issue]` | Validated. **Not wired yet**: escalations go to hub issues |
| `digest` | list of channels | `[github_issue]` | As above |

A channel is `github_issue`, `slack:#channel`, `email:addr`, `teams:<url>`, `webhook:<url>` or `discord:<url>`. See [notifications](../concepts/notifications.md).

## `schedules`

A mapping of schedule key to a local-time phrase. Keys you leave out keep their defaults. The grammar, the defaults for all 29 keys and the daylight-saving rules are in [schedules and time zones](schedules-and-timezones.md).

| Key | Type | Default | Effect |
|---|---|---|---|
| `<key>` | schedule phrase or raw cron | per key | Enforced: `render` rewrites the cron line carrying `# squadhq:schedule:<key>`; server mode's scheduler reads it directly. Unknown keys warn; deprecated keys (`seo_weekly`, `lessons_harvest`, `growth_nightly`, `okr_track`, `legal_weekly`, `finance_weekly`) warn with the new name |

## `agents`

A mapping of agent or skill name to `true`, `false` or `{enabled: bool, ...options}`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `<name>` | bool or mapping | enabled (skills: their manifest's `default_enabled`) | Catalogue skills read this directly, by id or snake_case id. The names of disabled agents are rendered into `SQUADHQ_DISABLED_AGENTS`, which workflows compare with job and schedule keys. See [switching roles off](../concepts/roles-and-skills.md#switching-roles-and-skills-off) |
| `<name>.<option>` | any | | Agent-specific options, for example `legal: {jurisdictions: [UK, EU]}`. Context |

Known names: `product_owner`, `architect`, `designer`, `engineer`, `reviewer`, `threat_modeler`, `security`, `concierge`, `chief_of_staff`, `daily_brief`, `weekly_rollup`, `growth`, `seo`, `legal`, `finance`, `ecosystem_watch`, `lessons_harvester`, `okr_tracker`, `debt_scanner`, `stuck_sweeper`, `hub_janitor`. Other names (for example skill ids) are accepted with a warning.

## `policies`

| Key | Type | Default | Effect |
|---|---|---|---|
| `wip_limit` | integer 1–100 | `5` | Rendered to `SQUADHQ_WIP_LIMIT`. **Not enforced yet**: the dispatcher uses `flood-guards.wip_limit_open_prs_per_project` in `config/flows.yml` (5) |
| `review_fix_rounds` | integer 0–10 | `2` | Rendered to `SQUADHQ_REVIEW_FIX_ROUNDS`. **Not enforced yet**: the cap of 2 is in the review-fix prompt and `flows.yml` |
| `triggers.allowed_associations` | list of `OWNER`, `MEMBER`, `COLLABORATOR`, `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `MANNEQUIN`, `NONE` | `[OWNER, MEMBER, COLLABORATOR]` | Enforced (Actions mode): rendered to `SQUADHQ_TRUSTED_ASSOCIATIONS`. Including `NONE` or `FIRST_TIMER` warns: anyone on the internet could trigger paid LLM jobs |
| `merge.mode` | `approvers` \| `risk_based` \| `auto` | `approvers` | Enforced through `config/projects.json`. `risk_based` behaves like `approvers` today. `auto` warns. See [merge policy](../concepts/merge-and-risk-policy.md#merge-modes) |
| `merge.risk_based.max_risk` | `low` \| `medium` \| `high` | `low` | Not enforced yet |
| `merge.risk_based.require_checks` | list of gates | `[sast, sca, secrets]` | Not enforced yet |
| `merge.risk_based.require_security_agent` | bool | `true` | Not enforced yet |
| `merge.always_human` | list of `auth`, `payments`, `migrations`, `workflows`, `infra`, `deps-major`, `secrets`, `pii` | first six | Rendered into `config/projects.json`. Not enforced yet; the core [risk floor](../concepts/merge-and-risk-policy.md#risk-floor) already forces most of these to `high` |
| `release.strategy` | `merge_is_prod` \| `release_tag` \| `promote_branch` | `merge_is_prod` | Not enforced yet |
| `release.promote_branch` | string | empty | Required when `strategy: promote_branch` |

## `security`

| Key | Type | Default | Effect |
|---|---|---|---|
| `gates` | list of `sast`, `sca`, `secrets`, `iac`, `license`, `sbom`, `dast`, `container` | `[secrets, sast, sca, license, iac, sbom]` | Enforced: rendered to `SQUADHQ_GATES`. The router runs `secrets`, `sast`, `sca`, `license`, `iac` on PRs and `sbom` on releases. `dast` and `container` are accepted but have no router job. If the variable is unset, the router defaults to `secrets,sast,sca,license,iac,sbom` |
| `show_agent_transcripts` | bool | `false` | Enforced: rendered to `SQUADHQ_DEBUG_TRANSCRIPTS`. `true` prints full agent transcripts in run logs. Never on public repositories |

The default is the same everywhere: omitting `security.gates`, or leaving `SQUADHQ_GATES` unset, means `secrets,sast,sca,license,iac,sbom`.

## `projects`

A list. Each entry:

| Key | Type | Default | Effect |
|---|---|---|---|
| `name` | string | required | Display name; must be unique. Used in reports and `budgets.per_project_usd` |
| `repo` | `owner/name` | required | Must be unique. Rendered into `config/projects.json` |
| `branch` | string | `main` | Rendered as `integration_branch`: where agents branch from and target |
| `packs` | list of pack ids | `[]` | Enforced: CI plan, tool allowlist, review/implement rules, risk paths. Warns if a pack is not under `packs/` |
| `pack_roots` | `pack: dir` | pack's own `root` | Enforced. The pack must be listed in `packs`; the dir must be relative |
| `environments.<name>.url` | URL | | Context; `production.url` becomes `frontend_url`, used by the SEO and Growth agents |
| `environments.<name>.api_url` | URL | | Context; `staging.api_url` (or `url`) becomes `staging_url` |
| `environments.<name>.urls.<key>` | URL | | Context; for `staging`, rendered as `staging_<key>_url` (keys must be snake_case) |
| `environments.<name>.deploy` | `vercel`, `railway`, `fly`, `netlify`, `render`, `heroku`, `k8s`, `helm`, `github-pages`, `cloudflare`, `aws`, `gcp`, `azure`, `custom`, `none` | `none` | Context. The optional production deploy workflow reads `SQUADHQ_DEPLOY_TARGET` instead |
| `environments.<name>.approval` | `none` \| `required` | `none` | Context. Not enforced by a workflow yet |
| `product.brief_file` | path | | Context for the Product Owner and reporting agents |
| `product.markets`, `product.keywords` | lists | `[]` | Context for Legal, Growth and SEO |
| `product.notes` | string | | Context: positioning and what the product must never claim |
| `agents` | as top-level `agents` | | Per-project switches for catalogue skills; rendered into `config/projects.json` |
| `merge_mode` | `approvers` \| `risk_based` \| `auto` | `policies.merge.mode` | Enforced, per project |
| `budget_usd` | number | from `budgets.per_project_usd` | Not enforced yet |
| `quality.arch` | `auto` \| `required` \| `off` | `auto` | Enforced. Architecture gate (ci-gate sub-check `arch`): `auto` runs the pack's arch tool when its config exists in the repo; `required` also fails when the config is missing |
| `quality.lint` | `auto` \| `default` \| `off` | `auto` | Enforced. Lint/format gate (sub-check `lint`): `auto` runs when a linter config exists or the linter needs none; `default` also uses the pack's default config when the repo has none |
| `quality.changed_coverage` | `enforce` \| `report` \| `off` | `report` | `report`: changed-lines coverage in the job summary only. `enforce`: required sub-check `changed-coverage`, failing below the pack's `min_changed` (only when `min_changed` > 0 and a coverage report exists) |

See [Quality gates](../concepts/quality-gates.md) for the tools each pack uses.

## Validating and rendering

```bash
squadhq validate                 # schema + semantic checks; exit 1 on errors
squadhq validate --explain       # also: model per job, UTC cron per schedule
squadhq render --dry-run --diff  # what would change
squadhq render                   # write config/models.json, auth.json, projects.json and crons
squadhq render --check           # CI: exit 1 if anything is stale
```

`render` prints the `SQUADHQ_*` variable values and the `gh variable set` commands to apply them, and the list of required secrets. Rendered files carry a "do not edit by hand" marker: edit `squadhq.yml` and re-render instead. See [variables and secrets](variables-and-secrets.md).
