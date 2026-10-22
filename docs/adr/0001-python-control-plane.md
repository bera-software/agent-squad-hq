# ADR-0001: Python control plane

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

The predecessor system was GitHub Actions YAML plus loose Python scripts with thousands of lines of tested logic (sweeper, ledger, capacity gate, merge policy). We need one control plane that runs in GitHub Actions and in a self-hosted server.

## Decision

The control plane is a single Python package, `squadhq` (Python 3.11+), installed in workflows by `setup-hub` and in the server image. New subpackages are fully typed and checked by `mypy` in CI; legacy modules are typed incrementally. Runtime dependencies stay minimal (PyYAML only); optional extras cover the server, Postgres and Bedrock.

## Consequences

Existing logic is reused rather than rewritten. Workflows call `python -m squadhq.<module>`. The server (FastAPI) shares the same modules. Legacy modules are not yet strictly typed.

## Alternatives considered

The client apps (dashboard, mobile) are TypeScript and talk to the server API; they are not part of the control plane.
