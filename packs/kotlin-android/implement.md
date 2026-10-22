# Android (Kotlin) — Engineer rules

Project CLAUDE.md and ADRs override these defaults.

- Follow the existing architecture (commonly MVVM / UDF with ViewModel +
  repository, Hilt or Koin for DI, Jetpack Compose UI). Do not introduce a
  second pattern alongside it.
- Coroutines: structured concurrency (`viewModelScope`, `lifecycleScope`);
  no `GlobalScope`; inject dispatchers so tests can swap them; collect flows
  lifecycle-aware (`collectAsStateWithLifecycle` / `repeatOnLifecycle`).
- UI state as immutable data classes; one source of truth per screen.
- Never block the main thread (disk, network, DB). Room queries via suspend
  functions or Flow; schema changes need a Room migration plus a migration
  test, reviewed as high risk.
- Security:
  - Secrets never in the APK (no API secrets in `BuildConfig`,
    `strings.xml` or source); the backend holds them.
  - Tokens in EncryptedSharedPreferences/DataStore with Keystore-backed keys,
    not plain SharedPreferences.
  - No cleartext traffic (`usesCleartextTraffic=false`, network security
    config); no custom `TrustManager` that accepts all certificates.
  - Components (`activity`, `service`, `receiver`, `provider`) are
    `android:exported="false"` unless they must be public; validate every
    Intent extra and deep link parameter as untrusted.
  - WebViews: JavaScript and `addJavascriptInterface` only when required,
    never for untrusted origins.
  - Request only the permissions the feature needs, at runtime, with
    rationale.
- Accessibility: content descriptions, touch targets of at least 48dp,
  support font scaling.
- Tests: JUnit unit tests for ViewModels/repositories (with
  `kotlinx-coroutines-test`), Compose UI tests for logic-bearing screens.
  Run `./gradlew lintDebug testDebugUnitTest` before pushing.
- Release builds, signing and store uploads are human-only; never touch
  keystores or signing configs.

## Quality gates
- Run the lint/format check `./gradlew lintDebug` before pushing (the `lint`
  gate); fix the findings rather than disabling rules.
