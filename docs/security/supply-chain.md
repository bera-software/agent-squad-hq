# Supply chain

## Pinning

- **GitHub Actions** are pinned to full 40-character commit SHAs, with the version in a trailing comment (`actions/checkout@3d3c… # v7.0.1`). Hub CI fails if any `uses:` in `.github/` or `onboarding/` is not SHA-pinned. The only exception is the project stub's call to the hub's own `router.yml`, which is pinned to a hub commit SHA rendered by onboarding and stub sync.
- **Scanner binaries** (gitleaks, OSV-Scanner, Trivy, actionlint) are downloaded at a fixed version and checked against a SHA-256 digest before they run. Semgrep and zizmor are installed from PyPI at exact versions.
- **Project stubs pin the hub** by commit SHA; upgrades move the pin through a reviewed stub-sync PR ([ADR-0009](../adr/0009-releases-and-pinning.md)).
- **Container images** in the deployment guides are meant to be pinned by digest (`ghcr.io/<owner>/agent-squad-hq@sha256:…`).

!!! warning "Python dependencies at run time"
    `setup-hub` installs the hub package with `pip`. If the hub ships `requirements/actions.lock`, it installs with `--require-hashes`; this release does not ship that file yet, so the package's single runtime dependency (PyYAML, `>=6.0.1,<7`) resolves at run time. `uv.lock` pins the development environment only.

## Dependabot

`.github/dependabot.yml` checks GitHub Actions (root and composite actions), pip, npm (`apps/`) and the Docker base image weekly. New releases must age **7 days** (`cooldown`) before Dependabot proposes them, which avoids pulling a compromised release in its first days. Action updates are grouped into one PR.

## Release artefacts

The release workflow (`.github/workflows/release.yml`) runs on a `v*` tag:

| Artefact | How it is produced and protected |
|---|---|
| Wheel and sdist | `python -m build` at a pinned `build` version |
| SBOM | CycloneDX JSON of the source tree (`anchore/sbom-action`), attached to the release and attested to the wheel and sdist (`actions/attest-sbom`) |
| Build provenance | SLSA build provenance attestation for `dist/*` (`actions/attest-build-provenance`) |
| PyPI package | Trusted publishing through OIDC from the `pypi` environment: no API token is stored |
| Container image | `ghcr.io/<owner>/agent-squad-hq`, tagged `X.Y.Z` and `X.Y`, built with BuildKit provenance (`mode=max`) and an SBOM, a provenance attestation pushed to the registry, and a **keyless cosign signature** |
| GitHub release | Generated notes, `dist/*` and the SBOM; tags with a `-` suffix are marked pre-release |

No release has been published yet; the first will be `v0.1.0`.

### Verifying a release

```bash
# Wheel or sdist provenance (GitHub CLI)
gh attestation verify agent_squad_hq-0.1.0-py3-none-any.whl --repo bera-software/agent-squad-hq

# Container image signature (keyless, GitHub Actions OIDC identity)
cosign verify ghcr.io/bera-software/agent-squad-hq@sha256:<digest> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/bera-software/agent-squad-hq/.github/workflows/release.yml@refs/tags/v'

# Container provenance attestation
gh attestation verify oci://ghcr.io/bera-software/agent-squad-hq@sha256:<digest> --repo bera-software/agent-squad-hq
```

## For your projects

The squad enforces the same ideas on the code it builds: secret scanning, SAST, SCA with licence checks, IaC scanning on PRs, and a CycloneDX SBOM on each project release. Dependency manifests and lockfiles always force `risk:high`, so a dependency change never merges on a model's word. See [SSDLC](ssdlc.md).

## Planned

- A hashed `requirements/actions.lock` for the workflows.
- An OpenSSF Scorecard workflow and badge.
- Signed tags for releases.
