# Containers (Docker / Kubernetes / Helm) — debt scan

Findings from this pack use the label `dept:backend`.

Look for (static analysis only; never touch a cluster or registry):
- hadolint warnings across Dockerfiles; `trivy config .` or `checkov -d .`
  misconfigurations (high/critical get `severity:high`).
- Base images past end of life or many versions behind; `latest` tags.
- Root containers, missing `securityContext`, missing resource limits,
  missing probes.
- Plain secrets in manifests/values; missing `.dockerignore`.
- Deprecated Kubernetes API versions (e.g. via `kubectl kustomize` /
  `helm template` output against the cluster's target version).
- Duplicated manifests across environments; chart versions not bumped with
  changes.
- TODO/FIXME clusters, ADR drift.
