# Managed cloud runtimes (AWS, GCP, Azure)

`deploy/terraform/` holds **examples**: one small, readable module per provider. Fork one and adapt it to your landing zone. They are not a supported product module. Each example:

- runs the same image as compose and Helm;
- takes secrets from the provider's secret manager, never from Terraform variables in plain text;
- passes `squadhq.yml` as `SQUADHQ_CONFIG_YAML`, so no config volume is needed and the root filesystem stays read-only;
- expects an existing VPC/VNet and Postgres. Database provisioning is deliberately left out because it is where your backup, HA and compliance choices live.

Pin the image **by digest** (`…/agent-squad-hq@sha256:…`) in every example.

## AWS — ECS Fargate (`deploy/terraform/aws-ecs`)

| Piece | Implementation |
|---|---|
| API | ECS service behind an ALB. HTTPS listener with a TLS 1.3 policy; health check on `/readyz` |
| Worker, scheduler | ECS services without a load balancer, 2 tasks each (the scheduler is leader-elected) |
| Migrations | `migrate` task definition: `aws ecs run-task --launch-type FARGATE --task-definition <output> ...` before you roll out a new image |
| Secrets | `secret_arns = { SQUADHQ_DB_URL = "arn:aws:secretsmanager:…", … }` injected through the task `secrets` block. The execution role can read only those ARNs |
| Network | Tasks run in private subnets. Egress is 443 to anywhere plus 5432 inside the VPC. Narrow it with AWS Network Firewall domain lists or an egress proxy |

```bash
cd deploy/terraform/aws-ecs
cp terraform.tfvars.example terraform.tfvars   # add vpc_id, subnets, certificate_arn, secret_arns
terraform init && terraform plan
```

You can also run EKS with the [Helm chart](kubernetes.md).

## GCP — Cloud Run (`deploy/terraform/gcp-cloud-run`)

Cloud Run services must serve HTTP, so the long-running loops become jobs:

| Piece | Implementation |
|---|---|
| API | Cloud Run service (min 1 instance), public. Authentication is done by the app: webhook signatures and bearer tokens |
| Worker | Cloud Run **job** `worker --once --max-jobs 200`, started every minute by Cloud Scheduler |
| Scheduler | Cloud Run job `scheduler --once`, every minute. Overlapping runs are harmless because of the leader lock and idempotency keys |
| Migrations | Cloud Run job `migrate`: `gcloud run jobs execute agent-squad-hq-migrate --wait` |
| Secrets | Secret Manager via `value_source.secret_key_ref`. The runtime service account gets `secretAccessor` on just those secrets |
| Database | Cloud SQL through a Serverless VPC Access connector (`PRIVATE_RANGES_ONLY` egress) |

The queue drains at most once a minute in this layout. If you need lower latency, run the worker on GKE with the Helm chart.

## Azure — Container Apps (`deploy/terraform/azure-container-apps`)

| Piece | Implementation |
|---|---|
| API | Container App with external HTTPS ingress and probes on `/healthz` and `/readyz` |
| Worker, scheduler | Container Apps with no ingress (long-running loops) |
| Migrations | Container Apps Job, manual trigger: `az containerapp job start -n agent-squad-hq-migrate -g rg-agent-squad-hq` |
| Secrets | Key Vault references, read by a user-assigned identity holding "Key Vault Secrets User" |
| Network | The environment is injected into your subnet. Use an Azure Firewall / NAT gateway with FQDN rules for egress |

## Which runtime?

| Need | Pick |
|---|---|
| Cheapest single tenant, 15-minute install | Compose on one VM ([docker-compose.md](docker-compose.md)) |
| Existing Kubernetes platform, HA | Helm ([kubernetes.md](kubernetes.md)) |
| No servers to patch, AWS | ECS Fargate |
| Scale to near zero, GCP | Cloud Run (accepts up to one minute of queue latency) |
| Azure-native identity and Key Vault | Container Apps |

## Validation status

The examples pass `terraform fmt` and `terraform validate` against current provider versions. They have **not** been applied against real accounts as part of this change. Run `terraform plan` in a sandbox account before you use one.
