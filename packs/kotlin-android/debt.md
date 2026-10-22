# Android (Kotlin) — debt scan

Findings from this pack use the label `dept:frontend` (mobile client).

Look for:
- Outdated or vulnerable dependencies (`./gradlew dependencyUpdates` if the
  versions plugin is present; OSV/Dependabot alerts). Known CVEs get
  `severity:high`.
- `targetSdk`/`compileSdk` behind the store's current requirement; deprecated
  APIs flagged by Android Lint (`./gradlew lintDebug` report).
- Lint baseline growth, `@Suppress`/`@SuppressLint` clusters.
- Exported components, cleartext config, plain-text token storage.
- `GlobalScope`, main-thread I/O, legacy `AsyncTask`/LiveData-only patterns
  where the project has moved to Flow.
- Oversized Activities/Fragments/ViewModels; mixed View/Compose patterns.
- Untested churn hotspots, TODO/FIXME clusters, ADR drift.
