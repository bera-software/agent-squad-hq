# Kubernetes (Helm)

The chart is at `deploy/helm/agent-squad-hq`. It deploys `api`, `worker` and `scheduler` as separate Deployments from one image. Schema migrations run in a pre-install/pre-upgrade hook Job.

## Prerequisites

- Kubernetes 1.27 or later. Pods run under the Pod Security Standard `restricted` profile.
- A Postgres 14+ database (managed RDS, Cloud SQL, Azure Flexible Server, or CloudNativePG in the cluster).
- An ingress controller with TLS (ingress-nginx plus cert-manager, or your cloud's controller).
- A secrets source. Use External Secrets Operator, Vault Agent, or the CSI secrets store driver. Avoid plain Helm values for secrets.

## Install

```bash
kubectl create namespace squadhq
kubectl label namespace squadhq pod-security.kubernetes.io/enforce=restricted

# Secrets: create once (or sync them with External Secrets; see below)
kubectl -n squadhq create secret generic squadhq-secrets \
  --from-literal=SQUADHQ_DB_URL='postgresql://squadhq:...@pg.internal:5432/squadhq?sslmode=require' \
  --from-literal=SQUADHQ_WEBHOOK_SECRET_GITHUB="$(openssl rand -hex 32)" \
  --from-literal=SQUADHQ_GITHUB_TOKEN='...' --from-literal=GH_TOKEN='...' \
  --from-literal=SQUADHQ_API_TOKENS="admin:admin:$(printf %s "$ADMIN_TOKEN" | openssl dgst -sha256 | awk '{print $NF}')"

helm upgrade --install hq deploy/helm/agent-squad-hq -n squadhq \
  --set image.repository=ghcr.io/<owner>/agent-squad-hq \
  --set image.digest=sha256:<digest> \
  --set secrets.existingSecret=squadhq-secrets \
  --set config.hubRepo=acme/agent-squad-hq \
  --set ingress.enabled=true --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=hq.example.com' --set 'ingress.hosts[0].paths[0].path=/' \
  --set 'ingress.hosts[0].paths[0].pathType=Prefix' \
  --set-file squadhqYml=./squadhq.yml
```

Generate `ADMIN_TOKEN` with `echo "sqh_$(openssl rand -hex 32)"`, or after install run `kubectl exec deploy/hq-agent-squad-hq-api -- python -m squadhq.server create-token --role admin`. The second option stores only the hash, in the database.

### External Secrets Operator example

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata: {name: squadhq-secrets, namespace: squadhq}
spec:
  refreshInterval: 1h
  secretStoreRef: {name: aws-secrets-manager, kind: ClusterSecretStore}
  target: {name: squadhq-secrets}
  dataFrom:
    - extract: {key: prod/squadhq}   # a JSON secret whose keys are the env var names
```

## What the chart sets up

| Resource | Notes |
|---|---|
| Deployments `api`, `worker`, `scheduler` | non-root uid 10001, `readOnlyRootFilesystem`, all capabilities dropped, `RuntimeDefault` seccomp, no service-account token mounted |
| Job `migrate` | Helm hook. The ConfigMap and the chart-managed Secret are created by the same hook phase so the Job can mount them |
| Service and Ingress | Only the API is exposed |
| NetworkPolicies | Default deny. DNS and the database egress are allowed. API ingress comes only from `networkPolicy.ingressFrom`. Outbound HTTPS is allowed for the **worker only** |
| PodDisruptionBudgets | One per component |
| HPA (optional) | `api.autoscaling.enabled`, `worker.autoscaling.enabled` |
| ServiceMonitor (optional) | Prometheus Operator scrape with a viewer bearer token |

The scheduler is safe at any replica count: only the holder of the database lock enqueues, and each fire carries an idempotency key (`sched:<key>:<instant>`). Workers scale horizontally. Claims use `FOR UPDATE SKIP LOCKED` on Postgres.

## Values you will most likely change

| Key | Default | Meaning |
|---|---|---|
| `config.mode` | `byo-compute` | In Kubernetes, use `byo-compute`. See the note below |
| `config.trustedAssociations` | `OWNER,MEMBER,COLLABORATOR` | `SQUADHQ_TRUSTED_ASSOCIATIONS` (shared with Actions mode). `config.allowedAssociations` is a deprecated alias |
| `config.trustedActors` | `""` | Jira, Trello or GitLab users allowed to trigger agents |
| `config.requiredChecks` | `hub / ci-gate` | Checks that must be green on the approved head commit before a merge |
| `config.allowForkMerges` | `false` | Allow merging fork-head PRs |
| `config.apiDocs` | `false` | Serve `/docs` and `/openapi.json` |
| `networkPolicy.databaseEgress` | 10.0.0.0/8:5432 | Narrow this to your database CIDR |
| `networkPolicy.httpsEgress` | 0.0.0.0/0:443 minus private ranges and metadata | Replace with FQDN policies (Cilium, Calico) or an egress gateway |
| `worker.terminationGracePeriodSeconds` | 120 | Time to finish the current job before SIGKILL |
| `metrics.serviceMonitor.enabled` | false | Needs a Secret holding a viewer token |

> **Worker mode on Kubernetes.** The built-in container runner shells out to `docker`/`podman`, and pods should not get a container socket. Use `byo-compute` on Kubernetes for now. A Kubernetes Job runner behind the same `ContainerRunner` interface (`squadhq.server.sandbox`), with a per-job namespace, a restricted PSS profile and gVisor or Kata RuntimeClass, is the intended follow-up.

## Upgrades

`helm upgrade` runs the `migrate` hook first. Migrations only add things (new tables or columns), so old pods keep working during a rolling update. For the full procedure see [upgrade-and-backup.md](upgrade-and-backup.md).

## Validation

```bash
helm lint deploy/helm/agent-squad-hq --set secrets.existingSecret=x
helm template hq deploy/helm/agent-squad-hq --set secrets.existingSecret=x | kubeconform -strict -
```
