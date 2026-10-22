# Go — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerabilities: `govulncheck ./...` (reachable vulnerabilities get
  `severity:high`). Outdated modules: `go list -u -m all`.
- Go versions past support in `go.mod`.
- `//nolint` clusters, ignored errors, `panic` in library code.
- Packages with circular-ish coupling or `internal` boundaries bypassed;
  oversized packages/files.
- HTTP servers/clients without timeouts; goroutine leaks.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  coverage), flaky tests (`time.Sleep` synchronisation), TODO/FIXME clusters,
  ADR drift.
