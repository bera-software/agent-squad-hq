# Python / Django — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable dependencies (`pip-audit`), outdated packages, and Django
  versions past end of support. Known CVEs get `severity:high`.
- `manage.py check --deploy` warnings.
- Model drift (`makemigrations --check --dry-run`), squashing candidates for
  very long migration chains.
- `__all__` serializers, unscoped querysets, views missing permission classes.
- N+1 patterns in list views; missing pagination.
- `# noqa` / `# type: ignore` clusters; fat views; oversized apps.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  `coverage.xml`), TODO/FIXME clusters, ADR drift.
