# Android (Kotlin) — review rules

Apply only to this pack's files. Formatting is owned by ktlint/detekt and
Android Lint.

## REQUEST CHANGES
- Secrets or private API keys in source, resources or `BuildConfig`.
- Tokens or personal data in plain SharedPreferences, external storage or
  logs (`Log.d` of sensitive values).
- `usesCleartextTraffic="true"`, permissive network security config, or a
  trust-all `TrustManager`/`HostnameVerifier`.
- A component newly `exported="true"` (or with an intent filter) without
  permission protection and input validation; unvalidated deep links.
- WebView with JavaScript enabled on untrusted content,
  `addJavascriptInterface` exposed to remote pages, or
  `setAllowFileAccess(true)` without need.
- Main-thread I/O; `GlobalScope`; leaked Context references in ViewModels.
- New dangerous permissions without justification.
- Room schema change without a migration.

## Security checklist (OWASP MASVS / ASVS-aligned)
- Storage: sensitive data encrypted at rest; `allowBackup` and data
  extraction rules exclude secrets.
- Network: TLS only; certificate pinning unchanged unless intentional.
- Platform: PendingIntents `FLAG_IMMUTABLE`; implicit intents not carrying
  sensitive data; ContentProviders not world-readable.
- Code: R8/ProGuard rules not disabling obfuscation for sensitive code.
- Auth: biometric prompts tied to Keystore crypto objects where they protect
  secrets.
- Dependencies: new SDKs justified (privacy and data collection reviewed).

## Observations (non-blocking)
- Recomposition hotspots, missing previews, accessibility gaps, oversized
  ViewModels.

## Quality gates
- A failing `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
