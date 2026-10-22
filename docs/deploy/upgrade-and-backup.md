# Upgrades, backups and disaster recovery

## What holds state

| Data | Where | Loss impact |
|---|---|---|
| Job queue, approvals, runs, tickets cache, briefs, API token hashes, scheduler cursors | Postgres (or SQLite in dev) | The tickets cache rebuilds from trackers. Queued jobs replay from the sweeper. Approvals must be re-requested |
| **Audit log** (`audit_events`) | Postgres | Evidence. Back it up and also export it to write-once storage |
| `squadhq.yml` | Your hub repo / ConfigMap | Kept in git |
| Secrets | Your secrets backend | Rotate them if a backup leaks |

The trackers (GitHub, Jira, …) remain the source of truth for tickets and PRs. The sweeper reconciles after any outage.

## Upgrading

Releases follow semver. Schema migrations are versioned (`schema_version` table) and **additive within a major version**: new tables, columns and indexes only. Older pods keep working against a newer schema, so rolling updates are safe.

### Docker Compose

```bash
cd deploy
docker compose exec postgres pg_dump -U squadhq -Fc squadhq > backup-$(date +%F).dump   # 1. back up
sed -i 's#^SQUADHQ_IMAGE=.*#SQUADHQ_IMAGE=ghcr.io/<owner>/agent-squad-hq@sha256:<new>#' .env  # 2. pin
docker compose pull
docker compose up -d          # 3. the migrate service runs first, then api, worker and scheduler restart
docker compose exec api python -m squadhq.audit verify   # 4. check
```

### Helm

```bash
helm upgrade hq deploy/helm/agent-squad-hq -n squadhq --reuse-values --set image.digest=sha256:<new>
```

The `migrate` pre-upgrade hook must succeed before the Deployments roll. If it fails, the release stops and the old pods keep serving.

### Cloud examples

Run the `migrate` task or job, then deploy the new image (ECS service update, `gcloud run deploy`, or a Container Apps revision).

### Rolling back

Roll the image back. Because migrations within a major version only add, the old code ignores new columns. Do **not** restore an old database dump just to roll back code. You would lose audit events.

### Graceful shutdown

Workers stop on SIGTERM after their current job. If they are killed sooner, the job's lease expires (`SQUADHQ_WORKER_LEASE_S`, default 900 s) and another worker claims it. Handlers are idempotent, so at-least-once delivery is safe. Set the grace period (`worker.terminationGracePeriodSeconds`, compose `stop_grace_period`) above your typical job length.

## Backups

| What | How | Frequency |
|---|---|---|
| Postgres | Managed PITR (RDS, Cloud SQL, Azure) or `pg_dump -Fc` / pgBackRest / CloudNativePG backups | Continuous WAL plus a daily full backup, kept 30 days |
| Audit export | `python -m squadhq.audit export --since <last_seq> --out audit-<date>.jsonl`, uploaded to an Object-Lock bucket | Daily |
| Audit head anchor | Record `head_hash` from `python -m squadhq.audit verify` alongside the export | Daily |
| SQLite (dev only) | `sqlite3 squadhq.db ".backup squadhq-$(date +%F).db"` while it runs (WAL-safe) | Daily |

Example cron job on the compose host:

```bash
0 3 * * * cd /opt/agent-squad-hq/deploy && \
  docker compose exec -T postgres pg_dump -U squadhq -Fc squadhq > /backups/squadhq-$(date +\%F).dump && \
  docker compose exec -T api python -m squadhq.audit export > /backups/audit-$(date +\%F).jsonl
```

Encrypt backups at rest and keep them in a different account or project from production.

## Restore and DR drill

Run this drill quarterly. The target is RPO ≤ 24 h (dump) or ≤ 5 min (PITR), and RTO ≤ 1 h.

1. Provision a fresh database and run `python -m squadhq.server migrate` against it with the **same** image version as the backup.
2. Restore: `pg_restore -d "$SQUADHQ_DB_URL" --clean --if-exists backup.dump`. The append-only triggers are restored with the schema; restore as the owner role.
3. Verify the audit chain: `python -m squadhq.audit verify` must print `"ok": true`. Its `head_hash` must match the anchored export of that day, or extend from it (`verify --file` checks an exported JSONL).
4. Start the api, worker and scheduler. The scheduler resumes from its cursors and enqueues only the latest missed fire per schedule, so there is no storm. The sweeper reconciles ticket state from the trackers.
5. Re-send any webhooks missed during the outage. GitHub's "Recent deliveries" page can redeliver; duplicates are ignored by delivery id. A redelivered `labeled` event re-creates any pending approval lost since the backup. You can also remove and re-add the approval label.

## Chaos check (Phase 4 exit criterion)

```bash
docker compose exec api python -m squadhq.server create-token --role admin --name chaos   # keep the token
curl -s -X POST -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"kind":"sweep"}' localhost:8080/api/v1/jobs
docker compose kill worker && sleep 5 && docker compose up -d worker
# after SQUADHQ_WORKER_LEASE_S the job is re-claimed and ends "done" (or "dead" with last_error)
curl -s -H "Authorization: Bearer $T" localhost:8080/api/v1/jobs | jq '.counts'
```
