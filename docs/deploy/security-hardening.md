# Security hardening

This guide covers what server mode does by default, what you must configure yourself, and how to tighten it further. Threat model context: the server receives untrusted input (webhooks, issue text) and holds credentials that can push code. Every hardening step aims to keep a compromised component, prompt or job from reaching beyond its own scope.

## Defaults you get

| Area | Default |
|---|---|
| API auth | Bearer tokens, 256-bit random. Only `sha256(token)` is stored. Comparison is constant-time against every candidate. Roles are `viewer < approver < admin`, plus `reporter`: bound to one project when it is minted, it can only `POST /api/v1/runs` for that project. Each project's `SQUADHQ_SERVER_TOKEN` must be a reporter token |
| Webhooks | Every delivery must carry a valid HMAC signature: GitHub, Jira and Linear use HMAC-SHA256; Trello uses HMAC-SHA1 over body plus callback URL; GitLab uses its token. Unknown or unconfigured sources get 401 or 404. Linear deliveries outside a 60 s window are rejected. Deliveries are deduplicated by delivery id **and** by SHA-256 of the signed body, so a replayed body under a new delivery id is ignored |
| Registered projects only | Events for repositories or tracker projects that are not registered (`squadhq.yml` `projects`, or the hub repo) are dropped and audited (`webhook.dropped_unregistered`) before any ticket, approval or dispatch |
| Trigger trust | Code decides whether an event may start agent work, never a model. Trusted author associations come from `SQUADHQ_TRUSTED_ASSOCIATIONS` (the same variable as Actions mode; default `OWNER, MEMBER, COLLABORATOR`; `SQUADHQ_ALLOWED_ASSOCIATIONS` is a deprecated alias). Jira, Trello and GitLab carry no association, so only the bot (a GitHub identity, matched for GitHub events only) and `SQUADHQ_TRUSTED_ACTORS` qualify. Trello events are `MEMBER` only when `idMemberCreator` or the username is listed in `SQUADHQ_TRUSTED_ACTORS`. Linear events are `MEMBER` only when the actor payload says `guest: false`, or the actor's id, email or name is trusted; guests and unproven users are `NONE`. Refusals are written to the audit log |
| Merges | Performed only by deterministic code after a human approval (`POST /approvals/{id}/decision`), see [ADR 0005](../adr/0005-agents-never-merge-or-deploy.md). Merge approvals are requested **only** by a `pull_request` `labeled` event adding `needs-approver-merge`, applied by our bot or a trusted labeler. They are never re-derived from a PR's current labels (comments cannot create or re-pin one), and their subject is always `pr#N`. The approval **pins the head commit carried by that label event**, and a `pull_request.synchronize` (new commits) supersedes any pending merge approval (`approval.superseded` in the audit log); approving is refused (409) if the head has moved, and fork-head PRs are refused unless `SQUADHQ_ALLOW_FORK_MERGES=true`. Checks are read **by commit SHA**, never by branch name; there must be at least one, every `SQUADHQ_REQUIRED_CHECKS` entry (default `hub / ci-gate`) must be present, and all must be green. On GitHub a required check only counts if it was produced by `SQUADHQ_REQUIRED_CHECKS_APP` (default `github-actions`, app id 15368) and by the workflow `SQUADHQ_REQUIRED_CHECKS_WORKFLOW` (default `.github/workflows/agents.yml`; empty = any workflow), because any installed app can post a check with any name. GitLab checks are matched by name only, so protect GitLab targets with branch rules. The merge call carries `sha=` so the host rejects a moved head. No model is in that path |
| Secret hygiene | Job errors, results and container log tails are redacted (GitHub/Anthropic/OpenAI/AWS/Slack token shapes, bearer headers, `sqh_` API tokens, and the exact values of known secrets) before they are stored. Error details, results and payloads in `/api/v1/jobs` are shown to admins only |
| Request limits | 1 MiB API bodies, 5 MiB webhook bodies (`SQUADHQ_*_MAX_BODY_BYTES`), enforced while the body streams |
| Response headers | `nosniff`, `no-store`, `DENY` framing, `no-referrer`, a request id on every response |
| Audit | Append-only table with UPDATE/DELETE blocked by database triggers. Each event is hash-chained: `hash = sha256(prev_hash + canonical_json(event))`. Check it with `python -m squadhq.audit verify` |
| Containers | Non-root uid 10001, read-only root filesystem, `no-new-privileges`, all capabilities dropped, tmpfs `/tmp`, `tini` as PID 1 |
| Metrics | `/metrics` requires a token unless `SQUADHQ_METRICS_PUBLIC=true` |
| Run reports | `POST /api/v1/runs` rejects non-finite or out-of-range numbers (cost ≤ 10 000 USD, duration ≤ 7 days), a `started_at` outside the last `SQUADHQ_RUN_MAX_SKEW_DAYS` (default 7), and `extra` over 256 KiB. Validation errors never echo the request input |
| Push devices | A push token belongs to the API token (by id) that registered it. Re-registering someone else's push token returns 409 unless you are an admin |
| API docs | `/docs` and `/openapi.json` are off unless `SQUADHQ_API_DOCS=true` |

## Must-do checklist

