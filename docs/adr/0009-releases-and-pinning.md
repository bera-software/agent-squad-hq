# ADR-0009: Releases, signing and pinning

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

Projects called the router at `@main`, so any hub change reached every project at once.

## Decision

Releases use semantic versioning. The release workflow publishes with SLSA build provenance, an SBOM, keyless cosign signatures for the image and PyPI trusted publishing. Project stubs pin the hub router by commit SHA; stub sync opens a PR to move the pin on each hub release.

## Consequences

Upgrades are explicit and reviewable per project. Signed tags are still planned.

## Alternatives considered

Pinning by tag was rejected because tags can be moved.
