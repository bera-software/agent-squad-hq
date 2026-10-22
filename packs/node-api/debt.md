# Node.js API — debt scan

Findings from this pack use the label `dept:backend`.

Look for:
- Vulnerable or outdated packages: `npm audit --omit=dev`, `npm outdated`.
  Known high/critical advisories get `severity:high`.
- End-of-life Node.js or framework major versions.
- Routes without validation or auth guards; error handlers leaking internals.
- `any`, `@ts-ignore`, `eslint-disable` clusters.
- Business logic in controllers/route files; oversized services or modules.
- Outbound HTTP calls without timeouts or retries policy.
- Raw SQL built from strings; missing indexes behind slow queries if visible.
- Untested churn hotspots (`git log --since="90 days ago" --name-only` versus
  `coverage/lcov.info`), TODO/FIXME clusters, ADR drift.
