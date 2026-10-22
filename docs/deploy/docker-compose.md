# Self-hosting with Docker Compose (single VM)

Server mode runs the control plane as three processes from one image, plus Postgres:

| Service | Command | What it does |
|---|---|---|
| `api` | `serve` | REST API v1, signed webhook intake, approvals, audit, `/metrics` |
| `worker` | `worker` | Drains the job queue: sweeps, ticket transitions, janitor, hub loops, coding-job dispatch, approval actions |
| `scheduler` | `scheduler` | Timezone-aware cron from `squadhq.yml`. Leader-elected, so it is safe to run more than one |
| `migrate` | `migrate` | One-shot schema migration. Runs before the others start |
| `postgres` | | State store, queue and audit log |
| `ollama` | | Optional local model server (`--profile ollama`) |

## Requirements

- A Linux VM with 2 vCPU, 4 GB RAM and 20 GB disk. Hetzner CX22, a DigitalOcean 4 GB droplet or similar is enough.
- Docker Engine 24 or later with the `docker compose` v2 plugin, and `openssl`.
- A DNS name and a TLS-terminating reverse proxy. The API binds to `127.0.0.1:8080` only.

## Install

The installer takes one command and about five minutes:

```bash
git clone https://github.com/<owner>/agent-squad-hq && cd agent-squad-hq
./deploy/install.sh
```

The installer does five things:

1. checks for docker, the compose plugin, openssl and a running daemon;
2. asks for the hub repo, the mode, the public URL, the image and a GitHub token;
3. generates the Postgres password, the webhook secrets and an admin API token with `openssl rand`. Only the token's SHA-256 hash is written;
4. writes `deploy/.env` (mode 600) and `deploy/squadhq.yml` (copied from `squadhq.example.yml` if that file is missing);
5. pulls or builds the image, runs `docker compose up -d`, waits for `/readyz`, and prints the admin token once.

To run it without prompts, preset the answers and pass `--yes`:

```bash
SQUADHQ_HUB_REPO=acme/agent-squad-hq SQUADHQ_GITHUB_TOKEN=... ./deploy/install.sh --yes
```

`--dry-run` prints the plan and validates the compose configuration; it writes nothing, starts nothing and prints no token. A real run fails (with the `migrate` and `api` logs) if `/readyz` is not up within 120 s (`SQUADHQ_READY_TRIES` × 2 s).

The installer generates the GitHub, Linear, Jira and GitLab webhook secrets. Trello signs webhooks with your Trello app's API secret, which Trello issues; pass it as `SQUADHQ_WEBHOOK_SECRET_TRELLO` (or at the prompt), or Trello webhooks stay rejected.

### Manual install

```bash
cd deploy
cp .env.example .env && chmod 600 .env          # then fill it in
cp ../squadhq.example.yml squadhq.yml            # then edit it
docker compose build api                         # only `api` carries build: (one image for all services)
docker compose run --rm api create-token --role admin --print-only
# paste "env_entry" into SQUADHQ_API_TOKENS in .env and keep "token" safe
docker compose up -d
```

## Reverse proxy (Caddy example)

```caddyfile
hq.example.com {
    reverse_proxy 127.0.0.1:8080
    request_body {
        max_size 5MB
    }
}
```

Set `SQUADHQ_TRUSTED_PROXIES=127.0.0.1` (the default) so the request logs show the real client IP.

## Connect your sources

Only registered projects are processed: list every repo (and tracker project) under `projects:` in `squadhq.yml`. Events for anything else are dropped and audited.

| Source | Webhook URL | Secret variable | Notes |
|---|---|---|---|
| GitHub (org webhook or App) | `/api/v1/webhooks/github` | `SQUADHQ_WEBHOOK_SECRET_GITHUB` | content type `application/json`; events: issues, issue_comment, pull_request, pull_request_review, check_run, push |
| Jira Cloud | `/api/v1/webhooks/jira` | `SQUADHQ_WEBHOOK_SECRET_JIRA` | admin webhook with a secret (`X-Hub-Signature`) |
| Linear | `/api/v1/webhooks/linear` | `SQUADHQ_WEBHOOK_SECRET_LINEAR` | signing secret from the webhook settings |
| Trello | `/api/v1/webhooks/trello` | `SQUADHQ_WEBHOOK_SECRET_TRELLO` (the app secret) | also set `SQUADHQ_TRELLO_CALLBACK_URL` to the exact public URL |
| GitLab | `/api/v1/webhooks/gitlab` | `SQUADHQ_WEBHOOK_SECRET_GITLAB` | the "secret token" field |

Deliveries are rejected in any of these cases: the source has no secret configured, the signature is missing, or the signature is wrong. Redeliveries are deduplicated by delivery id. Jira, Trello and GitLab events carry no author association, so agent work they request is refused unless the actor is the bot or is listed in `SQUADHQ_TRUSTED_ACTORS`.

## Modes

- **`byo-compute`** (the default). Coding jobs become a `workflow_dispatch` of `SQUADHQ_DISPATCH_WORKFLOW` in the project repo, so code never leaves GitHub-hosted runners. The workflow reports its `ExecutionResult` back with `POST /api/v1/runs`, using the project's `SQUADHQ_SERVER_TOKEN` secret. Mint it per project as a reporter token: `docker compose exec api python -m squadhq.server token create --role reporter --project <owner/repo> --name <repo>-ci`. A reporter token can only report runs for its own project.
- **`worker`**. Coding jobs run as sandboxed containers on this host:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.worker-mode.yml up -d
  ```

  The worker reaches the docker daemon only through a socket proxy that allows container operations and nothing else. Read [security-hardening.md](security-hardening.md) before you enable this mode.

## Operating it

```bash
docker compose ps
docker compose logs -f worker                       # JSON logs, one object per line
docker compose exec api python -m squadhq.server scheduler --list   # resolved UTC crons + next fire times
docker compose exec api python -m squadhq.audit verify              # audit chain check
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/api/v1/jobs?status=dead
curl -s -X POST -H "Authorization: Bearer $TOKEN" localhost:8080/api/v1/jobs/<id>/retry
curl -s -H "Authorization: Bearer $TOKEN" localhost:8080/metrics
```

When you change `squadhq.yml`, run `docker compose restart scheduler api worker`.

### Smoke test (after install)

1. `curl -fsS localhost:8080/readyz` returns `{"status":"ready",...}`.
2. In GitHub, send a webhook "ping". The response is `200 {"ping": true}`.
3. Label a test issue `needs-human`. `GET /api/v1/approvals` now lists an `escalation` entry.
4. Kill the worker in the middle of a job (`docker compose kill worker; docker compose up -d worker`). When its lease expires the job is claimed again and completes.

## Troubleshooting

| Symptom | Check |
|---|---|
| Webhooks return 401 | Is the secret set for that source? GitHub shows the response body under the webhook's "Recent deliveries" |
| Jobs stay `queued` | `docker compose logs worker`, and check `SQUADHQ_DB_URL` matches across services |
| Jobs go `dead` | `GET /api/v1/jobs?status=dead` shows `last_error`. Fix the cause, then `POST /jobs/{id}/retry` |
| Scheduler never fires | `scheduler --list` prints `errors` for invalid schedule phrases and the zone in use |
