# Next.js / React — debt scan

Findings from this pack use the label `dept:frontend`.

Look for:
- Vulnerable or outdated packages: `npm audit --omit=dev` and `npm outdated`.
  Known high/critical advisories get `severity:high`.
- End-of-life Next.js / React / Node versions.
- `any`, `@ts-ignore`, `@ts-expect-error` and `eslint-disable` accumulation
  (count per directory; one umbrella ticket per cluster).
- Dead code and unused exports (e.g. `npx knip` or `npx ts-prune` if present).
- Duplicated or near-duplicate components; oversized components (more than
  ~300 lines or many responsibilities).
- `"use client"` at high levels of the tree pulling whole subtrees client-side.
- Server Actions / Route Handlers without validation or auth checks.
- Missing accessibility basics (unlabelled inputs, click handlers on divs,
  missing alt text).
- Bundle bloat: heavy libraries imported for trivial use (date, lodash,
  icon packs imported whole).
- Untested churn hotspots: `git log --since="90 days ago" --name-only`
  versus `coverage/lcov.info`.
- TODO/FIXME accumulation and drift from ADRs.
