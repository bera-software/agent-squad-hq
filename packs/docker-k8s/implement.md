# Containers (Docker / Kubernetes / Helm) — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

## Hard limits
- Never run `kubectl apply/delete/edit/exec`, `helm install/upgrade/uninstall`,
  `docker push`, or anything that talks to a cluster or registry. You have no
  cluster credentials and must not look for any. Rendering (`helm template`,
  `kubectl kustomize`), linting and local `docker build` are allowed.
- Deploys are human-only, through the project's reviewed pipeline.

## Dockerfiles
- Pin base images to a specific version tag, ideally with a digest; never
  `latest`. Prefer minimal/distroless or slim images.
- Multi-stage builds; build tools stay out of the final image.
- Run as a non-root user (`USER 10001` or a named user); no `sudo`.
- No secrets in `ARG`/`ENV`/`COPY`; use BuildKit secret mounts
  (`--mount=type=secret`). Keep a `.dockerignore` excluding `.git`, `.env*`,
  credentials and build output.
- `COPY` only what is needed; pin package versions where practical and clean
  package caches in the same layer.
- Add a `HEALTHCHECK` or rely on orchestrator probes, consistently with the
  project.

## Kubernetes / Helm
- `securityContext`: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
  `readOnlyRootFilesystem: true` where possible, drop all capabilities.
- Resource `requests` and `limits` on every container; liveness/readiness
  probes.
- No `privileged`, `hostNetwork`, `hostPID`, `hostPath` unless justified in
  an ADR.
- Secrets via `Secret` references from an external secret manager
  (External Secrets, Sealed Secrets, CSI), never plain values in manifests or
  `values.yaml`.
- Least-privilege RBAC; dedicated ServiceAccount with
  `automountServiceAccountToken: false` unless the pod calls the API.
- NetworkPolicies kept or added for new workloads.
- Render changed charts (`helm template`) and kustomizations to check they
  are valid before pushing.
- Pin base images by digest when a ticket asks for it: look the digest up with
  `docker buildx imagetools inspect <image>:<tag>` and write `<image>:<tag>@sha256:<digest>`;
  never invent a digest.
