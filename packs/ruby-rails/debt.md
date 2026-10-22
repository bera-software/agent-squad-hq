# Ruby on Rails — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable gems: `bundle exec bundle-audit check --update` (advisories get
  `severity:high`); outdated: `bundle outdated --strict`. Rails/Ruby versions
  past security support.
- Brakeman warnings (`bundle exec brakeman -q`), including ignored ones in
  `config/brakeman.ignore`.
- `permit!`, unscoped finds, `html_safe` clusters.
- RuboCop todo file growth and `rubocop:disable` clusters.
- Fat models/controllers, callback chains, oversized service objects.
- Missing foreign-key indexes, N+1 hotspots.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  coverage), TODO/FIXME clusters, ADR drift.
