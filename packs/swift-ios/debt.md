# iOS (Swift) — debt scan

Findings from this pack use the label `dept:frontend` (mobile client).
Some checks need a macOS runner; note when a check could not run.

Look for:
- Outdated dependencies (`swift package show-dependencies`, compare with
  latest releases); packages with known advisories get `severity:high`.
- Deployment target and Swift language mode behind current requirements;
  deprecated API warnings.
- Force-unwrap and `try!` clusters; `swiftlint:disable` clusters;
  `@unchecked Sendable` usage.
- `UserDefaults` used for credentials; ATS exceptions.
- Oversized views/view controllers, duplicated UI components.
- Privacy manifest gaps for SDKs and required-reason APIs.
- Untested churn hotspots, TODO/FIXME clusters, ADR drift.