1. **TLS everywhere.** Terminate TLS at the proxy or load balancer. Do not expose port 8080 to the internet (compose binds it to 127.0.0.1).
2. **Narrow the GitHub token.** Prefer a GitHub App installation token with the minimal permissions: issues and pull requests read/write, contents read, actions write for dispatch, checks read. Refresh it through your secrets backend instead of using a long-lived PAT.
3. **Webhook secrets.** Use 32 random bytes per source and rotate yearly. Rotate by setting the new secret in both places within the source's retry window.
4. **Tokens per human.** Issue one approver token per person (`create-token --role approver --name alice`) so the audit log names the person. Revoke with `DELETE /api/v1/tokens/{id}`.
5. **Database role.** Give the application role `INSERT, SELECT` only on `audit_events` (`REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM squadhq;`). Run migrations with a separate owner role.
6. **Off-box audit anchor.** Export the chain daily (`python -m squadhq.audit export --since <last_seq>`) to write-once storage (S3 Object Lock, GCS bucket lock, Azure immutable blob) and record the head hash. An attacker who controls the database could rewrite the whole chain, but cannot change an anchored head.

## Secrets backends

The server reads `SQUADHQ_X` or, if set, the file at `SQUADHQ_X_FILE`. Any backend that produces environment variables or files works:

| Backend | How |
|---|---|
| Docker / compose | `.env` with mode 600, or Docker secrets mounted at `/run/secrets/*` with `SQUADHQ_..._FILE` |
| Kubernetes | External Secrets Operator (AWS SM, GCP SM, Azure KV, Vault) → Secret → `secrets.existingSecret` |
| HashiCorp Vault | Vault Agent injector writes files; point `SQUADHQ_..._FILE` at them |
| AWS Secrets Manager | ECS `secrets` block (see `deploy/terraform/aws-ecs`) |
| GCP Secret Manager | Cloud Run `secret_key_ref` |
| Azure Key Vault | Container Apps Key Vault references with a managed identity |

Never put secrets in `squadhq.yml`, Helm values committed to git, or Terraform variables in plain text.

## Network egress

The worker is the only component that needs the internet. The API needs inbound traffic only; the scheduler needs only the database.

Allowlist for the worker:

- `api.github.com`, `github.com`, `uploads.github.com` (or your GHES host);
- your tracker: `*.atlassian.net`, `api.linear.app`, `api.trello.com`, your GitLab;
- your LLM provider(s): `api.anthropic.com`, `api.openai.com`, Bedrock, Vertex or Azure OpenAI endpoints, or nothing if you use local Ollama;
- notifications: Slack, Teams, SMTP or PagerDuty endpoints that you configured.

How to enforce it:

- **Compose / VM.** Attach `egress` and `squadhq-egress` to a forward proxy such as Squid or Smokescreen with the allowlist, and set `HTTPS_PROXY` for the worker and executor containers. Alternatively, drop direct outbound traffic with nftables on the host.
- **Kubernetes.** The chart's NetworkPolicy already limits egress to 443 for the worker only, and blocks private ranges and `169.254.169.254` (cloud metadata). Upgrade to FQDN policies (CiliumNetworkPolicy `toFQDNs`) or route through an egress gateway.
- **Cloud.** Use AWS Network Firewall domain lists, GCP Secure Web Proxy, or Azure Firewall application rules.

## Coding-job sandbox (worker mode)

Each coding job runs in a fresh container (`squadhq.server.sandbox.DockerContainerRunner`):

- `--read-only` root, tmpfs `/tmp` (noexec) and `/work`, `--cap-drop ALL`, `no-new-privileges`, `--user 10001:10001`;
- `--cpus`, `--memory` (swap disabled) and `--pids-limit` limits, plus a wall-clock timeout after which the container is killed. Timed-out jobs are not retried, to avoid burning cost;
- `--network squadhq-egress` only, with the egress allowlist applied as described above;
- **per-job credentials** are passed by name (`-e GH_TOKEN`). Their values never appear in argv or `ps`. Mint short-lived, repo-scoped tokens: the `TokenMinter` interface exists for this, and the default `EnvTokenMinter` is for development;
- results come back only through `/out/execution-result.json` (the neutral `ExecutionResult`). The per-job host directory is created inside a private `0700` parent, is itself `0700` and owned by the container user (chowned when the worker runs as root; otherwise the container runs as the worker's uid), and is deleted after the run.

The worker reaches Docker through `tecnativa/docker-socket-proxy`, which allows container operations and nothing else (no exec, build, volumes, swarm or system). The daemon is still powerful. For multi-tenant or high-risk use:

- run executor containers under **gVisor** (`--runtime=runsc`) or **Kata Containers**, or use rootless Docker / Podman;
- run the worker on a dedicated VM or node pool with no other workloads;
- keep the executor image minimal and pinned by digest, and scan it (Trivy) in CI.

## Untrusted input and prompt injection

Issue and PR text reaches models only through the agent prompts, fenced with `squadhq.untrusted.fence`. The server itself never interprets that text. It stores it, routes events by labels and associations, and acts on approvals. Approval decisions come from authenticated approver tokens, never from a comment.

## Logging and privacy

Logs are JSON with `request_id` and `job_id`. Request bodies, tokens and webhook payloads are not logged. Database URLs are redacted (`redact_url`). Ship logs to your SIEM. The audit log is the record of who decided what.
