# React SPA (Vite) — debt scan

Findings from this pack use the label `dept:frontend`.

Look for:
- Vulnerable or outdated packages: `npm audit --omit=dev`, `npm outdated`.
  Known high/critical advisories get `severity:high`.
- End-of-life React, Vite or Node versions.
- `any`, `@ts-ignore` and `eslint-disable` accumulation (cluster per folder).
- Dead code and unused exports; duplicated or oversized components.
- Secrets or private endpoints in `VITE_*` variables or source.
- Token storage in `localStorage`/`sessionStorage`.
- Missing accessibility basics.
- Bundle bloat (check `vite build` output sizes; heavy libraries for trivial
  use).
- Untested churn hotspots (`git log --since="90 days ago" --name-only`
  versus `coverage/lcov.info`), TODO/FIXME clusters, ADR drift.
