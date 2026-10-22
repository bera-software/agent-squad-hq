# Observability

## Actions mode

| Signal | Where |
|---|---|
| What each job did | The job's step summary. Skipped work always says why (for example "approver merge required — <reason>") |
| Which jobs an event fired | Set `SQUADHQ_RUN_MAP=true` to add a job that prints every event fact the router's gates read |
| Failures | `escalation` issues in the hub, classified by `squadhq.failure_handler` as TRANSIENT, INFRA, AGENT or UNKNOWN. Transient failures are retried (up to two re-runs) before escalating; repeats comment on the open issue |
| Cost per run | The `Cost ledger — <YYYY-MM>` hub issue: one JSON line per coding-agent run (job, model, cost, duration, turns, auth mode, fallback, auto-merged) |
| Hub-loop spend | `state/usage.json` in the hub |
| Capacity state | `state/capacity.json` in the hub |
| Daily and weekly view | The Daily Brief and Weekly Rollup issues |
| Agent transcripts | Off by default. `SQUADHQ_DEBUG_TRANSCRIPTS=true` prints them in the run log (never on public repositories) |

## Server mode

The server provides three standard signals.

**Logs.** JSON, one object per line, with `request_id` and `job_id` from context (`SQUADHQ_LOG_FORMAT`, `SQUADHQ_LOG_LEVEL`). Send a request id with `X-Request-ID` to correlate.

**Metrics.** `GET /metrics` in Prometheus text format (version 0.0.4), from a built-in registry with no client library. It needs a bearer token unless `SQUADHQ_METRICS_PUBLIC` is set. The Helm chart includes a `ServiceMonitor` template.

**Traces.** OpenTelemetry spans are emitted only when `SQUADHQ_OTEL_ENABLED=true` and the `opentelemetry` packages are installed; otherwise tracing is a no-op. Configure the exporter with the standard `OTEL_*` variables.

Other endpoints for operators:

| Endpoint | Use |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness and readiness |
| `GET /api/v1/jobs?status=dead` | Jobs that exhausted their retries, with `last_error`; re-queue with `POST /api/v1/jobs/{id}/retry` |
| `GET /api/v1/runs`, `GET /api/v1/costs?month=YYYY-MM` | Agent runs and spend by project, role and model |
| `GET /api/v1/schedules` | Resolved schedules and budgets |
| `GET /api/v1/audit`, `/audit/verify`, `/audit/export` | Hash-chained audit events, chain verification, export |

`squadhq server scheduler --list` prints the next fire times and any invalid schedule phrases. `python -m squadhq.audit verify` checks the audit chain from the command line.

For deployment specifics see [Docker Compose](../deploy/docker-compose.md#operating-it) and [Kubernetes](../deploy/kubernetes.md).
