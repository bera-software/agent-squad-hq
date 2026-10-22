# Containers (Docker / Kubernetes / Helm) — review rules

Apply only to this pack's files. Every PR touching these paths is at least
`risk:high` by path floor; never argue it lower. Dockerfile style is owned by
hadolint; chart structure by `helm lint`.

## REQUEST CHANGES
- Base image on `latest` or unpinned; image tags in manifests on `latest`.
- Container running as root, `privileged: true`, `allowPrivilegeEscalation`
  not false, added capabilities (e.g. `SYS_ADMIN`, `NET_ADMIN`).
- `hostPath`, `hostNetwork`, `hostPID`/`hostIPC` without an ADR.
- Missing resource limits/requests on new containers.
- Secrets in Dockerfile `ARG`/`ENV`, in manifests, ConfigMaps or
  `values.yaml`; `.env` files copied into images.
- RBAC with wildcard verbs/resources, or `cluster-admin` bindings.
- Services exposed as `LoadBalancer`/`NodePort` or Ingress added without
  TLS and a stated need.
- NetworkPolicy removed or made allow-all.

## Security checklist (CIS Benchmarks / ASVS-aligned)
- Image provenance: images from trusted registries; digest pinning for
  production; signature verification unchanged.
- Supply chain: `trivy config .` / `checkov` findings for changed files
  addressed or justified; image vulnerability scan not disabled.
- Pod security: namespace Pod Security Standards (`restricted`) labels not
  weakened.
- Service account tokens not auto-mounted where unused.
- Compose files: no published database ports to `0.0.0.0` in shared
  environments; no default passwords.

## Observations (non-blocking)
- Large images, poor layer caching, missing probes, duplicated manifests that
  should be templated, missing PodDisruptionBudgets for replicated services.
