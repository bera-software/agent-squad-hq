# PHP / Laravel — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable packages: `composer audit` (advisories get `severity:high`);
  outdated: `composer outdated --direct`. Laravel/PHP versions past security
  support.
- Mass-assignment risk (`$guarded = []`), `$request->all()` writes.
- `{!! !!}` usage, raw queries with input, `env()` outside config.
- Fat controllers, duplicated validation outside Form Requests.
- PHPStan baseline growth and `@phpstan-ignore` clusters.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  `coverage.xml`), TODO/FIXME clusters, ADR drift.
