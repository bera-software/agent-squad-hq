# Quickstart: server mode

Server mode runs the control plane as a service you host instead of as GitHub Actions workflows. Choose it when you need Jira, Linear, Trello or GitLab as the tracker, a scheduler that handles daylight saving properly, an approvals API for the dashboard and mobile app, or a hash-chained audit log in a database.

!!! warning "Less proven than Actions mode"
    Server mode is implemented and tested in-process, and the deployment examples pass `helm lint` and `terraform validate`. They have not been applied against real cloud accounts as part of the 0.1.0 work. Try it in a sandbox first.

## What runs

One image (`deploy/docker/Dockerfile`, published as `ghcr.io/<owner>/agent-squad-hq` by the release workflow) runs as several processes:

| Process | Command | Job |
|---|---|---|
| API | `squadhq server serve` | REST API v1, signed webhook intake, approvals, audit, `/metrics` |
| Worker | `squadhq server worker` | Drains the job queue: sweeps, ticket transitions, janitor, hub loops, coding-job dispatch, approval actions |
| Scheduler | `squadhq server scheduler` | Timezone-aware cron from `squadhq.yml`, leader-elected |
| Migrate | `squadhq server migrate` | One-shot schema migration |
| Postgres | | State, queue and audit log (SQLite is available for a single process) |

Install the server extra to run it outside a container: `pip install 'agent-squad-hq[server,postgres]'` (or `pip install -e '.[server,postgres]'` from source).

## Install

The fastest path is Docker Compose on one VM:

```bash
git clone https://github.com/berasoftware/agent-squad-hq && cd agent-squad-hq
./deploy/install.sh
```

The installer checks prerequisites, asks for the hub repository, the mode, the public URL, the image and a GitHub token, generates the Postgres password, webhook secrets and an admin API token, writes `deploy/.env` (mode 600) and `deploy/squadhq.yml`, starts the stack and waits for `/readyz`.

Full instructions, including a manual install and a Caddy reverse-proxy example, are in [Docker Compose](../deploy/docker-compose.md). For Kubernetes see [Helm](../deploy/kubernetes.md); for AWS, GCP and Azure see [managed cloud runtimes](../deploy/cloud.md).

## Configure

1. **`squadhq.yml`** is the same file as in Actions mode ([reference](../configuration/squadhq-yml.md)). The scheduler reads `schedules:` in `org.timezone` with real daylight-saving handling. Set `trackers.default` and the tracker block (`jira`, `linear` or `trello`).
2. **Server settings** come from `SQUADHQ_*` environment variables. Any secret can be supplied as a file with `SQUADHQ_<NAME>_FILE`. Validate them with `squadhq server check`. The full list is in `deploy/.env.example`.
3. **API tokens**: `squadhq server create-token --role admin` prints a token once and stores only its SHA-256 hash. Roles are `viewer`, `approver` and `admin`.

## Connect your sources

Point each source's webhook at `https://<your-host>/api/v1/webhooks/<source>` (`github`, `jira`, `linear`, `trello` or `gitlab`) and set the matching `SQUADHQ_WEBHOOK_SECRET_<SOURCE>`. Deliveries without a valid signature are rejected. See the [table in the Compose guide](../deploy/docker-compose.md#connect-your-sources).

Jira, Trello and GitLab events carry no GitHub-style author association. Agent work they request is refused unless the actor is the bot or is listed in `SQUADHQ_TRUSTED_ACTORS`. See [trackers](../concepts/trackers.md#trust-model).

## Choose where coding agents run

- **`byo-compute`** (default, `SQUADHQ_MODE=byo-compute`): the worker dispatches the hub's `squadhq-agent.yml` workflow in the project repository, so code stays on GitHub-hosted runners. Each project needs the optional stub `onboarding/optional/squadhq-agent.yml.stub`, installed as `.github/workflows/squadhq-agent.yml` by `python -m squadhq.onboard apply <owner/repo> <Name> --server` or kept current by `python -m squadhq.stub_sync --server`, the repository variable `SQUADHQ_SERVER_URL` (must be `https://`) and the secret `SQUADHQ_SERVER_TOKEN` so the run can report its result with `POST /api/v1/runs`. That secret must be a **per-project reporter token** (`python -m squadhq.server token create --role reporter --project <owner/repo> --name <repo>-ci`). A reporter token can only report runs for its own project, so anyone who can read the project's secrets gains nothing else. Never put an admin token there. The server needs `SQUADHQ_GITHUB_TOKEN` to dispatch.
- **`worker`** (`SQUADHQ_MODE=worker`): coding jobs run in an ephemeral container on your host with a read-only root filesystem, no capabilities, a non-root user, resource limits, a time limit and per-job credentials. Docker cannot filter egress by host name, so put the job network behind an egress proxy or firewall. Read [security hardening](../deploy/security-hardening.md) first.
  - **Account pool (worker mode).** The worker selects one coding-agent account per job: the hub's `config/auth.json` `pool` when it has one, otherwise every `ANTHROPIC_API_KEY_1..5` (ids `api-<n>`) and `CLAUDE_CODE_OAUTH_TOKEN_1..5` (ids `sub-<n>`) set in the worker's environment (`SQUADHQ_POOL_STRATEGY=ordered|round_robin`). Only accounts whose secret is set are used, and only the selected credential is passed into the job container (as `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`, plus `SQUADHQ_ACCOUNT=<id>`). A capacity trip pauses that account for 60 minutes (store kv `capacity/accounts`, shared by every worker replica) and the job is retried on the next account; when every account is tripped the job waits `retry_max_s`, or runs on the metered `ANTHROPIC_API_KEY` with `fallback_on_capacity_trip`. `GET /api/v1/capacity` (viewer) shows each account's state, never a secret value. In `byo-compute` mode the pool is the Actions one ([account pool](../configuration/squadhq-yml.md#account-pool)).

## Smoke test

```bash
curl -fsS https://<your-host>/readyz
curl -fsS -H "Authorization: Bearer $TOKEN" https://<your-host>/api/v1/projects
curl -fsS -H "Authorization: Bearer $TOKEN" https://<your-host>/api/v1/audit/verify
```

Then follow the [smoke test in the Compose guide](../deploy/docker-compose.md#smoke-test-after-install).

## Next steps

- [Security hardening](../deploy/security-hardening.md) and [upgrades and backups](../deploy/upgrade-and-backup.md)
- [Observability](../operations/observability.md): JSON logs, Prometheus metrics, optional OpenTelemetry
- The preview [web dashboard](../apps/dashboard.md) and [mobile app](../apps/mobile.md) talk to this API
