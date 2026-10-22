# Python / FastAPI — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable dependencies: `pip-audit` (or `uv run pip-audit`), outdated
  packages via `pip list --outdated` / `uv pip list --outdated`. Known
  CVEs get `severity:high`.
- End-of-life Python versions (check `requires-python`).
- `# type: ignore`, `# noqa`, broad `except Exception: pass` clusters.
- Endpoints without `response_model` or without auth dependencies.
- Business logic in routers; oversized modules (more than ~500 lines).
- Blocking calls in async code; missing httpx timeouts.
- Multiple Alembic heads or models drifting from migrations
  (`alembic check`).
- Untested churn hotspots (`git log --since="90 days ago" --name-only`
  versus `coverage.xml`), TODO/FIXME clusters, ADR drift.
