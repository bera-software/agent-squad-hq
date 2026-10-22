# .NET — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable packages: `dotnet list package --vulnerable --include-transitive`
  (known CVEs get `severity:high`); outdated: `dotnet list package --outdated`.
- Target frameworks past end of support.
- `#pragma warning disable`, `!` null-forgiving and `[SuppressMessage]`
  clusters.
- Sync-over-async, `async void`, missing cancellation tokens.
- Fat controllers, oversized classes, duplicated mapping code.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  coverage), TODO/FIXME clusters, ADR drift.
